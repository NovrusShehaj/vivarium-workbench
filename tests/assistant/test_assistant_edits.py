"""Tools, proposals, apply/undo and scoped commits."""
from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vivarium_workbench_assistant.edits import apply as ap
from vivarium_workbench_assistant.edits import validators
from vivarium_workbench_assistant.edits.proposals import ProposalStore
from vivarium_workbench_assistant.services import AssistantServices
from vivarium_workbench_assistant.tools import execute_tools
from vivarium_workbench_assistant.tools.registry import ToolArgError, ToolContext, default_registry, validate_args


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def ctx(workspace):
    services = AssistantServices()
    return ToolContext(ws_root=workspace, mode="local", conversation_id="c_" + "a" * 16, run_id="r_" + "b" * 16,
                       scope="local", persist=True, session_key=None, model_label="local/fake", services=services)


def call(ctx, name, **args):
    spec = default_registry().get(name)
    return asyncio.run(spec.handler(ctx, validate_args(spec, args)))


# -- registry ------------------------------------------------------------------

def test_tool_argument_validation():
    reg = default_registry()
    with pytest.raises(ToolArgError):
        validate_args(reg.get("read_file"), {})
    with pytest.raises(ToolArgError):
        validate_args(reg.get("read_file"), {"path": "x", "surprise": 1})
    with pytest.raises(ToolArgError):
        validate_args(reg.get("get_study"), {"slug": "../etc"})
    with pytest.raises(ToolArgError):
        validate_args(reg.get("read_file"), ["not", "an", "object"])


def test_tool_schemas_have_no_top_level_combinators():
    for spec in default_registry().all():
        assert spec.parameters.get("type") == "object"
        assert not {"oneOf", "anyOf", "allOf"} & set(spec.parameters), spec.name


# -- read tools ----------------------------------------------------------------

def test_read_tools(ctx):
    r = call(ctx, "read_file", path="notes/readme.md")
    assert r.ok and "hello world" in r.content and "sha256" in r.content
    r = call(ctx, "read_file", path="notes/readme.md", start_line=2, end_line=2)
    assert r.content.splitlines()[-1] == "2: hello world"
    assert not call(ctx, "read_file", path=".env").ok
    assert "readme.md" in call(ctx, "list_dir", path="notes").content
    assert "notes/readme.md" in call(ctx, "search", query="hello").content
    assert "objective" in call(ctx, "get_study", slug="growth").content
    assert "question" in call(ctx, "get_investigation", slug="inv1").content
    assert "## main" in call(ctx, "get_git_status").content


def test_read_preflight_flags_gitignored(ctx):
    spec = default_registry().get("read_file")
    assert spec.preflight(ctx, {"path": "scratch/tmp.txt"}) == {"gitignored": True}
    assert spec.preflight(ctx, {"path": "notes/readme.md"}) == {"gitignored": False}


def test_validate_spec_tool_uses_workspace_schema(ctx, workspace):
    schemas = workspace / ".pbg" / "schemas"
    schemas.mkdir(parents=True)
    (schemas / "study.schema.json").write_text('{"type": "object", "required": ["name", "objective"]}')
    ok = call(ctx, "validate_spec", path="studies/growth/study.yaml")
    assert '"ok": true' in ok.content
    (workspace / "studies" / "growth" / "study.yaml").write_text("name: growth\n")
    bad = call(ctx, "validate_spec", path="studies/growth/study.yaml")
    assert "objective" in bad.content and '"ok": false' in bad.content


# -- propose tools -------------------------------------------------------------

