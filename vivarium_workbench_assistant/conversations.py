"""Conversation persistence: append-only JSONL in the per-user data directory.

Layout (plan §6.6)::

    <data dir>/assistant/workspaces/<ws_key>/workspace.json
    <data dir>/assistant/workspaces/<ws_key>/conversations/<cid>.jsonl

``ws_key = sha256(realpath(ws_root))[:16]``. Directories are ``0700`` and
files ``0600``. Conversations never live inside the workspace (the catch-all
route serves the workspace tree, and it is a shared git repository) nor in
browser storage.

Records, one JSON object per line: a ``header``, then ``message`` records
(user/assistant/tool, with ``parent_id`` branching for regenerate), ``rename``
and ``delete_message``. Only the context *manifest* (ids, paths, hashes, token
estimates) is stored — never a second copy of file contents — and message
text is redacted of known credentials before it is written.

When persistence is off (a preference, or the default on hosted deployments)
the same API is served from memory for the server process lifetime. On a
hosted deployment every conversation is additionally scoped to the browser
session, so sessions never see each other's history.
"""
from __future__ import annotations

import builtins
import hashlib
import json
import os
import re
import secrets as _secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from vivarium_workbench.lib import user_dirs
from vivarium_workbench_assistant.secrets import get_logger, redact_obj

log = get_logger("vivarium_workbench_assistant.conversations")

CID_RE = re.compile(r"^c_[0-9a-f]{16}$")
MID_RE = re.compile(r"^m_[0-9a-f]{12}$")
MAX_TITLE = 120
MAX_RECORDS = 20_000
SCHEMA_VERSION = 1


class ConversationNotFound(KeyError):
    pass


def workspace_key(ws_root: Path) -> str:
    real = os.path.realpath(str(ws_root))
    return hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]


def scope_key(scope: str) -> str:
    """Directory-safe form of a session scope ("local" stays readable)."""
    if scope == "local":
        return "local"
    return "s_" + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:20]


def new_conversation_id() -> str:
    return "c_" + _secrets.token_hex(8)


def new_message_id() -> str:
    return "m_" + _secrets.token_hex(6)


def derive_title(text: str) -> str:
    line = " ".join((text or "").split())
    return (line[:60] + "…") if len(line) > 60 else (line or "New conversation")


def assistant_data_dir() -> Path:
    return user_dirs.user_data_dir() / "assistant"


