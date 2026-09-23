"""Tool registry: specs (JSON Schema), argument validation, result shaping.

A tool is data (:class:`ToolSpec`) plus an async handler. The chat service
offers the specs to the model, validates every call's arguments against the
schema, asks ``policy.decide`` whether to run it, and truncates + redacts the
result before it goes back to the model (wrapped as untrusted content).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from vivarium_workbench_assistant.providers.base import ToolDef
from vivarium_workbench_assistant.secrets import redact

Category = Literal["read", "propose", "execute", "shell"]


@dataclass
class ToolContext:
    ws_root: Path
    mode: str
    conversation_id: str
    run_id: str
    scope: str
    persist: bool
    session_key: str | None
    model_label: str
    services: Any                      # the AssistantServices bundle (proposals, audit, prefs)


@dataclass
class ToolResult:
    ok: bool
    content: str
    summary: str
    truncated: bool = False
    data: dict[str, Any] | None = None


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]
Preflight = Callable[[ToolContext, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    category: Category
    handler: Handler
    max_result_bytes: int = 32_768
    timeout_s: float = 30.0
    #: Optional synchronous check before the policy decision (e.g. gitignored?).
    preflight: Preflight | None = None
    warnings: tuple[str, ...] = field(default=())

    def to_def(self) -> ToolDef:
        return ToolDef(name=self.name, description=self.description, parameters=self.parameters)


class ToolArgError(ValueError):
    pass


def validate_args(spec: ToolSpec, args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ToolArgError("arguments must be a JSON object")
    try:
        Draft202012Validator(spec.parameters).validate(args)
    except ValidationError as exc:
        loc = "/".join(str(p) for p in exc.absolute_path) or "(arguments)"
        raise ToolArgError(f"{loc}: {exc.message}"[:300]) from None
    return args


def args_hash(name: str, args: dict[str, Any]) -> str:
    blob = json.dumps({"n": name, "a": args}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def shape_result(spec: ToolSpec, result: ToolResult) -> ToolResult:
    """Truncate to the tool's byte budget and redact credentials."""
    content = redact(result.content)
    raw = content.encode("utf-8")
    truncated = result.truncated
    if len(raw) > spec.max_result_bytes:
        content = raw[: spec.max_result_bytes].decode("utf-8", "ignore") + \
            f"\n[… truncated to {spec.max_result_bytes} bytes …]"
        truncated = True
    return ToolResult(ok=result.ok, content=content, summary=redact(result.summary)[:300],
                      truncated=truncated, data=result.data)


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = {s.name: s for s in specs}

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def offered(self, *, include: set[str]) -> list[ToolSpec]:
        return [s for s in self._specs.values() if s.category in include]


def default_registry() -> ToolRegistry:
    from vivarium_workbench_assistant.tools import execute_tools, propose_tools, read_tools
    return ToolRegistry(read_tools.SPECS + propose_tools.SPECS + execute_tools.SPECS)