def test_propose_edit_requires_current_hash_and_writes_nothing(ctx, workspace):
    path = workspace / "notes" / "readme.md"
    before = path.read_text()
    stale = call(ctx, "propose_edit", path="notes/readme.md", base_sha256="0" * 64, new_content="x\n",
                 rationale="r")
    assert not stale.ok and "changed" in stale.content
    ok = call(ctx, "propose_edit", path="notes/readme.md", base_sha256=sha(before),
              new_content="# Notes\nhello there\n", rationale="friendlier")
    assert ok.ok and ok.data["op"] == "modify" and path.read_text() == before     # nothing written
    p = ctx.services.proposals.get(workspace, ok.data["proposal_id"], scope="local", persist=True)
    f = p.files[0]
    assert f.base_sha256 == sha(before) and "+hello there" in f.unified_diff and "-hello world" in f.unified_diff


def test_propose_edit_with_patch_and_bad_patch(ctx, workspace):
    before = (workspace / "notes" / "readme.md").read_text()
    patch = "--- a/notes/readme.md\n+++ b/notes/readme.md\n@@ -1,2 +1,2 @@\n # Notes\n-hello world\n+hello patch\n"
    ok = call(ctx, "propose_edit", path="notes/readme.md", base_sha256=sha(before), patch=patch, rationale="r")
    assert ok.ok
    bad = call(ctx, "propose_edit", path="notes/readme.md", base_sha256=sha(before),
               patch="@@ -1,1 +1,1 @@\n-nope\n+x\n", rationale="r")
    assert not bad.ok and "does not apply" in bad.content
    both = call(ctx, "propose_edit", path="notes/readme.md", base_sha256=sha(before), patch=patch,
                new_content="x", rationale="r")
    assert not both.ok


def test_propose_refuses_protected_targets(ctx):
    for path in (".git/config", ".env", "reports/index.html", ".pbg/state.json", "studies/growth/runs.db"):
        r = call(ctx, "propose_create", path=path, content="x", rationale="r")
        assert not r.ok, path


def test_proposals_flag_executable_code_and_validate(ctx, workspace):
    r = call(ctx, "propose_create", path="pkg/processes/new.py", content="def f(:\n", rationale="r")
    assert r.ok and r.data["executable_code"] is True
    assert r.data["validation"][0]["ok"] is False
    y = call(ctx, "propose_create", path="notes/x.yaml", content="a: [1, 2\n", rationale="r")
    assert y.data["validation"][0]["ok"] is False


def test_validators_direct():
    assert validators.validate(Path("."), "a.json", "{}")[0]["ok"]
    assert not validators.validate(Path("."), "a.json", "{")[0]["ok"]
    assert validators.validate(Path("."), "a.toml", "x = 1")[0]["ok"]
    assert validators.validate(Path("."), "a.py", "x = 1\n")[0]["ok"]
    assert validators.validate(Path("."), "a.txt", "anything") == []


# -- apply / undo / commit -----------------------------------------------------------

def _proposal_with_edit(ctx, workspace, new="# Notes\nhello applied\n"):
    before = (workspace / "notes" / "readme.md").read_text()
    r = call(ctx, "propose_edit", path="notes/readme.md", base_sha256=sha(before), new_content=new, rationale="r")
    return ctx.services.proposals.get(workspace, r.data["proposal_id"], scope="local", persist=True)


def test_apply_and_undo(ctx, workspace):
    p = _proposal_with_edit(ctx, workspace)
    res = ap.apply_files(workspace, p, None)
    assert res["results"][0]["status"] == "applied"
    assert (workspace / "notes" / "readme.md").read_text() == "# Notes\nhello applied\n"
    assert p.status == "applied"
    ap.undo_files(workspace, p, None)
    assert (workspace / "notes" / "readme.md").read_text() == "# Notes\nhello world\n"
    assert p.files[0].status == "undone"


def test_stale_proposal_becomes_conflict(ctx, workspace):
    p = _proposal_with_edit(ctx, workspace)
    (workspace / "notes" / "readme.md").write_text("# Notes\nsomeone else edited\n")
    res = ap.apply_files(workspace, p, None)
    assert res["results"][0]["status"] == "conflict"
    assert (workspace / "notes" / "readme.md").read_text() == "# Notes\nsomeone else edited\n"


