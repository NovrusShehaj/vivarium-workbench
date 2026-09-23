"""The assistant's service bundle and deployment-mode rules.

One :class:`AssistantServices` instance lives for the server process (created
by the extension). It decides, per request:

* **scope** — ``"local"`` on a single-user loopback server; otherwise the
  browser session key, so hosted sessions never share credentials, history,
  preferences, proposals or approvals;
* **instances** — the user's ``config.json`` in local mode; in hosted mode
  only the operator's deploy-config ``assistant:`` block (``instances:`` and/or
  ``credential_env``), never user-editable;
* **preferences** — the config file in local mode; per-session memory in
  hosted mode;
* **persistence** — conversation history on disk only when allowed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vivarium_workbench.lib import deploy_config, user_dirs
from vivarium_workbench_assistant import policy
from vivarium_workbench_assistant.audit import AuditLog
from vivarium_workbench_assistant.config import (
    AssistantConfig,
    AssistantPreferences,
    ConfigStore,
    CredentialRef,
    ProviderInstance,
)
from vivarium_workbench_assistant.conversations import ConversationStore, assistant_data_dir
from vivarium_workbench_assistant.edits.proposals import ProposalStore
from vivarium_workbench_assistant.models_registry import ModelRegistry
from vivarium_workbench_assistant.providers.adapters import ProviderAdapter
from vivarium_workbench_assistant.providers.auth_google import GoogleTokenProvider
from vivarium_workbench_assistant.providers.profiles import PROFILES
from vivarium_workbench_assistant.secrets import LOCAL_SCOPE, SecretStore, get_logger
from vivarium_workbench_assistant.streaming import RunRegistry
from vivarium_workbench_assistant.tools.registry import ToolRegistry, default_registry

log = get_logger("vivarium_workbench_assistant.services")


@dataclass
class AssistantServices:
    config: ConfigStore = field(default_factory=ConfigStore)
    secrets: SecretStore = field(default_factory=SecretStore)
    conversations: ConversationStore = field(default_factory=ConversationStore)
    proposals: ProposalStore = field(default_factory=ProposalStore)
    runs: RunRegistry = field(default_factory=RunRegistry)
    tools: ToolRegistry = field(default_factory=default_registry)
    audit: AuditLog = field(default_factory=lambda: AuditLog(lambda: assistant_data_dir() / "audit.jsonl"))
    #: (scope, conversation id) -> granted "category:tool" keys ("allow for this conversation")
    grants: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    #: hosted mode: per-session preferences
    session_prefs: dict[str, AssistantPreferences] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.google = GoogleTokenProvider(self.secrets)
        self.models = ModelRegistry(
            cache_path=lambda: assistant_data_dir() / "models.cache.json",
            overrides_path=lambda: user_dirs.user_config_dir() / "assistant" / "models.overrides.json",
            persist_cache=lambda: policy.mode() == "local",
        )

    # -- mode / scope ------------------------------------------------------------
    def mode(self) -> policy.Mode:
        return policy.mode()

    def scope_for(self, session_key: str | None) -> str:
        if self.mode() == "local":
            return LOCAL_SCOPE
        return session_key or "anonymous"

    def capabilities(self) -> policy.Capabilities:
        return policy.capabilities(self.preferences(LOCAL_SCOPE).persist_conversations
                                   if self.mode() == "local" else True)

    # -- configuration --------------------------------------------------------------
    def load_config(self) -> AssistantConfig:
        if self.mode() == "local":
            return self.config.load()
        return AssistantConfig(instances=self.hosted_instances())

    def hosted_instances(self) -> list[ProviderInstance]:
        hp = policy.hosted_policy()
        block = deploy_config.assistant_block()
        out: list[ProviderInstance] = []
        seen: set[str] = set()
        for raw in block.get("instances") or []:
            if not isinstance(raw, dict):
                continue
            try:
                inst = ProviderInstance.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 - a bad operator entry is skipped, not fatal
                log.warning("ignoring invalid operator assistant instance: %s", type(exc).__name__)
                continue
            if inst.type in hp.providers_allowed and inst.id not in seen:
                out.append(inst)
                seen.add(inst.id)
        for ptype, env_var in hp.credential_env.items():
            if ptype not in PROFILES or ptype not in hp.providers_allowed or ptype in seen:
                continue
            if "env" not in PROFILES[ptype].credential_sources:
                continue
            try:
                out.append(ProviderInstance(id=ptype.replace("_", "-"), type=ptype,  # type: ignore[arg-type]
                                            display_name=PROFILES[ptype].display_name,
                                            credential=CredentialRef(source="env", env_var=env_var)))
                seen.add(ptype)
            except Exception:  # noqa: BLE001
                continue
        return out

    def instance(self, iid: str) -> ProviderInstance | None:
        return self.load_config().instance(iid)

    def preferences(self, scope: str) -> AssistantPreferences:
        if self.mode() == "local":
            return self.config.load().preferences
        return self.session_prefs.setdefault(scope, AssistantPreferences(persist_conversations=False))

    def set_preferences(self, scope: str, prefs: AssistantPreferences) -> AssistantPreferences:
        if self.mode() == "local":
            def upd(cfg: AssistantConfig) -> AssistantConfig:
                cfg.preferences = prefs
                return cfg
            return self.config.update(upd).preferences
        prefs = prefs.model_copy(update={"persist_conversations": prefs.persist_conversations
                                         and policy.hosted_policy().persist_conversations})
        self.session_prefs[scope] = prefs
        return prefs

    def persist(self, scope: str) -> bool:
        if self.mode() == "local":
            return self.preferences(scope).persist_conversations
        return policy.hosted_policy().persist_conversations and self.preferences(scope).persist_conversations

    # -- providers ------------------------------------------------------------------
    def adapter(self, inst: ProviderInstance, scope: str) -> ProviderAdapter:
        mode = self.mode()
        return ProviderAdapter(inst, mode=mode if mode != "disabled" else "hosted", secrets=self.secrets,
                               google=self.google, hosted=policy.hosted_policy() if mode != "local" else None,
                               scope=scope)

    def model_allowlist(self, inst: ProviderInstance) -> tuple[str, ...] | None:
        if self.mode() == "local":
            return None
        allowed = policy.hosted_policy().models_allowed.get(inst.type)
        return allowed or None

    def grants_for(self, scope: str, conversation_id: str) -> set[str]:
        return self.grants.setdefault((scope, conversation_id), set())


def data_root() -> Path:
    return assistant_data_dir()


def public_instance(inst: ProviderInstance, services: AssistantServices, scope: str) -> dict[str, Any]:
    """An instance for the browser: config (no secrets) + credential status."""
    data = inst.model_dump(mode="json")
    local = services.mode() == "local"
    status = services.secrets.status(inst, scope=scope, show_hint=local)
    data["credential_status"] = status.to_json()
    profile = PROFILES[inst.type]
    data["locality"] = profile.locality
    data["profile"] = profile.to_json()
    if not local:
        # Operators' key-file paths / env var names are not the user's business.
        data["credential"] = {"source": inst.credential.source}
    return data
