"""Conversation store, audit log and model registry."""
from __future__ import annotations

import json
import os
import stat
import time

import pytest

from vivarium_workbench_assistant.audit import AuditLog, session_hash
from vivarium_workbench_assistant.config import ProviderInstance
from vivarium_workbench_assistant.conversations import (
    ConversationNotFound,
    ConversationStore,
    branch_to,
    latest_leaf,
    scope_key,
)
from vivarium_workbench_assistant.models_registry import ModelRegistry
from vivarium_workbench_assistant.providers.base import Cap, ModelInfo
from vivarium_workbench_assistant.secrets import register_known_secret

KEY = "sk-ant-api03-conversation-secret-0123456789"


@pytest.fixture
def store(tmp_path):
    return ConversationStore(root=lambda: tmp_path / "assistant")


def test_create_append_get_list_rename_delete(store, workspace, tmp_path):
    c = store.create(workspace, scope="local", persist=True)
    cid = c["id"]
    m = store.append_message(workspace, cid, {"role": "user", "parent_id": None,
                                              "parts": [{"type": "text", "text": "Why is growth blocked?"}]},
                             scope="local", persist=True)
    store.append_message(workspace, cid, {"role": "assistant", "parent_id": m["id"],
                                          "parts": [{"type": "text", "text": "Because."}], "status": "complete"},
                         scope="local", persist=True)
    conv = store.get(workspace, cid, scope="local", persist=True)
    assert conv["title"] == "Why is growth blocked?"          # auto-titled
    assert [x["role"] for x in conv["messages"]] == ["user", "assistant"]
    store.rename(workspace, cid, "Renamed", scope="local", persist=True)
    assert store.list(workspace, scope="local", persist=True)[0]["title"] == "Renamed"
    store.delete(workspace, cid, scope="local", persist=True)
    with pytest.raises(ConversationNotFound):
        store.get(workspace, cid, scope="local", persist=True)


def test_files_are_private_and_outside_the_workspace(store, workspace, tmp_path):
    c = store.create(workspace, scope="local", persist=True)
    files = list((tmp_path / "assistant").rglob("*.jsonl"))
    assert files and not any(str(f).startswith(str(workspace)) for f in files)
    if os.name == "posix":
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
        assert stat.S_IMODE(files[0].parent.stat().st_mode) == 0o700
    assert c["id"].startswith("c_")


def test_known_secrets_are_redacted_before_writing(store, workspace, tmp_path):
    register_known_secret(KEY)
    c = store.create(workspace, scope="local", persist=True)
    store.append_message(workspace, c["id"], {"role": "user", "parts": [{"type": "text", "text": f"my key {KEY}"}]},
                         scope="local", persist=True)
    raw = "".join(p.read_text() for p in (tmp_path / "assistant").rglob("*.jsonl"))
    assert KEY not in raw and "[redacted]" in raw


