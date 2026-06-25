"""Structured JSON logging + durable file primitives (non-negotiable #7, §7.4).

Every log line is valid JSON with ``timestamp`` / ``level`` / ``service`` /
``request_id`` (here ``run_id``). Time-series go to append-only NDJSON with a leading
UTC ``ts`` so a torn last line costs at most one record. State files are written
temp-then-rename so a crash never leaves a half-written ``state.json``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """RFC3339 UTC, millisecond precision, e.g. ``2026-07-01T08:00:00.123Z``."""
    return utc_now().strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc_now().microsecond // 1000:03d}Z"


def utc_stamp() -> str:
    """Compact UTC stamp for filenames / run ids, e.g. ``20260701T080000Z``."""
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def short_uuid(n: int = 6) -> str:
    return uuid.uuid4().hex[:n]


class _JsonFormatter(logging.Formatter):
    def __init__(self, service: str, run_id: str | None = None) -> None:
        super().__init__()
        self.service = service
        self.run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": utc_now_iso(),
            "level": record.levelname.lower(),
            "service": self.service,
            "request_id": getattr(record, "run_id", None) or self.run_id,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def get_logger(
    name: str,
    *,
    service: str = "spddi-perf",
    run_id: str | None = None,
    logfile: str | os.PathLike[str] | None = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """A structured-JSON logger that writes to stderr and (optionally) a run logfile."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False
    fmt = _JsonFormatter(service=service, run_id=run_id)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if logfile:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Emit a log line carrying arbitrary structured ``fields`` (merged into JSON)."""
    logger.log(level, message, extra={"fields": fields})


# --- Durable file primitives -----------------------------------------------------

def atomic_write_json(path: str | os.PathLike[str], obj: Any) -> None:
    """Write JSON to ``path`` atomically (temp file in the same dir, then rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, default=str, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def append_ndjson(path: str | os.PathLike[str], obj: dict[str, Any]) -> None:
    """Append one record (with a leading ``ts`` if absent) to an NDJSON time-series."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if "ts" not in obj:
        obj = {"ts": utc_now_iso(), **obj}
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, default=str, separators=(",", ":")) + "\n")
