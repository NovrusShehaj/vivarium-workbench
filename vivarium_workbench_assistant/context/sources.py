"""Context sources: resolve one context spec into text, with provenance.

Every source takes the workspace root explicitly (never a process global) and
reads through the sandbox or the core's library functions in-process — never
over loopback HTTP, and never by importing the workspace's own Python (the
HTTP process must not execute workspace code).
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from vivarium_workbench.lib import sensitive_paths as _sp
from vivarium_workbench_assistant import sandbox
from vivarium_workbench_assistant.sandbox import SandboxError

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$", re.IGNORECASE)
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
COMPOSITE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,199}$")
MAX_ITEM_CHARS = 200_000
GIT_TIMEOUT_S = 15


class ContextError(Exception):
    """A spec could not be resolved (message is safe to show)."""


@dataclass
class Resolved:
    kind: str
    label: str
    content: str
    path: str | None = None
    sha256: str = ""
    truncated: bool = False
    flags: set[str] = field(default_factory=set)
    #: Needs explicit approval (gitignored file) before it may be sent.
    needs_confirmation: str | None = None


def _git(ws_root: Path, *args: str, input_text: str | None = None, limit: int = 262_144) -> str:
    try:
        r = subprocess.run(["git", "-C", str(ws_root), *args], capture_output=True, text=True,
                           timeout=GIT_TIMEOUT_S, input=input_text)
    except (OSError, subprocess.SubprocessError):
        raise ContextError("git is not available for this workspace") from None
    if r.returncode not in (0, 1):
        raise ContextError("git could not read this workspace (is it a git repository?)")
    return r.stdout[:limit]


def _yaml_text(path_rel: str, ws_root: Path) -> sandbox.FileRead:
    return sandbox.read_text_file(ws_root, path_rel)


def page_summary(ws_root: Path, spec: dict[str, Any]) -> Resolved:
    lines = [f"Workspace: {Path(ws_root).name}"]
    page = str(spec.get("page") or "")
    if page and re.match(r"^[a-z][a-z0-9-]{0,40}$", page):
        lines.append(f"Current page: {page}")
    inv = spec.get("investigation")
    if isinstance(inv, str) and SLUG_RE.match(inv):
        lines.append(f"Active investigation: {inv}")
    st = spec.get("study")
    if isinstance(st, str) and SLUG_RE.match(st):
        lines.append(f"Open study: {st}")
    comp = spec.get("composite")
    if isinstance(comp, str) and COMPOSITE_ID_RE.match(comp):
        lines.append(f"Open composite: {comp}")
    return Resolved(kind="page_summary", label="Page summary", content="\n".join(lines))


def study(ws_root: Path, spec: dict[str, Any]) -> Resolved:
    slug = str(spec.get("slug") or "")
    if not SLUG_RE.match(slug):
        raise ContextError("invalid study slug")
    from vivarium_workbench.lib.study_spec import study_spec_path
    path = study_spec_path(ws_root, slug)
    try:
        rel = path.resolve().relative_to(Path(ws_root).resolve()).as_posix()
    except (ValueError, OSError):
        raise ContextError(f"study {slug} was not found") from None
    if not path.is_file():
        raise ContextError(f"study {slug} was not found")
    fr = _yaml_text(rel, ws_root)
    parts = [f"# {rel}\n{fr.text}"]
    try:
        from vivarium_workbench.lib.study_spec import load_study_detail_spec
        detail = load_study_detail_spec(ws_root, slug) or {}
        status = {k: detail.get(k) for k in (
            "_effective_status", "phase", "design_status", "implementation_status", "simulation_status",
            "evaluation_status", "gate_status", "expert_review_status", "computed_gate_verdict",
            "derived_status", "status_disagreements") if detail.get(k) not in (None, "", [], {})}
        if status:
            parts.append("# computed status\n" + json.dumps(status, indent=2, default=str)[:20_000])
    except Exception:  # noqa: BLE001 - the spec alone is still useful context
        pass
    return Resolved(kind="study", label=f"study: {slug}", content="\n\n".join(parts), path=rel,
                    sha256=fr.sha256, truncated=fr.truncated)


def investigation(ws_root: Path, spec: dict[str, Any]) -> Resolved:
    slug = str(spec.get("slug") or "")
    if not SLUG_RE.match(slug):
        raise ContextError("invalid investigation slug")
    from vivarium_workbench.lib.workspace_paths import WorkspacePaths
    inv_dir = WorkspacePaths.load(ws_root).investigations / slug
    for name in ("investigation.yaml", "spec.yaml"):
        p = inv_dir / name
        if p.is_file():
            rel = p.resolve().relative_to(Path(ws_root).resolve()).as_posix()
            fr = _yaml_text(rel, ws_root)
            return Resolved(kind="investigation", label=f"investigation: {slug}",
                            content=f"# {rel}\n{fr.text}", path=rel, sha256=fr.sha256, truncated=fr.truncated)
    raise ContextError(f"investigation {slug} was not found")


def composite(ws_root: Path, spec: dict[str, Any]) -> Resolved:
    cid = str(spec.get("id") or "")
    if not COMPOSITE_ID_RE.match(cid):
        raise ContextError("invalid composite id")
    ws_yaml = Path(ws_root) / "workspace.yaml"
    try:
        ws_data = yaml.safe_load(ws_yaml.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        ws_data = {}
    package = ws_data.get("package_path") or ("pbg_" + str(ws_data.get("name", "")).replace("-", "_"))
    from vivarium_workbench.lib.composite_lookup import discover_workspace_composites
    recs = discover_workspace_composites(Path(ws_root), package)
    rec = recs.get(cid)
    if rec is None:
        raise ContextError(f"composite {cid} is not a workspace composite (installed composites are "
                           "not read as files)")
    rel = str(rec.get("source") or "")
    fr = sandbox.read_text_file(ws_root, rel)
    return Resolved(kind="composite", label=f"composite: {cid}", content=f"# {rel}\n{fr.text}",
                    path=rel, sha256=fr.sha256, truncated=fr.truncated)


def run_log(ws_root: Path, spec: dict[str, Any]) -> Resolved:
    run_id = str(spec.get("run_id") or "")
    if not RUN_ID_RE.match(run_id):
        raise ContextError("invalid run id")
    tail = int(spec.get("tail_lines") or 200)
    tail = max(10, min(tail, 1000))
    from vivarium_workbench.lib.workspace_paths import WorkspacePaths
    pbg = WorkspacePaths.load(ws_root).pbg
    log_path = pbg / "runs" / run_id / "run.log"
    try:
        rel = log_path.resolve().relative_to(Path(ws_root).resolve()).as_posix()
    except (ValueError, OSError):
        raise ContextError("run log not found") from None
    if not log_path.is_file():
        raise ContextError(f"no run log for run {run_id}")
    _target, data = sandbox.read_bytes_checked(ws_root, rel)
    lines = data.decode("utf-8", "replace").splitlines()
    shown = lines[-tail:]
    return Resolved(kind="run_log", label=f"run log: {run_id} (last {len(shown)} lines)",
                    content="\n".join(shown), path=rel, sha256=sandbox.sha256_bytes(data),
                    truncated=len(lines) > len(shown))


def _safe_changed_paths(ws_root: Path, paths: list[str] | None) -> list[str]:
    names = _git(ws_root, "diff", "--name-only", "-z").split("\0")
    names += _git(ws_root, "diff", "--cached", "--name-only", "-z").split("\0")
    out: list[str] = []
    wanted = set(paths or [])
    for n in names:
        if not n or n in out:
            continue
        if wanted and n not in wanted:
            continue
        if _sp.is_sensitive_rel(n) or sandbox.is_data_artifact(n):
            continue
        out.append(n)
    return out


def git_diff(ws_root: Path, spec: dict[str, Any]) -> Resolved:
    paths = spec.get("paths")
    if paths is not None and (not isinstance(paths, list) or not all(isinstance(p, str) for p in paths)):
        raise ContextError("paths must be a list of workspace paths")
    safe = _safe_changed_paths(ws_root, paths)
    if not safe:
        return Resolved(kind="git_diff", label="git diff", content="(no uncommitted changes to show)")
    stat = _git(ws_root, "diff", "HEAD", "--stat", "--no-color", "--", *safe, limit=16_384)
    diff = _git(ws_root, "diff", "HEAD", "--no-color", "--no-ext-diff", "--", *safe, limit=120_000)
    truncated = len(diff) >= 120_000
    return Resolved(kind="git_diff", label=f"git diff ({len(safe)} files)",
                    content=f"$ git diff --stat\n{stat}\n$ git diff\n{diff}", truncated=truncated)


def manifest(ws_root: Path, spec: dict[str, Any]) -> Resolved:
    from vivarium_workbench.lib import workspace_manifest_views as mv
    data: dict[str, Any] = {}
    for key, fn in (("workspace", mv.manifest_workspace_section), ("studies", mv.manifest_studies_section),
                    ("health", mv.manifest_health_section)):
        try:
            data[key] = fn(Path(ws_root))
        except Exception as exc:  # noqa: BLE001 - one broken section must not hide the rest
            data[key] = {"error": type(exc).__name__}
    try:
        ws_yaml = yaml.safe_load((Path(ws_root) / "workspace.yaml").read_text(encoding="utf-8")) or {}
        package = ws_yaml.get("package_path") or ("pbg_" + str(ws_yaml.get("name", "")).replace("-", "_"))
        from vivarium_workbench.lib.composite_lookup import discover_workspace_composites
        data["composites"] = sorted(discover_workspace_composites(Path(ws_root), package))
    except Exception:  # noqa: BLE001
        pass
    text = json.dumps(data, indent=2, default=str)
    return Resolved(kind="manifest", label="workspace manifest", content=text[:60_000],
                    truncated=len(text) > 60_000)


def file(ws_root: Path, spec: dict[str, Any], *, mode: str) -> Resolved:
    rel = str(spec.get("path") or "")
    try:
        fr = sandbox.read_text_file(ws_root, rel)
    except SandboxError as exc:
        raise ContextError(str(exc)) from None
    res = Resolved(kind="file", label=fr.path, content=fr.text, path=fr.path, sha256=fr.sha256,
                   truncated=fr.truncated)
    if fr.path in sandbox.gitignored(ws_root, [fr.path]):
        if mode != "local":
            raise ContextError(f"{fr.path} is gitignored; ignored files are never sent on shared deployments")
        res.flags.add("gitignored")
        if not spec.get("include_ignored"):
            res.needs_confirmation = f"{fr.path} is gitignored. Include it anyway?"
        else:
            res.flags.add("gitignored-override")
    return res


def search(ws_root: Path, spec: dict[str, Any]) -> Resolved:
    query = str(spec.get("query") or "")
    if not query.strip() or len(query) > 200:
        raise ContextError("search needs a query (at most 200 characters)")
    glob = spec.get("glob")
    if glob is not None and (not isinstance(glob, str) or len(glob) > 100 or ".." in glob or glob.startswith("/")):
        raise ContextError("invalid glob")
    hits = search_workspace(ws_root, query, glob=glob, max_results=int(spec.get("max_results") or 50))
    lines = [f"{h['path']}:{h['line']}: {h['text']}" for h in hits]
    return Resolved(kind="search_results", label=f"search: {query}",
                    content="\n".join(lines) or "(no matches)")


def search_workspace(ws_root: Path, query: str, *, glob: str | None = None,
                     max_results: int = 50) -> list[dict[str, Any]]:
    """Fixed-string search of tracked, non-ignored text files (``git grep``)."""
    max_results = max(1, min(max_results, 200))
    args = ["grep", "-n", "-I", "-F", "--no-color", "-i", "-e", query]
    if glob:
        args += ["--", glob]
    try:
        out = _git(ws_root, *args, limit=400_000)
    except ContextError:
        out = ""
    hits: list[dict[str, Any]] = []
    for line in out.splitlines():
        path, _, rest = line.partition(":")
        num, _, text = rest.partition(":")
        if not path or not num.isdigit():
            continue
        if _sp.is_sensitive_rel(path) or sandbox.is_data_artifact(path):
            continue
        if sandbox.vwbignored(ws_root, [path]):
            continue
        hits.append({"path": path, "line": int(num), "text": text.strip()[:300]})
        if len(hits) >= max_results:
            break
    return hits


def paste(ws_root: Path, spec: dict[str, Any]) -> Resolved:
    text = str(spec.get("text") or "")
    if not text.strip():
        raise ContextError("pasted text is empty")
    label = str(spec.get("label") or "pasted text")[:80]
    return Resolved(kind="user_paste", label=label, content=text[:MAX_ITEM_CHARS],
                    truncated=len(text) > MAX_ITEM_CHARS)


def resolve(ws_root: Path, spec: dict[str, Any], *, mode: str) -> Resolved:
    kind = spec.get("kind")
    try:
        if kind == "page_summary":
            return page_summary(ws_root, spec)
        if kind == "study":
            return study(ws_root, spec)
        if kind == "investigation":
            return investigation(ws_root, spec)
        if kind == "composite":
            return composite(ws_root, spec)
        if kind == "run_log":
            return run_log(ws_root, spec)
        if kind == "git_diff":
            return git_diff(ws_root, spec)
        if kind == "manifest":
            return manifest(ws_root, spec)
        if kind == "file":
            return file(ws_root, spec, mode=mode)
        if kind == "search":
            return search(ws_root, spec)
        if kind == "paste":
            return paste(ws_root, spec)
    except SandboxError as exc:
        raise ContextError(str(exc)) from None
    raise ContextError(f"unknown context kind {kind!r}")
