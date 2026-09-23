"""Helpers for the browser E2E tests (see conftest.py for the fixtures)."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
FIXTURE_WS = REPO / "tests" / "_fixtures" / "ws_increase_demo"
COMPOSITE = "pbg_ws_increase_demo.composites.increase-demo"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_yaml(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(obj, sort_keys=False), encoding="utf-8")


def make_workspace(root: Path) -> Path:
    """A copy of ws_increase_demo plus an investigation and studies in every
    gate state, so the status system, the investigation graph and the tests
    band all render."""
    ws = root / "ws"
    shutil.copytree(FIXTURE_WS, ws, ignore=shutil.ignore_patterns("__pycache__", "server", "composite-state-cache"))
    _write_yaml(ws / "investigations" / "e2e-inv" / "investigation.yaml", {
        "name": "e2e-inv", "title": "E2E investigation", "status": "in_progress",
        "question": "Does the growth rate control the final level?",
        "studies": ["growth-accepted", "growth-refuted", "growth-partial", "growth-blocked"],
    })
    tests = [
        {"name": "LEVEL-GROWS", "classification": "primary", "description": "level increases",
         "measure": {"kind": "final", "path": "level"}, "pass_if": {"op": ">=", "value": 5}},
        {"name": "LEVEL-BOUNDED", "classification": "supporting", "description": "level stays bounded",
         "measure": {"kind": "final", "path": "level"}, "pass_if": {"op": "<=", "value": 100}},
    ]
    for slug, conf, gate, res in (("growth-accepted", "Accepted", "passed", ("PASS", "PASS")),
                                  ("growth-refuted", "Refuted", "failed", ("FAIL", "PASS")),
                                  ("growth-partial", "Investigating", "needs_calibration", ("PASS", "FAIL")),
                                  ("growth-blocked", "Blocked", "blocked", ("SKIP", "SKIP"))):
        _write_yaml(ws / "studies" / slug / "study.yaml", {
            "name": slug, "title": slug.replace("-", " ").title(), "confidence": conf, "gate_status": gate,
            "question": f"Does {slug} change the level?", "claim": "The level grows with the rate.",
            "baseline": [{"name": "base", "composite": COMPOSITE, "params": {"rate": 2.0}}],
            "behavior_tests": tests,
            "runs": [{"name": f"{slug}-run", "status": "completed",
                      "outcomes": {"LEVEL-GROWS": {"result": res[0]}, "LEVEL-BOUNDED": {"result": res[1]}}}],
            "findings": [
                {"id": "F1", "status": "confirms", "statement": "Level grows with rate."},
                {"id": "F2", "status": "partial", "statement": "Bounded only for small rates."},
                {"id": "F3", "status": "contradicts", "statement": "Growth is not linear."},
            ],
        })
    git = ["git", "-c", "user.name=e2e", "-c", "user.email=e2e@example.invalid"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=ws, check=True)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run([*git, "commit", "-qm", "e2e workspace"], cwd=ws, check=True)
    return ws


@dataclass
class Workbench:
    ws: Path
    home: Path
    port: int = 0
    extensions: tuple[str, ...] = ()
    proc: subprocess.Popen | None = None
    log: Path | None = None
    env_extra: dict[str, str] = field(default_factory=dict)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        for k in list(env):
            if k.startswith(("VIVARIUM_WORKBENCH_", "VIVARIUM_DASHBOARD_")) and k != "VIVARIUM_WORKBENCH_E2E":
                env.pop(k)
        env.update({
            "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
            "VIVARIUM_WORKBENCH_CONFIG_DIR": str(self.home / "config"),
            "VIVARIUM_WORKBENCH_DATA_DIR": str(self.home / "data"),
            "VIVA_HOME": str(self.home / "viva"),
            "VIVA_API_BASE": "http://127.0.0.1:9", "SMS_API_BASE": "http://127.0.0.1:9",
            "GH_CONFIG_DIR": str(self.home / "gh"),
            "HOME": str(self.home / "userhome"),
        })
        env.update(self.env_extra)
        return env

    def start(self, port: int | None = None) -> Workbench:
        self.port = port or free_port()
        (self.home / "userhome").mkdir(parents=True, exist_ok=True)
        self.log = self.home / f"serve-{self.port}.log"
        cmd = [sys.executable, "-m", "vivarium_workbench.cli", "serve", "--workspace", str(self.ws),
               "--port", str(self.port)]
        for ext in self.extensions:
            cmd += ["--enable-extension", ext]
        self.proc = subprocess.Popen(cmd, cwd=str(REPO), env=self.env(),
                                     stdout=self.log.open("w"), stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early:\n{self.log.read_text()[-4000:]}")
            try:
                with urllib.request.urlopen(self.base + "/health", timeout=2) as r:
                    if r.status == 200:
                        return self
            except OSError:
                time.sleep(0.2)
        raise RuntimeError(f"server did not come up:\n{self.log.read_text()[-4000:]}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


def hermetic(context, allowed_origins: list[str]) -> None:
    """Abort every request that does not go to one of ``allowed_origins``."""
    def handler(route):
        url = route.request.url
        if url.startswith(("data:", "blob:", "about:")) or any(url.startswith(o) for o in allowed_origins):
            route.continue_()
        else:
            route.abort()
    context.route("**/*", handler)


# Records the theme on <html> at the moment <body> (or the first stylesheet)
# is inserted — i.e. before anything could paint — to detect a flash of the
# wrong theme (plan §17.3). Stored on window.__vivFirstPaint.
FIRST_PAINT_PROBE = """
(() => {
  window.__vivFirstPaint = null;
  const record = (why) => {
    if (window.__vivFirstPaint) return;
    const r = document.documentElement;
    window.__vivFirstPaint = { why, theme: r && r.getAttribute('data-theme'),
                               pref: r && r.getAttribute('data-theme-pref') };
  };
  const obs = new MutationObserver((muts) => {
    for (const m of muts) for (const n of m.addedNodes) {
      if (n.nodeType === 1 && (n.tagName === 'BODY' ||
          (n.tagName === 'LINK' && /stylesheet/i.test(n.getAttribute('rel') || '')))) {
        record(n.tagName); obs.disconnect(); return;
      }
    }
  });
  obs.observe(document, { childList: true, subtree: true });
})();
"""


def new_context(browser, wb: Workbench, *, os_scheme: str = "light", stored: str | None = None,
                extra_origins: list[str] | None = None, **kw):
    ctx = browser.new_context(color_scheme=os_scheme, **kw)
    hermetic(ctx, [wb.base, *(extra_origins or [])])
    if stored is not None:
        ctx.add_init_script(
            "try { if (!sessionStorage.getItem('__e2e_seeded')) { localStorage.setItem('viv.theme', %r); "
            "sessionStorage.setItem('__e2e_seeded', '1'); } } catch (e) {}" % stored)
    ctx.add_init_script(FIRST_PAINT_PROBE)
    return ctx
