"""Provider-neutral types: messages, requests, stream events, errors.

Every provider (whatever its wire protocol) is driven through these types, so
the chat service, the tool loop and the tests never see vendor payloads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Union


class Cap(str, Enum):
    STREAMING = "streaming"
    TOOLS = "tools"
    PARALLEL_TOOLS = "parallel_tools"
    VISION = "vision"
    JSON_SCHEMA = "json_schema"
    SYSTEM_PROMPT = "system_prompt"
    MODEL_DISCOVERY = "model_discovery"
    TOKEN_COUNT = "token_count"
    REASONING = "reasoning"


@dataclass(frozen=True)
class ModelInfo:
    id: str
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    #: ``None`` = unknown (chat allowed; tools need a probe or an override).
    caps: frozenset[Cap] | None = None
    source: Literal["discovered", "configured", "manual"] = "discovered"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name or self.id,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "caps": sorted(c.value for c in self.caps) if self.caps is not None else None,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@dataclass
class TextPart:
    text: str
    type: Literal["text"] = "text"


@dataclass
class ToolCallPart:
    id: str
    name: str
    arguments: dict[str, Any]
    type: Literal["tool_call"] = "tool_call"


@dataclass
class ToolResultPart:
    tool_call_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


Part = Union[TextPart, ToolCallPart, ToolResultPart]


@dataclass
class ChatMessage:
    role: Literal["user", "assistant", "tool"]
    parts: list[Part]

    def text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))


@dataclass(frozen=True)
class ToolDef:
    """A tool as offered to a model (JSON-Schema parameters)."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ChatRequest:
    model: str
    system: str | None
    messages: list[ChatMessage]
    tools: list[ToolDef] | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    request_id: str = ""


# ---------------------------------------------------------------------------
# Normalized stream events
# ---------------------------------------------------------------------------

@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCallStart:
    id: str
    name: str


@dataclass
class ToolCallDelta:
    id: str
    arguments_fragment: str


@dataclass
class ToolCallEnd:
    id: str


@dataclass
class UsageEvent:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class DoneEvent:
    stop_reason: str | None = None


StreamEvent = Union[TextDelta, ToolCallStart, ToolCallDelta, ToolCallEnd, UsageEvent, DoneEvent]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

ErrorKind = Literal[
    "auth", "permission", "not_found", "rate_limited", "overloaded", "context_length",
    "invalid_request", "network", "timeout", "cancelled", "server", "config",
    "blocked_by_policy", "unknown",
]

#: Kinds worth an automatic retry — but only before the first token.
RETRYABLE_KINDS: frozenset[str] = frozenset({"rate_limited", "overloaded", "server", "network", "timeout"})


class ProviderError(Exception):
    """A provider failure with a redacted, user-facing message."""

    def __init__(
        self,
        kind: ErrorKind,
        safe_message: str,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.kind: ErrorKind = kind
        self.safe_message = safe_message
        self.status = status
        self.retry_after_s = retry_after_s
        self.retryable = (kind in RETRYABLE_KINDS) if retryable is None else retryable

    def to_client(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "message": self.safe_message,
                               "retryable": self.retryable}
        if self.retry_after_s is not None:
            out["retry_after_s"] = self.retry_after_s
        if self.status is not None:
            out["status"] = self.status
        return out


@dataclass
class ValidationResult:
    ok: bool
    latency_ms: int | None = None
    models_count: int | None = None
    error: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok}
        if self.latency_ms is not None:
            out["latency_ms"] = self.latency_ms
        if self.models_count is not None:
            out["models_count"] = self.models_count
        if self.error is not None:
            out["error"] = self.error
        if self.notes:
            out["notes"] = list(self.notes)
        return out
