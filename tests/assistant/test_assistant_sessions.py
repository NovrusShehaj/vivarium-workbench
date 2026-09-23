"""Two browser sessions bound to DIFFERENT workspaces stream at the same time
(plan §17.2, §6.3 rule 2): each run's context and each conversation's storage
use the run's own workspace root, captured when the run starts. Also covers the
concurrent-run cap. Real app under uvicorn, real sockets, fake provider.
"""
from __future__ import annotations

import shutil
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

from .fake_llm import Scripted, add_local_provider, openai_text_events

BASE = "/api/ext/assistant"


@pytest.fixture
def two_workspaces(workspace, tmp_path, git) -> tuple[Path, Path]:
    ws_b = tmp_path / "ws_b"
    shutil.copytree(workspace, ws_b)
    (ws_b / "workspace.yaml").write_text("name: ws_b\ndescription: The second workspace\n")
    shutil.rmtree(ws_b / "studies" / "growth")
    (ws_b / "studies" / "decay").mkdir(parents=True)
    (ws_b / "studies" / "decay" / "study.yaml").write_text(
        "schema_version: 3\nname: decay\nobjective: Decay things (workspace B only)\nstatus: draft\n"
        "baseline: []\nvariants: []\nruns: []\n")
    git(ws_b, "add", "-A")
    git(ws_b, "commit", "-q", "-m", "workspace b")
    return workspace, ws_b


@pytest.fixture
def live_unbound(monkeypatch):
    """The real app under uvicorn with NO workspace override: every request is
    routed through its session's binding (X-VW-Session)."""
    import uvicorn

    from vivarium_workbench.api.app import create_app
    from vivarium_workbench.lib import server_runtime, session_registry
    monkeypatch.setenv("VIVARIUM_WORKBENCH_EXTENSIONS", "assistant")
    server_runtime.configure(bind_host="127.0.0.1")
    app = create_app()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    yield base
    server.should_exit = True
    thread.join(timeout=10)
    session_registry.clear()


def _client(base: str, session: str) -> httpx.Client:
    return httpx.Client(base_url=base, headers={"Origin": base, "X-VW-Session": session}, timeout=30)


def _wait(pred, timeout=10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_two_sessions_two_workspaces_stream_concurrently(two_workspaces, live_unbound, fake_llm):
    from vivarium_workbench.lib import session_registry
    ws_a, ws_b = two_workspaces
    key_a, key_b = session_registry.mint_key(), session_registry.mint_key()
    session_registry.rebind(key_a, ws_a)
    session_registry.rebind(key_b, ws_b)
    a, b = _client(live_unbound, key_a), _client(live_unbound, key_b)
    add_local_provider(a, fake_llm)            # providers are per user, not per workspace
    cid_a = a.post(f"{BASE}/conversations", json={}).json()["id"]
    cid_b = b.post(f"{BASE}/conversations", json={}).json()["id"]
    slow = [f"t{i} " for i in range(12)]
    fake_llm.queue(Scripted(events=openai_text_events(*slow), delay_s=0.15),
                   Scripted(events=openai_text_events(*slow), delay_s=0.15))

    results: dict[str, str] = {}

    def stream(client: httpx.Client, cid: str, slug: str, tag: str) -> None:
        with client.stream("POST", f"{BASE}/conversations/{cid}/runs",
                           json={"message": f"Explain {slug}.", "provider_instance": "local", "model": "fake-model",
                                 "context": [{"kind": "study", "slug": slug}]}) as r:
            results[tag] = r.read().decode()

    ta = threading.Thread(target=stream, args=(a, cid_a, "growth", "a"))
    tb = threading.Thread(target=stream, args=(b, cid_b, "decay", "b"))
    ta.start()
    tb.start()
    # Both provider streams are open at the same time.
    assert _wait(lambda: len(fake_llm.posts()) == 2, timeout=10)
    assert fake_llm.completed_streams == 0, "the runs did not overlap"
    # A third concurrent run (same local user) is refused by the run cap.
    c3 = a.post(f"{BASE}/conversations", json={}).json()["id"]
    r3 = a.post(f"{BASE}/conversations/{c3}/runs", json={"message": "x", "provider_instance": "local",
                                                        "model": "fake-model"})
    assert r3.status_code == 429
    ta.join(timeout=30)
    tb.join(timeout=30)
    assert "run.end" in results["a"] and "run.end" in results["b"]

    # Each provider request carried ITS workspace's study, never the other's.
    bodies = [str(p["json"]) for p in fake_llm.posts()]
    growth = [x for x in bodies if "Grow things" in x]
    decay = [x for x in bodies if "Decay things (workspace B only)" in x]
    assert len(growth) == 1 and len(decay) == 1
    assert "Decay things" not in growth[0] and "Grow things" not in decay[0]

    # Conversations are stored per workspace: each session sees only its own.
    ids_a = {c["id"] for c in a.get(f"{BASE}/conversations").json()["conversations"]}
    ids_b = {c["id"] for c in b.get(f"{BASE}/conversations").json()["conversations"]}
    assert cid_a in ids_a and cid_a not in ids_b
    assert cid_b in ids_b and cid_b not in ids_a
    assert b.get(f"{BASE}/conversations/{cid_a}").status_code == 404
    msgs = a.get(f"{BASE}/conversations/{cid_a}").json()["messages"]
    assert msgs[0]["context_manifest"][0]["path"] == "studies/growth/study.yaml"
    a.close()
    b.close()
