"""
A brief, explicit pause on writes, for taking or restoring a backup that
must match one moment across SQLite AND the JSON records.

This is a single-process pilot, so the pause is a flag file both halves of
the system look at:

  * the API refuses every mutating request (POST/PUT/PATCH/DELETE) with
    503 and Retry-After while the file exists (main.py middleware);
  * the execution gate refuses to start any JDE write or test attempt
    (mcp_server execution.begin reads JDE_WRITE_PAUSE_FILE).

Reads keep working. The flag records who paused, why and when, so a pause
left behind by a crashed script is visible and can be cleared by hand
(delete the file).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from ..config import settings

PAUSE_ENV = "JDE_WRITE_PAUSE_FILE"
FILE_NAME = "WRITE_PAUSED"


def pause_file() -> str:
    return os.environ.get(PAUSE_ENV) or os.path.join(settings.data_dir, FILE_NAME)


def status() -> Optional[dict]:
    path = pause_file()
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"reason": "unreadable pause file", "by": "unknown", "since": None}


def pause(reason: str, by: str) -> None:
    path = pause_file()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    body = {"reason": reason, "by": by, "since": datetime.now(timezone.utc).isoformat(), "pid": os.getpid()}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".pause-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(body, f)
    os.replace(tmp, path)


def resume() -> None:
    try:
        os.remove(pause_file())
    except FileNotFoundError:
        pass


@contextmanager
def paused(reason: str, by: str, *, settle_seconds: float = 0.0) -> Iterator[None]:
    """Pause writes, let requests already in flight finish, run the body,
    and always resume -- even when the body fails."""
    if status() is not None:
        raise RuntimeError(f"writes are already paused ({status()}); refusing to start a second pause")
    pause(reason, by)
    try:
        if settle_seconds:
            time.sleep(settle_seconds)
        yield
    finally:
        resume()
