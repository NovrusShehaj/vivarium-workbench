"""Append-only audit log of assistant actions (``<data dir>/assistant/audit.jsonl``).

One JSON object per line: timestamp, a *hash* of the browser session, the
conversation and run, the event (``tool_call``, ``approval``, ``denial``,
``edit_applied``, ``edit_undone``, ``commit``, ``credential_set``,
``credential_removed``, ``provider_error`` …), the tool with **redacted**
arguments, the decision, duration and result size, and for edits the path with
before/after SHA-256 and the commit SHA.

Never logged: API keys, ``Authorization`` headers, raw secret environment
values, request/response bodies. Every record passes through
``secrets.redact_obj`` (exact known secrets + credential shapes) first.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from vivarium_workbench.lib import user_dirs
from vivarium_workbench_assistant.secrets import get_logger, redact_obj

log = get_logger("vivarium_workbench_assistant.audit")

_DROP_KEYS = frozenset({"api_key", "access_token", "authorization", "headers", "password", "secret", "token"})


def session_hash(session_key: str | None) -> str | None:
    if not session_key:
        return None
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:12]


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("[redacted]" if str(k).lower() in _DROP_KEYS else _scrub(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


class AuditLog:
    def __init__(self, path: Callable[[], Path]) -> None:
        self._path = path
        self._lock = threading.Lock()

    def record(self, event: str, *, session_key: str | None = None, conversation: str | None = None,
               run: str | None = None, **fields: Any) -> None:
        rec: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "event": event,
            "session": session_hash(session_key),
            "conversation": conversation,
            "run": run,
        }
        for k, v in fields.items():
            if v is not None:
                rec[k] = v
        rec = redact_obj(_scrub(rec))
        line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False, default=str)
        try:
            with self._lock:
                user_dirs.append_private_line(self._path(), line)
        except OSError as exc:
            log.warning("audit log write failed: %s", type(exc).__name__)

    def tail(self, n: int = 200) -> list[dict[str, Any]]:
        path = self._path()
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
