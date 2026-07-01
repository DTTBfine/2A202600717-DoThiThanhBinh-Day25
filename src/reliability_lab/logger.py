"""Structured JSON logger for request tracing.

Writes newline-delimited JSON to reports/trace.jsonl by default.
Each record: {"ts": <epoch>, "event": "<name>", ...fields}
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_log_path: Path = Path("reports/trace.jsonl")
_enabled: bool = True


def set_path(path: str | Path) -> None:
    global _log_path
    _log_path = Path(path)


def set_enabled(enabled: bool) -> None:
    global _enabled
    _enabled = enabled


def emit(event: str, **fields: Any) -> None:
    if not _enabled:
        return
    record: dict[str, Any] = {"ts": round(time.time(), 3), "event": event}
    record.update(fields)
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _lock:
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(_log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
