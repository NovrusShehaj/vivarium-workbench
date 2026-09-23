"""Shared fixtures for the assistant extension tests.

Isolation guarantees for every test in this directory:

* the per-user config and data directories point at a temporary directory
  (nothing is written to the developer's ``~/.config`` / ``~/.local/share``);
* the OS keychain is NEVER touched: the ``keyring`` backend is replaced by an
  in-memory one (or the "fail" backend in spawned servers);
* the server-runtime facts and extension env vars are reset.

No live provider is ever called: ``fake_llm`` is a local HTTP server.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from .fake_llm import FakeLLM


class MemoryKeyring:
    """A minimal in-memory ``keyring`` backend (priority > 0 = usable)."""

    priority = 1

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


def _as_backend(mem: MemoryKeyring):
    import keyring.backend

    class _Backend(keyring.backend.KeyringBackend):
        priority = 1

        def get_password(self, service, username):  # noqa: D401
            return mem.get_password(service, username)

        def set_password(self, service, username, password):
            mem.set_password(service, username, password)

        def delete_password(self, service, username):
            mem.delete_password(service, username)

    return _Backend()


@pytest.fixture(autouse=True)
def _assistant_isolation(tmp_path, monkeypatch):
    from vivarium_workbench.lib import server_runtime
    cfg = tmp_path / "_user_config"
    data = tmp_path / "_user_data"
    monkeypatch.setenv("VIVARIUM_WORKBENCH_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("VIVARIUM_WORKBENCH_DATA_DIR", str(data))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    for name in ("VIVARIUM_WORKBENCH_EXTENSIONS", "VIVARIUM_WORKBENCH_ASSISTANT_MODE",
                 "VIVARIUM_WORKBENCH_DEPLOY_CONFIG", "VIVARIUM_WORKBENCH_READONLY",
                 "VIVARIUM_WORKBENCH_DISABLE_CSRF", server_runtime.BIND_HOST_ENV):
        monkeypatch.delenv(name, raising=False)
    server_runtime.reset()
    import keyring
    import keyring.backends.fail
    previous = keyring.get_keyring()
    keyring.set_keyring(keyring.backends.fail.Keyring())
    yield {"config": cfg, "data": data}
    keyring.set_keyring(previous)
    server_runtime.reset()


@pytest.fixture
def memory_keyring():
    import keyring
    mem = MemoryKeyring()
    keyring.set_keyring(_as_backend(mem))
    return mem


@pytest.fixture
def fake_llm():
    srv = FakeLLM().start()
    yield srv
    srv.stop()


def _git(ws: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(ws), *args], check=True, capture_output=True, text=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"})


@pytest.fixture
def workspace(tmp_path) -> Path:
    """A small git-backed workspace with a study, an investigation and files."""
    ws = tmp_path / "ws"
    (ws / "studies" / "growth").mkdir(parents=True)
    (ws / "investigations" / "inv1").mkdir(parents=True)
    (ws / "notes").mkdir()
    (ws / "workspace.yaml").write_text("name: ws\ndescription: A test workspace\n")
    (ws / "studies" / "growth" / "study.yaml").write_text(
        "schema_version: 3\nname: growth\nobjective: Grow things\nstatus: draft\n"
        "baseline:\n- name: core\n  composite: pkg.composites.core\n  params: {}\nvariants: []\nruns: []\n")
    (ws / "investigations" / "inv1" / "investigation.yaml").write_text("name: inv1\nquestion: Why?\n")
    (ws / "notes" / "readme.md").write_text("# Notes\nhello world\n")
    (ws / ".env").write_text("SECRET_TOKEN=do-not-send\n")
    (ws / ".gitignore").write_text("scratch/\n.env\n")
    (ws / "scratch").mkdir()
    (ws / "scratch" / "tmp.txt").write_text("ignored file\n")
    _git(ws, "init", "-q", "-b", "main")
    _git(ws, "config", "user.email", "test@example.com")
    _git(ws, "config", "user.name", "Test")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "init")
    return ws


@pytest.fixture
def git():
    return _git


@pytest.fixture
def assistant_client(workspace, monkeypatch):
    """A TestClient for the app with the assistant enabled in LOCAL mode."""
    from fastapi.testclient import TestClient

    from vivarium_workbench.api.app import create_app, get_workspace
    monkeypatch.setenv("VIVARIUM_WORKBENCH_EXTENSIONS", "assistant")
    monkeypatch.setenv("VIVARIUM_WORKBENCH_ASSISTANT_MODE", "local")
    app = create_app()
    app.dependency_overrides[get_workspace] = lambda: workspace
    client = TestClient(app, headers={"Origin": "http://testserver"})
    client.app_ref = app  # type: ignore[attr-defined]
    return client


@pytest.fixture
def live_assistant(workspace, monkeypatch):
    """The real app under uvicorn on 127.0.0.1 (real sockets, real middleware).

    The server-runtime facts are recorded as for ``serve`` (loopback bind), so
    the Host guard is active and the assistant resolves LOCAL mode itself.
    Yields an ``httpx.Client`` with a same-origin ``Origin`` header.
    """
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    from vivarium_workbench.api.app import create_app, get_workspace
    from vivarium_workbench.lib import server_runtime
    monkeypatch.setenv("VIVARIUM_WORKBENCH_EXTENSIONS", "assistant")
    server_runtime.configure(bind_host="127.0.0.1")
    app = create_app()
    app.dependency_overrides[get_workspace] = lambda: workspace
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    client = httpx.Client(base_url=base, headers={"Origin": base}, timeout=30)
    client.app_ref = app  # type: ignore[attr-defined]
    yield client
    client.close()
    server.should_exit = True
    thread.join(timeout=10)
