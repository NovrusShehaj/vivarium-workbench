"""Request/response models for ``/api/ext/assistant/*`` (pydantic v2).

Request models use ``extra="forbid"`` (unlike the permissive core payloads) so
typos and unexpected fields are rejected. Credentials arrive as
``SecretStr`` so they never appear in ``repr``/validation errors, and no
response model has a field that could carry one. The browser-facing
TypeScript declarations are generated from these models
(``static/assistant.generated.d.ts``, see ``python -m vivarium_workbench_assistant.generate_ts``).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr

_STRICT = ConfigDict(extra="forbid")


class ContextSpec(BaseModel):
    """One requested piece of context (see ``context/sources.py``)."""

    model_config = _STRICT
    kind: Literal["page_summary", "study", "investigation", "composite", "run_log", "git_diff",
                  "manifest", "file", "search", "paste"]
    page: Optional[str] = Field(default=None, max_length=64)
    investigation: Optional[str] = Field(default=None, max_length=128)
    study: Optional[str] = Field(default=None, max_length=128)
    composite: Optional[str] = Field(default=None, max_length=200)
    slug: Optional[str] = Field(default=None, max_length=128)
    id: Optional[str] = Field(default=None, max_length=200)
    run_id: Optional[str] = Field(default=None, max_length=128)
    tail_lines: Optional[int] = Field(default=None, ge=10, le=1000)
    paths: Optional[list[str]] = Field(default=None, max_length=50)
    path: Optional[str] = Field(default=None, max_length=512)
    include_ignored: bool = False
    query: Optional[str] = Field(default=None, max_length=200)
    glob: Optional[str] = Field(default=None, max_length=100)
    max_results: Optional[int] = Field(default=None, ge=1, le=200)
    label: Optional[str] = Field(default=None, max_length=80)
    text: Optional[str] = Field(default=None, max_length=200_000)

    def spec(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class RunRequest(BaseModel):
    model_config = _STRICT
    action: Literal["send", "retry", "regenerate"] = "send"
    message: Optional[str] = Field(default=None, max_length=100_000)
    parent_id: Optional[str] = Field(default=None, pattern=r"^m_[0-9a-f]{12}$")
    context: Optional[list[ContextSpec]] = Field(default=None, max_length=20)
    provider_instance: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    model: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$")
    agent: bool = False


class ConversationCreate(BaseModel):
    model_config = _STRICT
    title: Optional[str] = Field(default=None, max_length=120)


class ConversationPatch(BaseModel):
    model_config = _STRICT
    title: str = Field(min_length=1, max_length=120)


class DeleteAllRequest(BaseModel):
    model_config = _STRICT
    confirm: str = Field(max_length=200)


class CredentialRefBody(BaseModel):
    model_config = _STRICT
    source: Literal["keyring", "env", "session", "adc", "key_file", "none"]
    env_var: Optional[str] = Field(default=None, max_length=128)
    key_file_path: Optional[str] = Field(default=None, max_length=1024)


class InstanceCreate(BaseModel):
    model_config = _STRICT
    id: Optional[str] = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,39}$")
    type: Literal["anthropic", "openai", "vertex", "ai_studio", "openrouter", "openai_compatible"]
    display_name: Optional[str] = Field(default=None, max_length=80)
    credential: Optional[CredentialRefBody] = None
    base_url: Optional[str] = Field(default=None, max_length=2048)
    preset: Optional[Literal["ollama", "lm_studio", "custom"]] = None
    project: Optional[str] = Field(default=None, max_length=64)
    location: Optional[str] = Field(default=None, max_length=64)
    default_model: Optional[str] = Field(default=None, max_length=200)
    manual_models: list[str] = Field(default_factory=list, max_length=200)
    first_token_timeout_s: Optional[float] = Field(default=None, ge=5, le=900)
    ca_bundle_path: Optional[str] = Field(default=None, max_length=1024)
    send_attribution_headers: bool = False
    context_window_override: Optional[int] = Field(default=None, ge=1024, le=10_000_000)


class InstancePatch(BaseModel):
    model_config = _STRICT
    display_name: Optional[str] = Field(default=None, max_length=80)
    enabled: Optional[bool] = None
    credential: Optional[CredentialRefBody] = None
    base_url: Optional[str] = Field(default=None, max_length=2048)
    preset: Optional[Literal["ollama", "lm_studio", "custom"]] = None
    project: Optional[str] = Field(default=None, max_length=64)
    location: Optional[str] = Field(default=None, max_length=64)
    default_model: Optional[str] = Field(default=None, max_length=200)
    manual_models: Optional[list[str]] = Field(default=None, max_length=200)
    first_token_timeout_s: Optional[float] = Field(default=None, ge=5, le=900)
    ca_bundle_path: Optional[str] = Field(default=None, max_length=1024)
    send_attribution_headers: Optional[bool] = None
    context_window_override: Optional[int] = Field(default=None, ge=1024, le=10_000_000)
    make_default: Optional[bool] = None


class CredentialSet(BaseModel):
    """Set a credential. The value is write-only: no endpoint ever returns it."""

    model_config = _STRICT
    api_key: Optional[SecretStr] = None
    access_token: Optional[SecretStr] = None
    session_only: bool = False


class CredentialStatusView(BaseModel):
    configured: bool
    source: str
    hint: Optional[str] = None
    notice: Optional[str] = None


class ApprovalDecision(BaseModel):
    model_config = _STRICT
    decision: Literal["approve", "deny"]
    scope: Literal["once", "conversation"] = "once"
    args_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProposalAction(BaseModel):
    model_config = _STRICT
    paths: Optional[list[str]] = Field(default=None, max_length=50)
    commit: bool = False


class PreferencesPatch(BaseModel):
    model_config = _STRICT
    default_instance: Optional[str] = Field(default=None, max_length=40)
    auto_context: Optional[Literal["off", "page_summary", "page_and_selection"]] = None
    confirm_first_cloud_send: Optional[bool] = None
    persist_conversations: Optional[bool] = None
    retention_days: Optional[int] = Field(default=None, ge=0, le=3650)
    system_prompt_extra: Optional[str] = Field(default=None, max_length=4000)
    tools: Optional[dict[Literal["read", "execute", "shell"], Literal["auto", "ask", "disabled"]]] = None
    panel_shortcut: Optional[str] = Field(default=None, max_length=40)


class ContextPreviewRequest(BaseModel):
    model_config = _STRICT
    context: list[ContextSpec] = Field(default_factory=list, max_length=20)
    provider_instance: Optional[str] = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,39}$")
    model: Optional[str] = Field(default=None, max_length=200)
    message: Optional[str] = Field(default=None, max_length=100_000)


class SuggestBody(BaseModel):
    model_config = _STRICT
    kind: Literal["repo-name", "pr-title", "pr-body"]
    provider_instance: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    model: str = Field(min_length=1, max_length=200)


class ModelCapabilitiesBody(BaseModel):
    model_config = _STRICT
    model: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$")
    tools: Optional[bool] = None
    context_window: Optional[int] = Field(default=None, ge=1024, le=10_000_000)
    max_output_tokens: Optional[int] = Field(default=None, ge=16, le=1_000_000)


class AssistantStatus(BaseModel):
    enabled: bool
    available: bool
    reason: str
    mode: str
    capabilities: dict[str, Any]
    providers_configured: int
    default_instance: Optional[str] = None
    persist_conversations: bool
    keyring_available: bool
    google_auth_installed: bool
    config_read_only: Optional[str] = None
    storage: dict[str, str] = Field(default_factory=dict)


class ConversationSummary(BaseModel):
    id: str
    title: str
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    message_count: int = 0


class ContextManifestEntry(BaseModel):
    id: str
    kind: str
    label: str
    tokens: int
    truncated: bool
    path: Optional[str] = None
    sha256: Optional[str] = None
    flags: list[str] = Field(default_factory=list)
    dropped: bool = False
    error: Optional[str] = None
    needs_confirmation: Optional[str] = None


class ContextPreview(BaseModel):
    items: list[ContextManifestEntry]
    total_tokens: int
    budget_tokens: int
    warnings: list[str]
    locality: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None


class StreamEventRunStart(BaseModel):
    """SSE ``run.start`` payload (v1)."""

    v: int
    run_id: str
    conversation_id: str
    message_id: str
    user_message_id: str
    provider_instance: str
    model: str
    locality: str
    context_manifest: list[ContextManifestEntry]
    warnings: list[str]
    agent: bool


MODELS: list[type[BaseModel]] = [
    ContextSpec, RunRequest, ConversationCreate, ConversationPatch, DeleteAllRequest, CredentialRefBody,
    InstanceCreate, InstancePatch, CredentialSet, CredentialStatusView, ApprovalDecision, ProposalAction,
    PreferencesPatch, ContextPreviewRequest, SuggestBody, ModelCapabilitiesBody, AssistantStatus,
    ConversationSummary, ContextManifestEntry, ContextPreview, StreamEventRunStart,
]
