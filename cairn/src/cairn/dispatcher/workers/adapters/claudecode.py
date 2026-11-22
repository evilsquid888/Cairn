from __future__ import annotations

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, SeedSessionDriver
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env
from cairn.dispatcher.workers.transcript import ClaudeTranscriptParser, TranscriptParser, parse_json_lines


ANTHROPIC_VERSION = "2023-06-01"


class ClaudeCodeDriver(SeedSessionDriver):
    type_name = "claudecode"

    def local_binary(self) -> str | None:
        return "claude"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        if worker.env.get("CAIRN_HOST_AUTH"):
            return HealthResult(ok=True, status=None, detail="host CLI auth; health check skipped")
        env = worker.env
        return http_ping(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers={
                "Authorization": f"Bearer {env['ANTHROPIC_AUTH_TOKEN']}",
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json_body={
                "model": env["ANTHROPIC_MODEL"],
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        if worker.env.get("CAIRN_HOST_AUTH"):
            return "host CLI auth (health check skipped)"
        return f"POST {worker.env['ANTHROPIC_BASE_URL']}/v1/messages (model={worker.env['ANTHROPIC_MODEL']})"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        return DriverResult(
            argv=[
                "claude",
                "--session-id",
                session,
                "--dangerously-skip-permissions",
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--",
                prompt,
            ],
            session=session,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return [
            "claude",
            "-r",
            session,
            "--dangerously-skip-permissions",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--",
            prompt,
        ]

    def new_transcript_parser(self) -> TranscriptParser:
        return ClaudeTranscriptParser()

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        events = parse_json_lines(stdout)
        for obj in reversed(events):
            if obj.get("type") == "result" and isinstance(obj.get("result"), str) and obj["result"]:
                return obj["result"]
        texts: list[str] = []
        for obj in events:
            message = obj.get("message")
            if obj.get("type") != "assistant" or not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, list):
                texts.extend(
                    b["text"]
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                )
        if texts:
            return "".join(texts)
        return stdout
