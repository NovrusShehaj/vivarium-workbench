"""Execute tools — allowlisted command templates, always approval-gated.

Nothing here runs in the HTTP process's interpreter: every tool spawns a
subprocess (argv list, ``shell=False``, ``stdin`` closed, ``cwd`` inside the
workspace) with a **sanitized environment** built from an allowlist (the
server's provider keys, ``VIVARIUM_WORKBENCH_GH_TOKEN``, ``AWS_*`` and
``GOOGLE_APPLICATION_CREDENTIALS`` never reach it), a timeout, per-stream
output caps, and a new process group that is killed on timeout or cancel.

* ``run_workspace_lint`` — ``python scripts/lint-workspace.py`` (docs/USAGE.md);
* ``run_tests``          — ``python -m pytest`` with a sanitized selector;
* ``run_composite_smoke``— a short composite run through the workbench's own
  detached run subsystem, polled to completion;
* ``shell``              — free-form argv; local mode only, disabled by default,
  approved per invocation, never "always allow".
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

from vivarium_workbench_assistant import sandbox
from vivarium_workbench_assistant.sandbox import SandboxError
from vivarium_workbench_assistant.tools.registry import ToolContext, ToolResult, ToolSpec

OUTPUT_CAP = 64 * 1024
ENV_ALLOW = ("PATH", "HOME", "LANG", "TMPDIR", "TEMP", "TMP", "USER", "LOGNAME", "TERM", "SYSTEMROOT")
SELECTOR_RE = re.compile(r"^[A-Za-z0-9_./\-]+(?:::[A-Za-z0-9_\[\]\-.]+)*$")

DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\b.*\s-[a-zA-Z]*r"), "recursive delete"),
    (re.compile(r"\bgit\b.*\bpush\b.*(--force|-f\b)"), "force push"),
    (re.compile(r"\b(curl|wget)\b.*\|\s*(sh|bash|zsh|python)"), "pipes a download into a shell"),
    (re.compile(r"\bchmod\b.*\s-R"), "recursive permission change"),
    (re.compile(r"\bsudo\b"), "runs as root"),
    (re.compile(r"\bgit\b.*\breset\b.*--hard"), "discards uncommitted work"),
    (re.compile(r"\bgit\b.*\bclean\b"), "deletes untracked files"),
    (re.compile(r"(>|>>)\s*/"), "writes outside the workspace"),
)


def sanitized_env(ws_root: Path, *, venv_python: str | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOW or k.startswith("LC_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["NO_COLOR"] = "1"
    if venv_python:
        venv = Path(venv_python).parent.parent
        if (venv / "pyvenv.cfg").is_file():
            env["VIRTUAL_ENV"] = str(venv)
            env["PATH"] = str(venv / "bin") + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = str(ws_root)
    return env


def dangerous_warnings(argv: list[str]) -> list[str]:
    line = " ".join(argv)
    return [label for pat, label in DANGEROUS_PATTERNS if pat.search(line)]


async def run_process(argv: list[str], *, cwd: Path, env: dict[str, str], timeout_s: float,
                      cap: int = OUTPUT_CAP) -> dict[str, Any]:
    """Run ``argv``; return exit code, capped stdout/stderr, duration, timeout flag."""
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )

    bufs = {"out": bytearray(), "err": bytearray()}
    over = {"out": False, "err": False}

    async def read(stream: asyncio.StreamReader | None, key: str) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            buf = bufs[key]
            room = cap - len(buf)
            if room > 0:
                buf.extend(chunk[:room])
            if len(chunk) > room:
                over[key] = True

    timed_out = False
    try:
        await asyncio.wait_for(asyncio.gather(read(proc.stdout, "out"), read(proc.stderr, "err")),
                               timeout=timeout_s)
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_group(proc)
        await proc.wait()
    except asyncio.CancelledError:
        _kill_group(proc)
        raise
    out, err = bytes(bufs["out"]), bytes(bufs["err"])
    out_over, err_over = over["out"], over["err"]
    return {
        "exit_code": proc.returncode,
        "stdout": out.decode("utf-8", "replace") + ("\n[… output truncated …]" if out_over else ""),
        "stderr": err.decode("utf-8", "replace") + ("\n[… output truncated …]" if err_over else ""),
        "duration_s": round(time.monotonic() - t0, 2),
        "timed_out": timed_out,
    }


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _format(res: dict[str, Any], argv: list[str]) -> ToolResult:
    status = "timed out" if res["timed_out"] else f"exit code {res['exit_code']}"
    content = (f"$ {' '.join(argv)}\n[{status}, {res['duration_s']} s]\n"
               f"--- stdout ---\n{res['stdout']}\n--- stderr ---\n{res['stderr']}")
    ok = not res["timed_out"] and res["exit_code"] == 0
    return ToolResult(ok=ok, content=content, summary=f"{argv[0] if argv else 'command'}: {status}")


def _interpreter(ws_root: Path) -> str:
    try:
        from vivarium_workbench.lib.env_resolver import resolve_interpreter
        return resolve_interpreter(ws_root)
    except Exception:  # noqa: BLE001
        return sys.executable


async def run_workspace_lint(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    script = Path(ctx.ws_root) / "scripts" / "lint-workspace.py"
    if not script.is_file():
        return ToolResult(ok=False, content="this workspace has no scripts/lint-workspace.py",
                          summary="no lint script")
    py = _interpreter(ctx.ws_root)
    argv = [py, "scripts/lint-workspace.py"]
    res = await run_process(argv, cwd=ctx.ws_root, env=sanitized_env(ctx.ws_root, venv_python=py), timeout_s=300)
    return _format(res, ["python", "scripts/lint-workspace.py"])


async def run_tests(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    selector = args.get("selector")
    argv_tail: list[str] = []
    if selector:
        selector = str(selector)
        if not SELECTOR_RE.match(selector) or ".." in selector:
            return ToolResult(ok=False, content="invalid test selector", summary="invalid selector")
        path_part = selector.split("::", 1)[0]
        try:
            sandbox.resolve_in_workspace(ctx.ws_root, path_part)
        except SandboxError as exc:
            return ToolResult(ok=False, content=str(exc), summary="selector outside the workspace")
        argv_tail = [selector]
    py = _interpreter(ctx.ws_root)
    argv = [py, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider", *argv_tail]
    res = await run_process(argv, cwd=ctx.ws_root, env=sanitized_env(ctx.ws_root, venv_python=py), timeout_s=600)
    return _format(res, ["python", "-m", "pytest", "-q", "-x", *argv_tail])


async def run_composite_smoke(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """A short run through the workbench's own detached run subsystem."""
    cid = str(args["id"])
    steps = float(args.get("steps") or 3)

    def start() -> tuple[dict[str, Any], int]:
        from vivarium_workbench.lib.composite_test_run_views import composite_test_run
        return composite_test_run(ctx.ws_root, {"id": cid, "steps": steps, "label": "assistant smoke"})

    body, status = await asyncio.to_thread(start)
    if status >= 300 or "run_id" not in body:
        return ToolResult(ok=False, content=f"could not start the run: {body.get('error') or status}",
                          summary="run not started")
    run_id = str(body["run_id"])
    from vivarium_workbench.lib.composite_run_views import build_composite_run_status
    deadline = time.monotonic() + 600
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state, _code = await asyncio.to_thread(build_composite_run_status, ctx.ws_root, run_id)
        st = str(state.get("status") or "")
        if st and st not in ("running", "queued", "pending", "starting"):
            break
        await asyncio.sleep(2)
    else:
        return ToolResult(ok=False, content=f"run {run_id} did not finish within 10 minutes (still running)",
                          summary="run still running")
    log_tail = ""
    try:
        from vivarium_workbench_assistant.context import sources
        log_tail = sources.run_log(ctx.ws_root, {"run_id": run_id, "tail_lines": 60}).content
    except Exception:  # noqa: BLE001
        pass
    ok = str(state.get("status")) in ("complete", "completed", "succeeded", "success", "done")
    return ToolResult(ok=ok, content=f"run {run_id}: status {state.get('status')}\n\n{log_tail}",
                      summary=f"composite smoke run {run_id}: {state.get('status')}")


