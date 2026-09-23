"""Filesystem sandbox for everything the assistant reads or proposes to write.

Layered rules (plan §10.5, §10.7), evaluated on the workspace-relative,
NFC-normalised, case-insensitive path:

1. **Hard denylist** — shared with the core's static route
   (``vivarium_workbench.lib.sensitive_paths``): ``.git/``, ``.env*``, keys,
   credential JSON, ``secrets/``, ``.pbg/server/``, ``.pbg/state.json`` …
   plus content sniffing for PEM private keys and service-account JSON.
2. **Containment** — the real path (symlinks followed) must stay inside the
   workspace; writes never go through a symlink, into ``.git/``, ``.pbg/``,
   ``reports/`` (generated) or onto data artifacts.
3. **``.gitignore``** — ignored files are left out of automatic context, search
   and listings; reading one needs explicit approval (local mode only).
4. **``.vwbignore``** — an optional, committed, gitignore-syntax file; matches
   are as strict as the denylist and cannot be overridden from the UI.
5. **Type and size** — binary files (a NUL byte in the first 8 KB) are
   refused; large files are truncated head/tail; data artifacts (``runs.db``,
   ``*.zarr``, ``*.parquet`` …) are never sent raw.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from vivarium_workbench.lib import sensitive_paths as _sp

BINARY_SNIFF_BYTES = 8192
DEFAULT_MAX_BYTES = 256 * 1024
GIT_TIMEOUT_S = 10

#: Data artifacts: never sent raw (the assistant uses the summary APIs instead).
DATA_ARTIFACT_GLOBS: tuple[str, ...] = (
    "runs.db", "*.db", "*.sqlite", "*.sqlite3", "*.parquet", "*.feather", "*.arrow",
    "*.h5", "*.hdf5", "*.nc", "*.npz", "*.npy", "*.pkl", "*.pickle", "*.joblib", "*.pt", "*.ckpt",
)
DATA_ARTIFACT_DIRS: tuple[str, ...] = ("parquet-runs", "out")
DATA_ARTIFACT_DIR_SUFFIXES: tuple[str, ...] = (".zarr",)

#: Directories never listed or searched (noise / environment, not project content).
SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".idea", ".vscode", "build", "dist", ".ipynb_checkpoints",
})

#: Workspace-relative prefixes a proposal may never write to.
WRITE_DENIED_PREFIXES: tuple[str, ...] = (".git", ".pbg", "reports")


class SandboxError(Exception):
    """A path was refused. The message is safe to show and to give the model."""


def _norm(rel: str) -> str:
    try:
        return _sp.normalize_rel(rel)
    except _sp.UnsafePath as exc:
        raise SandboxError(f"invalid path: {exc}") from None


def is_data_artifact(rel: str) -> bool:
    low = rel.lower()
    parts = PurePosixPath(low).parts
    if any(p in DATA_ARTIFACT_DIRS for p in parts[:-1]):
        return True
    if any(p.endswith(DATA_ARTIFACT_DIR_SUFFIXES) for p in parts):
        return True
    return any(fnmatch.fnmatchcase(parts[-1], g) for g in DATA_ARTIFACT_GLOBS) if parts else False


def _write_denied(rel: str) -> bool:
    low = rel.lower()
    return any(low == p or low.startswith(p + "/") for p in WRITE_DENIED_PREFIXES)


def resolve_in_workspace(ws_root: Path, rel: str, *, for_write: bool = False) -> Path:
    """The real path of ``rel`` inside ``ws_root``, or :class:`SandboxError`."""
    clean = _norm(rel)
    if _sp.is_sensitive_rel(clean):
        raise SandboxError(f"{clean}: denied by the sensitive-file policy")
    try:
        root = Path(ws_root).resolve(strict=True)
    except (OSError, RuntimeError):
        raise SandboxError("the workspace directory is not available") from None
    target = (root / clean).resolve(strict=False)
    if target != root and not target.is_relative_to(root):
        raise SandboxError(f"{clean}: escapes the workspace (symlink or traversal)")
    real_rel = target.relative_to(root).as_posix() if target != root else ""
    if real_rel and _sp.is_sensitive_rel(real_rel):
        raise SandboxError(f"{clean}: denied by the sensitive-file policy")
    if vwbignored(root, [clean, real_rel] if real_rel and real_rel != clean else [clean]):
        raise SandboxError(f"{clean}: excluded by .vwbignore")
    if for_write:
        if _write_denied(clean) or (real_rel and _write_denied(real_rel)):
            raise SandboxError(f"{clean}: the assistant may not write to .git/, .pbg/ or reports/")
        if is_data_artifact(clean) or (real_rel and is_data_artifact(real_rel)):
            raise SandboxError(f"{clean}: data artifacts are never edited by the assistant")
        if (root / clean).is_symlink():
            raise SandboxError(f"{clean}: refusing to write through a symlink")
        parent = target.parent
        if parent.exists() and not parent.resolve().is_relative_to(root):
            raise SandboxError(f"{clean}: parent directory escapes the workspace")
    return target


@dataclass
class FileRead:
    path: str
    text: str
    size: int
    sha256: str
    truncated: bool


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes_checked(ws_root: Path, rel: str) -> tuple[Path, bytes]:
    """Read a file after the sandbox checks, narrowing the TOCTOU window.

    Opens with ``O_NOFOLLOW`` where available and compares the opened file's
    ``fstat`` with the checked path's ``stat``.
    """
    target = resolve_in_workspace(ws_root, rel)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except FileNotFoundError:
        raise SandboxError(f"{rel}: no such file") from None
    except IsADirectoryError:
        raise SandboxError(f"{rel}: is a directory") from None
    except OSError as exc:
        raise SandboxError(f"{rel}: cannot be opened ({exc.strerror or 'error'})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise SandboxError(f"{rel}: not a regular file")
        try:
            st2 = os.stat(target)
        except OSError:
            raise SandboxError(f"{rel}: changed while opening") from None
        if (st.st_ino, st.st_dev) != (st2.st_ino, st2.st_dev):
            raise SandboxError(f"{rel}: changed while opening")
        if st.st_size > 64 * 1024 * 1024:
            raise SandboxError(f"{rel}: too large ({st.st_size} bytes)")
        chunks = []
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            chunks.append(b)
        return target, b"".join(chunks)
    finally:
        os.close(fd)


def read_text_file(ws_root: Path, rel: str, *, max_bytes: int = DEFAULT_MAX_BYTES,
                   allow_data_artifacts: bool = False) -> FileRead:
    clean = _norm(rel)
    if not allow_data_artifacts and is_data_artifact(clean):
        raise SandboxError(f"{clean}: data artifacts are summarised, never sent raw")
    _target, data = read_bytes_checked(ws_root, clean)
    head = data[:BINARY_SNIFF_BYTES]
    if b"\x00" in head:
        raise SandboxError(f"{clean}: binary file")
    if _sp.looks_like_credential(data[: 1 << 20]):
        raise SandboxError(f"{clean}: contains credential material and is never read")
    digest = sha256_bytes(data)
    text = data.decode("utf-8", errors="replace")
    truncated = False
    if len(data) > max_bytes:
        half = max_bytes // 2
        head_t = data[:half].decode("utf-8", errors="ignore")
        tail_t = data[-half:].decode("utf-8", errors="ignore")
        omitted = len(data) - 2 * half
        text = f"{head_t}\n\n[… {omitted} bytes omitted …]\n\n{tail_t}"
        truncated = True
    return FileRead(path=clean, text=text, size=len(data), sha256=digest, truncated=truncated)


# ---------------------------------------------------------------------------
# Ignore rules
# ---------------------------------------------------------------------------

def _is_git_repo(root: Path) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def gitignored(ws_root: Path, rels: list[str]) -> set[str]:
    """The subset of ``rels`` git ignores (empty outside a git repository)."""
    rels = [r for r in rels if r]
    if not rels:
        return set()
    try:
        r = subprocess.run(
            ["git", "-C", str(ws_root), "check-ignore", "--stdin", "-z"],
            input="\0".join(rels) + "\0", capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if r.returncode not in (0, 1):
        return set()
    return {p for p in r.stdout.split("\0") if p}


def _vwb_patterns(root: Path) -> list[str]:
    f = root / ".vwbignore"
    if not f.is_file():
        return []
    out = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _fallback_match(patterns: list[str], rel: str) -> bool:
    """A small gitignore-style matcher (last match wins; ``!`` negates)."""
    low = rel.lower()
    parts = PurePosixPath(low).parts
    matched = False
    for raw in patterns:
        neg = raw.startswith("!")
        pat = raw[1:] if neg else raw
        pat = pat.lower()
        dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        anchored = pat.startswith("/") or "/" in pat
        pat = pat.lstrip("/")
        hit = False
        if anchored:
            hit = fnmatch.fnmatchcase(low, pat) or any(
                fnmatch.fnmatchcase("/".join(parts[: i + 1]), pat) for i in range(len(parts) - 1))
        else:
            candidates = parts[:-1] if dir_only else parts
            hit = any(fnmatch.fnmatchcase(p, pat) for p in candidates)
            if not hit and "**" in pat:
                hit = fnmatch.fnmatchcase(low, pat.replace("**/", "*"))
        if hit:
            matched = not neg
    return matched


def vwbignored_set(ws_root: Path, rels: list[str]) -> set[str]:
    """The subset of ``rels`` matched by the workspace's ``.vwbignore``.

    Uses git's own matcher (``check-ignore --no-index`` with ``.vwbignore`` as
    the excludes file, keeping only matches whose source is ``.vwbignore``);
    falls back to a small gitignore-style matcher outside a git repository.
    """
    root = Path(ws_root)
    patterns = _vwb_patterns(root)
    rels = [r for r in rels if r]
    if not patterns or not rels:
        return set()
    if _is_git_repo(root):
        try:
            r = subprocess.run(
                ["git", "-C", str(root), "-c", f"core.excludesFile={root / '.vwbignore'}",
                 "check-ignore", "--no-index", "-v", "--stdin", "-z"],
                input="\0".join(rels) + "\0", capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
            )
            if r.returncode in (0, 1):
                fields = r.stdout.split("\0")
                hits: set[str] = set()
                for i in range(0, len(fields) - 3, 4):
                    source, _line, pattern, path = fields[i], fields[i + 1], fields[i + 2], fields[i + 3]
                    if source.endswith(".vwbignore") and not pattern.startswith("!"):
                        hits.add(path)
                return hits
        except (OSError, subprocess.SubprocessError):
            pass
    return {r for r in rels if _fallback_match(patterns, r)}


def vwbignored(ws_root: Path, rels: list[str]) -> bool:
    """True when any of ``rels`` matches the workspace's ``.vwbignore``."""
    return bool(vwbignored_set(ws_root, rels))


