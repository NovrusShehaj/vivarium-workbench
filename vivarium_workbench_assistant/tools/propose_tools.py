"""Propose tools: the model suggests file changes; nothing is written.

Each call adds (or replaces) one file change on the run's proposal after the
same checks the apply step repeats: sandbox for writing, a base hash that must
match the current file, size limits, a server-side unified diff, validation
and an executable-code flag. The user reviews and applies from the panel.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from vivarium_workbench_assistant import sandbox
from vivarium_workbench_assistant.edits import validators
from vivarium_workbench_assistant.edits.proposals import (
    MAX_CONTENT,
    MAX_FILES,
    FileChange,
    PatchError,
    apply_unified_patch,
    unified_diff,
)
from vivarium_workbench_assistant.sandbox import SandboxError
from vivarium_workbench_assistant.tools.registry import ToolContext, ToolResult, ToolSpec


def _err(message: str) -> ToolResult:
    return ToolResult(ok=False, content=message, summary=message[:200])


def _current(ctx: ToolContext, rel: str) -> tuple[str, str | None, str | None]:
    """(clean path, current text or None, sha256 or None) — for writing."""
    target = sandbox.resolve_in_workspace(ctx.ws_root, rel, for_write=True)
    clean = target.relative_to(Path(ctx.ws_root).resolve()).as_posix()
    if not target.exists():
        return clean, None, None
    fr = sandbox.read_text_file(ctx.ws_root, clean, max_bytes=MAX_CONTENT * 4)
    if fr.truncated:
        raise SandboxError(f"{clean}: too large to edit with a proposal")
    return clean, fr.text, fr.sha256


def _record(ctx: ToolContext, fc: FileChange) -> ToolResult:
    svc = ctx.services
    store = svc.proposals
    p = store.for_run(ctx.ws_root, ctx.run_id, ctx.conversation_id, scope=ctx.scope, persist=ctx.persist,
                      model_label=ctx.model_label)
    others = [f for f in p.files if f.path != fc.path]
    if len(others) >= MAX_FILES:
        return _err(f"a proposal may change at most {MAX_FILES} files")
    p.files = others + [fc]
    p.refresh_status()
    store.save(ctx.ws_root, p, scope=ctx.scope, persist=ctx.persist)
    problems = [v["message"] for v in fc.validation if not v.get("ok")]
    note = f" Validation problems: {'; '.join(problems)}." if problems else ""
    summary = f"proposed {fc.op} {fc.path} (+{fc.additions} −{fc.deletions})"
    return ToolResult(
        ok=True,
        content=(f"Proposed {fc.op} of {fc.path} (+{fc.additions} −{fc.deletions}). Nothing was written: "
                 f"the user reviews and applies proposals.{note}"),
        summary=summary,
        data={"proposal_id": p.id, "path": fc.path, "op": fc.op, "additions": fc.additions,
              "deletions": fc.deletions, "validation": fc.validation, "executable_code": fc.executable_code},
    )


def _make(ctx: ToolContext, path: str, op: str, before: str | None, base: str | None,
          after: str | None, rationale: str) -> FileChange:
    diff, adds, dels = unified_diff(path, before, after)
    return FileChange(path=path, op=op, base_sha256=base, new_content=after, unified_diff=diff,
                      additions=adds, deletions=dels,
                      validation=validators.validate(ctx.ws_root, path, after),
                      executable_code=validators.executable_code(ctx.ws_root, path),
                      rationale=rationale[:2000])


async def propose_edit(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    def run() -> ToolResult:
        try:
            clean, before, sha = _current(ctx, str(args["path"]))
        except SandboxError as exc:
            return _err(str(exc))
        if before is None:
            return _err(f"{clean} does not exist; use propose_create")
        if args["base_sha256"] != sha:
            return _err(f"{clean} changed (sha256 is {sha}); read it again and propose against the current content")
        if ("new_content" in args) == ("patch" in args):
            return _err("give exactly one of new_content or patch")
        if "new_content" in args:
            after = str(args["new_content"])
        elif "patch" in args:
            try:
                after = apply_unified_patch(before, str(args["patch"]))
            except PatchError as exc:
                return _err(f"the patch does not apply to {clean}: {exc}")
        else:
            return _err("give new_content or patch")
        if len(after.encode("utf-8")) > MAX_CONTENT:
            return _err("the new content is too large")
        if after == before:
            return _err("the proposed content is identical to the current file")
        return _record(ctx, _make(ctx, clean, "modify", before, sha, after, str(args.get("rationale") or "")))
    return await asyncio.to_thread(run)


async def propose_create(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    def run() -> ToolResult:
        try:
            clean, before, _sha = _current(ctx, str(args["path"]))
        except SandboxError as exc:
            return _err(str(exc))
        if before is not None:
            return _err(f"{clean} already exists; use propose_edit")
        content = str(args["content"])
        if len(content.encode("utf-8")) > MAX_CONTENT:
            return _err("the content is too large")
        return _record(ctx, _make(ctx, clean, "create", None, None, content, str(args.get("rationale") or "")))
    return await asyncio.to_thread(run)


async def propose_delete(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    def run() -> ToolResult:
        try:
            clean, before, sha = _current(ctx, str(args["path"]))
        except SandboxError as exc:
            return _err(str(exc))
        if before is None:
            return _err(f"{clean} does not exist")
        return _record(ctx, _make(ctx, clean, "delete", before, sha, None, str(args.get("rationale") or "")))
    return await asyncio.to_thread(run)


_PATH = {"type": "string", "minLength": 1, "maxLength": 512}
_RATIONALE = {"type": "string", "maxLength": 2000, "description": "Why this change is needed."}

SPECS: list[ToolSpec] = [
    ToolSpec("propose_edit",
             "Propose changing an existing file. Give base_sha256 (from read_file) and either the complete "
             "new_content or a unified-diff patch. Nothing is written until the user applies it.",
             {"type": "object",
              "properties": {"path": _PATH, "base_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                             "new_content": {"type": "string"}, "patch": {"type": "string"},
                             "rationale": _RATIONALE},
              # Exactly one of new_content / patch (checked by the handler: some
              # providers reject a top-level oneOf in tool schemas).
              "required": ["path", "base_sha256", "rationale"], "additionalProperties": False},
             "propose", propose_edit, max_result_bytes=4096),
    ToolSpec("propose_create", "Propose creating a new file. Nothing is written until the user applies it.",
             {"type": "object", "properties": {"path": _PATH, "content": {"type": "string"}, "rationale": _RATIONALE},
              "required": ["path", "content", "rationale"], "additionalProperties": False},
             "propose", propose_create, max_result_bytes=4096),
    ToolSpec("propose_delete", "Propose deleting a file. The user confirms every deletion.",
             {"type": "object", "properties": {"path": _PATH, "rationale": _RATIONALE},
              "required": ["path", "rationale"], "additionalProperties": False},
             "propose", propose_delete, max_result_bytes=4096),
]