def test_undo_refuses_after_later_edits(ctx, workspace):
    p = _proposal_with_edit(ctx, workspace)
    ap.apply_files(workspace, p, None)
    (workspace / "notes" / "readme.md").write_text("changed again\n")
    res = ap.undo_files(workspace, p, None)
    assert res["results"][0]["status"] == "conflict"
    assert (workspace / "notes" / "readme.md").read_text() == "changed again\n"


def test_create_and_delete(ctx, workspace):
    r = call(ctx, "propose_create", path="notes/new/deep.md", content="new\n", rationale="r")
    p = ctx.services.proposals.get(workspace, r.data["proposal_id"], scope="local", persist=True)
    ap.apply_files(workspace, p, None)
    assert (workspace / "notes" / "new" / "deep.md").read_text() == "new\n"
    ap.undo_files(workspace, p, None)
    assert not (workspace / "notes" / "new" / "deep.md").exists()
    ctx2 = ToolContext(**{**ctx.__dict__, "run_id": "r_" + "c" * 16})
    d = call(ctx2, "propose_delete", path="notes/readme.md", rationale="r")
    p2 = ctx.services.proposals.get(workspace, d.data["proposal_id"], scope="local", persist=True)
    ap.apply_files(workspace, p2, None)
    assert not (workspace / "notes" / "readme.md").exists()


def _git_out(ws, *args):
    return subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True, check=True).stdout


def test_scoped_commit_commits_only_the_applied_file(ctx, workspace, git):
    # Unrelated work: a staged change and an unstaged change in other files.
    (workspace / "investigations" / "inv1" / "investigation.yaml").write_text("name: inv1\nquestion: Staged?\n")
    git(workspace, "add", "investigations/inv1/investigation.yaml")
    (workspace / "studies" / "growth" / "study.yaml").write_text("name: growth\nunstaged: true\n")
    p = _proposal_with_edit(ctx, workspace)
    res = ap.apply_files(workspace, p, None)
    commit = ap.commit_applied(workspace, p, res["results"], conversation_id="c_x", model_label="local/fake")
    assert commit["committed"] is True and commit["paths"] == ["notes/readme.md"]
    assert _git_out(workspace, "show", "--name-only", "--format=", "HEAD").split() == ["notes/readme.md"]
    msg = _git_out(workspace, "log", "-1", "--format=%B")
    assert "Assisted-by: vivarium-workbench-assistant" in msg
    assert "Assistant-Model: local/fake" in msg and "Assistant-Conversation: c_x" in msg
    status = _git_out(workspace, "status", "--porcelain")
    assert "M  investigations/inv1/investigation.yaml" in status       # still staged, not committed
    assert " M studies/growth/study.yaml" in status                    # still unstaged


def test_file_with_prior_uncommitted_changes_is_not_swept_into_commit(ctx, workspace):
    (workspace / "notes" / "readme.md").write_text("# Notes\nhello world\nmy own edit\n")
    p = _proposal_with_edit(ctx, workspace, new="# Notes\nhello world\nmy own edit\nassistant line\n")
    res = ap.apply_files(workspace, p, None)
    commit = ap.commit_applied(workspace, p, res["results"], conversation_id="c_x", model_label="m")
    assert commit["committed"] is False and commit["skipped"] == ["notes/readme.md"]
    assert _git_out(workspace, "log", "--format=%s") .splitlines()[0] == "init"


def test_new_file_is_added_by_name_only(ctx, workspace):
    (workspace / "notes" / "untracked-user-file.md").write_text("mine\n")
    r = call(ctx, "propose_create", path="notes/assistant.md", content="made by assistant\n", rationale="r")
    p = ctx.services.proposals.get(workspace, r.data["proposal_id"], scope="local", persist=True)
    res = ap.apply_files(workspace, p, None)
    commit = ap.commit_applied(workspace, p, res["results"], conversation_id="c", model_label="m")
    assert commit["committed"]
    assert _git_out(workspace, "show", "--name-only", "--format=", "HEAD").split() == ["notes/assistant.md"]
    assert "?? notes/untracked-user-file.md" in _git_out(workspace, "status", "--porcelain")


