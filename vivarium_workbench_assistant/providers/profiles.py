"""Provider profiles — data, not code branches.

A *profile* describes a provider type: its wire protocol, auth scheme, default
endpoint, the hosts it is pinned to, how models are discovered and which
credential sources make sense. A *provider instance* (``config.ProviderInstance``)
is a user's configured use of a profile.

No model identifiers live here: models come from discovery or manual entry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from vivarium_workbench_assistant.providers.base import Cap

Wire = Literal["openai-chat", "anthropic-messages"]
AuthScheme = Literal["x-api-key", "bearer-static", "bearer-google-oauth", "optional-bearer"]
Discovery = Literal["openai", "anthropic", "gemini-native", "none"]
CredentialSource = Literal["keyring", "env", "session", "adc", "key_file", "none"]

#: Vertex location: "global" or a region such as "us-central1" / "europe-west4".
VERTEX_LOCATION_RE = re.compile(r"^(global|[a-z]+-[a-z]+[0-9]+)$")
#: GCP project ids: 6-30 chars, lowercase letters, digits and hyphens.
GCP_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


@dataclass(frozen=True)
class ProviderProfile:
    type: str
    display_name: str
    locality: Literal["cloud", "local"]
    wire: Wire
    auth: AuthScheme
    #: Default base URL (``None`` = derived or user supplied).
    base_url: str | None
    #: Hosts a cloud profile is pinned to unless the instance sets an explicit
    #: custom base URL. ``*.suffix`` / ``*-suffix`` match exactly one leading label.
    official_hosts: tuple[str, ...]
    discovery: Discovery
    credential_sources: tuple[CredentialSource, ...]
    required_fields: tuple[str, ...] = ()
    default_headers: dict[str, str] = field(default_factory=dict)
    #: Suggested environment variable for the "env" credential source.
    default_env_var: str | None = None
    #: Ask for usage in streamed responses (``stream_options.include_usage``).
    stream_usage: bool = False
    caps: frozenset[Cap] = frozenset({Cap.STREAMING, Cap.SYSTEM_PROMPT})
    #: Where the provider documents how it handles API data (shown in Settings).
    data_usage_url: str | None = None
    notes: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "type": self.type,
            "display_name": self.display_name,
            "locality": self.locality,
            "wire": self.wire,
            "auth": self.auth,
            "base_url": self.base_url,
            "required_fields": list(self.required_fields),
            "credential_sources": list(self.credential_sources),
            "default_env_var": self.default_env_var,
            "discovery": self.discovery,
            "data_usage_url": self.data_usage_url,
            "notes": self.notes,
        }


_CHAT_CAPS = frozenset({Cap.STREAMING, Cap.SYSTEM_PROMPT, Cap.TOOLS})

PROFILES: dict[str, ProviderProfile] = {
    "anthropic": ProviderProfile(
        type="anthropic",
        display_name="Anthropic",
        locality="cloud",
        wire="anthropic-messages",
        auth="x-api-key",
        base_url="https://api.anthropic.com",
        official_hosts=("api.anthropic.com",),
        discovery="anthropic",
        credential_sources=("keyring", "env", "session"),
        default_headers={"anthropic-version": "2023-06-01"},
        default_env_var="ANTHROPIC_API_KEY",
        caps=_CHAT_CAPS | {Cap.MODEL_DISCOVERY},
        data_usage_url="https://privacy.anthropic.com/",
    ),
    "openai": ProviderProfile(
        type="openai",
        display_name="OpenAI",
        locality="cloud",
        wire="openai-chat",
        auth="bearer-static",
        base_url="https://api.openai.com/v1",
        official_hosts=("api.openai.com",),
        discovery="openai",
        credential_sources=("keyring", "env", "session"),
        default_env_var="OPENAI_API_KEY",
        stream_usage=True,
        caps=_CHAT_CAPS | {Cap.MODEL_DISCOVERY},
        data_usage_url="https://openai.com/enterprise-privacy/",
    ),
    "vertex": ProviderProfile(
        type="vertex",
        display_name="Google Vertex AI",
        locality="cloud",
        wire="openai-chat",
        auth="bearer-google-oauth",
        base_url=None,
        # Global endpoint, and regional ones such as us-central1-aiplatform.googleapis.com.
        official_hosts=("aiplatform.googleapis.com", "*-aiplatform.googleapis.com"),
        discovery="none",
        credential_sources=("adc", "key_file", "session"),
        required_fields=("project", "location"),
        caps=_CHAT_CAPS,
        data_usage_url="https://cloud.google.com/vertex-ai/generative-ai/docs/data-governance",
        notes=("Uses Google Cloud credentials (Application Default Credentials, a "
               "service-account key file path, or a pasted short-lived access token). "
               "Add the model ids your project can use; Vertex offers no model list here."),
    ),
    "ai_studio": ProviderProfile(
        type="ai_studio",
        display_name="Google AI Studio (Gemini API)",
        locality="cloud",
        wire="openai-chat",
        auth="bearer-static",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        official_hosts=("generativelanguage.googleapis.com",),
        discovery="gemini-native",
        credential_sources=("keyring", "env", "session"),
        default_env_var="GEMINI_API_KEY",
        caps=_CHAT_CAPS | {Cap.MODEL_DISCOVERY},
        data_usage_url="https://ai.google.dev/gemini-api/terms",
    ),
    "openrouter": ProviderProfile(
        type="openrouter",
        display_name="OpenRouter",
        locality="cloud",
        wire="openai-chat",
        auth="bearer-static",
        base_url="https://openrouter.ai/api/v1",
        official_hosts=("openrouter.ai",),
        discovery="openai",
        credential_sources=("keyring", "env", "session"),
        default_env_var="OPENROUTER_API_KEY",
        caps=_CHAT_CAPS | {Cap.MODEL_DISCOVERY},
        data_usage_url="https://openrouter.ai/privacy",
        notes="OpenRouter may route a request to different upstream providers.",
    ),
    "openai_compatible": ProviderProfile(
        type="openai_compatible",
        display_name="Local / OpenAI-compatible",
        locality="local",
        wire="openai-chat",
        auth="optional-bearer",
        base_url=None,
        official_hosts=(),
        discovery="openai",
        credential_sources=("none", "keyring", "env", "session"),
        required_fields=("base_url",),
        caps=frozenset({Cap.STREAMING, Cap.SYSTEM_PROMPT, Cap.MODEL_DISCOVERY}),
        notes=("Any server implementing /chat/completions with streaming "
               "(Ollama, LM Studio, vLLM, llama.cpp server). The workbench server "
               "makes the call, so browser CORS settings do not matter."),
    ),
}


@dataclass(frozen=True)
class Preset:
    id: str
    display_name: str
    base_url: str | None
    notes: str = ""


PRESETS: dict[str, Preset] = {
    "ollama": Preset("ollama", "Ollama", "http://127.0.0.1:11434/v1",
                     "Start Ollama and pull a model (ollama pull <model>)."),
    "lm_studio": Preset("lm_studio", "LM Studio", "http://localhost:1234/v1",
                        "Start the LM Studio local server and load a model."),
    "custom": Preset("custom", "Custom", None,
                     "Base URL of any OpenAI-compatible server, e.g. http://127.0.0.1:8000/v1."),
}


def get_profile(provider_type: str) -> ProviderProfile:
    try:
        return PROFILES[provider_type]
    except KeyError as exc:
        raise KeyError(f"unknown provider type {provider_type!r}") from exc


def vertex_base_url(project: str, location: str) -> str:
    """The Vertex OpenAI-compatible endpoint base for ``project``/``location``."""
    if not GCP_PROJECT_RE.match(project or ""):
        raise ValueError("invalid GCP project id")
    if not VERTEX_LOCATION_RE.match(location or ""):
        raise ValueError("invalid Vertex location")
    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    return f"https://{host}/v1/projects/{project}/locations/{location}/endpoints/openapi"


def host_is_official(profile: ProviderProfile, host: str) -> bool:
    """``host`` is one of the profile's official hosts.

    A pattern ``*.suffix`` or ``*-suffix`` matches exactly ONE leading label
    (letters, digits, hyphens; no dots) — e.g. ``us-central1-aiplatform…``.
    """
    h = (host or "").lower().rstrip(".")
    for pattern in profile.official_hosts:
        p = pattern.lower()
        if p.startswith(("*.", "*-")):
            suffix = p[1:]            # ".example.com" / "-aiplatform.googleapis.com"
            label = h[: -len(suffix)] if h.endswith(suffix) else ""
            if label and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label):
                return True
        elif h == p:
            return True
    return False
