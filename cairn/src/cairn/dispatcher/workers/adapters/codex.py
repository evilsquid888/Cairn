from __future__ import annotations

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, RegexSessionDriver
from cairn.dispatcher.workers.transcript import CodexTranscriptParser, TranscriptParser, parse_json_lines
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


class CodexDriver(RegexSessionDriver):
    type_name = "codex"

    def __init__(self, local: bool = False):
        self.local = local

    def local_binary(self) -> str | None:
        return "codex"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        if worker.env.get("CAIRN_HOST_AUTH"):
            return HealthResult(ok=True, status=None, detail="host CLI auth; health check skipped")
        env = worker.env
        return http_ping(
            f"{env['CODEX_BASE_URL']}/responses",
            headers={
                "Authorization": f"Bearer {env['OPENAI_API_KEY']}",
                "content-type": "application/json",
            },
            json_body={
                "model": env["CODEX_MODEL"],
                "input": [{"role": "user", "content": "ping"}],
                "stream": False,
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        if worker.env.get("CAIRN_HOST_AUTH"):
            return "host CLI auth (health check skipped)"
        return f"POST {worker.env['CODEX_BASE_URL']}/responses (model={worker.env['CODEX_MODEL']})"

    def new_transcript_parser(self) -> TranscriptParser | None:
        return CodexTranscriptParser()

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        for obj in parse_json_lines(stdout):
            if obj.get("type") == "thread.started" and obj.get("thread_id"):
                return str(obj["thread_id"])
        return super().extract_session(session, stdout, stderr)

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        last: str | None = None
        for obj in parse_json_lines(stdout):
            item = obj.get("item")
            if (
                obj.get("type") == "item.completed"
                and isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                last = item["text"]
        return last if last is not None else stdout

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        if self.local or worker.env.get("CAIRN_HOST_AUTH"):
            return DriverResult(
                argv=[
                    "codex",
                    "exec",
                    "--json",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--",
                    prompt,
                ]
            )
        env = worker.env
        return DriverResult(
            argv=[
                "codex",
                "exec",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "--model",
                env["CODEX_MODEL"],
                "-c",
                'model_provider="cairn"',
                "-c",
                'model_providers.cairn.name="cairn"',
                "-c",
                'model_providers.cairn.wire_api="responses"',
                "-c",
                'model_reasoning_effort="high"',
                "-c",
                f'model_providers.cairn.base_url="{env["CODEX_BASE_URL"]}"',
                "-c",
                'model_providers.cairn.env_key="OPENAI_API_KEY"',
                "--",
                prompt,
            ]
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        if self.local or worker.env.get("CAIRN_HOST_AUTH"):
            return [
                "codex",
                "exec",
                "resume",
                session,
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "--",
                prompt,
            ]
        env = worker.env
        return [
            "codex",
            "exec",
            "resume",
            session,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            env["CODEX_MODEL"],
            "-c",
            'model_provider="cairn"',
            "-c",
            'model_providers.cairn.name="cairn"',
            "-c",
            'model_providers.cairn.wire_api="responses"',
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            f'model_providers.cairn.base_url="{env["CODEX_BASE_URL"]}"',
            "-c",
            'model_providers.cairn.env_key="OPENAI_API_KEY"',
            "--",
            prompt,
        ]