def test_revert_commit(ctx, workspace):
    p = _proposal_with_edit(ctx, workspace)
    res = ap.apply_files(workspace, p, None)
    ap.commit_applied(workspace, p, res["results"], conversation_id="c", model_label="m")
    out = ap.revert_commit(workspace, p)
    assert out["reverted"] is True
    assert (workspace / "notes" / "readme.md").read_text() == "# Notes\nhello world\n"


def test_commit_skipped_when_workstream_branch_not_checked_out(ctx, workspace):
    (workspace / ".pbg").mkdir(exist_ok=True)
    (workspace / ".pbg" / "state.json").write_text('{"active_branch": "feature-x"}')
    p = _proposal_with_edit(ctx, workspace)
    res = ap.apply_files(workspace, p, None)
    out = ap.commit_applied(workspace, p, res["results"], conversation_id="c", model_label="m")
    assert out["committed"] is False and "feature-x" in out["reason"]


# -- execute tools ----------------------------------------------------------------------

def test_sanitized_env_drops_secrets(monkeypatch, workspace):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("VIVARIUM_WORKBENCH_GH_TOKEN", "ghp_should_not_leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-should-not-leak")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
    env = execute_tools.sanitized_env(workspace)
    joined = " ".join(f"{k}={v}" for k, v in env.items())
    for bad in ("should-not-leak", "should_not_leak", "sa.json"):
        assert bad not in joined
    assert "PATH" in env


def test_shell_runs_argv_in_workspace_without_secrets(monkeypatch, ctx, workspace):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-reach-child")
    r = call(ctx, "shell", argv=[sys.executable, "-c",
                                 "import os; print(os.getcwd()); print('OPENAI_API_KEY' in os.environ)"])
    assert r.ok
    assert str(workspace.resolve()) in r.content and "False" in r.content
    assert not call(ctx, "shell", argv=["ls"], cwd="../").ok


def test_run_tests_selector_is_sanitized(ctx):
    for bad in ("; rm -rf /", "../outside", "tests/x.py && echo", "$(whoami)"):
        assert not call(ctx, "run_tests", selector=bad).ok


def test_process_timeout_kills_the_group(workspace):
    res = asyncio.run(execute_tools.run_process(
        [sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"],
        cwd=workspace, env=execute_tools.sanitized_env(workspace), timeout_s=1))
    assert res["timed_out"] is True


def test_dangerous_warnings():
    assert "recursive delete" in execute_tools.dangerous_warnings(["rm", "-rf", "x"])
    assert "force push" in execute_tools.dangerous_warnings(["git", "push", "--force"])
    assert execute_tools.dangerous_warnings(["pytest", "-q"]) == []


def test_lint_tool_without_script(ctx):
    r = call(ctx, "run_workspace_lint")
    assert not r.ok and "no scripts/lint-workspace.py" in r.content


def test_lint_tool_runs_workspace_script(ctx, workspace):
    (workspace / "scripts").mkdir()
    (workspace / "scripts" / "lint-workspace.py").write_text("print('lint ok')\n")
    r = call(ctx, "run_workspace_lint")
    assert r.ok and "lint ok" in r.content


def test_proposal_store_persists_privately(workspace, tmp_path):
    store = ProposalStore(root=lambda: tmp_path / "assistant")
    p = store.for_run(workspace, "r_" + "d" * 16, "c_1", scope="local", persist=True)
    again = store.for_run(workspace, "r_" + "d" * 16, "c_1", scope="local", persist=True)
    assert p.id == again.id
    files = list((tmp_path / "assistant").rglob("p_*.json"))
    assert files and os.name != "posix" or oct(files[0].stat().st_mode & 0o777) == "0o600"
