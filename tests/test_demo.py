"""The demo scenario end to end: the real runtime with scripted sessions, no accounts."""

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from chiefofstaff import demo, sharing
from chiefofstaff.bus import Bus
from chiefofstaff.models import DraftDecision
from chiefofstaff.store import Store
from chiefofstaff.watchdog import WatchConfig


async def until(check, seconds=20):
    for _ in range(int(seconds / 0.1)):
        if result := check():
            return result
        await asyncio.sleep(0.1)
    raise AssertionError("timed out")


# Programs that reach out (the network, the owner's accounts, their agents, their desktop), each a
# stand-in first on the PATH that notes it ran: the demo says nothing leaves this machine.
REACHING_OUT = (
    "gh", "copilot", "codex", "claude", "osascript", "uv", "uvx", "pip", "pip3", "npm", "npx",
    "node", "go", "java", "keytool", "curl", "wget", "docker", "brew", "security", "open",
    "xdg-open", "notify-send", "terminal-notifier", "du",
)  # fmt: skip
# What the demo runs itself, on this machine only: git in its own project, ps to tell its
# sessions' processes.
ITS_OWN = {"git", "ps"}


@pytest.fixture
def programs(tmp_path, monkeypatch) -> list[str]:
    """Every program run while the test runs (its name), the stand-ins' too."""
    stand_ins = tmp_path / "stand-ins"
    stand_ins.mkdir()
    for name in REACHING_OUT:
        (stand_ins / name).write_text(f'#!/bin/sh\necho {name} >> "{stand_ins}/ran"\nexit 1\n')
        (stand_ins / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{stand_ins}{os.pathsep}{os.environ['PATH']}")
    ran: list[str] = []
    start = subprocess.Popen.__init__

    def spied(self, args, *more, **options):
        first = args.split()[0] if isinstance(args, str) else str(next(iter(args)))
        ran.append(Path(first).name)
        start(self, args, *more, **options)

    monkeypatch.setattr(subprocess.Popen, "__init__", spied)
    yield ran
    if (noted := stand_ins / "ran").exists():
        ran.extend(noted.read_text().split())


async def test_the_demo_scenario_plays_through(tmp_path, programs):
    project = demo.make_project(tmp_path / "projects")
    store = Store(tmp_path / "data")
    runtime = demo.DemoRuntime(
        store,
        Bus(),
        project,
        autoplay=False,
        watch=WatchConfig(interval=0.3, stall=3, long=600, cooldown=30),
        slack_delay=0,
    )
    await runtime.start()
    try:
        await runtime.owner_send(runtime.router_id(), demo.KICKOFF)
        tasks = await until(lambda: len(runtime.tasks.listed()) == 3 and runtime.tasks.listed())
        titles = {task["title"]: task for task in tasks}
        rate = titles["Rate-limit login attempts"]
        assert [dep["title"] for dep in runtime.tasks.deps(rate["id"])] == [
            "Fix the flaky login test"
        ]
        # Each on its own agent (Codex and Claude scripted too, needing nothing installed).
        on = {title: runtime.agent(task["session_id"]) for title, task in titles.items()}
        assert on == {
            "Fix the flaky login test": "copilot",
            "Rate-limit login attempts": "codex",
            "Compare feature-flag services": "claude",
        }

        question = await until(lambda: store.drafts() and store.drafts()[0])
        # The fix told the rate limiting what it changed, worker to worker.
        [told] = store.all("SELECT session_id, meta FROM messages WHERE kind='tell'")
        assert told["session_id"] == titles["Fix the flaky login test"]["session_id"]
        assert json.loads(told["meta"])["target"] == rate["session_id"]
        assert question["kind"] == "decision" and question["target"]["recommendation"]
        await runtime.drafts.resolve(
            question["id"], DraftDecision(action="answer", text="Per account")
        )

        runtime.slack.wake()
        draft = await until(lambda: next((d for d in store.drafts() if d["kind"] == "slack"), None))
        assert draft["target"]["to"] == "Maya Chen"
        await runtime.drafts.resolve(draft["id"], DraftDecision(action="send"))
        assert runtime.slack.mcp.sent[0]["channel_id"] == demo.MAYA_DM
        # The DM it answered is followed, and read back as Slack lists it (QA U3: its stamp
        # was "1", and every poll after failed: "could not convert string to float").
        assert [s["thread"] for s in runtime.followed("slack:")] == [f"slack:{demo.MAYA_DM}"]
        await runtime.slack.poll_once()

        done = await until(
            lambda: (
                all(t["status"] == "review" for t in runtime.tasks.listed())
                and runtime.tasks.listed()
            )
        )
        assert len(done) == 3
        reports = {t["title"]: t["result"] for t in done}
        assert "per account" in reports["Rate-limit login attempts"]
        nudged = store.all("SELECT text FROM messages WHERE kind='watchdog'")
        assert nudged, "the Watchdog unstuck the research worker"

        fix = runtime.task_actions.view(runtime.tasks.get(titles["Fix the flaky login test"]["id"]))
        await until(
            lambda: runtime.task_actions.view(runtime.tasks.get(fix["id"]))["receipts"]["checkouts"]
        )
        found = runtime.task_actions.view(runtime.tasks.get(fix["id"]))["receipts"]
        assert found["tests"]["passed"] and found["changed"]["commits"] == 1
        assert found["verdict"] == "verified" and found["tests"]["failed_before"]
        assert json.loads(json.dumps(found))  # The UI gets plain JSON.
        limited = runtime.task_actions.view(runtime.tasks.get(rate["id"]))["receipts"]
        assert [c["state"] for c in limited["criteria"]] == ["verified", "claimed"]

        # A teammate's review, drafted for the owner: past its first minute it is looked for on
        # GitHub, the demo's own (QA U4: the owner's gh read acme/matching as them).
        runtime.review_request()
        store.execute("UPDATE drafts SET created_at='2026-01-01T00:00:00+00:00'")
        await runtime.drafts.check_posted_by_owner(every=0)
        assert [d["status"] for d in store.drafts() if d["kind"] == "github"] == ["pending"]

        # A share link is sealed as for real, but nothing is uploaded: it opens in this demo
        # only, pasted on Home, and the dialog says so (QA U6: it promised a gist, and the
        # public viewer said "revoked").
        preview = await runtime.shares.preview(fix["id"])
        assert [account["login"] for account in preview["accounts"]] == ["you"]
        assert preview["demo"] is True
        shared = await runtime.shares.create(fix["id"], {}, preview["digest"], 7, "you")
        assert shared["demo"] is True and shared["share"]["state"] == "live"
        opened = await runtime.shares.open_link(shared["link"])
        assert opened["message"]["meta"]["shared"]["card"]["title"] == "Fix the flaky login test"
        await runtime.shares.revoke(shared["share"]["id"])
        with pytest.raises(ValueError, match="turned it off"):
            await runtime.shares.opened(shared["link"])
        found = sharing.parse_link(shared["link"])
        elsewhere = sharing.make_link("f" * 32, found["key"], shared["expires_at"])
        with pytest.raises(ValueError, match="only links made in this demo"):
            await runtime.shares.opened(elsewhere)  # (Never fetched from GitHub.)

        # The owner changes the decision: the finished rate limiting is redone and confirmed.
        await runtime.owner_send(
            runtime.router_id(), "Change of plan: limit login attempts per IP address only"
        )
        redone = await until(
            lambda: (
                (task := runtime.tasks.get(rate["id"]))["status"] == "review"
                and "per IP address only" in task["result"]
                and task
            )
        )
        assert runtime.tasks.runs(redone["id"])[-1]["reason"] == "decision changed"
        [impact] = store.all("SELECT status, target FROM drafts WHERE kind='change'")
        # Its worker confirmed, and its work changed (E123): the card stays for the owner.
        assert impact["status"] == "pending"
        assert all(item["ack"] for item in json.loads(impact["target"])["tasks"])
    finally:
        await runtime.stop()
    assert set(programs) <= ITS_OWN, f"the demo ran {sorted(set(programs) - ITS_OWN)}"


async def test_settings_shows_the_demos_codex_and_claude_ready(tmp_path, programs):
    """QA U7: Settings → Agents & models looked for Codex and Claude Code on this computer: "Not
    installed" (or, installed, it ran them and said "Can't run here") beside their scripted
    workers. In the demo they are the demo's: ready, scripted, nothing to sign in to."""
    project = demo.make_project(tmp_path / "projects")
    runtime = demo.DemoRuntime(Store(tmp_path / "data"), Bus(), project, autoplay=False)
    await runtime.start()
    try:
        looked = await runtime.look_for_agents()
    finally:
        await runtime.stop()
    rows = {row["name"]: row for row in looked["agents"]}
    assert set(rows) == {"codex", "claude"}
    for row in rows.values():
        assert row["found"] and row["enabled"] and not row["problem"], row
        assert row["status"] == "ready" and row["pays"] == demo.SCRIPTED, row
        assert [model["id"] for model in row["models"]] == ["demo-model"], row
    assert set(programs) <= ITS_OWN, f"the demo ran {sorted(set(programs) - ITS_OWN)}"
