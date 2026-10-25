from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse

from cairn.paths import artifacts_dir, log_dir
from cairn.server.db import get_conn
from cairn.server.models import ArtifactEntry, CreateRunRequest, Run, UpdateRunRequest
from cairn.server.services import get_project_or_404

router = APIRouter(tags=["runs"])

TAIL_MAX_LINES = 10_000
WORKLOG_MAX_BYTES = 1_000_000
ARTIFACT_LIMIT = 500


def _run_from_row(row: sqlite3.Row) -> Run:
    return Run(**dict(row))


def _get_run_or_404(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Run not found")
    return row


def _safe_file(raw_path: str | None, root: Path) -> Path:
    if not raw_path:
        raise HTTPException(404, "No file recorded for this run")
    try:
        path = Path(raw_path).expanduser().resolve()
    except OSError as exc:
        raise HTTPException(404, "Recorded file is not readable") from exc
    if not path.is_relative_to(root.resolve()):
        raise HTTPException(403, "Recorded file is outside the configured directory")
    if not path.is_file():
        raise HTTPException(404, "Recorded file not found")
    return path


def _tail_text(path: Path, lines: int) -> str:
    limit = max(1, min(lines, TAIL_MAX_LINES))
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        chunk = b""
        while position > 0 and chunk.count(b"\n") <= limit:
            step = min(65_536, position)
            position -= step
            handle.seek(position)
            chunk = handle.read(step) + chunk
    text = chunk.decode("utf-8", errors="replace")
    parts = text.splitlines()
    if len(parts) > limit:
        parts = parts[-limit:]
    return "\n".join(parts) + ("\n" if parts else "")


def _read_text(path: Path, max_bytes: int) -> str:
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    text = data[:max_bytes].decode("utf-8", errors="replace")
    if len(data) > max_bytes:
        text += "\n\n[truncated]\n"
    return text


@router.post("/projects/{project_id}/runs", response_model=Run, status_code=201)
def create_run(project_id: str, body: CreateRunRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        try:
            conn.execute(
                """
                INSERT INTO runs (
                    id, project_id, task_type, worker, intent_id, status,
                    started_at, log_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    body.id,
                    project_id,
                    body.task_type,
                    body.worker,
                    body.intent_id,
                    body.status,
                    body.started_at,
                    body.log_path,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Run already exists") from exc
        row = _get_run_or_404(conn, body.id)
        return _run_from_row(row)


@router.patch("/runs/{run_id}", response_model=Run)
def update_run(run_id: str, body: UpdateRunRequest):
    with get_conn() as conn:
        _get_run_or_404(conn, run_id)
        updates = body.model_dump(exclude_unset=True)
        if updates:
            assignments = ", ".join(f"{column} = ?" for column in updates)
            conn.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",
                (*updates.values(), run_id),
            )
        row = _get_run_or_404(conn, run_id)
        return _run_from_row(row)


@router.get("/projects/{project_id}/runs", response_model=list[Run])
def list_runs(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            "SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC",
            (project_id,),
        ).fetchall()
        return [_run_from_row(row) for row in rows]


@router.get("/runs/{run_id}", response_model=Run)
def get_run(run_id: str):
    with get_conn() as conn:
        return _run_from_row(_get_run_or_404(conn, run_id))


@router.get("/runs/{run_id}/log")
def get_run_log(run_id: str, tail: int = Query(default=2000, ge=1, le=TAIL_MAX_LINES)):
    with get_conn() as conn:
        row = _get_run_or_404(conn, run_id)
    path = _safe_file(row["log_path"], log_dir())
    return PlainTextResponse(
        _tail_text(path, tail),
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/runs/{run_id}/worklog")
def get_run_worklog(run_id: str):
    with get_conn() as conn:
        row = _get_run_or_404(conn, run_id)
    path = _safe_file(row["worklog_path"], log_dir())
    return PlainTextResponse(
        _read_text(path, WORKLOG_MAX_BYTES),
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/projects/{project_id}/artifacts", response_model=list[ArtifactEntry])
def list_artifacts(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)

    root = (artifacts_dir() / project_id.replace("/", "-")).resolve()
    if not root.is_dir():
        return []

    entries: list[ArtifactEntry] = []
    for path in sorted(root.rglob("*")):
        if len(entries) >= ARTIFACT_LIMIT:
            break
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            continue
        entries.append(
            ArtifactEntry(
                path=resolved.relative_to(root).as_posix(),
                size=resolved.stat().st_size,
            )
        )
    return entries


@router.get("/projects/{project_id}/artifacts/{artifact_path:path}")
def get_artifact(project_id: str, artifact_path: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)

    root = (artifacts_dir() / project_id.replace("/", "-")).resolve()
    target = (root / artifact_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "Artifact not found")
    return FileResponse(target)