def test_memory_mode_writes_nothing(store, workspace, tmp_path):
    c = store.create(workspace, scope="local", persist=False)
    store.append_message(workspace, c["id"], {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
                         scope="local", persist=False)
    assert store.get(workspace, c["id"], scope="local", persist=False)["messages"]
    assert not (tmp_path / "assistant").exists()


def test_hosted_sessions_are_isolated(store, workspace):
    a = store.create(workspace, scope="session-a", persist=True)
    store.create(workspace, scope="session-b", persist=True)
    assert [c["id"] for c in store.list(workspace, scope="session-a", persist=True)] == [a["id"]]
    with pytest.raises(ConversationNotFound):
        store.get(workspace, a["id"], scope="session-b", persist=True)
    assert scope_key("session-a").startswith("s_") and "session-a" not in scope_key("session-a")


def test_retention_and_delete_all(store, workspace):
    old = store.create(workspace, scope="local", persist=True)
    store.create(workspace, scope="local", persist=True)
    # Age the first conversation.
    path = next(p for p in store._conv_dir(workspace, "local").glob("*.jsonl") if old["id"] in p.name)
    lines = path.read_text().splitlines()
    header = json.loads(lines[0])
    header["created_at"] = time.time() - 40 * 86400
    path.write_text(json.dumps(header) + "\n")
    assert len(store.list(workspace, scope="local", persist=True, retention_days=30)) == 1
    assert store.delete_all(workspace, scope="local", persist=True) == 1
    assert store.list(workspace, scope="local", persist=True) == []


def test_invalid_ids_never_touch_the_filesystem(store, workspace):
    for bad in ("../../etc/passwd", "c_zz", "c_" + "a" * 16 + "/x"):
        with pytest.raises(ConversationNotFound):
            store.get(workspace, bad, scope="local", persist=True)


def test_branching_helpers():
    msgs = [
        {"id": "m_u1", "parent_id": None, "created_at": 1},
        {"id": "m_a1", "parent_id": "m_u1", "created_at": 2},
        {"id": "m_a2", "parent_id": "m_u1", "created_at": 3},     # regenerated sibling
        {"id": "m_u2", "parent_id": "m_a2", "created_at": 4},
    ]
    assert latest_leaf(msgs) == "m_u2"
    assert [m["id"] for m in branch_to(msgs, "m_u2")] == ["m_u1", "m_a2", "m_u2"]
    assert [m["id"] for m in branch_to(msgs, "m_a1")] == ["m_u1", "m_a1"]


def test_audit_log_never_contains_secrets(tmp_path):
    register_known_secret(KEY)
    log = AuditLog(lambda: tmp_path / "audit.jsonl")
    log.record("tool_call", session_key="sess-1", conversation="c_1", run="r_1", tool="read_file",
               args_redacted={"path": "x", "api_key": "abc", "note": f"Bearer {KEY}"},
               headers={"Authorization": f"Bearer {KEY}"})
    raw = (tmp_path / "audit.jsonl").read_text()
    rec = json.loads(raw)
    assert KEY not in raw and "sess-1" not in raw
    assert rec["session"] == session_hash("sess-1") and rec["headers"] == "[redacted]"
    assert rec["args_redacted"]["api_key"] == "[redacted]"
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "audit.jsonl").stat().st_mode) == 0o600


def _inst(**kw):
    base = {"id": "local", "type": "openai_compatible", "display_name": "L", "credential": {"source": "none"},
            "base_url": "http://127.0.0.1:1/v1"}
    base.update(kw)
    return ProviderInstance.model_validate(base)


def test_model_registry_merges_discovery_manual_and_overrides(tmp_path):
    (tmp_path / "ov.json").write_text(json.dumps({"*": {"m1": {"context_window": 32768}},
                                                  "local": {"m2": {"caps": ["tools"]}}}))
    reg = ModelRegistry(cache_path=lambda: tmp_path / "cache.json", overrides_path=lambda: tmp_path / "ov.json")
    inst = _inst(manual_models=["m2"], default_model="m3")
    reg.store(inst, [ModelInfo(id="m1")])
    models = {m.id: m for m in reg.merged(inst, reg.cached(inst)[0])}
    assert set(models) == {"m1", "m2", "m3"}
    assert models["m1"].context_window == 32768
    assert Cap.TOOLS in models["m2"].caps and models["m2"].source == "manual"
    assert models["m3"].source == "configured"
    assert reg.fresh(inst)
    # A changed endpoint misses the cache; invalidation is per instance.
    assert reg.cached(_inst(base_url="http://127.0.0.1:2/v1", manual_models=[]))[0] == []
    reg.invalidate("local")
    assert reg.cached(inst)[0] == []


def test_model_allowlist_filters(tmp_path):
    reg = ModelRegistry(cache_path=lambda: tmp_path / "c.json", overrides_path=lambda: tmp_path / "o.json")
    inst = _inst()
    out = reg.merged(inst, [ModelInfo(id="a"), ModelInfo(id="b")], allowlist=("b",))
    assert [m.id for m in out] == ["b"]
