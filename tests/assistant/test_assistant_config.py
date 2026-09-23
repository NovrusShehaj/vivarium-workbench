"""Assistant configuration: validation, 0600 persistence, versioning, quarantine."""
from __future__ import annotations

import json
import os
import stat

import pytest
from pydantic import ValidationError

from vivarium_workbench_assistant.config import (
    AssistantConfig,
    ConfigStore,
    CredentialRef,
    ProviderInstance,
)


def inst(**kw):
    base = {"id": "anthropic", "type": "anthropic", "display_name": "Anthropic",
            "credential": {"source": "keyring"}}
    base.update(kw)
    return ProviderInstance.model_validate(base)


def test_valid_instances():
    inst()
    inst(id="ollama", type="openai_compatible", preset="ollama", credential={"source": "none"},
         display_name="Ollama")
    inst(id="vertex", type="vertex", project="my-project-1", location="us-central1", credential={"source": "adc"})


def test_preset_fills_base_url():
    i = inst(id="ollama", type="openai_compatible", preset="ollama", credential={"source": "none"})
    assert i.base_url == "http://127.0.0.1:11434/v1"
    with pytest.raises(ValidationError):
        inst(id="custom", type="openai_compatible", preset="custom", credential={"source": "none"})


@pytest.mark.parametrize("bad", [
    {"id": "Bad Id"},
    {"id": "9starts-with-digit"},
    {"credential": {"source": "adc"}},                  # anthropic cannot use ADC
    {"credential": {"source": "env"}},                  # env needs a name
    {"type": "vertex", "credential": {"source": "adc"}},  # vertex needs project/location
    {"type": "vertex", "project": "p", "location": "us-central1", "credential": {"source": "adc"}},
    {"type": "vertex", "project": "my-project", "location": "../../x", "credential": {"source": "adc"}},
    {"default_model": "bad model id with spaces"},
    {"unknown_field": 1},
])
def test_invalid_instances(bad):
    with pytest.raises(ValidationError):
        inst(**bad)


def test_attribution_headers_only_for_openrouter():
    assert inst(send_attribution_headers=True).send_attribution_headers is False
    o = inst(id="or", type="openrouter", send_attribution_headers=True)
    assert o.send_attribution_headers is True


def test_duplicate_ids_rejected():
    with pytest.raises(ValidationError):
        AssistantConfig(instances=[inst(), inst()])


def test_save_is_owner_only_and_has_no_secrets(tmp_path):
    store = ConfigStore(tmp_path / "assistant" / "config.json")
    cfg = AssistantConfig(instances=[inst(credential=CredentialRef(source="env", env_var="ANTHROPIC_API_KEY"))])
    store.save(cfg)
    path = tmp_path / "assistant" / "config.json"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert data["instances"][0]["credential"] == {"source": "env", "env_var": "ANTHROPIC_API_KEY",
                                                  "key_file_path": None}
    assert store.load().instances[0].id == "anthropic"


def test_invalid_file_is_quarantined(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    store = ConfigStore(path)
    assert store.load().instances == []
    assert not path.exists()
    assert list(tmp_path.glob("config.json.invalid-*"))


def test_newer_version_is_read_only(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 99, "instances": [], "preferences": {}, "future": True}))
    store = ConfigStore(path)
    store.load()
    assert store.read_only_reason
    with pytest.raises(PermissionError):
        store.save(AssistantConfig())
    assert json.loads(path.read_text())["version"] == 99       # never clobbered


def test_default_instance_reset_when_missing():
    cfg = AssistantConfig.model_validate({"instances": [], "preferences": {"default_instance": "gone"}})
    assert cfg.preferences.default_instance is None
