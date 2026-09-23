"""Deployment mode, hosted policy (fail closed) and the tool permission engine."""
from __future__ import annotations

import pytest
import yaml

from vivarium_workbench.lib import server_runtime
from vivarium_workbench_assistant import policy


def _deploy(tmp_path, monkeypatch, block):
    path = tmp_path / "deploy.yaml"
    path.write_text(yaml.safe_dump({"assistant": block}))
    monkeypatch.setenv("VIVARIUM_WORKBENCH_DEPLOY_CONFIG", str(path))


def test_unknown_bind_fails_closed_to_hosted():
    assert policy.mode() == "hosted"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_bind_is_local(host):
    server_runtime.configure(bind_host=host)
    assert policy.mode() == "local"


def test_non_loopback_proxy_or_base_path_is_hosted(monkeypatch):
    server_runtime.configure(bind_host="0.0.0.0")
    assert policy.mode() == "hosted"
    server_runtime.configure(bind_host="127.0.0.1", base_path="/wb")
    assert policy.mode() == "hosted"
    server_runtime.configure(bind_host="127.0.0.1")
    monkeypatch.setenv("VIVARIUM_WORKBENCH_TRUST_PROXY", "1")
    assert policy.mode() == "hosted"


def test_operator_pin(monkeypatch):
    server_runtime.configure(bind_host="127.0.0.1")
    monkeypatch.setenv("VIVARIUM_WORKBENCH_ASSISTANT_MODE", "disabled")
    assert policy.mode() == "disabled"
    assert policy.availability()[0] is False
    monkeypatch.setenv("VIVARIUM_WORKBENCH_ASSISTANT_MODE", "bogus")
    assert policy.mode() == "local"


def test_hosted_is_disabled_without_operator_opt_in():
    ok, reason = policy.availability()
    assert ok is False
    assert "shared deployments" in reason


def test_hosted_opt_in_and_capabilities(tmp_path, monkeypatch):
    _deploy(tmp_path, monkeypatch, {"enabled": True, "providers_allowed": ["anthropic"],
                                    "credential_env": {"anthropic": "ANTHROPIC_API_KEY"}})
    assert policy.availability() == (True, "")
    caps = policy.capabilities()
    assert caps.mode == "hosted"
    assert caps.config_editable is False
    assert caps.user_credentials is False
    assert caps.persistent_credentials is False
    assert caps.persist_conversations is False
    assert caps.tools_execute is False and caps.tools_shell is False
    assert caps.apply_edits is False
    assert caps.provider_types == ("anthropic",)


def test_local_capabilities():
    server_runtime.configure(bind_host="127.0.0.1")
    caps = policy.capabilities()
    assert caps.config_editable and caps.user_credentials and caps.tools_shell and caps.apply_edits
    assert set(caps.provider_types) == {"anthropic", "openai", "vertex", "ai_studio", "openrouter", "openai_compatible"}


def test_malformed_operator_block_contributes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "deploy.yaml"
    path.write_text("assistant: [this, is, not, a, mapping]\n")
    monkeypatch.setenv("VIVARIUM_WORKBENCH_DEPLOY_CONFIG", str(path))
    assert policy.hosted_policy().enabled is False



def _local_caps():
    server_runtime.configure(bind_host="127.0.0.1")
    return policy.capabilities()


PREFS = {"read": "auto", "execute": "ask", "shell": "disabled"}


def test_decide_read_auto_and_ask():
    caps = _local_caps()
    assert policy.decide(category="read", tool="read_file", tool_prefs=PREFS, caps=caps).action == "allow"
    assert policy.decide(category="read", tool="read_file", tool_prefs={**PREFS, "read": "ask"},
                         caps=caps).action == "ask"
    assert policy.decide(category="read", tool="read_file", tool_prefs={**PREFS, "read": "ask"},
                         grants={"read:read_file"}, caps=caps).action == "allow"
    assert policy.decide(category="read", tool="read_file", tool_prefs={**PREFS, "read": "disabled"},
                         caps=caps).action == "deny"


def test_decide_gitignored_read_asks_locally_denies_hosted(tmp_path, monkeypatch):
    caps = _local_caps()
    assert policy.decide(category="read", tool="read_file", tool_prefs=PREFS, gitignored=True,
                         caps=caps).action == "ask"
    server_runtime.reset()
    _deploy(tmp_path, monkeypatch, {"enabled": True, "providers_allowed": ["openai"]})
    assert policy.decide(category="read", tool="read_file", tool_prefs=PREFS, gitignored=True).action == "deny"


def test_decide_propose_is_allowed_apply_never():
    caps = _local_caps()
    assert policy.decide(category="propose", tool="propose_edit", tool_prefs=PREFS, caps=caps).action == "allow"
    assert policy.decide(category="apply", tool="anything", tool_prefs=PREFS, caps=caps).action == "deny"


def test_decide_execute_always_asks_unless_granted():
    caps = _local_caps()
    assert policy.decide(category="execute", tool="run_tests", tool_prefs=PREFS, caps=caps).action == "ask"
    assert policy.decide(category="execute", tool="run_tests", tool_prefs=PREFS,
                         grants={"execute:run_tests"}, caps=caps).action == "allow"
    assert policy.decide(category="execute", tool="run_tests", tool_prefs={**PREFS, "execute": "disabled"},
                         caps=caps).action == "deny"


def test_decide_shell_disabled_by_default_and_never_granted():
    caps = _local_caps()
    assert policy.decide(category="shell", tool="shell", tool_prefs=PREFS, caps=caps).action == "deny"
    enabled = {**PREFS, "shell": "ask"}
    assert policy.decide(category="shell", tool="shell", tool_prefs=enabled, caps=caps).action == "ask"
    # A conversation grant can never auto-allow the shell.
    assert policy.decide(category="shell", tool="shell", tool_prefs=enabled,
                         grants={"shell:shell", "execute:shell"}, caps=caps).action == "ask"


def test_decide_hosted_never_executes(tmp_path, monkeypatch):
    _deploy(tmp_path, monkeypatch, {"enabled": True, "providers_allowed": ["openai"]})
    assert policy.decide(category="execute", tool="run_tests", tool_prefs=PREFS).action == "deny"
    assert policy.decide(category="shell", tool="shell", tool_prefs={**PREFS, "shell": "ask"}).action == "deny"
