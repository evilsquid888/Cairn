"""Live adapter format-drift contract checks.

These run the REAL worker CLIs on a trivial prompt and assert that each
adapter's parser + extraction still understand the live output. They are NOT
part of the default suite (they need the host CLIs authed and make real,
billed calls). They live outside `testpaths = ["tests"]`, so a bare
`pytest` never collects them.

Run them with the wrapper:

    cairn/scripts/verify-adapters

or directly:

    uv run pytest tests_live/ -v

Each test SKIPS when its CLI is missing or fails to produce output
(environmental: not on PATH, auth/network) and FAILS only when the CLI runs
but its output no longer matches what the parser expects (format drift). A
failing test here means "a client changed its format — update the parser."
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.registry import get_driver

PROMPT = "Reply with exactly the single word: pong"
RUN_TIMEOUT = 180
RAW_RATIO_MAX = 0.5


def _worker(wtype: str, **env: str) -> WorkerConfig:
    return WorkerConfig(
        name=f"contract-{wtype}",
        type=wtype,
        task_types=["explore"],
        max_running=1,
        priority=0,
        env={"CAIRN_HOST_AUTH": "1", **env},
    )


def _run(wtype: str, **env: str):
    """Run the local CLI once; return (driver, stdout, stderr) or skip."""
    driver = get_driver(wtype, "local")
    binary = driver.local_binary()
    if not binary or shutil.which(binary) is None:
        pytest.skip(f"{binary or wtype} not on PATH")
    worker = _worker(wtype, **env)
    session = driver.prepare_session()
    argv = driver.build_execute(worker, PROMPT, session).argv
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
            env={**os.environ, **worker.env},
        )
    except subprocess.TimeoutExpired:
        pytest.skip(f"{wtype} timed out ({RUN_TIMEOUT}s) — environmental")
    if proc.returncode != 0 or not proc.stdout.strip():
        pytest.skip(
            f"{wtype} produced no output (rc={proc.returncode}); likely auth/network. "
            f"stderr: {proc.stderr[:200]!r}"
        )
    return driver, proc.stdout, proc.stderr


def _events(driver, stdout: str):
    parser = driver.new_transcript_parser()
    events = []
    for line in stdout.splitlines():
        events += parser.feed(line)
    events += parser.finish()
    return events


def _raw_ratio(events) -> float:
    return 1.0 if not events else sum(1 for e in events if e.kind == "raw") / len(events)


def _assert_not_raw_extraction(text: str) -> None:
    assert text.strip(), "extract_response_text returned empty"
    assert not text.lstrip().startswith(
        '{"type"'
    ), "extraction fell back to raw NDJSON — the final-text shape drifted"


@pytest.mark.adapter_contract
def test_claudecode_contract():
    driver, stdout, stderr = _run("claudecode")
    events = _events(driver, stdout)
    kinds = [e.kind for e in events]
    assert _raw_ratio(events) < RAW_RATIO_MAX, f"high raw ratio — format drift? kinds={kinds}"
    assert set(kinds) & {"assistant_text", "result"}, f"no content events — drift? kinds={kinds}"
    _assert_not_raw_extraction(driver.extract_response_text(stdout, stderr))


@pytest.mark.adapter_contract
def test_codex_contract():
    driver, stdout, stderr = _run("codex")
    events = _events(driver, stdout)
    kinds = [e.kind for e in events]
    assert _raw_ratio(events) < RAW_RATIO_MAX, f"high raw ratio — format drift? kinds={kinds}"
    assert set(kinds) & {"assistant_text", "result"}, f"no content events — drift? kinds={kinds}"
    _assert_not_raw_extraction(driver.extract_response_text(stdout, stderr))
    assert driver.extract_session(None, stdout, stderr), "session id not recoverable — resume path drifted"


@pytest.mark.adapter_contract
def test_opencode_contract():
    driver, stdout, stderr = _run("opencode")
    events = _events(driver, stdout)
    kinds = [e.kind for e in events]
    assert _raw_ratio(events) < RAW_RATIO_MAX, f"high raw ratio — format drift? kinds={kinds}"
    assert "assistant_text" in kinds, f"no assistant_text — drift? kinds={kinds}"
    _assert_not_raw_extraction(driver.extract_response_text(stdout, stderr))


@pytest.mark.adapter_contract
def test_pi_contract():
    driver, stdout, stderr = _run("pi")
    events = _events(driver, stdout)
    kinds = [e.kind for e in events]
    assert _raw_ratio(events) < RAW_RATIO_MAX, f"high raw ratio — format drift? kinds={kinds}"
    assert "assistant_text" in kinds, f"no assistant_text — drift? kinds={kinds}"
    _assert_not_raw_extraction(driver.extract_response_text(stdout, stderr))
