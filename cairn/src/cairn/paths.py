from __future__ import annotations

import os
from pathlib import Path


def log_dir() -> Path:
    """Host directory where the dispatcher writes per-run transcripts."""
    raw = os.environ.get("CAIRN_LOG_DIR")
    path = Path(raw).expanduser() if raw else Path.home() / ".local" / "share" / "cairn" / "logs"
    return path.resolve()


def artifacts_dir() -> Path:
    """Host directory where collected project artifacts live."""
    raw = os.environ.get("CAIRN_ARTIFACTS_DIR")
    path = Path(raw).expanduser() if raw else Path.cwd() / "artifacts"
    return path.resolve()