class ConversationStore:
    def __init__(self, *, root: Callable[[], Path] | None = None) -> None:
        self._root = root or assistant_data_dir
        self._lock = threading.RLock()
        # In-memory conversations: (scope, ws_key) -> cid -> records
        self._mem: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}

    # -- locations --------------------------------------------------------------
    def _conv_dir(self, ws_root: Path, scope: str) -> Path:
        base = self._root() / "workspaces" / workspace_key(ws_root)
        if scope != "local":
            base = base / "sessions" / scope_key(scope)
        return base / "conversations"

    def _file(self, ws_root: Path, scope: str, cid: str) -> Path:
        if not CID_RE.match(cid):
            raise ConversationNotFound(cid)
        return self._conv_dir(ws_root, scope) / f"{cid}.jsonl"

    def _write_workspace_json(self, ws_root: Path) -> None:
        base = self._root() / "workspaces" / workspace_key(ws_root)
        target = base / "workspace.json"
        if target.is_file():
            return
        user_dirs.write_private_text(target, json.dumps({
            "path": os.path.realpath(str(ws_root)), "name": Path(ws_root).name,
        }) + "\n")

    # -- raw record IO ------------------------------------------------------------
    def _read(self, ws_root: Path, scope: str, cid: str, persist: bool) -> list[dict[str, Any]]:
        if not persist:
            recs = self._mem.get((scope, workspace_key(ws_root)), {}).get(cid)
            if recs is None:
                raise ConversationNotFound(cid)
            return list(recs)
        path = self._file(ws_root, scope, cid)
        if not path.is_file():
            raise ConversationNotFound(cid)
        out: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= MAX_RECORDS:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue            # a torn final line after a crash is skipped
                if isinstance(rec, dict):
                    out.append(rec)
        if not out or out[0].get("t") != "header":
            raise ConversationNotFound(cid)
        return out

    def _append(self, ws_root: Path, scope: str, cid: str, record: dict[str, Any], persist: bool) -> None:
        record = redact_obj(record)
        if not persist:
            bucket = self._mem.setdefault((scope, workspace_key(ws_root)), {})
            if cid not in bucket and record.get("t") != "header":
                raise ConversationNotFound(cid)
            bucket.setdefault(cid, []).append(record)
            return
        path = self._file(ws_root, scope, cid)
        if record.get("t") != "header" and not path.is_file():
            raise ConversationNotFound(cid)
        user_dirs.ensure_private_dir(path.parent)
        user_dirs.append_private_line(path, json.dumps(record, separators=(",", ":"), ensure_ascii=False))

    # -- public API ---------------------------------------------------------------
    def create(self, ws_root: Path, *, scope: str, persist: bool, title: str | None = None) -> dict[str, Any]:
        with self._lock:
            cid = new_conversation_id()
            header = {
                "t": "header", "v": SCHEMA_VERSION, "conversation_id": cid,
                "workspace_key": workspace_key(ws_root), "workspace_name": Path(ws_root).name,
                "title": (title or "New conversation")[:MAX_TITLE], "created_at": time.time(),
            }
            if persist:
                self._write_workspace_json(ws_root)
            self._append(ws_root, scope, cid, header, persist)
            return self._summary([header])

    def append_message(self, ws_root: Path, cid: str, message: dict[str, Any], *,
                       scope: str, persist: bool) -> dict[str, Any]:
        with self._lock:
            recs = self._read(ws_root, scope, cid, persist)
            msg = dict(message)
            msg["t"] = "message"
            msg.setdefault("id", new_message_id())
            msg.setdefault("created_at", time.time())
            self._append(ws_root, scope, cid, msg, persist)
            # Auto-title from the first user message.
            header = recs[0]
            has_title = any(r.get("t") == "rename" for r in recs) or header.get("title") not in (None, "", "New conversation")
            if msg.get("role") == "user" and not has_title:
                text = "".join(p.get("text", "") for p in msg.get("parts") or [] if isinstance(p, dict))
                self._append(ws_root, scope, cid, {"t": "rename", "title": derive_title(text),
                                                   "at": time.time(), "auto": True}, persist)
            return redact_obj(msg)

    def rename(self, ws_root: Path, cid: str, title: str, *, scope: str, persist: bool) -> dict[str, Any]:
        with self._lock:
            self._read(ws_root, scope, cid, persist)
            self._append(ws_root, scope, cid, {"t": "rename", "title": title.strip()[:MAX_TITLE] or "Untitled",
                                               "at": time.time()}, persist)
            return self.get(ws_root, cid, scope=scope, persist=persist)

    def get(self, ws_root: Path, cid: str, *, scope: str, persist: bool) -> dict[str, Any]:
        with self._lock:
            recs = self._read(ws_root, scope, cid, persist)
        summary = self._summary(recs)
        deleted = {r.get("id") for r in recs if r.get("t") == "delete_message"}
        summary["messages"] = [
            {k: v for k, v in r.items() if k != "t"}
            for r in recs if r.get("t") == "message" and r.get("id") not in deleted
        ]
        return summary

    def list(self, ws_root: Path, *, scope: str, persist: bool,
             retention_days: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if retention_days:
                self.apply_retention(ws_root, retention_days, scope=scope, persist=persist)
            out: list[dict[str, Any]] = []
            if not persist:
                for cid, recs in self._mem.get((scope, workspace_key(ws_root)), {}).items():
                    out.append(self._summary(recs))
            else:
                d = self._conv_dir(ws_root, scope)
                if d.is_dir():
                    for path in d.glob("c_*.jsonl"):
                        cid = path.stem
                        if not CID_RE.match(cid):
                            continue
                        try:
                            out.append(self._summary(self._read(ws_root, scope, cid, True)))
                        except ConversationNotFound:
                            continue
            out.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
            return out

    def delete(self, ws_root: Path, cid: str, *, scope: str, persist: bool) -> None:
        with self._lock:
            if not persist:
                bucket = self._mem.get((scope, workspace_key(ws_root)), {})
                if bucket.pop(cid, None) is None:
                    raise ConversationNotFound(cid)
                return
            path = self._file(ws_root, scope, cid)
            if not path.is_file():
                raise ConversationNotFound(cid)
            path.unlink()

    def delete_all(self, ws_root: Path, *, scope: str, persist: bool) -> int:
        with self._lock:
            if not persist:
                bucket = self._mem.pop((scope, workspace_key(ws_root)), {})
                return len(bucket)
            n = 0
            d = self._conv_dir(ws_root, scope)
            if d.is_dir():
                for path in d.glob("c_*.jsonl"):
                    try:
                        path.unlink()
                        n += 1
                    except OSError:
                        pass
            return n

    def apply_retention(self, ws_root: Path, days: int, *, scope: str, persist: bool) -> int:
        cutoff = time.time() - days * 86400
        removed = 0
        for s in builtins.list(self._iter_summaries(ws_root, scope, persist)):
            if (s.get("updated_at") or 0) < cutoff:
                try:
                    self.delete(ws_root, s["id"], scope=scope, persist=persist)
                    removed += 1
                except ConversationNotFound:
                    pass
        return removed

    def clear_memory_scope(self, scope: str) -> None:
        with self._lock:
            for key in [k for k in self._mem if k[0] == scope]:
                self._mem.pop(key, None)

    # -- helpers --------------------------------------------------------------------
    def _iter_summaries(self, ws_root: Path, scope: str, persist: bool) -> builtins.list[dict[str, Any]]:
        if not persist:
            return [self._summary(r) for r in self._mem.get((scope, workspace_key(ws_root)), {}).values()]
        d = self._conv_dir(ws_root, scope)
        out: builtins.list[dict[str, Any]] = []
        if d.is_dir():
            for path in d.glob("c_*.jsonl"):
                try:
                    out.append(self._summary(self._read(ws_root, scope, path.stem, True)))
                except ConversationNotFound:
                    continue
        return out

    @staticmethod
    def _summary(recs: builtins.list[dict[str, Any]]) -> dict[str, Any]:
        header = recs[0]
        title = header.get("title") or "New conversation"
        updated = header.get("created_at") or 0
        count = 0
        for r in recs[1:]:
            if r.get("t") == "rename" and r.get("title"):
                title = r["title"]
            if r.get("t") == "message":
                count += 1
            ts = r.get("created_at") or r.get("at")
            if isinstance(ts, (int, float)) and ts > updated:
                updated = ts
        return {"id": header.get("conversation_id"), "title": title,
                "created_at": header.get("created_at"), "updated_at": updated, "message_count": count}


def branch_to(messages: list[dict[str, Any]], leaf_id: str | None) -> list[dict[str, Any]]:
    """The ``parent_id`` chain ending at ``leaf_id`` (oldest first)."""
    by_id = {m.get("id"): m for m in messages}
    chain: list[dict[str, Any]] = []
    cur = by_id.get(leaf_id) if leaf_id else None
    seen: set[str] = set()
    while cur is not None and cur.get("id") not in seen:
        seen.add(str(cur.get("id")))
        chain.append(cur)
        cur = by_id.get(cur.get("parent_id"))
    chain.reverse()
    return chain


def latest_leaf(messages: list[dict[str, Any]]) -> str | None:
    """The newest message that no other message continues (the active branch tip)."""
    parents = {m.get("parent_id") for m in messages}
    leaves = [m for m in messages if m.get("id") not in parents]
    if not leaves:
        return None
    leaves.sort(key=lambda m: m.get("created_at") or 0)
    return leaves[-1].get("id")
