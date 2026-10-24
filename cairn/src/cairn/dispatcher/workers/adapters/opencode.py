from __future__ import annotations

import uuid

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, SeedSessionDriver
from cairn.dispatcher.workers.health import HealthResult


class OpenCodeDriver(SeedSessionDriver):
    """OpenCode CLI worker.

    The worker image does not bake in OpenCode: the host binary and an
    ``opencode auth export`` file are bind-mounted by dispatch.yaml. Before every
    run the wrapper imports that export into the container-local OpenCode DB,
    then execs the real CLI.
    """

    type_name = "opencode"

    def local_binary(self) -> str | None:
        return "opencode"

    def prepare_session(self) -> str | None:
        # OpenCode session IDs must start with "ses"; a bare UUID is rejected.
        return "ses_" + uuid.uuid4().hex

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        # OpenCode has no stable unauthenticated HTTP health endpoint. Host-auth
        # workers are validated by the CLI at task time; do not block startup.
        return HealthResult(ok=True, status=None, detail="OpenCode host auth; health check skipped")

    def describe_health(self, worker: WorkerConfig) -> str:
        return "OpenCode host auth (health check skipped)"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        return DriverResult(argv=self._argv(worker, prompt, session), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return self._argv(worker, prompt, session)

    def _argv(self, worker: WorkerConfig, prompt: str, session: str | None) -> list[str]:
        auth_file = worker.env.get(
            "OPENCODE_AUTH_FILE", "/home/kali/.config/opencode/auth-export.json"
        )
        model = worker.env.get("OPENCODE_MODEL")
        opencode_argv = [
            "opencode",
            "run",
            "--standalone",
            "--auto",
            "--format",
            "default",
        ]
        if model:
            opencode_argv.extend(["--model", model])
        if session:
            opencode_argv.extend(["--session", session])
        opencode_argv.append(prompt)

        # Positional args avoid quoting the (large, arbitrary) prompt into a shell string.
        script = (
            'if [ -s "$1" ]; then\n'
            '  opencode auth import --standalone "$1" >/dev/null 2>&1 || true\n'
            "fi\n"
            "shift\n"
            'exec "$@"\n'
        )
        return ["/bin/sh", "-lc", script, "--", auth_file, *opencode_argv]
