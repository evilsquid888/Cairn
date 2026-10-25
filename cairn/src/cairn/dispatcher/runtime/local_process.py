from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from contextlib import suppress

from cairn.dispatcher.runtime.process import ProcessResult

LOG = logging.getLogger(__name__)

READ_CHUNK_SIZE = 65536
STREAM_JOIN_TIMEOUT_SECONDS = 5.0
FORCE_KILL_REAP_TIMEOUT_SECONDS = 2.0


class LocalProcess:
    """Runs a worker command as a host subprocess.

    Mirrors the container ManagedProcess surface (start/communicate/kill/cancel) but
    executes on the dispatcher host: its own process group so children are killed as a
    group, a Python-enforced timeout instead of the ``timeout`` coreutil, and a
    SIGTERM -> grace -> SIGKILL shutdown so the CLI can flush its session before dying.
    """

    def __init__(
        self,
        command: list[str],
        cwd: str,
        env: dict[str, str],
        timeout_seconds: int | None = None,
        term_grace_seconds: int = 5,
        log_path: str | None = None,
    ):
        self.command = command
        self.env = env
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._term_grace = max(1.0, float(term_grace_seconds))
        self._process: subprocess.Popen[str] | None = None
        self._stdout_chunks: list[str] = []
        self._stderr_chunks: list[str] = []
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._timed_out = False
        self._cancel_reason: str | None = None
        self._kill_lock = threading.Lock()
        self._log_path = log_path
        self._log_file = None
        self._log_lock = threading.RLock()

    def start(self) -> None:
        if self._log_path:
            try:
                self._log_file = open(self._log_path, "a", encoding="utf-8", errors="replace")
            except OSError as exc:
                LOG.warning("failed to open task log path=%s error=%s", self._log_path, exc)
        self._process = subprocess.Popen(
            self.command,
            cwd=self._cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        self._stdout_thread = threading.Thread(
            target=self._drain, args=(self._process.stdout, self._stdout_chunks), daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._drain, args=(self._process.stderr, self._stderr_chunks), daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._process is not None
        wait_for = float(self._timeout_seconds) if self._timeout_seconds is not None else timeout
        try:
            self._process.wait(timeout=wait_for)
        except subprocess.TimeoutExpired:
            self._timed_out = True
            self._terminate()
        with suppress(subprocess.TimeoutExpired):
            self._process.wait(timeout=FORCE_KILL_REAP_TIMEOUT_SECONDS)
        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=STREAM_JOIN_TIMEOUT_SECONDS)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=STREAM_JOIN_TIMEOUT_SECONDS)
        self._close_log()
        returncode = self._process.returncode
        if returncode is None:
            returncode = 137 if self._timed_out else 1
        return ProcessResult(
            returncode=returncode,
            stdout="".join(self._stdout_chunks),
            stderr="".join(self._stderr_chunks),
            timed_out=self._timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        self._terminate()

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self._terminate()

    def _terminate(self) -> None:
        with self._kill_lock:
            process = self._process
            if process is None or process.poll() is not None:
                return
            self._signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=self._term_grace)
                return
            except subprocess.TimeoutExpired:
                pass
            self._signal_group(process, signal.SIGKILL)

    @staticmethod
    def _signal_group(process: subprocess.Popen[str], sig: int) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError):
            with suppress(ProcessLookupError, PermissionError, ValueError):
                process.send_signal(sig)

    def _drain(self, pipe, sink: list[str]) -> None:
        try:
            for chunk in iter(lambda: pipe.read(READ_CHUNK_SIZE), ""):
                sink.append(chunk)
                self._append_log(chunk)
        except (ValueError, OSError):
            pass
        finally:
            with suppress(Exception):
                pipe.close()

    def _append_log(self, text: str) -> None:
        if not text or self._log_file is None:
            return
        try:
            with self._log_lock:
                self._log_file.write(text)
                self._log_file.flush()
        except (OSError, ValueError) as exc:
            LOG.warning("failed to write task log path=%s error=%s", self._log_path, exc)
            self._close_log()

    def _close_log(self) -> None:
        with self._log_lock:
            if self._log_file is None:
                return
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None
