"""scripts/owner_files.py: a live run of Claude workers on the owner's own `claude` checks Claude's
state file key by key (more-agents.md 5.12.5), never printing a value."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "owner_files.py"


@pytest.fixture
def owner_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    spec = importlib.util.spec_from_file_location("owner_files", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def changed(path: Path, change):
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def test_a_live_run_may_add_untrusted_projects_of_its_own_and_change_claudes_own_keys(
    owner_files, tmp_path
):
    state = tmp_path / "config" / ".claude.json"
    owner = {"hasTrustDialogAccepted": True, "allowedTools": []}
    state.write_text(json.dumps({"numStartups": 1, "projects": {"/owner/app": owner}}))
    before = owner_files.record()
    root = tmp_path / "live"
    changed(state, lambda d: d.update(numStartups=2, newCache={"a": 1}))
    changed(state, lambda d: d["projects"].update({str(root / "w1"): {"lastSessionId": "x"}}))
    assert owner_files.changes(before, [str(root)]) == ["claude .claude.json: newCache changed"], (
        "a key no plain run changes is named, not its value"
    )
    assert owner_files.changes(before, [str(root)], {"newCache"}) == []
    assert owner_files.changed_keys(before) == ["newCache", "numStartups"]


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (lambda d, r: d.update(mcpServers={"x": {}}), "claude .claude.json: mcpServers changed"),
        (lambda d, r: d.update(oauthAccount={"x": 1}), "claude .claude.json: oauthAccount changed"),
        (
            lambda d, r: d["projects"].update({r: {"hasTrustDialogAccepted": True}}),
            "claude .claude.json: a project trusted",
        ),
        (
            lambda d, r: d["projects"]["/owner/app"].update(allowedTools=["Bash"]),
            "claude .claude.json: another project changed",
        ),
        (
            lambda d, r: d["projects"].pop("/owner/app"),
            "claude .claude.json: a project removed (theirs)",
        ),
    ],
    ids=["mcp servers", "account", "trust", "another project", "a project removed"],
)
def test_a_live_run_may_change_no_trust_project_server_or_account(
    owner_files, tmp_path, change, said
):
    state = tmp_path / "config" / ".claude.json"
    owner = {"hasTrustDialogAccepted": True, "allowedTools": []}
    state.write_text(json.dumps({"projects": {"/owner/app": owner}}))
    before = owner_files.record()
    root = tmp_path / "live"
    changed(state, lambda d: change(d, str(root / "w1")))
    assert owner_files.changes(before, [str(root)], {"mcpServers", "oauthAccount"}) == [said]
