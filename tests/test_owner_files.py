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


def test_a_live_run_allows_claudes_own_caches_and_sync_marks_only(owner_files, tmp_path):
    """Claude's own housekeeping in any session of the owner's signed-in Claude (2.1.322): the
    hourly cache of their Consumer Terms answer and the mark of a skill sync round. A synced
    skill itself, or a marketplace refreshed (no worker's process updates: DISABLE_AUTOUPDATER),
    is still a change."""
    config = tmp_path / "config"
    state = config / ".claude.json"
    state.write_text(
        json.dumps(
            {
                "groveChoiceSeenAt": {"a": 1},
                "cachedExperimentData": {"a": 1},
                "cachedExperimentFeatures": {"a": 1},
                "cachedGrowthBookFeatures": {"a": 1},
                "cachedGrowthBookFeaturesAt": 1,
                "projects": {},
            }
        )
    )
    synced = config / "skills" / "synced" / "org_skill"
    synced.mkdir(parents=True)
    (synced / ".last-complete-round").write_text("1")
    (synced / "SKILL.md").write_text("one")
    (config / "plugins").mkdir()
    (config / "plugins" / "known_marketplaces.json").write_text('{"m": {"lastUpdated": "1"}}')
    before = owner_files.record()
    root = str(tmp_path / "live")
    changed(
        state,
        lambda d: d.update(
            groveChoiceSeenAt={"a": 2},
            cachedExperimentData={"a": 2},
            cachedExperimentFeatures={"a": 2},
            cachedGrowthBookFeatures={"a": 2},
            cachedGrowthBookFeaturesAt=2,
        ),
    )
    (synced / ".last-complete-round").write_text("2")
    assert owner_files.changes(before, [root]) == []
    assert owner_files.changes(before) == [
        "claude skills/synced/org_skill/.last-complete-round: changed"
    ], "outside a live run, byte for byte"
    (synced / "SKILL.md").write_text("two")
    (config / "plugins" / "known_marketplaces.json").write_text('{"m": {"lastUpdated": "2"}}')
    assert owner_files.changes(before, [root]) == [
        "claude plugins/known_marketplaces.json: changed",
        "claude skills/synced/org_skill/SKILL.md: changed",
    ]
