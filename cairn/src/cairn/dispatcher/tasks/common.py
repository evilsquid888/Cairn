from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.paths import log_dir

PROCESS_COMMUNICATE_GRACE_SECONDS = 15
LOG_PREVIEW_LIMIT = 1200
GRAPH_SNAPSHOT_ROOT = "/tmp/cairn-prompts"
LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class ConcludeWriteResult:
    status: str
    fact_id: str | None = None


def preview(text: str, limit: int = LOG_PREVIEW_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def did_timeout(result: ProcessResult) -> bool:
    return not result.cancelled and (result.timed_out or result.returncode in (124, 137))


def cancel_reason(result: ProcessResult, cancellation: TaskCancellation | None = None) -> str | None:
    if result.cancelled:
        return result.cancel_reason or "cancelled"
    if cancellation is not None:
        return cancellation.reason
    return None


def communicate_timeout(timeout_seconds: int, grace_seconds: int = PROCESS_COMMUNICATE_GRACE_SECONDS) -> int:
    return timeout_seconds + grace_seconds


def task_healthcheck_enabled(config: DispatchConfig) -> bool:
    if config.runtime.execution == "local":
        return False
    return config.runtime.worker_healthcheck == "startup_and_task"


def write_graph_snapshot_reference(
    container_manager: ContainerManager,
    container_name: str,
    graph_yaml: str,
    *,
    phase: str,
) -> str:
    path = f"{GRAPH_SNAPSHOT_ROOT}/{phase}-{uuid.uuid4().hex[:12]}/graph.yaml"
    container_manager.write_text_file(container_name, path, graph_yaml)
    return (
        "The graph YAML snapshot is stored in this file inside the current container:\n\n"
        f"{path}\n\n"
        "Before using the graph, read the entire file and treat its contents as the YAML snapshot "
        "for this Graph section."
    )


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _run_status(result: ProcessResult) -> str:
    if result.cancelled:
        return "cancelled"
    if result.timed_out:
        return "timeout"
    if result.returncode != 0:
        return "failed"
    return "completed"


def _finish_run(
    client: CairnClient | None,
    run_id: str | None,
    *,
    status: str,
    error: str | None,
    worklog_path: Path | None,
) -> None:
    if client is None or run_id is None:
        return
    try:
        response = client.update_run(
            run_id,
            status=status,
            finished_at=_utcnow(),
            error=error,
            worklog_path=str(worklog_path) if worklog_path is not None else None,
        )
    except Exception as exc:  # observability must never fail the task
        LOG.warning("failed to update run run_id=%s error=%s", run_id, exc)
        return
    if not response.ok:
        LOG.warning("failed to update run run_id=%s status=%s", run_id, response.status_code)


def _copy_worklog(
    container_manager: ContainerManager,
    container_name: str,
    project_dir: Path,
) -> Path | None:
    try:
        text = container_manager.read_text_file(container_name, "/home/kali/workspace/WORKLOG.md")
    except Exception as exc:
        LOG.warning("failed to read worklog container=%s error=%s", container_name, exc)
        return None
    if not text:
        return None
    target = project_dir / "WORKLOG.md"
    try:
        target.write_text(text, encoding="utf-8")
        os.chmod(target, 0o600)
    except OSError as exc:
        LOG.warning("failed to write worklog path=%s error=%s", target, exc)
        return None
    return target


def run_worker_process(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
    client: CairnClient | None = None,
    project_id: str | None = None,
    intent_id: str | None = None,
) -> ProcessResult:
    run_id = "run_" + uuid.uuid4().hex
    project_dir: Path | None = None
    log_path: Path | None = None
    if project_id:
        try:
            project_dir = log_dir() / project_id.replace("/", "-")
            project_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(project_dir, 0o700)
            log_path = project_dir / f"{run_id}-{phase}-{worker.name}.log"
            log_path.write_text(
                f"=== Cairn run {run_id} ===\n"
                f"started: {_utcnow()}\n"
                f"phase: {phase}\n"
                f"worker: {worker.name}\n"
                f"intent: {intent_id or '-'}\n"
                f"timeout: {timeout_seconds}s\n\n",
                encoding="utf-8",
            )
            os.chmod(log_path, 0o600)
        except OSError as exc:
            LOG.warning("failed to prepare task log project=%s error=%s", project_id, exc)
            project_dir = None
            log_path = None

    if client is not None and project_id:
        try:
            response = client.create_run(
                project_id,
                run_id,
                phase,
                worker.name,
                intent_id,
                _utcnow(),
                str(log_path) if log_path is not None else None,
            )
            if not response.ok:
                LOG.warning("failed to create run project=%s run=%s status=%s", project_id, run_id, response.status_code)
        except Exception as exc:
            LOG.warning("failed to create run project=%s run=%s error=%s", project_id, run_id, exc)

    LOG.info(
        "starting container exec container=%s worker=%s phase=%s timeout=%ss run=%s",
        container_name,
        worker.name,
        phase,
        timeout_seconds,
        run_id,
    )
    process = container_manager.build_exec_process(
        container_name,
        dict(worker.env),
        argv,
        timeout_seconds=timeout_seconds,
        log_path=str(log_path) if log_path is not None else None,
    )
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    try:
        result = process.communicate(timeout=communicate_timeout(timeout_seconds))
    except Exception as exc:
        worklog_path = _copy_worklog(container_manager, container_name, project_dir) if project_dir else None
        _finish_run(client, run_id, status="failed", error=preview(str(exc)), worklog_path=worklog_path)
        raise
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)

    worklog_path = _copy_worklog(container_manager, container_name, project_dir) if project_dir else None
    status = _run_status(result)
    error = None
    if status in ("failed", "timeout", "cancelled"):
        error = preview(cancel_reason(result, cancellation) or result.stderr or result.stdout or f"exit {result.returncode}")
    _finish_run(client, run_id, status=status, error=error, worklog_path=worklog_path)
    return result


