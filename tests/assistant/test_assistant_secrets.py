"""Credential storage and redaction: keys never leak into logs or responses."""
from __future__ import annotations

import logging

import pytest

from vivarium_workbench_assistant import secrets as sec
from vivarium_workbench_assistant.config import CredentialRef, ProviderInstance

KEY = "sk-ant-api03-TESTKEY-abcdefghijklmnopqrstuvwxyz0123456789"


def _inst(source="keyring", **kw):
    return ProviderInstance(id="anthropic", type="anthropic", display_name="Anthropic",
                            credential=CredentialRef(source=source, **kw))


@pytest.mark.parametrize("text", [
    f"Authorization: Bearer {KEY}",
    f"x-api-key: {KEY}",
    f'{{"api_key": "{KEY}"}}',
    "AIzaSyA-1234567890abcdefghijklmnopqrstuvwxyz",
    "ya29.a0AfH6SMBx-very-long-oauth-token-value",
    "ghp_" + "a" * 36,
    "https://example.com/v1/models?key=supersecretvalue123&x=1",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    "sk-or-v1-0123456789abcdef0123456789abcdef",
    "AKIAABCDEFGHIJKLMNOP",
])
def test_redact_patterns(text):
    out = sec.redact(text)
    assert "[redacted" in out
    for secret_part in ("TESTKEY", "SyA-1234567890", "very-long-oauth", "a" * 30, "supersecretvalue123",
                        "MIIabc", "0123456789abcdef0123", "ABCDEFGHIJKLMNOP"):
        assert secret_part not in out


def test_redact_known_exact_value_anywhere():
    weird = "custom-proxy-token-without-a-known-prefix-9f8e7d"
    sec.register_known_secret(weird)
    assert weird not in sec.redact(f"upstream said: invalid token {weird}!")
    assert sec.redact("nothing secret here") == "nothing secret here"


def test_redact_obj_recurses():
    sec.register_known_secret(KEY)
    out = sec.redact_obj({"a": [KEY, {"b": KEY}], "n": 3})
    assert KEY not in repr(out) and out["n"] == 3


def test_log_records_are_redacted(caplog):
    sec.register_known_secret(KEY)
    lg = sec.get_logger("vivarium_workbench_assistant.test_logger")
    with caplog.at_level(logging.INFO, logger="vivarium_workbench_assistant.test_logger"):
        lg.info("calling provider with %s", KEY)
        try:
            raise RuntimeError(f"boom {KEY}")
        except RuntimeError:
            lg.exception("failed")
    assert KEY not in caplog.text
    assert "[redacted]" in caplog.text


def test_install_log_redaction_quiets_http_libraries():
    sec.install_log_redaction()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore.http11").getEffectiveLevel() >= logging.WARNING


def test_keyring_store_roundtrip(memory_keyring):
    store = sec.SecretStore()
    inst = _inst("keyring")
    assert store.set(inst, KEY, persist=True) == "keyring"
    assert memory_keyring.store[("vivarium-workbench-assistant", "anthropic:api_key")] == KEY
    assert store.get(inst) == KEY
    st = store.status(inst)
    assert st.configured and st.source == "keyring" and st.hint == "••••6789"
    assert KEY not in repr(st.to_json())
    store.delete("anthropic")
    assert store.get(inst) is None
    assert not memory_keyring.store


def test_no_keychain_falls_back_to_session():
    store = sec.SecretStore()
    inst = _inst("keyring")
    assert store.set(inst, KEY, persist=True) == "session"
    session_inst = _inst("session")
    assert store.get(session_inst) == KEY


def test_session_scopes_are_isolated():
    store = sec.SecretStore()
    inst = _inst("session")
    store.set(inst, KEY, persist=False, scope="session-A")
    assert store.get(inst, scope="session-A") == KEY
    assert store.get(inst, scope="session-B") is None
    assert store.get(inst) is None           # local scope


def test_env_source_reads_value_at_use_time(monkeypatch):
    store = sec.SecretStore()
    inst = _inst("env", env_var="MY_ANTHROPIC_KEY")
    assert store.get(inst) is None
    monkeypatch.setenv("MY_ANTHROPIC_KEY", KEY)
    assert store.get(inst) == KEY
    st = store.status(inst)
    assert st.configured and st.source == "env" and st.hint is None


@pytest.mark.parametrize("name", ["VIVARIUM_WORKBENCH_GH_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY",
                                  "AWS_WHATEVER", "lowercase", "1BAD", "GOOGLE_APPLICATION_CREDENTIALS"])
def test_denied_env_var_names(name):
    with pytest.raises(sec.SecretStoreError):
        sec.validate_env_var_name(name)


def test_env_source_cannot_read_denied_variable(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "b" * 36)
    store = sec.SecretStore()
    inst = _inst("env", env_var="GITHUB_TOKEN")
    assert store.get(inst) is None


def test_rejects_malformed_credentials():
    store = sec.SecretStore()
    inst = _inst("session")
    for bad in ("", "   ", "a\nb", "x" * 9000):
        with pytest.raises(sec.SecretStoreError):
            store.set(inst, bad, persist=False)


def test_hint_only_for_long_values():
    assert sec.masked_hint("short") is None
    assert sec.masked_hint("0123456789abcdef") == "••••cdef"


def test_secret_findings_for_composer_warning():
    assert "Anthropic-style key" in sec.secret_findings(f"here is my key {KEY}")
    assert sec.secret_findings("just a question about growth") == []


def test_stream_redactor_handles_values_split_across_chunks():
    secret = "sk-proj-STREAMSPLIT-0123456789abcdef"
    sec.register_known_secret(secret)
    r = sec.StreamRedactor()
    chunks = ["The key is ", secret[:7], secret[7:20], secret[20:] + " and", " more text"]
    out = "".join(r.feed(c) for c in chunks) + r.flush()
    assert secret not in out and "[redacted]" in out
    assert out.endswith(" and more text")
    # Text that merely starts like a secret is released at the end.
    r2 = sec.StreamRedactor()
    assert r2.feed("sk-p") == "" and r2.flush() == "sk-p"
    # Ordinary text flows through without delay.
    assert sec.StreamRedactor().feed("hello") == "hello"
