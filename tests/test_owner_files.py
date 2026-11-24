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


def test_a_live_run_allows_claudes_own_caches_and_sync_marks(owner_files, tmp_path):
    """Claude's own housekeeping in any session of the owner's signed-in Claude (2.1.322): the
    hourly caches of their Consumer Terms answer and its config, of its model options and client
    data, and the mark of a skill sync round. A marketplace refreshed (no worker's process
    updates: DISABLE_AUTOUPDATER) is still a change."""
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
                "additionalModelOptionsAnsweredAt": 1,
                "clientDataCacheSlots": {"a": 1},
                "groveConfigCache": {"a": 1},
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
            additionalModelOptionsAnsweredAt=2,
            clientDataCacheSlots={"a": 2},
            groveConfigCache={"a": 2},
        ),
    )
    (synced / ".last-complete-round").write_text("2")
    assert owner_files.changes(before, [root]) == []
    assert owner_files.changes(before) == [
        "claude skills/synced/org_skill/.last-complete-round: changed"
    ], "outside a live run, byte for byte"
    (config / "plugins" / "known_marketplaces.json").write_text('{"m": {"lastUpdated": "2"}}')
    assert owner_files.changes(before, [root]) == [
        "claude plugins/known_marketplaces.json: changed"
    ]


def test_a_live_run_allows_claudes_skill_sync_and_no_other_skill_change(owner_files, tmp_path):
    """live r87 C: Claude Code's own sync of the owner's claude.ai skills, as a claude process
    starts, rewrote a bucket's manifest and a synced skill under ~/.claude/skills/synced/. A live
    run allows any change there (made, changed, removed); the owner's own skills and agents, and
    outside a live run the synced ones too, stay byte for byte."""
    config = tmp_path / "config"
    (config / ".claude.json").write_text(json.dumps({"projects": {}}))
    bucket = config / "skills" / "synced" / "bucket"
    (bucket / "skill-creator").mkdir(parents=True)
    (bucket / "manifest.json").write_text('{"v": 1}')
    (bucket / "skill-creator" / "SKILL.md").write_text("one")
    (bucket / "gone").mkdir()
    (bucket / "gone" / "SKILL.md").write_text("old")
    (config / "skills" / "mine").mkdir()
    (config / "skills" / "mine" / "SKILL.md").write_text("mine")
    (config / "agents").mkdir()
    (config / "agents" / "helper.md").write_text("helper")
    before = owner_files.record()
    root = str(tmp_path / "live")
    (bucket / "manifest.json").write_text('{"v": 2}')
    (bucket / "skill-creator" / "SKILL.md").write_text("two")
    (bucket / "gone" / "SKILL.md").unlink()
    (bucket / "new").mkdir()
    (bucket / "new" / "SKILL.md").write_text("new")
    assert owner_files.changes(before, [root]) == []
    assert owner_files.changes(before) == [
        "claude skills/synced/bucket/gone/SKILL.md: removed",
        "claude skills/synced/bucket/manifest.json: changed",
        "claude skills/synced/bucket/new/SKILL.md: made",
        "claude skills/synced/bucket/skill-creator/SKILL.md: changed",
    ], "outside a live run, byte for byte"
    (config / "skills" / "mine" / "SKILL.md").write_text("changed")
    (config / "skills" / "synced.md").write_text("not synced")
    (config / "agents" / "helper.md").write_text("changed")
    assert owner_files.changes(before, [root]) == [
        "claude agents/helper.md: changed",
        "claude skills/mine/SKILL.md: changed",
        "claude skills/synced.md: made",
    ]


def test_a_live_run_allows_claudes_own_profile_refresh_and_no_other_account_change(
    owner_files, tmp_path
):
    """f1fix-4: `oauthAccount` changed in the owner's .claude.json as the shard began: of its
    fields, only `profileFetchedAt`, when Claude (2.1.322) last fetched the owner's profile,
    which a plain `claude -p` refreshes on its own clock (F's calibration), with its
    `passesEligibilityCache`. Those are allowed; any other field of the account still fails."""
    state = tmp_path / "config" / ".claude.json"
    account = {"emailAddress": "a@acme.test", "billingType": "x", "profileFetchedAt": 1}
    state.write_text(
        json.dumps({"oauthAccount": account, "passesEligibilityCache": {"a": 1}, "projects": {}})
    )
    before = owner_files.record()
    root = str(tmp_path / "live")
    changed(state, lambda d: d.update(passesEligibilityCache={"a": 2}))
    changed(state, lambda d: d["oauthAccount"].update(profileFetchedAt=2))
    assert owner_files.changes(before, [root]) == []
    assert owner_files.changed_keys(before) == ["passesEligibilityCache"]
    for field in ("emailAddress", "billingType", "organizationUuid"):
        changed(state, lambda d, f=field: d["oauthAccount"].update({f: "other"}))
        assert owner_files.changes(before, [root], {"oauthAccount"}) == [
            "claude .claude.json: oauthAccount changed"
        ], field
        changed(state, lambda d: d.update(oauthAccount=dict(account, profileFetchedAt=3)))
