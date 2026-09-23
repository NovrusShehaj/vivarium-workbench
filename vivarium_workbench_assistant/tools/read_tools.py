"""Read tools: sandboxed, side-effect free, auto-approved by default.

All run in a worker thread (they touch the filesystem or spawn ``git``) with
the workspace root passed explicitly. Results are data, framed as untrusted
by the chat service.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from vivarium_workbench_assistant import sandbox
from vivarium_workbench_assistant.context import sources
from vivarium_workbench_assistant.context.sources import ContextError
from vivarium_workbench_assistant.edits import validators
from vivarium_workbench_assistant.sandbox import SandboxError
from vivarium_workbench_assistant.tools.registry import ToolContext, ToolResult, ToolSpec


def _ok(content: str, summary: str, truncated: bool = False) -> ToolResult:
    return ToolResult(ok=True, content=content, summary=summary, truncated=truncated)


def _err(message: str) -> ToolResult:
    return ToolResult(ok=False, content=message, summary=message[:200])


async def _thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(fn, *args, **kwargs)


async def list_dir(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = args.get("path") or ""
    try:
        entries = await _thread(sandbox.list_dir, ctx.ws_root, path)
    except SandboxError as exc:
        return _err(str(exc))
    lines = [f"{e['type'][0]} {e['path']}" + (f" ({e.get('size')} B)" if e.get("size") is not None else "")
             for e in entries]
    return _ok("\n".join(lines) or "(empty)", f"listed {len(entries)} entries in {path or '.'}")


def _read_preflight(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path") or "")
    try:
        clean = sandbox.resolve_in_workspace(ctx.ws_root, path).relative_to(ctx.ws_root.resolve()).as_posix()
    except (SandboxError, ValueError):
        return {"gitignored": False}
    return {"gitignored": clean in sandbox.gitignored(ctx.ws_root, [clean])}


async def read_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("path") or "")
    try:
        fr = await _thread(sandbox.read_text_file, ctx.ws_root, path)
    except SandboxError as exc:
        return _err(str(exc))
    text = fr.text
    start, end = args.get("start_line"), args.get("end_line")
    if start or end:
        lines = text.splitlines()
        s = max(1, int(start or 1))
        e = min(len(lines), int(end or len(lines)))
        text = "\n".join(f"{n}: {lines[n - 1]}" for n in range(s, e + 1))
    header = f"# {fr.path} (sha256 {fr.sha256}, {fr.size} bytes)\n"
    return _ok(header + text, f"read {fr.path}", truncated=fr.truncated)


async def search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    hits = await _thread(sources.search_workspace, ctx.ws_root, str(args["query"]),
                         glob=args.get("glob"), max_results=int(args.get("max_results") or 50))
    body = "\n".join(f"{h['path']}:{h['line']}: {h['text']}" for h in hits)
    return _ok(body or "(no matches)", f"{len(hits)} matches for {args['query']!r}")


async def _source(ctx: ToolContext, spec: dict[str, Any], summary: str) -> ToolResult:
    try:
        r = await _thread(sources.resolve, ctx.ws_root, spec, mode=ctx.mode)
    except ContextError as exc:
        return _err(str(exc))
    if r.needs_confirmation:
        return _err(r.needs_confirmation)
    return _ok(r.content, summary, truncated=r.truncated)


async def get_workspace_manifest(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _source(ctx, {"kind": "manifest"}, "read the workspace manifest")


async def get_study(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _source(ctx, {"kind": "study", "slug": args["slug"]}, f"read study {args['slug']}")


async def get_investigation(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _source(ctx, {"kind": "investigation", "slug": args["slug"]},
                         f"read investigation {args['slug']}")


async def get_composite(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _source(ctx, {"kind": "composite", "id": args["id"]}, f"read composite {args['id']}")


async def get_run_log(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _source(ctx, {"kind": "run_log", "run_id": args["run_id"],
                               "tail_lines": args.get("tail_lines") or 200}, f"read run log {args['run_id']}")


async def get_git_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    def run() -> str:
        from vivarium_workbench.lib import sensitive_paths as sp
        raw = sources._git(ctx.ws_root, "status", "--porcelain=v1", "--branch", limit=32_768)
        keep = []
        for line in raw.splitlines():
            path = line[3:].split(" -> ")[-1].strip('"') if len(line) > 3 else ""
            if line.startswith("##") or not sp.is_sensitive_rel(path):
                keep.append(line)
        return "\n".join(keep)
    try:
        text = await _thread(run)
    except ContextError as exc:
        return _err(str(exc))
    return _ok(text or "(clean)", "read git status")


async def get_git_diff(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _source(ctx, {"kind": "git_diff", "paths": args.get("paths")}, "read the git diff")


async def validate_spec(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("path") or "")
    try:
        fr = await _thread(sandbox.read_text_file, ctx.ws_root, path)
    except SandboxError as exc:
        return _err(str(exc))
    results = await _thread(validators.validate, ctx.ws_root, fr.path, fr.text)
    ok = all(r.get("ok") for r in results)
    return ToolResult(ok=True, content=json.dumps(results, indent=2),
                      summary=f"{fr.path}: {'valid' if ok else 'has problems'}")


_PATH = {"type": "string", "minLength": 1, "maxLength": 512,
         "description": "Workspace-relative path using forward slashes."}
_SLUG = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"}

SPECS: list[ToolSpec] = [
    ToolSpec("list_dir", "List the files and folders in a workspace directory ('' for the root).",
             {"type": "object", "properties": {"path": {"type": "string", "maxLength": 512}},
              "additionalProperties": False}, "read", list_dir),
    ToolSpec("read_file", "Read a text file from the workspace, optionally a line range.",
             {"type": "object", "properties": {"path": _PATH,
                                               "start_line": {"type": "integer", "minimum": 1},
                                               "end_line": {"type": "integer", "minimum": 1}},
              "required": ["path"], "additionalProperties": False},
             "read", read_file, preflight=_read_preflight),
    ToolSpec("search", "Case-insensitive fixed-string search over the workspace's tracked text files.",
             {"type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200},
                                               "glob": {"type": "string", "maxLength": 100},
                                               "max_results": {"type": "integer", "minimum": 1, "maximum": 200}},
              "required": ["query"], "additionalProperties": False}, "read", search),
    ToolSpec("get_workspace_manifest", "Summarise the workspace: name, studies, composites, health.",
             {"type": "object", "properties": {}, "additionalProperties": False}, "read", get_workspace_manifest),
    ToolSpec("get_study", "Read a study's study.yaml and its computed status.",
             {"type": "object", "properties": {"slug": _SLUG}, "required": ["slug"],
              "additionalProperties": False}, "read", get_study),
    ToolSpec("get_investigation", "Read an investigation's investigation.yaml.",
             {"type": "object", "properties": {"slug": _SLUG}, "required": ["slug"],
              "additionalProperties": False}, "read", get_investigation),
    ToolSpec("get_composite", "Read a workspace composite's spec file by composite id.",
             {"type": "object", "properties": {"id": {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_.]{0,199}$"}},
              "required": ["id"], "additionalProperties": False}, "read", get_composite),
    ToolSpec("get_run_log", "Read the tail of a simulation run's log.",
             {"type": "object", "properties": {"run_id": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"},
                                               "tail_lines": {"type": "integer", "minimum": 10, "maximum": 1000}},
              "required": ["run_id"], "additionalProperties": False}, "read", get_run_log),
    ToolSpec("get_git_status", "Show the workspace's git status (branch and changed files).",
             {"type": "object", "properties": {}, "additionalProperties": False}, "read", get_git_status),
    ToolSpec("get_git_diff", "Show uncommitted changes (optionally only some paths).",
             {"type": "object", "properties": {"paths": {"type": "array", "items": _PATH, "maxItems": 50}},
              "additionalProperties": False}, "read", get_git_diff),
    ToolSpec("validate_spec", "Validate a YAML/JSON/TOML/Python file (syntax, and the workspace schema "
             "for workspace.yaml, study.yaml and investigation.yaml).",
             {"type": "object", "properties": {"path": _PATH}, "required": ["path"],
              "additionalProperties": False}, "read", validate_spec),
]
