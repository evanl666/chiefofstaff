"""QA round 14 in Settings and on the command line: an away notice that did not go out is said
where the owner looks, and where notices go is the way, not one sending's receipt (E72); Send to
Chief's command is one that runs in the owner's terminal, also when Chief of Staff was started
with uvx (E63); no coding agent found, and nothing to prepare for a downgrade, said plainly
(E80)."""

import os
import sys
from pathlib import Path

import pytest
from conftest import Harness, call_tool, settle
from test_round6_drafts import away, spawn

from chiefofstaff import send
from chiefofstaff.__main__ import main
from chiefofstaff.models import Settings
from chiefofstaff.store import Store

RECEIPT = "Slack DM to the owner, queued message 1712345678.123456, trace 4bf92f3577b34da6"


def test_where_a_notice_went_is_the_way_without_its_receipt():
    from chiefofstaff.connector import channel_only

    for said, way in {
        RECEIPT: "Slack DM to the owner",
        "Slack DM from the acme-bot (ts 1712345678.123456)": "Slack DM from the acme-bot",
        "Slack DM (message id: 1a2b3c4d5e)": "Slack DM",
        "Slack DM in D0123ABCD": "Slack DM",
        "your own Slack DM": "your own Slack DM",
        "email to evan@acme.dev": "email to evan@acme.dev",
        "Teams chat 1:1 with you": "Teams chat 1:1 with you",
        # A review's: no piece of an id left, nor what led to one; the channel's own address
        # and name kept.
        "Email to evan@acme.dev (message-id <CAF123abc@mail.gmail.com>)": "Email to evan@acme.dev",
        "Teams chat with Evan, event id 19:abc123def@thread.v2": "Teams chat with Evan",
        "Slack DM, sent as message 1712345678.123456 in channel D07ABCDEF12": "Slack DM",
        "Slack DM, sent as message 1712… in channel D07ABCDEF12": "Slack DM",
        "Slack DM in D07ABCDEF12, from the acme-bot": "Slack DM, from the acme-bot",
        "Slack channel #ops-alerts-2024": "Slack channel #ops-alerts-2024",
        "SMS to +14155550100": "SMS to +14155550100",
        "SMS to +14155550100 (message id SM9f8e7d6c5b4a)": "SMS to +14155550100",
        "Email to evan@acme.dev, request id 0b7c2d0e-1f2a-4b3c-8d9e-0f1a2b3c4d5e": (
            "Email to evan@acme.dev"
        ),
        "Slack DM to you, 4bf92f3577b34da6": "Slack DM to you",
        "Slack DM to Ida2": "Slack DM to Ida2",
    }.items():
        assert channel_only(said) == way, said


async def notified(runtime, harness, **report):
    worker, _task = await spawn(runtime, harness, "Answer Bob about the release")
    await call_tool(runtime, "draft_reply", worker, text="Friday.", to="Bob", where="email")
    await runtime.connector.notify_away()
    await settle(runtime)
    messenger = runtime.toolset.last_special("messenger")
    await call_tool(runtime, "report_notified", messenger["id"], **report)
    harness.session(messenger["id"]).emit("session.idle")
    await settle(runtime)


async def test_an_away_notice_is_kept_as_its_way_and_a_failed_one_as_failed(tmp_path):
    """E72: Settings said "On · Slack DM to … queued message <n>, trace <id>" after a notice
    did not go out (its failure only inside the row, unfolded)."""
    harness = Harness(tmp_path / "data", Settings(away_notify=True, away_how="DM me"))
    runtime = await away(harness)
    await notified(runtime, harness, ok=True, where=RECEIPT, method="bot send", replies="")
    seen = runtime.connector.view()["away"]
    assert (seen["where"], seen["last"]["where"], seen["last"]["ok"]) == (
        "Slack DM to you",
        "Slack DM to you",
        True,
    )
    # Kept so: the messenger's next job and the Router's notes say the way, not a receipt.
    assert harness.store.get("away_where") == "Slack DM to you"
    await notified(runtime, harness, ok=False, where=RECEIPT, error="the bot could not sign in")
    last = runtime.connector.view()["away"]["last"]
    assert (last["ok"], last["where"], last["error"]) == (
        False,
        "Slack DM to you",
        "the bot could not sign in",
    )
    # As an earlier version kept it: shown as the way only.
    harness.store.set("away_where", RECEIPT)
    assert runtime.connector.view()["away"]["where"] == "Slack DM to you"


def installed(tmp_path: Path, *where: str) -> Path:
    for folder in where:
        script = tmp_path / folder / "chiefofstaff"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
    return tmp_path


@pytest.mark.skipif(os.name == "nt", reason="POSIX paths")
def test_send_to_chiefs_command_is_one_that_runs_in_the_owners_terminal(tmp_path, monkeypatch):
    """E63: started with uvx, there is no `chiefofstaff` in the owner's shell, and Settings and
    the guide said to run it."""
    root = installed(tmp_path, "cache/uv/archive-v0/abc/bin", "venv/bin", "tool/bin")
    throwaway = str(root / "cache/uv/archive-v0/abc")
    # uvx puts its environment's commands on the PATH it runs Chief of Staff with.
    monkeypatch.setenv("PATH", f"{throwaway}/bin{os.pathsep}/usr/bin")
    assert send.shell_command(throwaway) == ("uvx chiefofstaff", "uv tool install chiefofstaff")
    # Installed with `uv tool install`: on the owner's PATH.
    monkeypatch.setenv("PATH", f"{throwaway}/bin{os.pathsep}{root / 'tool/bin'}")
    assert send.shell_command(throwaway) == ("chiefofstaff", "")
    # Installed in an environment of its own, not on the PATH: by its full path.
    monkeypatch.setenv("PATH", "/usr/bin")
    venv = str(root / "venv")
    assert send.shell_command(venv) == (str(root / "venv/bin/chiefofstaff"), "")


async def test_the_page_has_its_command_only_when_it_is_not_chiefofstaff(tmp_path, monkeypatch):
    harness = Harness(tmp_path / "data", Settings())
    runtime = await harness.connect()
    assert "send_command" not in runtime.snapshot()
    runtime.send_command = ("uvx chiefofstaff", "uv tool install chiefofstaff")
    assert runtime.snapshot()["send_command"] == {
        "command": "uvx chiefofstaff",
        "install": "uv tool install chiefofstaff",
    }


def test_preparing_a_downgrade_with_no_other_agent_ever_used_says_so(tmp_path, monkeypatch, capsys):
    """E80: it said to close the workers on other agents when there never were any."""
    store = Store(tmp_path / "data")
    store.set("agent:w1", "copilot")
    monkeypatch.setattr(
        sys, "argv", ["chiefofstaff", "--prepare-downgrade", "--data-dir", str(store.directory)]
    )
    with pytest.raises(SystemExit) as ended:
        main()
    assert ended.value.code == 0
    assert capsys.readouterr().out.strip() == "Nothing to prepare: no workers ran on another agent."


async def test_no_coding_agent_found_says_which_are_supported(tmp_path, monkeypatch):
    """E80: "No supported agent found on this computer: the Codex CLI." The server says which
    are supported, and the page builds the line from it (test_round14_ui)."""
    harness = Harness(tmp_path / "data", Settings())
    runtime = await harness.connect()
    monkeypatch.setattr("shutil.which", lambda name, *args, **kwargs: None)
    found = await runtime.look_for_agents()
    assert found["agents"] == [] and found["supported"] == ["the Codex CLI"]
