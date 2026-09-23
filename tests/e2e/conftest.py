"""Browser end-to-end tests: Playwright (Python) + Chromium against a real server.

Opt-in (they start real servers and a browser): ``VIVARIUM_WORKBENCH_E2E=1``.
Needs the ``e2e`` extra (``pip install -e '.[e2e,assistant]'``) and a browser
(``python -m playwright install chromium``). CI runs them in
``.github/workflows/e2e.yml``.

Isolation: every server gets a throwaway copy of a fixture workspace, its own
config/data dirs and the failing keyring backend, so nothing here can read or
write the real keychain or ``~/.config``. Pages are hermetic: requests to any
host other than the server under test (CDN scripts, fonts) are aborted.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from .e2e_support import Workbench, make_workspace

E2E_ENABLED = os.environ.get("VIVARIUM_WORKBENCH_E2E") == "1"


def pytest_collection_modifyitems(config, items):
    if E2E_ENABLED:
        return
    skip = pytest.mark.skip(reason="browser E2E is opt-in: set VIVARIUM_WORKBENCH_E2E=1 "
                                   "(needs the e2e extra and `python -m playwright install chromium`)")
    here = Path(__file__).resolve().parent
    for item in items:
        if here in Path(str(item.fspath)).resolve().parents:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def e2e_root(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("e2e")


@pytest.fixture(scope="session")
def e2e_ws(e2e_root) -> Path:
    return make_workspace(e2e_root)


@pytest.fixture(scope="session")
def workbench(e2e_root, e2e_ws) -> Iterator[Workbench]:
    """The workbench with the assistant extension enabled (local mode)."""
    wb = Workbench(ws=e2e_ws, home=e2e_root / "home", extensions=("assistant",)).start()
    yield wb
    wb.stop()


@pytest.fixture(scope="session")
def playwright_instance():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(playwright_instance):
    b = playwright_instance.chromium.launch()
    yield b
    b.close()


