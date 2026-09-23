"""Deployment mode, operator policy and the tool permission engine.

**Mode** (plan §10.1):

* ``local``  — the server is bound to loopback with no proxy/base path (a
  single-user tool on the user's own machine), per
  ``vivarium_workbench.lib.server_runtime.deployment_mode``;
* ``hosted`` — anything else, *including an unknown bind* (fail closed);
* ``disabled`` — pinned by the operator.

``VIVARIUM_WORKBENCH_ASSISTANT_MODE=local|hosted|disabled`` pins the mode.

**Hosted** deployments have no per-user identity, so the assistant is disabled
there unless the deploy config's ``assistant:`` block sets ``enabled: true``;
even then configuration is operator-owned (no user-editable instances, no
user keys unless ``allow_user_credentials``, no persisted history unless
``persist_conversations``, no execute/shell tools) and nothing is shared
between browser sessions.

**Tool permissions** (plan §6.8): ``decide()`` is the only place a tool call is
allowed, sent for approval, or denied. The model cannot change the policy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from vivarium_workbench.lib import deploy_config, server_runtime
from vivarium_workbench.lib.env_compat import get_env

Mode = Literal["local", "hosted", "disabled"]
ToolCategory = Literal["read", "propose", "apply", "execute", "shell"]

MODE_ENV_SUFFIX = "ASSISTANT_MODE"


def mode(env: Mapping[str, str] | None = None) -> Mode:
    pinned = (get_env(MODE_ENV_SUFFIX, "", env=env) or "").strip().lower()
    if pinned in ("local", "hosted", "disabled"):
        return pinned  # type: ignore[return-value]
    return server_runtime.deployment_mode()


@dataclass(frozen=True)
class HostedPolicy:
    enabled: bool = False
    allow_user_credentials: bool = False
    providers_allowed: frozenset[str] = frozenset()
    #: provider type -> environment variable holding the operator's key
    credential_env: dict[str, str] = field(default_factory=dict)
    #: exact base URLs (scheme://host[:port][/path]) an operator permits
    base_url_allowlist: tuple[str, ...] = ()
    #: provider type -> allowed model ids (empty = any discovered)
    models_allowed: dict[str, tuple[str, ...]] = field(default_factory=dict)
    persist_conversations: bool = False
    tools_execute: bool = False
    #: Reviewed proposals may be applied to the (shared) workspace.
    apply_edits: bool = False


def _as_bool(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "on"))


def hosted_policy(block: Mapping[str, Any] | None = None) -> HostedPolicy:
    raw = dict(block if block is not None else deploy_config.assistant_block())
    providers = raw.get("providers_allowed") or []
    cred_env = raw.get("credential_env") or {}
    allow = raw.get("base_url_allowlist") or []
    models = raw.get("models_allowed") or {}
    tools = raw.get("tools") or {}
    return HostedPolicy(
        enabled=_as_bool(raw.get("enabled")),
        allow_user_credentials=_as_bool(raw.get("allow_user_credentials")),
        providers_allowed=frozenset(str(p) for p in providers if isinstance(p, str)),
        credential_env={str(k): str(v) for k, v in cred_env.items()
                        if isinstance(k, str) and isinstance(v, str)} if isinstance(cred_env, dict) else {},
        base_url_allowlist=tuple(str(u).rstrip("/") for u in allow if isinstance(u, str)),
        models_allowed={str(k): tuple(str(m) for m in (v or []) if isinstance(m, str))
                        for k, v in models.items()} if isinstance(models, dict) else {},
        persist_conversations=_as_bool(raw.get("persist_conversations")),
        tools_execute=isinstance(tools, dict) and str(tools.get("execute", "")).lower() == "ask",
        apply_edits=isinstance(tools, dict) and str(tools.get("apply", "")).lower() in ("ask", "review"),
    )


def availability() -> tuple[bool, str]:
    m = mode()
    if m == "disabled":
        return False, "The assistant is disabled by the operator."
    if m == "hosted" and not hosted_policy().enabled:
        return False, ("Disabled on shared deployments (no per-user authentication). "
                       "An operator can enable it with assistant.enabled in the deploy config.")
    return True, ""


@dataclass(frozen=True)
class Capabilities:
    mode: Mode
    #: Users may add/edit/remove provider instances (local mode only).
    config_editable: bool
    #: Users may store their own credentials.
    user_credentials: bool
    #: Keychain persistence of credentials (local mode only).
    persistent_credentials: bool
    persist_conversations: bool
    local_providers: bool
    tools_read: bool
    tools_execute: bool
    tools_shell: bool
    provider_types: tuple[str, ...]
    apply_edits: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "config_editable": self.config_editable,
            "user_credentials": self.user_credentials,
            "persistent_credentials": self.persistent_credentials,
            "persist_conversations": self.persist_conversations,
            "local_providers": self.local_providers,
            "tools": {"read": self.tools_read, "execute": self.tools_execute, "shell": self.tools_shell},
            "provider_types": list(self.provider_types),
            "apply_edits": self.apply_edits,
        }


ALL_TYPES = ("anthropic", "openai", "vertex", "ai_studio", "openrouter", "openai_compatible")


def capabilities(persist_pref: bool = True) -> Capabilities:
    m = mode()
    if m == "local":
        return Capabilities(
            mode=m, config_editable=True, user_credentials=True, persistent_credentials=True,
            persist_conversations=persist_pref, local_providers=True, tools_read=True,
            tools_execute=True, tools_shell=True, provider_types=ALL_TYPES,
        )
    hp = hosted_policy()
    allowed = tuple(t for t in ALL_TYPES if t in hp.providers_allowed)
    return Capabilities(
        mode=m, config_editable=False, user_credentials=hp.allow_user_credentials,
        persistent_credentials=False,
        persist_conversations=hp.persist_conversations and persist_pref,
        # An internal model service is reachable only when allowlisted.
        local_providers="openai_compatible" in allowed and bool(hp.base_url_allowlist),
        tools_read=True, tools_execute=hp.tools_execute, tools_shell=False,
        provider_types=allowed, apply_edits=hp.apply_edits,
    )


# ---------------------------------------------------------------------------
# Tool permission engine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    action: Literal["allow", "ask", "deny"]
    reason: str = ""


def grant_key(category: str, tool: str) -> str:
    return f"{category}:{tool}"


def decide(
    *,
    category: ToolCategory,
    tool: str,
    tool_prefs: Mapping[str, str],
    grants: frozenset[str] | set[str] = frozenset(),
    gitignored: bool = False,
    caps: Capabilities | None = None,
) -> Decision:
    """Allow, ask or deny one tool call. Pure; the caller enforces the result."""
    caps = caps or capabilities()
    if caps.mode == "disabled":
        return Decision("deny", "the assistant is disabled")
    if category == "read":
        pref = tool_prefs.get("read", "auto")
        if pref == "disabled" or not caps.tools_read:
            return Decision("deny", "read tools are disabled in Settings")
        if gitignored:
            if caps.mode != "local":
                return Decision("deny", "gitignored files are never read on shared deployments")
            return Decision("ask", "the file is gitignored")
        if pref == "ask" and grant_key("read", tool) not in grants:
            return Decision("ask", "read tools require approval (Settings)")
        return Decision("allow")
    if category == "propose":
        return Decision("allow")  # proposals have no side effects; applying is reviewed
    if category == "apply":
        return Decision("deny", "edits are applied only from the proposal review, never by a tool")
    if category == "execute":
        if not caps.tools_execute:
            return Decision("deny", "execution tools are not available on this deployment")
        if tool_prefs.get("execute", "ask") == "disabled":
            return Decision("deny", "execution tools are disabled in Settings")
        if grant_key("execute", tool) in grants:
            return Decision("allow")
        return Decision("ask", "execution requires approval")
    if category == "shell":
        if caps.mode != "local" or not caps.tools_shell:
            return Decision("deny", "the shell tool is never available on shared deployments")
        if tool_prefs.get("shell", "disabled") != "ask":
            return Decision("deny", "the shell tool is disabled (enable it in Settings)")
        return Decision("ask", "every shell command requires approval")
    return Decision("deny", "unknown tool category")
