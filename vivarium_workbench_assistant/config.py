"""Assistant configuration: provider instances + preferences (no secrets).

Stored at ``<user config dir>/assistant/config.json`` (``0600`` in a ``0700``
directory; see ``vivarium_workbench.lib.user_dirs``). The file holds only
non-secret settings — provider type, endpoint fields, the credential *source*
(and for ``env`` the variable *name*, for ``key_file`` the *path*) and the
default model. Keys live in ``secrets.SecretStore``.

Versioned: ``load_config`` migrates older versions in order, refuses to
overwrite a file written by a newer version (read-only with a warning) and
quarantines an unreadable file as ``config.json.invalid-<ts>``.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from vivarium_workbench.lib import user_dirs
from vivarium_workbench_assistant.providers.profiles import (
    GCP_PROJECT_RE,
    PRESETS,
    PROFILES,
    VERTEX_LOCATION_RE,
)
from vivarium_workbench_assistant.secrets import get_logger

log = get_logger("vivarium_workbench_assistant.config")

CONFIG_VERSION = 1
INSTANCE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$")

ProviderType = Literal["anthropic", "openai", "vertex", "ai_studio", "openrouter", "openai_compatible"]


class CredentialRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["keyring", "env", "session", "adc", "key_file", "none"]
    #: When ``source == "env"``: the variable NAME (its value is never stored).
    env_var: str | None = None
    #: Vertex, local mode only: the PATH of a service-account key file.
    key_file_path: str | None = None


class ProviderInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: ProviderType
    display_name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    credential: CredentialRef
    base_url: str | None = None
    preset: Literal["ollama", "lm_studio", "custom"] | None = None
    project: str | None = None
    location: str | None = None
    default_model: str | None = None
    manual_models: list[str] = Field(default_factory=list, max_length=200)
    first_token_timeout_s: float | None = Field(default=None, ge=5, le=900)
    ca_bundle_path: str | None = None
    send_attribution_headers: bool = False
    #: Manual context window for models whose provider does not report one.
    context_window_override: int | None = Field(default=None, ge=1024, le=10_000_000)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not INSTANCE_ID_RE.match(v):
            raise ValueError("instance id: lowercase letters, digits and '-', starting with a letter")
        return v

    @field_validator("default_model")
    @classmethod
    def _model(cls, v: str | None) -> str | None:
        if v is not None and not MODEL_ID_RE.match(v):
            raise ValueError("invalid model id")
        return v

    @field_validator("manual_models")
    @classmethod
    def _models(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for m in v:
            m = m.strip()
            if not MODEL_ID_RE.match(m):
                raise ValueError(f"invalid model id {m!r}")
            if m not in out:
                out.append(m)
        return out

    @model_validator(mode="after")
    def _consistent(self) -> "ProviderInstance":
        profile = PROFILES[self.type]
        if self.credential.source not in profile.credential_sources:
            raise ValueError(f"{profile.display_name} does not support credential source "
                             f"{self.credential.source!r}")
        if self.credential.source == "env" and not self.credential.env_var:
            raise ValueError("credential source 'env' needs env_var")
        if self.credential.source == "key_file" and not self.credential.key_file_path:
            raise ValueError("credential source 'key_file' needs key_file_path")
        if self.type == "vertex":
            if not (self.project and GCP_PROJECT_RE.match(self.project)):
                raise ValueError("Vertex needs a valid GCP project id")
            if not (self.location and VERTEX_LOCATION_RE.match(self.location)):
                raise ValueError("Vertex needs a location ('global' or a region such as us-central1)")
        if self.type == "openai_compatible":
            if not self.base_url:
                preset = PRESETS.get(self.preset or "")
                if preset is None or not preset.base_url:
                    raise ValueError("a local/OpenAI-compatible provider needs a base URL")
                self.base_url = preset.base_url
        if self.send_attribution_headers and self.type != "openrouter":
            self.send_attribution_headers = False
        return self


class AssistantPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_instance: str | None = None
    auto_context: Literal["off", "page_summary", "page_and_selection"] = "page_summary"
    confirm_first_cloud_send: bool = True
    persist_conversations: bool = True
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    system_prompt_extra: str | None = Field(default=None, max_length=4000)
    tools: dict[Literal["read", "execute", "shell"], Literal["auto", "ask", "disabled"]] = Field(
        default_factory=lambda: {"read": "auto", "execute": "ask", "shell": "disabled"})
    panel_shortcut: str | None = Field(default=None, max_length=40)


class AssistantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    instances: list[ProviderInstance] = Field(default_factory=list, max_length=50)
    preferences: AssistantPreferences = Field(default_factory=AssistantPreferences)

    @model_validator(mode="after")
    def _unique_ids(self) -> "AssistantConfig":
        ids = [i.id for i in self.instances]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate provider instance id")
        if self.preferences.default_instance and self.preferences.default_instance not in ids:
            self.preferences.default_instance = None
        return self

    def instance(self, iid: str) -> ProviderInstance | None:
        return next((i for i in self.instances if i.id == iid), None)


#: dict -> dict migrations keyed by the version they upgrade FROM.
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def config_path() -> Path:
    return user_dirs.user_config_dir() / "assistant" / "config.json"


class ConfigStore:
    """Load/save ``config.json`` with migration, quarantine and a process lock."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self.read_only_reason: str | None = None

    @property
    def path(self) -> Path:
        return self._path or config_path()

    def load(self) -> AssistantConfig:
        with self._lock:
            p = self.path
            if not p.is_file():
                return AssistantConfig()
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("config is not a JSON object")
                version = int(raw.get("version", 1))
                if version > CONFIG_VERSION:
                    self.read_only_reason = (
                        f"{p} was written by a newer version (v{version}); settings are read-only.")
                    log.warning("%s", self.read_only_reason)
                    raw = {k: v for k, v in raw.items() if k in ("instances", "preferences")}
                    raw["version"] = CONFIG_VERSION
                    return AssistantConfig.model_validate(raw)
                while version < CONFIG_VERSION:
                    raw = MIGRATIONS[version](raw)
                    version += 1
                    raw["version"] = version
                return AssistantConfig.model_validate(raw)
            except (ValueError, ValidationError, KeyError, OSError) as exc:
                quarantined = p.with_name(f"{p.name}.invalid-{int(time.time())}")
                try:
                    p.replace(quarantined)
                    user_dirs.ensure_private_dir(quarantined.parent)
                except OSError:
                    pass
                log.warning("assistant config was invalid (%s); moved aside to %s and using defaults",
                            type(exc).__name__, quarantined.name)
                return AssistantConfig()

    def save(self, cfg: AssistantConfig) -> None:
        with self._lock:
            if self.read_only_reason:
                raise PermissionError(self.read_only_reason)
            data = cfg.model_dump(mode="json")
            user_dirs.write_private_text(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def update(self, fn: Callable[[AssistantConfig], AssistantConfig]) -> AssistantConfig:
        """Read-modify-write under the lock; ``fn`` returns the new config."""
        with self._lock:
            cfg = fn(self.load())
            cfg = AssistantConfig.model_validate(cfg.model_dump(mode="json"))
            self.save(cfg)
            return cfg