def list_dir(ws_root: Path, rel: str = "", *, limit: int = 500) -> list[dict[str, object]]:
    """Entries of a workspace directory (sensitive/ignored/noise entries omitted)."""
    root = Path(ws_root).resolve()
    base = root if not rel else resolve_in_workspace(root, rel)
    if not base.is_dir():
        raise SandboxError(f"{rel or '.'}: not a directory")
    entries = []
    names = sorted(os.listdir(base))
    rel_prefix = "" if base == root else base.relative_to(root).as_posix() + "/"
    candidates = [rel_prefix + n for n in names]
    ignored = gitignored(root, candidates)
    for name, child_rel in zip(names, candidates):
        if name in SKIP_DIRS or child_rel in ignored:
            continue
        if _sp.is_sensitive_rel(child_rel):
            continue
        p = base / name
        try:
            real = p.resolve()
            if not real.is_relative_to(root):
                continue
        except OSError:
            continue
        is_dir = p.is_dir()
        entry: dict[str, object] = {"name": name, "path": child_rel, "type": "dir" if is_dir else "file"}
        if not is_dir:
            try:
                entry["size"] = p.stat().st_size
            except OSError:
                pass
        entries.append(entry)
        if len(entries) >= limit:
            break
    excluded = vwbignored_set(root, [str(e["path"]) for e in entries])
    return [e for e in entries if e["path"] not in excluded]
