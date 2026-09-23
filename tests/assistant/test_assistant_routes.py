"""The assistant HTTP API through the real FastAPI stack (TestClient + fake provider)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from .fake_llm import Scripted, add_local_provider, openai_text_events, openai_tool_call_events, read_sse

BASE = "/api/ext/assistant"


def new_conv(client) -> str:
    r = client.post(f"{BASE}/conversations", json={})
    assert r.status_code == 201
    return r.json()["id"]


def run(client, cid, **body):
    body.setdefault("provider_instance", "local")
    body.setdefault("model", "fake-model")
    r = client.post(f"{BASE}/conversations/{cid}/runs", json=body)
    return r


# -- basics ----------------------------------------------------------------------

def test_status_and_ui_contribution(assistant_client):
    st = assistant_client.get(f"{BASE}/status").json()
    assert st["available"] is True and st["mode"] == "local"
    assert st["keyring_available"] is False            # tests never use the real keychain
    ext = assistant_client.get("/api/ui-config").json()["extensions"]
    assert ext and ext[0]["id"] == "assistant" and ext[0]["panel"] is True
    assert assistant_client.get("/ext/assistant/assets/assistant.js").status_code == 200
    assert assistant_client.get("/ext/assistant/assets/%2e%2e/routes.py").status_code in (403, 404)


def test_mutations_require_origin(assistant_client, fake_llm):
    bare = TestClient(assistant_client.app)          # no Origin header
    r = bare.post(f"{BASE}/providers", json={"type": "anthropic"})
    assert r.status_code == 403 and r.json()["code"] == "origin_required"
    evil = bare.post(f"{BASE}/providers", json={"type": "anthropic"}, headers={"Origin": "http://evil.example"})
    assert evil.status_code == 403
    assert bare.get(f"{BASE}/providers").status_code == 200          # reads are fine


def test_no_cors_and_no_store(assistant_client):
    r = assistant_client.get(f"{BASE}/providers", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
    assert r.headers["cache-control"] == "no-store"


def test_provider_crud_and_credential_is_write_only(assistant_client, fake_llm):
    add_local_provider(assistant_client, fake_llm)
    key = "sk-proj-WRITEONLY0123456789abcdefghij"
    r = assistant_client.post(f"{BASE}/providers/local/credential", json={"api_key": key})
    assert r.status_code == 200 and key not in r.text
    assert r.json()["configured"] is True and r.json()["hint"].endswith("ghij")
    listing = assistant_client.get(f"{BASE}/providers").text
    assert key not in listing
    # Malformed credential bodies are refused without echoing the value.
    bad = assistant_client.post(f"{BASE}/providers/local/credential", json={"api_key": key, "extra": 1})
    assert bad.status_code == 400 and key not in bad.text
    assert assistant_client.delete(f"{BASE}/providers/local/credential").json()["configured"] is False
    assert assistant_client.delete(f"{BASE}/providers/local").status_code == 200
    assert assistant_client.get(f"{BASE}/providers").json()["providers"] == []


def test_invalid_provider_config_is_rejected(assistant_client):
    for body in ({"type": "vertex", "project": "p", "location": "x"},
                 {"type": "anthropic", "credential": {"source": "env", "env_var": "GITHUB_TOKEN"}},
                 {"type": "openai_compatible", "preset": "custom"},
                 {"type": "anthropic", "base_url": "https://user:pw@example.com"}):
        r = assistant_client.post(f"{BASE}/providers", json=body)
        if r.status_code == 201:
            # base_url credential-bearing URLs are accepted in config but refused by the outbound policy
            iid = r.json()["provider"]["id"]
            t = assistant_client.post(f"{BASE}/providers/{iid}/test", json={}).json()
            assert t["ok"] is False and t["error"]["kind"] == "blocked_by_policy"
        else:
            assert r.status_code in (400, 422), body


def test_models_discovery_and_capabilities_override(assistant_client, fake_llm):
    add_local_provider(assistant_client, fake_llm)
    models = assistant_client.get(f"{BASE}/providers/local/models?refresh=1").json()["models"]
    assert {m["id"] for m in models} == {"fake-model", "fake-tools"}
    assert next(m for m in models if m["id"] == "fake-tools")["caps"] is None
    r = assistant_client.post(f"{BASE}/providers/local/models/capabilities",
                              json={"model": "fake-tools", "tools": True, "context_window": 32768})
    assert "tools" in r.json()["model"]["caps"] and r.json()["model"]["context_window"] == 32768


def test_test_connection(assistant_client, fake_llm):
    add_local_provider(assistant_client, fake_llm)
    r = assistant_client.post(f"{BASE}/providers/local/test", json={}).json()
    assert r["ok"] is True and r["models_count"] == 2


# -- runs -------------------------------------------------------------------------

def test_streamed_run_events_and_persistence(assistant_client, fake_llm, workspace):
    add_local_provider(assistant_client, fake_llm)
    cid = new_conv(assistant_client)
    fake_llm.queue(Scripted(events=openai_text_events("Hello", " **world**")))
    r = run(assistant_client, cid, message="Why is growth blocked?",
            context=[{"kind": "page_summary", "page": "investigations"}, {"kind": "study", "slug": "growth"}])
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-store"
    events = read_sse(r.text)
    names = [n for n, _ in events]
    assert names[0] == "run.start" and names[-1] == "run.end"
    start = events[0][1]
    assert start["v"] == 1 and start["locality"] == "local"
    assert [i["kind"] for i in start["context_manifest"]] == ["page_summary", "study"]
    assert "".join(d["text"] for n, d in events if n == "text.delta") == "Hello **world**"
    assert ("usage", {"input_tokens": 12, "output_tokens": 5}) in events
    # The provider saw the untrusted-framed context and the system prompt.
    sent = fake_llm.posts()[-1]["json"]
    assert sent["messages"][0]["role"] == "system" and "DATA" in sent["messages"][0]["content"]
    assert 'untrusted="true"' in sent["messages"][-1]["content"]
    conv = assistant_client.get(f"{BASE}/conversations/{cid}").json()
    user, asst = conv["messages"]
    assert user["context_manifest"][1]["path"] == "studies/growth/study.yaml"
    assert "content" not in json.dumps(user["context_manifest"])          # manifest, not a copy
    assert asst["status"] == "complete" and asst["parts"][0]["text"] == "Hello **world**"
    assert conv["title"] == "Why is growth blocked?"


def test_regenerate_creates_a_sibling(assistant_client, fake_llm):
    add_local_provider(assistant_client, fake_llm)
    cid = new_conv(assistant_client)
    fake_llm.queue(Scripted(events=openai_text_events("first")), Scripted(events=openai_text_events("second")))
    run(assistant_client, cid, message="Q")
    user_id = assistant_client.get(f"{BASE}/conversations/{cid}").json()["messages"][0]["id"]
    r = run(assistant_client, cid, action="regenerate", parent_id=user_id)
    assert r.status_code == 200
    msgs = assistant_client.get(f"{BASE}/conversations/{cid}").json()["messages"]
    replies = [m for m in msgs if m["role"] == "assistant"]
    assert [m["parts"][0]["text"] for m in replies] == ["first", "second"]
    assert all(m["parent_id"] == user_id for m in replies)


def test_retry_after_error_and_pre_first_token_retry(assistant_client, fake_llm):
    add_local_provider(assistant_client, fake_llm)
    cid = new_conv(assistant_client)
    # A 503 before any token is retried once automatically.
    fake_llm.queue(Scripted(status=503, json_body={"error": {"message": "busy"}}, headers={"retry-after": "0"}),
                   Scripted(events=openai_text_events("recovered")))
    events = read_sse(run(assistant_client, cid, message="hi").text)
    assert any(n == "notice" for n, _ in events)
    assert "".join(d["text"] for n, d in events if n == "text.delta") == "recovered"
    # A non-retryable error is surfaced and persisted.
    fake_llm.queue(Scripted(status=401, json_body={"error": {"message": "bad key"}}))
    events = read_sse(run(assistant_client, cid, message="again").text)
    err = next(d for n, d in events if n == "error")
    assert err["kind"] == "auth" and err["retryable"] is False
    last = assistant_client.get(f"{BASE}/conversations/{cid}").json()["messages"][-1]
    assert last["status"] == "error"


def test_one_active_run_per_conversation_and_bad_refs(assistant_client, fake_llm):
    add_local_provider(assistant_client, fake_llm)
    cid = new_conv(assistant_client)
    assert run(assistant_client, cid, message="x", provider_instance="nope").status_code == 404
    assert run(assistant_client, "c_" + "0" * 16, message="x").status_code == 404
    assert run(assistant_client, cid, action="retry", parent_id="m_" + "0" * 12).status_code == 404
    assert run(assistant_client, cid, message="   ").status_code == 400


def _first_event(lines_iter, wanted):
    """Read SSE lines until an event named ``wanted`` arrives; return its data."""
    name = None
    for line in lines_iter:
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: ") and name == wanted:
            return json.loads(line[6:])
    raise AssertionError(f"no {wanted} event")


def _wait(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_cancel_endpoint_stops_the_provider_stream(live_assistant, fake_llm):
    c = live_assistant
    add_local_provider(c, fake_llm)
    cid = c.post(f"{BASE}/conversations", json={}).json()["id"]
    fake_llm.queue(Scripted(events=openai_text_events(*[f"t{i} " for i in range(200)]), delay_s=0.05))
    with c.stream("POST", f"{BASE}/conversations/{cid}/runs",
                  json={"message": "long", "provider_instance": "local", "model": "fake-model"}) as r:
        lines = r.iter_lines()
        rid = _first_event(lines, "run.start")["run_id"]
        _first_event(lines, "text.delta")
        assert c.post(f"{BASE}/runs/{rid}/cancel", json={}).json()["cancelled"] is True
        end = _first_event(lines, "run.end")
    assert end == {"stop_reason": "cancelled"}
    assert _wait(lambda: fake_llm.disconnects >= 1), "the provider stream was not closed"
    assert fake_llm.completed_streams == 0
    msg = c.get(f"{BASE}/conversations/{cid}").json()["messages"][-1]
    assert msg["status"] == "cancelled" and msg["parts"][0]["text"].startswith("t0 ")
    # Cancel is idempotent.
    assert c.post(f"{BASE}/runs/{rid}/cancel", json={}).json()["cancelled"] is False


def test_client_disconnect_cancels_upstream(live_assistant, fake_llm):
    """Spike S1: a browser disconnect propagates through every middleware layer
    to the producer, which closes the provider stream and keeps the partial text."""
    c = live_assistant
    add_local_provider(c, fake_llm)
    cid = c.post(f"{BASE}/conversations", json={}).json()["id"]
    fake_llm.queue(Scripted(events=openai_text_events(*[f"w{i} " for i in range(300)]), delay_s=0.05))
    with c.stream("POST", f"{BASE}/conversations/{cid}/runs",
                  json={"message": "long", "provider_instance": "local", "model": "fake-model"}) as r:
        lines = r.iter_lines()
        _first_event(lines, "run.start")
        _first_event(lines, "text.delta")
        # Leaving the block closes the connection mid-stream.
    assert _wait(lambda: fake_llm.disconnects >= 1, timeout=15), "disconnect did not reach the provider"
    assert fake_llm.completed_streams == 0

    def interrupted():
        msgs = c.get(f"{BASE}/conversations/{cid}").json()["messages"]
        return len(msgs) == 2 and msgs[-1]["status"] == "interrupted"
    assert _wait(interrupted, timeout=10)


def test_heartbeat_keeps_quiet_streams_alive(live_assistant, fake_llm, monkeypatch):
    from vivarium_workbench_assistant import streaming
    monkeypatch.setattr(streaming, "HEARTBEAT_S", 0.2)
    c = live_assistant
    add_local_provider(c, fake_llm)
    cid = c.post(f"{BASE}/conversations", json={}).json()["id"]
    events = openai_text_events("slow")
    fake_llm.queue(Scripted(events=[events[0]] + events[1:], delay_s=0.6))
    with c.stream("POST", f"{BASE}/conversations/{cid}/runs",
                  json={"message": "x", "provider_instance": "local", "model": "fake-model"}) as r:
        body = "".join(r.iter_text())
    assert ": ka" in body and "run.end" in body


def test_cancel_of_foreign_or_unknown_run_is_404(assistant_client):
    assert assistant_client.post(f"{BASE}/runs/r_{'0' * 16}/cancel", json={}).status_code == 404
    assert assistant_client.post(f"{BASE}/runs/not-a-run/cancel", json={}).status_code == 404


# -- agent mode: tools, approvals, proposals ----------------------------------------------

def enable_tools(client):
    client.post(f"{BASE}/providers/local/models/capabilities", json={"model": "fake-tools", "tools": True})


def test_agent_mode_requires_tool_capable_model(assistant_client, fake_llm):
    add_local_provider(assistant_client, fake_llm)
    cid = new_conv(assistant_client)
    r = run(assistant_client, cid, message="x", model="fake-tools", agent=True)
    assert r.status_code == 400 and "tool support is unknown" in r.json()["error"]


def test_agent_reads_and_proposes_then_user_applies(assistant_client, fake_llm, workspace):
    import hashlib
    add_local_provider(assistant_client, fake_llm)
    enable_tools(assistant_client)
    before = (workspace / "notes" / "readme.md").read_text()
    fake_llm.queue(
        Scripted(events=openai_tool_call_events("call_1", "read_file", {"path": "notes/readme.md"}, text="Let me look.")),
        Scripted(events=openai_tool_call_events("call_2", "propose_edit", {
            "path": "notes/readme.md", "base_sha256": hashlib.sha256(before.encode()).hexdigest(),
            "new_content": "# Notes\nhello assistant\n", "rationale": "update greeting"})),
        Scripted(events=openai_text_events("I proposed a change.")),
    )
    cid = new_conv(assistant_client)
    events = read_sse(run(assistant_client, cid, message="Update the greeting", model="fake-tools", agent=True).text)
    names = [n for n, _ in events]
    assert names.count("tool.call") == 2 and names.count("tool.result") == 2 and "proposal" in names
    calls = [d for n, d in events if n == "tool.call"]
    assert [c["requires_approval"] for c in calls] == [False, False]
    proposal = next(d for n, d in events if n == "proposal")
    # The tool result went back to the model as untrusted data.
    second = fake_llm.posts()[1]["json"]["messages"]
    tool_msg = next(m for m in second if m["role"] == "tool")
    assert 'untrusted="true"' in tool_msg["content"] and "hello world" in tool_msg["content"]
    # Nothing written until the user applies.
    assert (workspace / "notes" / "readme.md").read_text() == before
    pid = proposal["proposal_id"]
    p = assistant_client.get(f"{BASE}/proposals/{pid}").json()
    assert "undo" not in p and p["files"][0]["additions"] == 1
    r = assistant_client.post(f"{BASE}/proposals/{pid}/apply", json={"commit": True}).json()
    assert r["results"][0]["status"] == "applied" and r["commit"]["committed"] is True
    assert (workspace / "notes" / "readme.md").read_text() == "# Notes\nhello assistant\n"
    r = assistant_client.post(f"{BASE}/proposals/{pid}/revert", json={}).json()
    assert r["reverted"] is True
    assert (workspace / "notes" / "readme.md").read_text() == before
    audit = (Path(assistant_client.get(f"{BASE}/status").json()["storage"]["data"]) / "audit.jsonl").read_text()
    assert '"event":"tool_call"' in audit and '"event":"edit_applied"' in audit and '"event":"commit"' in audit


def test_execute_tool_waits_for_approval_bound_to_args(live_assistant, fake_llm, workspace):
    c = live_assistant
    add_local_provider(c, fake_llm)
    enable_tools(c)
    (workspace / "scripts").mkdir()
    (workspace / "scripts" / "lint-workspace.py").write_text("print('LINT-RAN')\n")
    fake_llm.queue(Scripted(events=openai_tool_call_events("call_1", "run_workspace_lint", {})),
                   Scripted(events=openai_text_events("done")))
    cid = c.post(f"{BASE}/conversations", json={}).json()["id"]
    with c.stream("POST", f"{BASE}/conversations/{cid}/runs",
                  json={"message": "lint", "provider_instance": "local", "model": "fake-tools",
                        "agent": True}) as r:
        lines = r.iter_lines()
        rid = _first_event(lines, "run.start")["run_id"]
        a = _first_event(lines, "tool.call")
        assert a["requires_approval"] and a["category"] == "execute" and a["grantable"] is True
        url = f"{BASE}/runs/{rid}/approvals/{a['approval_id']}"
        # The approval is bound to the exact arguments and to a browser request.
        assert c.post(url, json={"decision": "approve", "args_hash": "0" * 64}).status_code == 409
        import httpx
        assert httpx.post(str(c.base_url) + url, json={"decision": "approve",
                                                       "args_hash": a["args_hash"]}).status_code == 403
        ok = c.post(url, json={"decision": "approve", "scope": "once", "args_hash": a["args_hash"]})
        assert ok.status_code == 200
        result = _first_event(lines, "tool.result")
        assert result["ok"] is True
        _first_event(lines, "run.end")
    assert c.post(url, json={"decision": "approve", "args_hash": a["args_hash"]}).status_code == 404
    tool_msg = next(m for m in fake_llm.posts()[1]["json"]["messages"] if m["role"] == "tool")
    assert "LINT-RAN" in tool_msg["content"]


def test_denied_approval_is_reported(live_assistant, fake_llm, workspace):
    c = live_assistant
    add_local_provider(c, fake_llm)
    enable_tools(c)
    (workspace / "scripts").mkdir()
    (workspace / "scripts" / "lint-workspace.py").write_text("print('SHOULD-NOT-RUN')\n")
    fake_llm.queue(Scripted(events=openai_tool_call_events("call_1", "run_workspace_lint", {})),
                   Scripted(events=openai_text_events("ok")))
    cid = c.post(f"{BASE}/conversations", json={}).json()["id"]
    with c.stream("POST", f"{BASE}/conversations/{cid}/runs",
                  json={"message": "lint", "provider_instance": "local", "model": "fake-tools",
                        "agent": True}) as r:
        lines = r.iter_lines()
        rid = _first_event(lines, "run.start")["run_id"]
        a = _first_event(lines, "tool.call")
        c.post(f"{BASE}/runs/{rid}/approvals/{a['approval_id']}",
               json={"decision": "deny", "args_hash": a["args_hash"]})
        assert _first_event(lines, "tool.result")["ok"] is False
        _first_event(lines, "run.end")
    tool_msg = next(m for m in fake_llm.posts()[1]["json"]["messages"] if m["role"] == "tool")
    assert "declined" in tool_msg["content"] and "SHOULD-NOT-RUN" not in tool_msg["content"]


def test_denied_tool_is_reported_to_the_model(assistant_client, fake_llm):
    add_local_provider(assistant_client, fake_llm)
    enable_tools(assistant_client)
    fake_llm.queue(Scripted(events=openai_tool_call_events("c1", "shell", {"argv": ["whoami"]})),
                   Scripted(events=openai_text_events("ok")))
    cid = new_conv(assistant_client)
    events = read_sse(run(assistant_client, cid, message="x", model="fake-tools", agent=True).text)
    # shell is disabled by default, so it is not even offered; the call is refused.
    res = next(d for n, d in events if n == "tool.result")
    assert res["ok"] is False


def test_loop_detection_stops_repeated_calls(assistant_client, fake_llm):
    add_local_provider(assistant_client, fake_llm)
    enable_tools(assistant_client)
    same = Scripted(events=openai_tool_call_events("c", "list_dir", {"path": "notes"}))
    fake_llm.queue(*[same] * 6, Scripted(events=openai_text_events("never")))
    cid = new_conv(assistant_client)
    events = read_sse(run(assistant_client, cid, message="x", model="fake-tools", agent=True).text)
    assert any(n == "notice" and "loop detected" in d["message"] for n, d in events)
    assert len(fake_llm.posts()) == 4


# -- context preview, files, preferences, suggest ---------------------------------------------

def test_context_preview_calls_no_provider(assistant_client, fake_llm):
    add_local_provider(assistant_client, fake_llm)
    n_before = len(fake_llm.requests)
    r = assistant_client.post(f"{BASE}/context/preview", json={
        "context": [{"kind": "file", "path": "notes/readme.md"}, {"kind": "file", "path": ".env"}],
        "provider_instance": "local", "model": "fake-model",
        "message": "my key sk-ant-api03-abcdefghijklmnopqrstuvwxyz"}).json()
    assert len(fake_llm.requests) == n_before
    assert r["items"][0]["path"] == "notes/readme.md" and r["items"][1]["error"]
    assert r["locality"] == "local" and r["message_secret_findings"]


def test_file_viewer_and_search(assistant_client):
    r = assistant_client.get(f"{BASE}/files", params={"path": "notes/readme.md"}).json()
    assert r["text"].startswith("# Notes") and r["gitignored"] is False
    assert assistant_client.get(f"{BASE}/files", params={"path": ".env"}).status_code == 404
    assert assistant_client.get(f"{BASE}/files", params={"path": "../../etc/passwd"}).status_code == 404
    assert any(h["path"] == "notes/readme.md"
               for h in assistant_client.get(f"{BASE}/search", params={"q": "hello"}).json()["results"])


def test_preferences_guard_the_shell(assistant_client):
    r = assistant_client.patch(f"{BASE}/preferences", json={"tools": {"read": "auto", "execute": "ask",
                                                                      "shell": "auto"}})
    assert r.status_code == 400
    r = assistant_client.patch(f"{BASE}/preferences", json={"persist_conversations": False, "retention_days": 7})
    assert r.json()["persist_conversations"] is False and r.json()["retention_days"] == 7


def test_suggest_in_app(assistant_client, fake_llm):
    add_local_provider(assistant_client, fake_llm)
    fake_llm.queue(Scripted(events=openai_text_events('{"suggestion": "Add growth study", "rationale": "because"}')))
    r = assistant_client.post(f"{BASE}/suggest", json={"kind": "pr-title", "provider_instance": "local",
                                                        "model": "fake-model"})
    assert r.json() == {"suggestion": "Add growth study", "rationale": "because"}


def test_delete_all_requires_workspace_name(assistant_client, fake_llm, workspace):
    new_conv(assistant_client)
    assert assistant_client.post(f"{BASE}/conversations/delete-all", json={"confirm": "wrong"}).status_code == 400
    r = assistant_client.post(f"{BASE}/conversations/delete-all", json={"confirm": workspace.name})
    assert r.json()["deleted"] == 1


# -- hosted mode ----------------------------------------------------------------------------------

def _hosted_client(workspace, monkeypatch, tmp_path, block):
    from vivarium_workbench.api.app import create_app, get_workspace
    deploy = tmp_path / "deploy.yaml"
    deploy.write_text(yaml.safe_dump({"assistant": block}))
    monkeypatch.setenv("VIVARIUM_WORKBENCH_DEPLOY_CONFIG", str(deploy))
    monkeypatch.setenv("VIVARIUM_WORKBENCH_EXTENSIONS", "assistant")
    monkeypatch.delenv("VIVARIUM_WORKBENCH_ASSISTANT_MODE", raising=False)
    app = create_app()
    app.dependency_overrides[get_workspace] = lambda: workspace
    return app


def test_hosted_without_opt_in_is_disabled_everywhere(workspace, monkeypatch, tmp_path):
    app = _hosted_client(workspace, monkeypatch, tmp_path, {})
    c = TestClient(app, headers={"Origin": "http://testserver"})
    st = c.get(f"{BASE}/status").json()
    assert st["available"] is False and "shared deployments" in st["reason"]
    assert c.get(f"{BASE}/providers").status_code == 403
    assert c.post(f"{BASE}/conversations", json={}).status_code == 403
    assert c.get("/api/ui-config").json()["extensions"] == []        # no panel, no scripts


def test_hosted_opt_in_is_operator_managed_and_session_scoped(workspace, monkeypatch, tmp_path, fake_llm):
    block = {"enabled": True, "providers_allowed": ["openai_compatible"],
             "base_url_allowlist": [fake_llm.base + "/v1"],
             "instances": [{"id": "gateway", "type": "openai_compatible", "display_name": "Gateway",
                            "base_url": fake_llm.base + "/v1", "credential": {"source": "none"},
                            "default_model": "fake-model"}]}
    app = _hosted_client(workspace, monkeypatch, tmp_path, block)
    a = TestClient(app, headers={"Origin": "http://testserver", "X-VW-Session": "session-a"})
    b = TestClient(app, headers={"Origin": "http://testserver", "X-VW-Session": "session-b"})
    assert [p["id"] for p in a.get(f"{BASE}/providers").json()["providers"]] == ["gateway"]
    assert a.post(f"{BASE}/providers", json={"type": "anthropic"}).status_code == 403
    assert a.post(f"{BASE}/providers/gateway/credential", json={"api_key": "x" * 20}).status_code == 403
    cid = a.post(f"{BASE}/conversations", json={}).json()["id"]
    fake_llm.queue(Scripted(events=openai_text_events("hosted reply")))
    r = a.post(f"{BASE}/conversations/{cid}/runs", json={"message": "hi", "provider_instance": "gateway",
                                                         "model": "fake-model"})
    assert "hosted reply" in "".join(d.get("text", "") for n, d in read_sse(r.text) if n == "text.delta")
    # Session B sees nothing of session A.
    assert b.get(f"{BASE}/conversations").json()["conversations"] == []
    assert b.get(f"{BASE}/conversations/{cid}").status_code == 404
    assert a.get(f"{BASE}/conversations").json()["conversations"][0]["id"] == cid
    # Nothing persisted to disk in hosted mode by default.
    data_dir = Path(tmp_path / "_user_data")
    assert not list(data_dir.rglob("*.jsonl")) or all("audit" in p.name for p in data_dir.rglob("*.jsonl"))
    # Local-only surfaces are refused.
    assert a.get(f"{BASE}/audit").status_code == 403
    enabled_tools = a.patch(f"{BASE}/preferences", json={"tools": {"read": "auto", "execute": "ask",
                                                                   "shell": "ask"}})
    assert enabled_tools.status_code == 403


def test_hosted_requires_a_session(workspace, monkeypatch, tmp_path, fake_llm):
    block = {"enabled": True, "providers_allowed": ["anthropic"], "credential_env": {"anthropic": "ANTHROPIC_API_KEY"}}
    app = _hosted_client(workspace, monkeypatch, tmp_path, block)
    c = TestClient(app, headers={"Origin": "http://testserver"})
    # The core session middleware mints a session for browser requests; the
    # operator-defined instance comes from credential_env, never user config.
    providers = c.get(f"{BASE}/providers").json()["providers"]
    assert providers[0]["id"] == "anthropic" and providers[0]["credential"] == {"source": "env"}


# -- secret leak scan -----------------------------------------------------------------------------

def test_secret_never_leaves_the_server(assistant_client, fake_llm, workspace, tmp_path, caplog):
    import logging
    key = "sk-proj-LEAKSCAN-9f8e7d6c5b4a3210zyxwvutsrqponmlk"
    add_local_provider(assistant_client, fake_llm)
    responses = []
    with caplog.at_level(logging.DEBUG):
        responses.append(assistant_client.post(f"{BASE}/providers/local/credential", json={"api_key": key}))
        responses.append(assistant_client.get(f"{BASE}/providers"))
        responses.append(assistant_client.get(f"{BASE}/status"))
        responses.append(assistant_client.post(f"{BASE}/providers/local/test", json={}))
        responses.append(assistant_client.get(f"{BASE}/providers/local/models?refresh=1"))
        cid = new_conv(assistant_client)
        fake_llm.queue(Scripted(status=401, json_body={"error": {"message": f"Invalid key {key}"}}))
        responses.append(run(assistant_client, cid, message="hi"))
        fake_llm.queue(Scripted(events=openai_text_events(f"echo {key} back")))
        responses.append(run(assistant_client, cid, message="please"))
        responses.append(assistant_client.get(f"{BASE}/conversations/{cid}"))
        responses.append(assistant_client.get(f"{BASE}/audit"))
    # The key did reach the provider (in the Authorization header) …
    assert fake_llm.posts()[-1]["headers"]["authorization"] == f"Bearer {key}"
    # … but no response, log line or stored file contains it.
    for r in responses:
        assert key not in r.text, r.request.url
    assert key not in caplog.text
    for path in (tmp_path / "_user_data").rglob("*"):
        if path.is_file():
            assert key not in path.read_text(errors="ignore"), path
    for path in (tmp_path / "_user_config").rglob("*"):
        if path.is_file():
            assert key not in path.read_text(errors="ignore"), path
    for path in workspace.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            assert key not in path.read_text(errors="ignore"), path
