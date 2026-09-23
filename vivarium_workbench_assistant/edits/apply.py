"""Apply / undo reviewed proposals, and scoped, attributed commits.

Apply (per file, only after explicit review in the UI):

1. re-resolve the path through the sandbox for writing (no ``.git/``,
   ``.pbg/``, ``reports/``, data artifacts, symlinks or denylisted files);
2. **stale check** — the file's current SHA-256 must equal the proposal's
   ``base_sha256`` (a create requires the file to still be absent); otherwise
   the file becomes ``conflict`` ("File changed since proposal; regenerate");
3. snapshot the original bytes for **undo**, then write atomically
   (``vivarium_workbench.lib.atomic_io``) or delete.

Commit (optional, "Apply & commit") stages and commits **exactly the applied
paths** — never ``git add -A`` — with ``Assisted-by``/``Assistant-Model``/
``Assistant-Conversation`` trailers. A path is left out of the commit (and
reported) when it had uncommitted or staged changes of its own before the
assistant touched it, so unrelated work is never swept into an AI commit; the
commit is skipped entirely when a workstream branch is active but not checked
out. Undo restores the snapshot only while the file still has the content the
assistant wrote.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from vivarium_workbench.lib.atomic_io import atomic_write_text
from vivarium_workbench_assistant import sandbox
from vivarium_workbench_assistant.edits.proposals import Proposal
from vivarium_workbench_assistant.sandbox import SandboxError

GIT_TIMEOUT_S = 30


def _sha_of(path: Path) -> str | None:
    if not path.exists():
        return None
    return sandbox.sha256_bytes(path.read_bytes())


def _git(ws_root: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER",
                                                         "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                                                         "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(["git", "-C", str(ws_root), *args], capture_output=True, text=True,
                          timeout=GIT_TIMEOUT_S, input=input_text, env=env)


def _head_blob_sha(ws_root: Path, rel: str) -> str | None:
    """SHA-256 of ``rel`` at HEAD (``None`` if absent or not a git repository)."""
    r = subprocess.run(["git", "-C", str(ws_root), "show", f"HEAD:{rel}"], capture_output=True,
                       timeout=GIT_TIMEOUT_S)
    if r.returncode != 0:
        return None
    return sandbox.sha256_bytes(r.stdout)


def _is_git_repo(ws_root: Path) -> bool:
    try:
        return _git(ws_root, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def _staged(ws_root: Path, rel: str) -> bool:
    r = _git(ws_root, "diff", "--cached", "--name-only", "-z", "--", rel)
    return bool(r.stdout.strip("\0"))


def apply_files(ws_root: Path, proposal: Proposal, paths: list[str] | None) -> dict[str, Any]:
    """Apply the selected pending files. Mutates ``proposal``; returns per-file results."""
    results: list[dict[str, Any]] = []
    for fc in proposal.files:
        if paths is not None and fc.path not in paths:
            continue
        if fc.status != "pending":
            results.append({"path": fc.path, "status": fc.status, "message": "not pending"})
            continue
        try:
            target = sandbox.resolve_in_workspace(ws_root, fc.path, for_write=True)
        except SandboxError as exc:
            fc.status = "conflict"
            fc.message = str(exc)
            results.append({"path": fc.path, "status": "conflict", "message": str(exc)})
            continue
        current = _sha_of(target)
        expected = fc.base_sha256 if fc.op != "create" else None
        if current != expected:
            fc.status = "conflict"
            fc.message = ("File changed since the proposal was made; ask the assistant to regenerate it."
                          if fc.op != "create" else "The file now exists; ask the assistant to regenerate.")
            results.append({"path": fc.path, "status": "conflict", "message": fc.message})
            continue
        original = target.read_text(encoding="utf-8") if target.exists() else None
        head_sha = _head_blob_sha(ws_root, fc.path)
        was_clean = (current == head_sha) and not _staged(ws_root, fc.path) if _is_git_repo(ws_root) else False
        try:
            if fc.op == "delete":
                target.unlink()
                after = None
            else:
                parent = target.parent
                root = Path(ws_root).resolve()
                if not parent.exists():
                    # Create missing directories one level at a time, inside the workspace.
                    parent.mkdir(parents=True, exist_ok=True)
                    if not parent.resolve().is_relative_to(root):
                        raise SandboxError(f"{fc.path}: parent directory escapes the workspace")
                atomic_write_text(target, fc.new_content or "")
                after = _sha_of(target)
        except (OSError, SandboxError) as exc:
            fc.status = "conflict"
            fc.message = f"could not write: {exc}"
            results.append({"path": fc.path, "status": "conflict", "message": fc.message})
            continue
        proposal.undo[fc.path] = original
        fc.status = "applied"
        fc.after_sha256 = after
        fc.message = None
        results.append({"path": fc.path, "status": "applied", "before": current, "after": after,
                        "was_clean": was_clean})
    proposal.refresh_status()
    return {"results": results}


def _active_workstream_branch(ws_root: Path) -> str | None:
    from vivarium_workbench.lib.workspace_paths import WorkspacePaths
    try:
        state_file = WorkspacePaths.load(ws_root).pbg / "state.json"
        data = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
    except (OSError, ValueError, Exception):  # noqa: BLE001
        return None
    branch = data.get("active_branch") if isinstance(data, dict) else None
    return branch if isinstance(branch, str) and branch else None


def commit_applied(ws_root: Path, proposal: Proposal, results: list[dict[str, Any]], *,
                   conversation_id: str, model_label: str) -> dict[str, Any]:
    """Commit exactly the files this apply wrote (those that were clean before)."""
    if not _is_git_repo(ws_root):
        return {"committed": False, "reason": "the workspace is not a git repository"}
    applied = [r for r in results if r.get("status") == "applied"]
    clean = [r["path"] for r in applied if r.get("was_clean")]
    skipped = [r["path"] for r in applied if not r.get("was_clean")]
    if not clean:
        return {"committed": False, "skipped": skipped,
                "reason": "every applied file had uncommitted changes of its own; nothing was committed"}
    branch = _git(ws_root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    active = _active_workstream_branch(ws_root)
    if active and branch != active:
        return {"committed": False, "skipped": skipped + clean,
                "reason": f"the active workstream is {active!r} but {branch!r} is checked out"}
    # New files must be added by name; tracked paths (incl. deletions) are
    # committed with --only so other staged work stays staged.
    untracked = [p for p in clean if _git(ws_root, "ls-files", "--error-unmatch", "--", p).returncode != 0]
    existing_untracked = [p for p in untracked if (Path(ws_root) / p).exists()]
    if existing_untracked:
        r = _git(ws_root, "add", "--", *existing_untracked)
        if r.returncode != 0:
            return {"committed": False, "reason": "git add failed", "detail": r.stderr.strip()[:300]}
    names = ", ".join(clean[:3]) + (f" and {len(clean) - 3} more" if len(clean) > 3 else "")
    message = (
        f"assistant: update {names}\n\n"
        f"Assisted-by: vivarium-workbench-assistant\n"
        f"Assistant-Model: {model_label}\n"
        f"Assistant-Conversation: {conversation_id}\n"
    )
    r = _git(ws_root, "commit", "--only", "-F", "-", "--", *clean, input_text=message)
    if r.returncode != 0:
        return {"committed": False, "skipped": skipped,
                "reason": "git commit failed", "detail": (r.stderr or r.stdout).strip()[:300]}
    sha = _git(ws_root, "rev-parse", "HEAD").stdout.strip()
    proposal.commit_sha = sha
    return {"committed": True, "commit_sha": sha, "paths": clean, "skipped": skipped}


def undo_files(ws_root: Path, proposal: Proposal, paths: list[str] | None) -> dict[str, Any]:
    """Restore applied files from their snapshots (only if unchanged since apply)."""
    results: list[dict[str, Any]] = []
    for fc in proposal.files:
        if paths is not None and fc.path not in paths:
            continue
        if fc.status != "applied":
            continue
        try:
            target = sandbox.resolve_in_workspace(ws_root, fc.path, for_write=True)
        except SandboxError as exc:
            results.append({"path": fc.path, "status": "error", "message": str(exc)})
            continue
        if _sha_of(target) != fc.after_sha256:
            results.append({"path": fc.path, "status": "conflict",
                            "message": "the file changed after it was applied; not undone"})
            continue
        original = proposal.undo.get(fc.path)
        try:
            if original is None:
                if target.exists():
                    target.unlink()
            else:
                atomic_write_text(target, original)
        except OSError as exc:
            results.append({"path": fc.path, "status": "error", "message": f"could not restore: {exc}"})
            continue
        fc.status = "undone"
        results.append({"path": fc.path, "status": "undone"})
    proposal.refresh_status()
    return {"results": results, "commit_sha": proposal.commit_sha}


def revert_commit(ws_root: Path, proposal: Proposal) -> dict[str, Any]:
    """``git revert`` the proposal's commit when its files are otherwise clean."""
    sha = proposal.commit_sha
    if not sha:
        return {"reverted": False, "reason": "this proposal was not committed"}
    touched = [f.path for f in proposal.files if f.status == "applied"]
    dirty = _git(ws_root, "status", "--porcelain", "-z", "--", *touched).stdout.strip("\0")
    if dirty:
        return {"reverted": False, "reason": "those files have uncommitted changes; revert refused"}
    r = _git(ws_root, "revert", "--no-edit", sha)
    if r.returncode != 0:
        _git(ws_root, "revert", "--abort")
        return {"reverted": False, "reason": "git revert failed", "detail": (r.stderr or r.stdout).strip()[:300]}
    for f in proposal.files:
        if f.status == "applied":
            f.status = "undone"
    proposal.refresh_status()
    return {"reverted": True, "revert_sha": _git(ws_root, "rev-parse", "HEAD").stdout.strip()}