async def shell(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    argv = [str(a) for a in args["argv"]]
    cwd_rel = str(args.get("cwd") or "")
    try:
        cwd = Path(ctx.ws_root).resolve() if not cwd_rel else sandbox.resolve_in_workspace(ctx.ws_root, cwd_rel)
    except SandboxError as exc:
        return ToolResult(ok=False, content=str(exc), summary="cwd outside the workspace")
    if not cwd.is_dir():
        return ToolResult(ok=False, content="cwd is not a directory", summary="bad cwd")
    res = await run_process(argv, cwd=cwd, env=sanitized_env(ctx.ws_root), timeout_s=120)
    return _format(res, argv)


SPECS: list[ToolSpec] = [
    ToolSpec("run_workspace_lint", "Run the workspace linter (scripts/lint-workspace.py). Needs the user's approval.",
             {"type": "object", "properties": {}, "additionalProperties": False},
             "execute", run_workspace_lint, timeout_s=300),
    ToolSpec("run_tests", "Run the workspace's pytest suite, or one test file/node id. Needs the user's approval.",
             {"type": "object", "properties": {"selector": {"type": "string", "maxLength": 300}},
              "additionalProperties": False}, "execute", run_tests, timeout_s=600),
    ToolSpec("run_composite_smoke", "Start a short simulation run of a composite and wait for its result. "
             "Needs the user's approval.",
             {"type": "object", "properties": {"id": {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_.]{0,199}$"},
                                               "steps": {"type": "number", "minimum": 1, "maximum": 50}},
              "required": ["id"], "additionalProperties": False}, "execute", run_composite_smoke, timeout_s=620),
    ToolSpec("shell", "Run a command (argv list) in the workspace. Disabled unless the user enabled it in "
             "Settings; every command needs approval.",
             {"type": "object", "properties": {"argv": {"type": "array", "items": {"type": "string", "maxLength": 1000},
                                                        "minItems": 1, "maxItems": 64},
                                               "cwd": {"type": "string", "maxLength": 512}},
              "required": ["argv"], "additionalProperties": False}, "shell", shell, timeout_s=130),
]
