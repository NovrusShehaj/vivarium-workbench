"""The catch-all static route never serves sensitive files or symlink escapes.

``GET /{rel:path}`` falls through to the workspace tree, so before this change
any file in the workspace — ``.git/config``, ``.env``, private keys, the
per-server ``.pbg/server/`` runtime files — was one URL away from any page that
could reach the loopback port. The shared denylist lives in
``lib.sensitive_paths`` (also used by the assistant's file sandbox).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vivarium_workbench.api.app import create_app, get_workspace
from vivarium_workbench.lib import sensitive_paths as sp
from vivarium_workbench.lib import server_runtime
from vivarium_workbench.lib import static_serving


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "workspace.yaml").write_text("name: denylist-ws\n")
    files = {
        ".git/config": "[core]\n",
        ".git/HEAD": "ref: refs/heads/main\n",
        ".env": "TOKEN=do-not-serve\n",
        ".env.local": "TOKEN=do-not-serve\n",
        "config/prod.env": "TOKEN=do-not-serve\n",
        "keys/id_rsa": "private\n",
        "keys/server.pem": "private\n",
        "keys/deploy.key": "private\n",
        "gcp/credentials.json": "{}\n",
        "gcp/my-service-account.json": "{}\n",
        "secrets/token.txt": "do-not-serve\n",
        ".pbg/state.json": "{}\n",
        ".pbg/server/server-info": "{}\n",
        ".pbg/assistant/conversations/x.jsonl": "{}\n",
        ".netrc": "machine example.com password x\n",
        "investigations/x/spec.yaml": "name: x\n",
        "studies/s1/study.yaml": "name: s1\n",
        "reports/figures/fig.html": "<html>fig</html>",
        # Legitimate catch-all consumers (spike S6): generated viz HTML shown in
        # iframes, reference PDFs and images, run artifacts linked from pages.
        "reports/viz/comparative.html": "<html>viz</html>",
        "references/paper.pdf": "%PDF-1.4 references",
        "studies/s1/visualizations/plot.svg": "<svg>plot</svg>",
        "studies/s1/runs/run1/log.txt": "run log",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


@pytest.fixture
def client(ws: Path) -> TestClient:
    server_runtime.reset()
    app = create_app()
    app.dependency_overrides[get_workspace] = lambda: ws
    return TestClient(app)


# ---------------------------------------------------------------------------
# HTTP: denied paths are a plain 404 (indistinguishable from "missing")
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/.git/config",
    "/.git/HEAD",
    "/.GIT/config",
    "/.env",
    "/.ENV",
    "/.env.local",
    "/config/prod.env",
    "/keys/id_rsa",
    "/keys/server.pem",
    "/keys/deploy.key",
    "/gcp/credentials.json",
    "/gcp/my-service-account.json",
    "/secrets/token.txt",
    "/.pbg/state.json",
    "/.pbg/server/server-info",
    "/.pbg/assistant/conversations/x.jsonl",
    "/.netrc",
])
def test_sensitive_paths_are_404(client, path):
    r = client.get(path)
    assert r.status_code == 404
    assert b"do-not-serve" not in r.content
    assert b"private" not in r.content


@pytest.mark.parametrize("path,body", [
    ("/investigations/x/spec.yaml", "name: x"),
    ("/studies/s1/study.yaml", "name: s1"),
    ("/figures/fig.html", "<html>fig</html>"),   # reports/ tier
    ("/viz/comparative.html", "<html>viz</html>"),   # reports/ tier (iframe viz)
    ("/references/paper.pdf", "%PDF-1.4"),
    ("/studies/s1/visualizations/plot.svg", "<svg>plot</svg>"),
    ("/studies/s1/runs/run1/log.txt", "run log"),
    ("/workspace.yaml", "denylist-ws"),
])
def test_ordinary_workspace_files_are_still_served(client, path, body):
    r = client.get(path)
    assert r.status_code == 200
    assert body in r.text


def test_bundled_assets_still_served(client):
    r = client.get("/assets/theme.js")
    assert r.status_code == 200
    assert "vivTheme" in r.text


@pytest.mark.parametrize("path", [
    "/%2e%2e/etc/passwd",
    "/studies/%2e%2e/%2e%2e/etc/passwd",
    "/notes/s1%00.yaml",
    "/notes%5cs1%5cstudy.yaml",
])
def test_encoded_traversal_nul_and_backslash_are_403(client, path):
    assert client.get(path).status_code == 403


def test_directory_request_is_404(client):
    assert client.get("/investigations/x/").status_code == 404


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_symlink_escaping_the_workspace_is_404(client, ws, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-secret")
    (ws / "notes").mkdir()
    os.symlink(outside, ws / "notes" / "escape.txt")
    r = client.get("/notes/escape.txt")
    assert r.status_code == 404
    assert b"outside-secret" not in r.content


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_symlinked_directory_escape_is_404(client, ws, tmp_path):
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "data.txt").write_text("outside-secret")
    os.symlink(outside_dir, ws / "linked")
    assert client.get("/linked/data.txt").status_code == 404


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_innocent_name_symlinked_to_denied_file_is_404(client, ws):
    (ws / "notes").mkdir(exist_ok=True)
    os.symlink(ws / ".env", ws / "notes" / "readme.txt")
    r = client.get("/notes/readme.txt")
    assert r.status_code == 404
    assert b"do-not-serve" not in r.content


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_symlink_inside_workspace_to_ordinary_file_is_served(client, ws):
    os.symlink(ws / "studies" / "s1" / "study.yaml", ws / "alias.yaml")
    r = client.get("/alias.yaml")
    assert r.status_code == 200 and "name: s1" in r.text


# ---------------------------------------------------------------------------
# lib.static_serving.resolve_asset
# ---------------------------------------------------------------------------

def test_resolve_asset_raises_denied_for_sensitive(ws):
    with pytest.raises(static_serving.DeniedAsset):
        static_serving.resolve_asset(ws, ".git/config")


def test_resolve_asset_raises_traversal_for_malformed(ws):
    for rel in ("a/../b", "a\\b", "a\x00b", "a//b"):
        with pytest.raises(static_serving.AssetTraversal):
            static_serving.resolve_asset(ws, rel)


# ---------------------------------------------------------------------------
# lib.sensitive_paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel", ["", "/abs", "C:/x", "a/../b", "./a", "a//b", "a\\b", "a\x00"])
def test_normalize_rel_rejects(rel):
    with pytest.raises(sp.UnsafePath):
        sp.normalize_rel(rel)


def test_normalize_rel_nfc():
    decomposed = "cafe\u0301/x.txt"
    assert sp.normalize_rel(decomposed) == "caf\u00e9/x.txt"


@pytest.mark.parametrize("rel,sensitive", [
    (".git", True),
    ("sub/.git/objects/ab", True),
    (".Git/config", True),
    (".ssh/id_ed25519", True),
    ("home/.aws/credentials", True),
    ("x/.config/gcloud/application_default_credentials.json", True),
    (".pbg/server", True),
    (".pbg/server/server-info", True),
    (".pbg/assistant/audit.jsonl", True),
    (".pbg/state.json", True),
    ("prod.ENV", True),
    (".env.production", True),
    ("cert.PEM", True),
    ("id_rsa.pub", True),
    ("service_account_key.json", True),
    ("api.secret", True),
    (".pbg/schemas/study.schema.json", False),
    (".pbg/runs/abc/run.log", False),
    ("studies/s1/study.yaml", False),
    ("environment.yml", False),
    ("docs/secrets-policy.md", False),
    ("src/keyboard.py", False),
])
def test_is_sensitive_rel(rel, sensitive):
    assert sp.is_sensitive_rel(rel) is sensitive


def test_looks_like_credential():
    assert sp.looks_like_credential(b"x\n-----BEGIN RSA PRIVATE KEY-----\nabc")
    assert sp.looks_like_credential(b'{"type": "service_account", "project_id": "p"}')
    assert sp.looks_like_credential(b'{"private_key": "-----BEGIN PRIVATE KEY-----\\n"}')
    assert not sp.looks_like_credential(b"-----BEGIN CERTIFICATE-----")
    assert not sp.looks_like_credential(b"")


def test_resolve_contained(ws, tmp_path):
    assert sp.resolve_contained(ws, "studies/s1/study.yaml") == (ws / "studies/s1/study.yaml").resolve()
    assert sp.resolve_contained(ws, "missing/file.txt") is not None     # need not exist
    assert sp.resolve_contained(ws, ".env") is None
    assert sp.resolve_contained(ws, "../outside") is None
    assert sp.resolve_contained(tmp_path / "does-not-exist", "x") is None
