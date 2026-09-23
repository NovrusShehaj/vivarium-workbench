"""Proposals: model-suggested file changes awaiting human review.

Nothing is written to the workspace when a proposal is created. Each file
change carries the SHA-256 of the file it was computed against (stale-diff
detection), the server-computed unified diff, validation results and an
"executable code" flag. Proposals (with undo snapshots once applied) are stored
in the per-user data directory (``0600``), never in the workspace.
"""
from __future__ import annotations

import difflib
import json
import re
import secrets as _secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from vivarium_workbench.lib import user_dirs
from vivarium_workbench_assistant.conversations import assistant_data_dir, scope_key, workspace_key

PID_RE = re.compile(r"^p_[0-9a-f]{16}$")
MAX_FILES = 20
MAX_CONTENT = 512 * 1024


class FileChange(BaseModel):
    path: str
    op: Literal["modify", "create", "delete"]
    base_sha256: str | None
    new_content: str | None
    unified_diff: str
    additions: int = 0
    deletions: int = 0
    validation: list[dict[str, Any]] = Field(default_factory=list)
    executable_code: bool = False
    rationale: str = ""
    status: Literal["pending", "applied", "rejected", "conflict", "undone"] = "pending"
    after_sha256: str | None = None
    message: str | None = None


class Proposal(BaseModel):
    id: str
    conversation_id: str
    run_id: str
    created_at: float
    files: list[FileChange] = Field(default_factory=list)
    status: Literal["pending", "applied", "partially_applied", "rejected", "undone", "conflict"] = "pending"
    #: path -> original text (``None`` = the file did not exist). Filled on apply.
    undo: dict[str, str | None] = Field(default_factory=dict)
    commit_sha: str | None = None
    model_label: str = ""

    def file(self, path: str) -> FileChange | None:
        return next((f for f in self.files if f.path == path), None)

    def refresh_status(self) -> None:
        states = {f.status for f in self.files}
        if not states or states == {"pending"}:
            self.status = "pending"
        elif states <= {"applied"}:
            self.status = "applied"
        elif states <= {"rejected"}:
            self.status = "rejected"
        elif states <= {"undone", "rejected"}:
            self.status = "undone"
        elif "applied" in states:
            self.status = "partially_applied"
        elif "conflict" in states:
            self.status = "conflict"
        else:
            self.status = "pending"

    def public(self) -> dict[str, Any]:
        """The client view: no undo snapshots."""
        data = self.model_dump(mode="json", exclude={"undo"})
        data["can_undo"] = any(f.status == "applied" for f in self.files)
        return data


def new_proposal_id() -> str:
    return "p_" + _secrets.token_hex(8)


def unified_diff(path: str, before: str | None, after: str | None) -> tuple[str, int, int]:
    a = (before or "").splitlines(keepends=True)
    b = (after or "").splitlines(keepends=True)
    diff = list(difflib.unified_diff(a, b, fromfile=f"a/{path}" if before is not None else "/dev/null",
                                     tofile=f"b/{path}" if after is not None else "/dev/null", n=3))
    adds = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    dels = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    text = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in diff)
    return text, adds, dels


class PatchError(ValueError):
    pass


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def apply_unified_patch(original: str, patch: str) -> str:
    """Apply a unified diff to ``original`` strictly (every context line must match)."""
    src = original.splitlines(keepends=True)
    out: list[str] = []
    pos = 0
    lines = patch.splitlines(keepends=True)
    i = 0
    saw_hunk = False
    while i < len(lines):
        line = lines[i]
        if line.startswith(("---", "+++", "diff ", "index ")):
            i += 1
            continue
        m = _HUNK_RE.match(line)
        if not m:
            i += 1
            continue
        saw_hunk = True
        start = int(m.group(1)) - 1 if int(m.group(1)) > 0 else 0
        if start < pos:
            raise PatchError("overlapping or out-of-order hunks")
        out.extend(src[pos:start])
        pos = start
        i += 1
        last = ""
        while i < len(lines) and not lines[i].startswith("@@"):
            h = lines[i]
            if h.startswith("\\"):
                # "\ No newline at end of file" applies to the line before it.
                if last in ("+", " ") and out and out[-1].endswith("\n"):
                    out[-1] = out[-1][:-1]
                i += 1
                continue
            if h in ("\n", "\r\n"):          # an empty context line whose space was stripped
                h = " " + h
            tag, body = h[:1], h[1:]
            if tag == " ":
                if pos >= len(src) or src[pos].rstrip("\r\n") != body.rstrip("\r\n"):
                    raise PatchError(f"context mismatch near line {pos + 1}")
                out.append(src[pos])
                pos += 1
            elif tag == "-":
                if pos >= len(src) or src[pos].rstrip("\r\n") != body.rstrip("\r\n"):
                    raise PatchError(f"removed line does not match near line {pos + 1}")
                pos += 1
            elif tag == "+":
                out.append(body if body.endswith("\n") else body + "\n")
            elif h.startswith(("---", "+++", "diff ")):
                break
            else:
                raise PatchError("unrecognised patch line")
            last = tag
            i += 1
    if not saw_hunk:
        raise PatchError("the patch contains no hunks")
    out.extend(src[pos:])
    return "".join(out)


class ProposalStore:
    def __init__(self, *, root: Callable[[], Path] | None = None) -> None:
        self._root = root or assistant_data_dir
        self._lock = threading.RLock()
        self._mem: dict[tuple[str, str, str], Proposal] = {}
        #: (scope, ws_key, run_id) -> proposal id, so a run's calls share one proposal
        self._run_index: dict[tuple[str, str, str], str] = {}

    def _path(self, ws_root: Path, scope: str, pid: str) -> Path:
        if not PID_RE.match(pid):
            raise KeyError(pid)
        base = self._root() / "workspaces" / workspace_key(ws_root)
        if scope != "local":
            base = base / "sessions" / scope_key(scope)
        return base / "proposals" / f"{pid}.json"

    def save(self, ws_root: Path, p: Proposal, *, scope: str, persist: bool) -> None:
        with self._lock:
            if not persist:
                self._mem[(scope, workspace_key(ws_root), p.id)] = p.model_copy(deep=True)
                return
            user_dirs.write_private_text(self._path(ws_root, scope, p.id),
                                         json.dumps(p.model_dump(mode="json")) + "\n")

    def get(self, ws_root: Path, pid: str, *, scope: str, persist: bool) -> Proposal:
        with self._lock:
            if not persist:
                p = self._mem.get((scope, workspace_key(ws_root), pid))
                if p is None:
                    raise KeyError(pid)
                return p.model_copy(deep=True)
            path = self._path(ws_root, scope, pid)
            if not path.is_file():
                raise KeyError(pid)
            return Proposal.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def for_run(self, ws_root: Path, run_id: str, conversation_id: str, *, scope: str, persist: bool,
                model_label: str = "") -> Proposal:
        """The (single) proposal collecting a run's propose_* calls."""
        with self._lock:
            key = (scope, workspace_key(ws_root), run_id)
            pid = self._run_index.get(key)
            if pid is not None:
                try:
                    return self.get(ws_root, pid, scope=scope, persist=persist)
                except KeyError:
                    pass
            p = Proposal(id=new_proposal_id(), conversation_id=conversation_id, run_id=run_id,
                         created_at=time.time(), model_label=model_label)
            self._run_index[key] = p.id
            self.save(ws_root, p, scope=scope, persist=persist)
            return p