def project_allows_conclude_fallback(client: CairnClient, project_id: str, *, worker_name: str, intent_id: str) -> bool:
    project = client.get_project(project_id)
    if project.project.status == "active":
        return True
    LOG.info(
        "skip conclude fallback because project is no longer active project=%s intent=%s worker=%s status=%s",
        project_id,
        intent_id,
        worker_name,
        project.project.status,
    )
    return False


def best_effort_release_reason(client: CairnClient, project_id: str, worker_name: str) -> None:
    response = client.release_reason(project_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "reason release failed project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released reason project=%s worker=%s", project_id, worker_name)
    else:
        LOG.info(
            "reason release skipped project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )


def write_conclude_result(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> str:
    return write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
    ).status


def write_conclude_result_with_fact_id(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> ConcludeWriteResult:
    response = client.conclude(project_id, intent_id, worker_name, description)
    if response.ok:
        fact_id: str | None = None
        if isinstance(response.data, dict):
            fact = response.data.get("fact")
            if isinstance(fact, dict):
                candidate = fact.get("id")
                if isinstance(candidate, str) and candidate:
                    fact_id = candidate
        if total_ms is None:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
            )
        else:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s total_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
                total_ms,
            )
        return ConcludeWriteResult(status="success", fact_id=fact_id)
    if response.status_code == 403:
        LOG.info(
            "project became inactive during conclude project=%s intent=%s worker=%s",
            project_id,
            intent_id,
            worker_name,
        )
    else:
        LOG.warning(
            "conclude write failed project=%s intent=%s worker=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
            response.text,
        )
    best_effort_release(client, project_id, intent_id, worker_name)
    return ConcludeWriteResult(status="failed", fact_id=None)


def best_effort_release(client: CairnClient, project_id: str, intent_id: str, worker_name: str) -> None:
    response = client.release(project_id, intent_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "release failed project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released intent project=%s intent=%s worker=%s", project_id, intent_id, worker_name)
    else:
        LOG.info(
            "release skipped project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
