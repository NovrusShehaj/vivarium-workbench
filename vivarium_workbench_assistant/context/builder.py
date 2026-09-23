"""Context builder: resolve → filter → dedupe → prioritise → budget → frame.

The output is (a) the manifest recorded with the user message and shown to the
user ("what will be sent"), and (b) the prompt blocks, each wrapped as
untrusted data::

    <context id="c3" kind="file" path="studies/growth/study.yaml" sha256="…" untrusted="true">
    …
    </context>

No provider is called here; ``/context/preview`` runs exactly this.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vivarium_workbench_assistant.context import sources, tokens
from vivarium_workbench_assistant.context.sources import ContextError
from vivarium_workbench_assistant.secrets import redact, secret_findings

#: Lower sorts first. Pinned (explicit) items always precede automatic ones.
PRIORITY = {"user_paste": 0, "file": 1, "study": 2, "investigation": 2, "composite": 2, "run_log": 3,
            "git_diff": 4, "search_results": 5, "manifest": 6, "page_summary": 7}
MIN_ITEM_TOKENS = 200


@dataclass
class ContextItem:
    id: str
    kind: str
    label: str
    content: str
    path: str | None
    tokens_est: int
    priority: int
    pinned: bool
    sha256: str = ""
    truncated: bool = False
    dropped: bool = False
    flags: set[str] = field(default_factory=set)
    error: str | None = None
    needs_confirmation: str | None = None

    def manifest(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "label": self.label,
                               "tokens": self.tokens_est, "truncated": self.truncated}
        if self.path:
            out["path"] = self.path
        if self.sha256:
            out["sha256"] = self.sha256
        if self.flags:
            out["flags"] = sorted(self.flags)
        if self.dropped:
            out["dropped"] = True
        if self.error:
            out["error"] = self.error
        if self.needs_confirmation:
            out["needs_confirmation"] = self.needs_confirmation
        return out


@dataclass
class BuiltContext:
    items: list[ContextItem]
    total_tokens: int
    budget_tokens: int
    warnings: list[str]

    @property
    def included(self) -> list[ContextItem]:
        return [i for i in self.items if not i.dropped and not i.error and not i.needs_confirmation]

    def manifest(self) -> list[dict[str, Any]]:
        return [i.manifest() for i in self.items]

    def blocks(self) -> str:
        return "\n\n".join(frame(i) for i in self.included)


def _attr(v: str) -> str:
    return (v.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;"))


def frame(item: ContextItem) -> str:
    # Neutralise an embedded closing tag so content cannot break out of its block.
    body = item.content.replace("</context", "<\\/context")
    attrs = [f'id="{_attr(item.id)}"', f'kind="{_attr(item.kind)}"']
    if item.path:
        attrs.append(f'path="{_attr(item.path)}"')
    if item.sha256:
        attrs.append(f'sha256="{item.sha256[:16]}"')
    if item.truncated:
        attrs.append('truncated="true"')
    attrs.append('untrusted="true"')
    return f"<context {' '.join(attrs)}>\n{body}\n</context>"


def _truncate_to(text: str, max_tokens: int, instance_id: str | None) -> str:
    max_chars = int(max_tokens * tokens.CHARS_PER_TOKEN / max(tokens.factor(instance_id or ""), 0.5))
    if len(text) <= max_chars:
        return text
    half = max(max_chars // 2 - 40, 0)
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n[… {omitted} characters omitted to fit the context budget …]\n{text[-half:]}"


def build(
    ws_root: Path,
    specs: list[dict[str, Any]],
    *,
    mode: str,
    budget_tokens: int,
    instance_id: str | None = None,
) -> BuiltContext:
    items: list[ContextItem] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for n, spec in enumerate(specs):
        cid = f"c{n + 1}"
        pinned = spec.get("kind") != "page_summary"
        try:
            r = sources.resolve(ws_root, spec, mode=mode)
        except ContextError as exc:
            items.append(ContextItem(id=cid, kind=str(spec.get("kind") or "unknown"),
                                     label=str(spec.get("label") or spec.get("path") or spec.get("slug")
                                               or spec.get("kind") or "context"),
                                     content="", path=None, tokens_est=0, priority=99, pinned=pinned,
                                     error=str(exc)))
            continue
        # Dedupe on (path, content hash) — the same file attached twice.
        key = f"{r.path}|{hashlib.sha256(r.content.encode('utf-8', 'replace')).hexdigest()}"
        if key in seen:
            continue
        seen.add(key)
        flags = set(r.flags)
        content = r.content
        found = secret_findings(content)
        if found:
            content = redact(content)
            flags.add("secret-redacted")
            warnings.append(f"{r.label}: removed what looks like {', '.join(found)} before sending.")
        items.append(ContextItem(
            id=cid, kind=r.kind, label=r.label, content=content, path=r.path,
            tokens_est=tokens.estimate(content, instance_id) + 20, priority=PRIORITY.get(r.kind, 50),
            pinned=pinned, sha256=r.sha256, truncated=r.truncated, flags=flags,
            needs_confirmation=r.needs_confirmation,
        ))
    # Prioritise: pinned first, then by kind priority, keeping user order within a tier.
    order = sorted(range(len(items)), key=lambda i: (not items[i].pinned, items[i].priority, i))
    remaining = max(budget_tokens, 0)
    for i in order:
        it = items[i]
        if it.error or it.needs_confirmation:
            continue
        if it.tokens_est <= remaining:
            remaining -= it.tokens_est
            continue
        if remaining >= MIN_ITEM_TOKENS:
            it.content = _truncate_to(it.content, remaining - 20, instance_id)
            it.tokens_est = tokens.estimate(it.content, instance_id) + 20
            it.truncated = True
            remaining -= min(it.tokens_est, remaining)
            warnings.append(f"{it.label} was truncated to fit the model's context window.")
        else:
            it.dropped = True
            warnings.append(f"{it.label} did not fit the model's context window and was left out.")
    total = sum(i.tokens_est for i in items if not i.dropped and not i.error and not i.needs_confirmation)
    return BuiltContext(items=items, total_tokens=total, budget_tokens=budget_tokens, warnings=warnings)
