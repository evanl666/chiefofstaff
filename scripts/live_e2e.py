"""Live end-to-end test against real Copilot sessions.

Starts Chief of Staff as a real process with a fake Slack MCP server (same tool surface as
Slack's official server) and drives it through the HTTP API, exactly like the UI does:

A. owner -> Router -> isolated worker -> commit -> its report reaches the owner
B. Slack DM -> watcher -> Router -> Slack session reads Slack -> draft -> owner edits+sends
E. Slack thread: a reply without a mention goes straight to the session that answered
C. restart -> Router resumes with its memory
D. other Copilot sessions are listed
G. watchdog reviews a worker stuck on a hung command and acts on it
H. two tasks in one message, the second depending on the first (after=)
I. switching the Router's model, usage per session/model/range, the daily summary
J. distilling the owner profile, reviewing and using it

Uses your signed-in Copilot CLI and consumes premium requests. All Copilot sessions it
creates are deleted at the end.

    uv run python scripts/live_e2e.py
"""

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from copilot import CopilotClient, RuntimeConnection

sys.path.insert(0, str(Path(__file__).parent))
from fake_oauth_mcp import forget_sign_ins, keychain_accounts
from fake_oauth_mcp import serve_in_thread as serve_docs
from fake_slack_mcp import Workspace, serve_in_thread

PORT = 18766
SLACK_PORT = 18901
SLACK_TOKEN = "xoxp-live-e2e"
BASE = f"http://127.0.0.1:{PORT}"
DASHBOARD = "https://deploy.acme.dev/dashboard"
STAGING = "https://staging.deploy.acme.dev/dashboard"
# A company "docs" MCP server that needs sign-in, available to workers.
DOCS_PORT = 18904
DOCS_ENV = {
    "CHIEFOFSTAFF_WORKER_MCP": json.dumps(
        {"docs": {"type": "http", "url": f"http://127.0.0.1:{DOCS_PORT}/mcp", "tools": ["*"]}}
    )
}
# Short watchdog thresholds so a hung worker is reviewed within a minute.
WATCHDOG_ENV = {
    "CHIEFOFSTAFF_WATCHDOG_INTERVAL": "5",
    "CHIEFOFSTAFF_WATCHDOG_STALL": "30",
    "CHIEFOFSTAFF_WATCHDOG_COOLDOWN": "600",
    # A low limit, so the Router is rotated during the run and the scenarios after it must pass.
    "CHIEFOFSTAFF_ROUTER_CONTEXT": "45000",
}


def log(text: str):
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


class App:
    def __init__(self, data: Path, logfile: Path, extra_env: dict | None = None):
        self.data = data
        self.logfile = logfile
        self.extra_env = extra_env or {}
        self.process: subprocess.Popen | None = None
        self.http: httpx.AsyncClient | None = None

    async def start(self):
        env = {
            **os.environ,
            "CHIEFOFSTAFF_SLACK_MCP_URL": f"http://127.0.0.1:{SLACK_PORT}/mcp",
            "CHIEFOFSTAFF_SLACK_USER_TOKEN": SLACK_TOKEN,
            **WATCHDOG_ENV,
            **self.extra_env,
        }
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "chiefofstaff",
                "--no-browser",
                "--port",
                str(PORT),
                "--data-dir",
                str(self.data),
            ],
            stdout=subprocess.PIPE,
            stderr=self.logfile.open("a"),
            env=env,
            text=True,
        )
        token = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = await asyncio.to_thread(self.process.stdout.readline)
            if "Private launch URL" in line:
                token = line.split("#token=", 1)[1].strip()
                break
            if not line and self.process.poll() is not None:
                break
        if not token:
            raise RuntimeError("Chief of Staff did not start; see " + str(self.logfile))
        self.launch_token = token
        self.http = httpx.AsyncClient(base_url=BASE, headers={"origin": BASE}, timeout=60)
        for _ in range(100):
            try:
                if (await self.http.post("/api/auth", json={"token": token})).status_code == 200:
                    return
            except httpx.ConnectError:
                await asyncio.sleep(0.2)
        raise RuntimeError("Could not sign in.")

    async def stop(self):
        if self.http:
            await self.http.aclose()
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            await asyncio.to_thread(self.process.wait, 30)

    async def get(self, path: str):
        response = await self.http.get(path)
        response.raise_for_status()
        return response.json()

    async def post(self, path: str, body=None):
        response = await self.http.post(path, json=body)
        if response.status_code >= 400:
            raise RuntimeError(f"{path}: {response.status_code} {response.text}")
        return response.json()


async def wait_for(what: str, check, timeout: float, app: App | None = None):
    log(f"waiting: {what}")
    started = time.monotonic()
    last = ""
    while time.monotonic() - started < timeout:
        result = await check()
        if result:
            log(f"ok: {what} ({time.monotonic() - started:.0f}s)")
            return result
        if app:
            state = await app.get("/api/state")
            summary = ", ".join(
                f"{s['role']}:{s['title'][:24]}={s['status']}" for s in state["sessions"]
            )
            if summary != last:
                log(f"  sessions: {summary}")
                last = summary
        await asyncio.sleep(3)
    raise TimeoutError(f"Timed out waiting for: {what}")


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def transcript(messages: list[dict]) -> str:
    return "\n".join(f"    {m['kind']:>9}: {m['text'][:160]!r}" for m in messages)


async def scenario_worker(app: App, project: Path) -> dict:
    state = await app.get("/api/state")
    router = state["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": f"In the project {project}, add a file hello.txt containing exactly the line "
            "`hello from a worker` and commit it with the message 'Add hello'. Use an isolated "
            "worktree so main stays untouched. Tell me when it is done."
        },
    )

    async def worker_done():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        started = [
            m["meta"]["session_id"]
            for m in messages
            if m["kind"] == "notice" and m["text"].startswith("Started")
        ]
        workers = [await app.get(f"/api/sessions/{sid}") for sid in started]
        done = [w for w in workers if w["status"] == "done"]
        reports = [m for m in messages if done and m["meta"].get("from") == done[0]["id"]]
        return (done[0], messages) if done and reports else None

    worker, messages = await wait_for(
        "worker done + its report reaches the owner", worker_done, 900, app
    )
    await asyncio.sleep(10)
    messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
    report = next(i for i, m in enumerate(messages) if m["meta"].get("from") == worker["id"])
    after = [m for m in messages[report:] if m["kind"] == "assistant"]
    assert all(m["meta"].get("internal") for m in after), "event-turn replies must be footnotes"
    restated = [
        m for m in messages[report:] if m["kind"] == "update" and not m["meta"].get("needs_action")
    ]
    assert not restated, f"the report is the message; the Router does not restate it: {restated}"
    reported_text = messages[report]["text"]
    assert worker["branch"].startswith("chief/"), worker
    tree = Path(worker["cwd"])
    assert (tree / "hello.txt").read_text().strip() == "hello from a worker"
    assert "Add hello" in git(tree, "log", "--format=%s", "-1", worker["branch"])
    assert not (project / "hello.txt").exists(), "main checkout must stay untouched"
    assert git(project, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    log(f"worker branch {worker['branch']} has the commit; report: {reported_text!r}")
    tasks = (await app.get("/api/state"))["tasks"]
    task = next(t for t in tasks if t["session_id"] == worker["id"])
    found = task["receipts"] or {}
    criteria = [(c["state"], c["criterion"]) for c in found.get("criteria", [])]
    log(f"acceptance criteria: {criteria}; verdict {found.get('verdict')!r}")
    for item in found.get("criteria", []):
        how = f"{item['command']!r} (exit {item['exit_code']})"
        log(f"  {item['state']}: evidence {item['evidence']!r} -> {how}")
    for command in found.get("commands", []):
        log(f"  ran (exit {command['exit_code']}): {command['command'][:300]}")
    return worker


async def scenario_reopen(app: App, worker: dict):
    """A follow-up on a closed worker's task goes back to that worker, with its checkout."""
    state = await app.get("/api/state")
    router = state["router_id"]
    await app.post(f"/api/sessions/{worker['id']}/close")
    assert (await app.get(f"/api/sessions/{worker['id']}"))["closed_at"]
    await wait_for("its worktree is removed", lambda: gone(Path(worker["cwd"])), 60)
    assert worker["id"] in [s["id"] for s in (await app.get("/api/state"))["closed"]]
    mark = (await app.get(f"/api/sessions/{router}/messages"))[-1]["id"]
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": "Follow-up on the hello.txt task from earlier: append the line `second line` "
            "to hello.txt and commit it with the message 'Add second line'."
        },
    )
    tree = Path(worker["cwd"])
    # Branches live in the repository; the worktree itself is gone while the worker is closed.
    repo = Path(worker["checkouts"][0]["repo"])
    assert not tree.exists(), "a closed worker's clean worktree is removed"

    async def committed():
        if "Add second line" not in git(repo, "log", "--format=%s", "-1", worker["branch"]):
            return None
        row = await app.get(f"/api/sessions/{worker['id']}")
        return row if row["status"] == "done" else None

    row = await wait_for("the closed worker reopens and commits", committed, 600, app)

    async def reported():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        return any(m["meta"].get("from") == worker["id"] for m in messages)

    await wait_for("the follow-up's report reaches the owner", reported, 300, app)
    assert not row["closed_at"]
    messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
    started = [m for m in messages if m["kind"] == "notice" and m["text"].startswith("Started")]
    assert not started, f"no new worker for a follow-up: {started}"
    assert any(m["text"].startswith("Reopened") for m in messages), transcript(messages)
    assert (tree / "hello.txt").read_text().splitlines() == ["hello from a worker", "second line"]
    log("follow-up reopened the same worker and it committed in its worktree")

    # A different question about the same project goes to the worker that knows it.
    await app.post(f"/api/sessions/{worker['id']}/close")
    mark = (await app.get(f"/api/sessions/{router}/messages"))[-1]["id"]
    await app.post(
        f"/api/sessions/{router}/messages",
        {"text": "How many commits does main have in the demo project? Just tell me the number."},
    )

    async def routed():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        if any(m["kind"] == "notice" and m["text"].startswith("Started") for m in messages):
            return messages
        reported = any(
            m["kind"] == "inbound" and m["meta"].get("from") == worker["id"] for m in messages
        )
        # The Router may know the answer already from that worker's reports.
        answered = not any(m["text"].startswith("Reopened") for m in messages) and any(
            m["kind"] == "assistant" for m in messages
        )
        return messages if reported or answered else None

    messages = await wait_for("a related question is routed", routed, 600, app)
    started = [m for m in messages if m["kind"] == "notice" and m["text"].startswith("Started")]
    assert not started, f"related question started a new worker: {transcript(messages)}"
    if any(m["text"].startswith("Reopened") for m in messages):
        reused = ("Continued in", "New task")  # Continued its task, or gave it a new one.
        assert any(m["text"].startswith(reused) for m in messages), transcript(messages)
        log("a related question went to the worker that already knew the project")
    else:
        replies = " ".join(m["text"] for m in messages if m["kind"] == "assistant")
        assert re.search(r"\b1\b|\bone\b", replies, re.I), transcript(messages)
        log(f"the Router answered from what that worker had reported: {replies[:80]!r}")


async def scenario_slack(app: App, workspace: Workspace):
    async def slack_ready():
        state = await app.get("/api/state")
        sessions = [s for s in state["sessions"] if s["role"] == "slack"]
        return sessions[0] if sessions and state["slack"]["connected"] else None

    slack = await wait_for("Slack session and watcher ready", slack_ready, 120, app)
    workspace.post("C0DEPLOYS", "U0BOB001", f"Deploy dashboard lives here: {DASHBOARD} (pinned)")
    await asyncio.sleep(2)
    dm = workspace.post(
        "D0ALICE01",
        "U0ALICE1",
        "Hey! Could you send me the deploy dashboard link? Bob posted it in #deploys a while ago.",
    )
    log(f"Alice sent a DM (ts {dm['ts']})")
    await app.post("/api/settings", {**(await app.get("/api/state"))["settings"]})

    async def draft_ready():
        drafts = (await app.get("/api/state"))["drafts"]
        return drafts[0] if drafts else None

    draft = await wait_for("Slack reply drafted", draft_ready, 600, app)
    log(f"draft: {draft['text']!r} -> {draft['target']}")
    assert draft["kind"] == "slack" and draft["target"]["channel_id"] == "D0ALICE01", draft
    assert DASHBOARD in draft["text"], draft["text"]
    reads = [
        name for name, _ in workspace.calls if name in {"slack_read_channel", "slack_read_thread"}
    ]
    searches = [
        a["query"] for name, a in workspace.calls if name == "slack_search_public_and_private"
    ]
    assert reads or any(q not in {"to:me", "<@U0OWNER1>"} for q in searches), workspace.calls
    assert not workspace.sent, "nothing may be sent before approval"

    edited = f"Here you go: {DASHBOARD} 🙂"
    sent = await app.post(f"/api/drafts/{draft['id']}", {"action": "send", "text": edited})
    assert sent["status"] == "sent", sent
    assert [(m["channel"], m["text"]) for m in workspace.sent] == [("D0ALICE01", edited)]
    if draft["target"]["thread_ts"]:
        assert workspace.sent[0]["thread_ts"] == draft["target"]["thread_ts"]
    sends = [a for name, a in workspace.calls if name == "slack_send_message"]
    assert len(sends) == 1, "only the approved send may reach Slack"

    async def told_about_edit():
        messages = await app.get(f"/api/sessions/{slack['id']}/messages")
        return any("edited draft" in m["text"] for m in messages)

    await wait_for("Slack session told about the edit", told_about_edit, 30)
    await asyncio.sleep(45)
    router = (await app.get("/api/state"))["router_id"]
    inbound = [
        m
        for m in await app.get(f"/api/sessions/{router}/messages")
        if m["kind"] == "inbound" and m["meta"].get("source") == "slack"
    ]
    assert len(inbound) == 1, f"the DM must reach the Router exactly once, got {len(inbound)}"
    log("Slack round trip verified; the DM reached the Router once")


async def pending_draft(app: App, exclude: set[str]):
    drafts = [d for d in (await app.get("/api/state"))["drafts"] if d["id"] not in exclude]
    return drafts[0] if drafts else None


async def scenario_thread(app: App, workspace: Workspace):
    state = await app.get("/api/state")
    router = state["router_id"]
    slack = next(s for s in state["sessions"] if s["role"] == "slack")
    workspace.post("C0DEPLOYS", "U0BOB001", f"Staging dashboard: {STAGING} (for testing)")
    await asyncio.sleep(1)
    root = workspace.post(
        "C0DEPLOYS",
        "U0BOB001",
        f"<@U0OWNER1> quick check: is the deploy dashboard still {DASHBOARD}?",
    )
    log(f"Bob mentioned the owner in #deploys (ts {root['ts']})")
    first = await wait_for("reply drafted for Bob", lambda: pending_draft(app, set()), 600, app)
    assert first["target"]["channel_id"] == "C0DEPLOYS", first
    await app.post(f"/api/drafts/{first['id']}", {"action": "send"})
    sent = workspace.sent[-1]
    thread_root = first["target"]["thread_ts"] or sent["ts"]
    follows = (await app.get(f"/api/sessions/{slack['id']}"))["follows"]
    assert f"slack:C0DEPLOYS:{thread_root}" in follows, follows

    def router_slack_events(messages):
        return [
            m for m in messages if m["kind"] == "inbound" and m["meta"].get("source") == "slack"
        ]

    before = len(router_slack_events(await app.get(f"/api/sessions/{router}/messages")))
    router_sent_before = len(await app.get(f"/api/sessions/{router}/messages"))
    workspace.post(
        "C0DEPLOYS", "U0BOB001", "Thanks! Is there also a staging dashboard?", thread_root
    )
    log("Bob replied in the thread without mentioning the owner")
    second = await wait_for(
        "follow-up drafted without the Router", lambda: pending_draft(app, {first["id"]}), 600, app
    )
    assert second["target"]["channel_id"] == "C0DEPLOYS", second
    assert STAGING in second["text"], second["text"]
    router_messages = await app.get(f"/api/sessions/{router}/messages")
    assert len(router_slack_events(router_messages)) == before, "the Router was not involved"
    new_router = router_messages[router_sent_before:]
    assert any(
        m["kind"] == "notice" and m["text"].startswith("Slack thread in C0DEPLOYS")
        for m in new_router
    ), new_router
    assert not [m for m in new_router if m["kind"] == "assistant"], "no Router turn"
    slack_messages = await app.get(f"/api/sessions/{slack['id']}/messages")
    assert any(m["meta"].get("thread") == f"slack:C0DEPLOYS:{thread_root}" for m in slack_messages)
    await app.post(f"/api/drafts/{second['id']}", {"action": "send"})
    assert workspace.sent[-1]["thread_ts"] == thread_root
    log(f"thread follow-up answered directly: {second['text']!r}")


async def scenario_delegate(app: App, workspace: Workspace):
    """The owner reacts with the delegate emoji to a message that was not addressed to them."""
    router = (await app.get("/api/state"))["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    request = workspace.post(
        "C0DEPLOYS",
        "U0BOB001",
        "Could someone work out 17 times 23 and write the result into answer.txt? "
        "Tell me here when it's done.",
    )
    workspace.react(request, "U0OWNER1", "robot_face")
    log(f"the owner reacted :robot_face: to Bob's request in #deploys (ts {request['ts']})")

    async def answered():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        for message in messages:
            if message["kind"] == "notice" and message["text"].startswith("Started"):
                worker = await app.get(f"/api/sessions/{message['meta']['session_id']}")
                if (Path(worker["cwd"]) / "answer.txt").exists():
                    return messages, worker
        return None

    messages, worker = await wait_for("a worker handles the delegated request", answered, 600, app)
    assert "391" in (Path(worker["cwd"]) / "answer.txt").read_text()
    delegated = [m for m in messages if m["kind"] == "inbound" and m["meta"].get("delegated")]
    assert len(delegated) == 1, messages

    async def reply():
        drafts = (await app.get("/api/state"))["drafts"]
        return next(
            (
                d
                for d in drafts
                if d["kind"] == "slack" and d["target"]["channel_id"] == "C0DEPLOYS"
            ),
            None,
        )

    draft = await wait_for("a reply to Bob in his thread", reply, 600, app)
    assert draft["target"]["thread_ts"] == request["ts"], draft["target"]
    assert "391" in draft["text"], draft["text"]
    await app.post(f"/api/drafts/{draft['id']}", {"action": "send"})
    assert workspace.sent[-1]["thread_ts"] == request["ts"]
    log(f"delegated by reaction, done and answered in the thread: {draft['text']!r}")


async def scenario_watchdog(app: App, project: Path):
    # A long, quiet test run: the worker has to wait, so the Watchdog sees a silent worker.
    (project / "run_tests.sh").write_text(
        "#!/bin/sh\necho 'Running 840 integration tests against staging (about 3 minutes)...'\n"
        "sleep 180\necho '840 passed'\n"
    )
    (project / "run_tests.sh").chmod(0o755)
    git(project, "add", ".")
    git(project, "-c", "user.email=e2e@test", "-c", "user.name=e2e", "commit", "-qm", "tests")
    state = await app.get("/api/state")
    router = state["router_id"]
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": f"In {project}, run ./run_tests.sh (the suite takes about three minutes; let "
            "it finish) and tell me whether the tests pass. Do not change any files."
        },
    )

    async def assessed():
        sessions = (await app.get("/api/state"))["sessions"]
        watchdog = next((s for s in sessions if s["role"] == "watchdog"), None)
        if not watchdog:
            return None
        notes = [
            m
            for m in await app.get(f"/api/sessions/{watchdog['id']}/messages")
            if m["kind"] == "notice" and m["meta"].get("session_id")
        ]
        return (watchdog, notes[0]) if notes else None

    watchdog, verdict = await wait_for("watchdog review and verdict", assessed, 600, app)
    worker_id = verdict["meta"]["session_id"]
    log(f"watchdog verdict: {verdict['text']!r}")
    reviews = [
        m
        for m in await app.get(f"/api/sessions/{watchdog['id']}/messages")
        if m["kind"] == "inbound" and m["meta"].get("review") == worker_id
    ]
    assert reviews, "the verdict follows a review packet"
    action = verdict["text"].split(", ")[1].split(".")[0]
    if action in {"nudge", "interrupt"}:
        worker_messages = await app.get(f"/api/sessions/{worker_id}/messages")
        assert any(m["kind"] == "watchdog" for m in worker_messages), "nudge delivered"
    elif action == "escalate":
        router_messages = await app.get(f"/api/sessions/{router}/messages")
        assert any(m["meta"].get("status", "").startswith("watchdog") for m in router_messages)
    log(f"watchdog acted: {action}")
    await app.post(f"/api/sessions/{worker_id}/close")


async def scenario_dependencies(app: App, project: Path):
    router = (await app.get("/api/state"))["router_id"]
    before = len(await app.get(f"/api/sessions/{router}/messages"))
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": f"Two tasks in {project}: (1) add a file VERSION containing exactly 1.4.2 and "
            "commit it; (2) once that is done, add CHANGELOG.md whose first line is "
            "'# <the version from task 1>' and commit it. Use isolated worktrees."
        },
    )

    async def both_done():
        messages = (await app.get(f"/api/sessions/{router}/messages"))[before:]
        started = [
            m["meta"]["session_id"]
            for m in messages
            if m["kind"] == "notice" and m["text"].startswith("Started")
        ]
        workers = [await app.get(f"/api/sessions/{sid}") for sid in started]
        if len(workers) >= 2 and all(w["status"] == "done" for w in workers[:2]):
            return workers[:2]
        if len(workers) == 1 and workers[0]["status"] == "done":
            tree = Path(workers[0]["cwd"])
            if (tree / "CHANGELOG.md").exists():
                return workers  # The Router gave both steps to one worker, in order.
        return None

    done = await wait_for("both dependent tasks done", both_done, 900, app)
    if len(done) == 1:
        tree = Path(done[0]["cwd"])
        assert (tree / "VERSION").read_text().strip() == "1.4.2"
        assert (tree / "CHANGELOG.md").read_text().splitlines()[0].strip() == "# 1.4.2"
        order = git(tree, "log", "--format=%s", "--reverse", "main..HEAD")
        log(f"one worker did both steps in order (the Router's choice): {order.splitlines()}")
        return
    first, second = done
    transcript = await app.get(f"/api/sessions/{second['id']}/messages")
    assert any("Results of the tasks this one waited for" in m["text"] for m in transcript), (
        "the second task started from the first one's result"
    )
    changelog = Path(second["cwd"]) / "CHANGELOG.md"
    assert changelog.read_text().splitlines()[0].strip() == "# 1.4.2", changelog.read_text()
    log(f"dependency chain: “{first['title']}” → “{second['title']}”; CHANGELOG starts with 1.4.2")


async def scenario_workspace(app: App, root: Path):
    """A folder that is not a configured project: the Router asks, the owner picks it."""
    other = root / "other-project"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    (other / "README.md").write_text("# Other project\n")
    git(other, "add", ".")
    git(other, "-c", "user.email=e2e@test", "-c", "user.name=e2e", "commit", "-qm", "init")
    router = (await app.get("/api/state"))["router_id"]
    mark = (await app.get(f"/api/sessions/{router}/messages"))[-1]["id"]
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": f"In {other}, add a file notes.txt containing exactly `from the folder test` "
            "and commit it with the message 'Add notes'. Use an isolated worktree."
        },
    )

    async def asked():
        drafts = (await app.get("/api/state"))["drafts"]
        return next((d for d in drafts if d["kind"] == "workspace"), None)

    question = await wait_for("the Router asks which folder", asked, 300, app)
    paths = [option["path"] for option in question["target"]["options"]]
    assert str(other) in paths, paths
    await app.post(f"/api/drafts/{question['id']}", {"action": "choose", "choice": str(other)})
    settings = (await app.get("/api/state"))["settings"]
    assert str(other) in settings["projects"], settings["projects"]

    async def committed():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        for message in messages:
            if message["kind"] == "notice" and message["text"].startswith("Started"):
                worker = await app.get(f"/api/sessions/{message['meta']['session_id']}")
                if worker["status"] == "done" and worker["branch"]:
                    log_line = git(other, "log", "--format=%s", "-1", worker["branch"])
                    if "Add notes" in log_line:
                        return worker
        return None

    worker = await wait_for("the worker commits in the chosen folder", committed, 600, app)
    assert (Path(worker["cwd"]) / "notes.txt").read_text().strip() == "from the folder test"
    assert not (other / "notes.txt").exists(), "main checkout stays untouched"
    log(f"Router asked for the folder; worker committed on {worker['branch']} in {other.name}")


async def scenario_setup(app: App):
    """A new ability: a setup worker finds the company docs tool, the owner signs in once."""
    router = (await app.get("/api/state"))["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": "Set up access to our company docs so workers can read them, then tell me "
            "the launch date written in doc 42."
        },
    )

    async def asked():
        drafts = (await app.get("/api/state"))["drafts"]
        return next((d for d in drafts if d["kind"] == "signin"), None)

    card = await wait_for("a sign-in card", asked, 300, app)
    assert card["target"]["server"] == "docs", card
    # The owner signs in: the browser follows the authorization URL back to Copilot.
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as http:
        assert (await http.get(card["target"]["url"])).status_code == 200

    async def closed():
        drafts = (await app.get("/api/state"))["drafts"]
        return not any(d["id"] == card["id"] for d in drafts)

    await wait_for("the sign-in card closes by itself", closed, 60, app)

    async def answered():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        dates = ("Nov 18", "November 18", "11/18", "11-18")
        found = [
            m
            for m in messages
            if (m["kind"] == "update" or m["meta"].get("from"))
            and any(d in m["text"] for d in dates)
        ]
        return messages if found else None

    await wait_for("the launch date from the doc", answered, 600, app)
    notes = (await app.get("/api/state"))["setup_notes"]
    assert any("docs" in f"{topic} {entry['note']}".lower() for topic, entry in notes.items()), (
        notes
    )
    log(f"signed in once; the doc was read; environment notes: {list(notes)}")


async def scenario_ask_user(app: App):
    """A worker asks the owner directly and waits for the answer."""
    router = (await app.get("/api/state"))["router_id"]
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": "Start a worker that asks me directly (with ask_user) whether the greeting "
            "should be 'hi' or 'hello', then writes my choice into greeting.txt in its own "
            "folder and reports done."
        },
    )

    async def asked():
        drafts = (await app.get("/api/state"))["drafts"]
        return next((d for d in drafts if d["kind"] == "question"), None)

    question = await wait_for("the worker's question", asked, 300, app)
    worker_id = question["session_id"]
    assert (await app.get(f"/api/sessions/{worker_id}"))["status"] == "waiting"
    await app.post(f"/api/drafts/{question['id']}", {"action": "answer", "text": "hello"})

    async def written():
        worker = await app.get(f"/api/sessions/{worker_id}")
        path = Path(worker["cwd"]) / "greeting.txt"
        return worker if worker["status"] == "done" and path.exists() else None

    worker = await wait_for("the answer is used", written, 300, app)
    assert (Path(worker["cwd"]) / "greeting.txt").read_text().strip() == "hello"
    log("the worker asked the owner directly and used the answer")


async def scenario_ask_session(app: App):
    """A worker asks another worker and gets the answer back."""
    router = (await app.get("/api/state"))["router_id"]

    async def started_after(mark, done=True):
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        for message in messages:
            if message["kind"] == "notice" and message["text"].startswith("Started"):
                worker = await app.get(f"/api/sessions/{message['meta']['session_id']}")
                if worker["status"] == "done" or not done:
                    return worker
        return None

    mark = (await app.get(f"/api/sessions/{router}/messages"))[-1]["id"]
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": "Start a worker titled 'Keeper'. Tell it: the code word is 'pineapple'; it "
            "only needs to remember it and report done."
        },
    )
    keeper = await wait_for("the Keeper worker", lambda: started_after(mark), 300, app)
    mark = (await app.get(f"/api/sessions/{router}/messages"))[-1]["id"]
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": f"Now start a separate new worker, not the Keeper ({keeper['id']}), that asks "
            "the Keeper for the code word with ask_session and writes the word into code.txt in "
            "its own folder. Do not tell it the word yourself."
        },
    )

    async def written():
        worker = await started_after(mark)
        if worker and (Path(worker["cwd"]) / "code.txt").exists():
            return worker
        return None

    worker = await wait_for("the code word from the Keeper", written, 600, app)
    assert "pineapple" in (Path(worker["cwd"]) / "code.txt").read_text()
    messages = await app.get(f"/api/sessions/{worker['id']}/messages")
    assert any(m["kind"] == "ask" and m["meta"]["target"] == keeper["id"] for m in messages)
    assert any(m["meta"].get("answered_by") == keeper["id"] for m in messages)
    log("one worker asked another and used its answer")


def published_repo(root: Path, name: str) -> tuple[Path, Path]:
    """A repository with an origin, and a local clone that is one commit behind it."""
    origin = root / f"{name}.git"
    git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    local = root / name
    git(root, "clone", "-q", str(origin), str(local))
    for repo, file in ((local, "README.md"), (root / f"{name}-other", "LATEST.md")):
        if repo != local:
            git(root, "clone", "-q", str(origin), str(repo))
        else:
            git(repo, "checkout", "-q", "-b", "main")
        (repo / file).write_text(f"{name} {file}\n")
        git(repo, "add", ".")
        git(repo, "-c", "user.email=e2e@test", "-c", "user.name=e2e", "commit", "-qm", file)
        git(repo, "push", "-q", "origin", "main")
    return origin, local


async def scenario_repos(app: App, root: Path):
    """One task across two local projects and one repository that is only on a remote, in yolo."""
    _, api = published_repo(root, "api")
    _, web = published_repo(root, "web")
    lib, _ = published_repo(root, "lib")
    state = await app.get("/api/state")
    settings = {**state["settings"], "worker_yolo": True}
    settings["projects"] = [*settings["projects"], str(api), str(web)]
    await app.post("/api/settings", settings)
    router = state["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": f"One task across three repositories, for a single worker: in the {api} and "
            f"{web} projects and in the library at file://{lib} (not on this machine), add a "
            "file CHANGE.md containing exactly `renamed user to account` and commit it with the "
            "message 'Rename user' in each. Use isolated worktrees."
        },
    )

    async def done():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        for message in messages:
            if message["kind"] == "notice" and message["text"].startswith("Started"):
                worker = await app.get(f"/api/sessions/{message['meta']['session_id']}")
                if worker["status"] == "done" and len(worker["checkouts"]) >= 3:
                    return worker
        return None

    worker = await wait_for("one worker commits in three repositories", done, 900, app)
    assert worker["yolo"], "workers follow the yolo setting"
    for checkout in worker["checkouts"]:
        tree = Path(checkout["path"])
        assert (tree / "CHANGE.md").read_text().strip() == "renamed user to account", checkout
        assert "Rename user" in git(tree, "log", "--format=%s", "-1"), checkout
        assert (tree / "LATEST.md").exists(), f"started from origin's latest: {checkout}"
        assert git(tree, "branch", "--show-current").strip() == worker["branch"]
    assert {Path(c["path"]).name for c in worker["checkouts"]} >= {"api", "web", "lib"}
    assert not (api / "CHANGE.md").exists() and not (web / "CHANGE.md").exists()
    settings["worker_yolo"] = False
    await app.post("/api/settings", settings)
    log(f"one worker, {len(worker['checkouts'])} checkouts on {worker['branch']}, in yolo")


async def scenario_added_repo_sandboxed(app: App, root: Path):
    """A sandboxed worker adds a project mid-task; it commits there after being reattached."""
    _, api = published_repo(root, "orders")
    _, web = published_repo(root, "shop")
    state = await app.get("/api/state")
    # The sandbox is a choice (off by default): this worker runs in it.
    settings = {**state["settings"], "worker_yolo": False, "worker_sandbox": True}
    settings["projects"] = [*settings["projects"], str(api), str(web)]
    await app.post("/api/settings", settings)
    router = state["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": f"Start one worker with only the {api} project, in an isolated worktree. "
            "Tell it: add a file CHANGE.md containing exactly `renamed user to account` and "
            f"commit it with the message 'Rename user'; then add the {web} project with "
            "add_repo and do the same there. Do not give it the second project at the start."
        },
    )

    async def done():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        for message in messages:
            if message["kind"] == "notice" and message["text"].startswith("Started"):
                worker = await app.get(f"/api/sessions/{message['meta']['session_id']}")
                if worker["status"] == "done" and len(worker["checkouts"]) >= 2:
                    return worker
        return None

    worker = await wait_for("a sandboxed worker commits in a project it added", done, 900, app)
    assert not worker["yolo"]
    for checkout in worker["checkouts"]:
        tree = Path(checkout["path"])
        assert (tree / "CHANGE.md").read_text().strip() == "renamed user to account", checkout
        assert "Rename user" in git(tree, "log", "--format=%s", "-1"), checkout
    for project in (api, web):
        assert git(project, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
        assert not (project / "CHANGE.md").exists()
    messages = await app.get(f"/api/sessions/{worker['id']}/messages")
    went_on = [m for m in messages if "Go on with the task" in m["text"]]
    log(f"sandboxed worker added a project and committed in both; reattached: {bool(went_on)}")
    await app.post("/api/settings", {**(await app.get("/api/state"))["settings"],
                                     "worker_sandbox": False})  # fmt: skip


# A private repository that only gh's non-active account can see, like a company's: create it
# under your second account, with an open PR #1 that adds `mean(xs)` dividing by len(xs).
COMPANY_REPO = os.environ.get(
    "CHIEFOFSTAFF_E2E_COMPANY_REPO", "evan666-lgtm/cos-account-routing-test"
)


def second_account_token() -> str:
    owner = COMPANY_REPO.split("/")[0]
    found = subprocess.run(
        ["gh", "auth", "token", "--hostname", "github.com", "--user", owner],
        capture_output=True, text=True,
    )  # fmt: skip
    return found.stdout.strip() if found.returncode == 0 else ""


async def scenario_company_review(app: App):
    """The owner asks for a review by the repository's short name; only the non-active gh
    account can see it. The worker reads the PR, drafts a review with line comments, and the
    approved review is published through that account."""
    token = second_account_token()
    if not token:
        log(f"SKIPPED company review: no gh account {COMPANY_REPO.split('/')[0]} signed in")
        return
    state = await app.get("/api/state")
    router = state["router_id"]
    before = {d["id"] for d in state["drafts"]}
    name = COMPANY_REPO.split("/")[1]
    await app.post(
        f"/api/sessions/{router}/messages",
        {"text": f"Review {name} PR #1 and draft the review for me, with comments on the lines "
                 "that need them."},
    )  # fmt: skip

    async def drafted():
        draft = await pending_draft(app, before)
        return draft if draft and draft["kind"] == "github" else None

    draft = await wait_for("the company PR review is drafted", drafted, 900, app)
    target = draft["target"]
    assert target["repo"] == COMPANY_REPO and target["number"] == 1, target
    comments = target.get("comments") or []
    places = [f"{c['path']}:{c['line']}" for c in comments]
    log(f"drafted with {len(comments)} line comment(s): {places}")
    content = {f"comment:{index}": c["body"] + " (approved)" for index, c in enumerate(comments)}
    sent = await app.post(
        f"/api/drafts/{draft['id']}", {"action": "send", "text": draft["text"], "content": content}
    )
    assert sent["status"] == "sent" and "pullrequestreview" in sent["outcome"], sent
    env = {**os.environ, "GH_TOKEN": token}
    published = json.loads(subprocess.run(
        ["gh", "api", f"repos/{COMPANY_REPO}/pulls/1/comments"], env=env,
        capture_output=True, text=True, check=True,
    ).stdout)  # fmt: skip
    review = int(sent["outcome"].rsplit("pullrequestreview-", 1)[1])
    mine = {
        (c["path"], c["line"], c["body"])
        for c in published
        if c["pull_request_review_id"] == review
    }
    wanted = {(c["path"], c["line"], c["body"] + " (approved)") for c in comments}
    assert comments and wanted <= mine, f"this review's edited line comments: {mine}"
    log(f"published through the second account: {sent['outcome']}")


async def router_mark(app: App, router: str) -> int:
    return max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)


async def started_since(app: App, router: str, mark: int) -> list[dict]:
    messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
    return [
        await app.get(f"/api/sessions/{m['meta']['session_id']}")
        for m in messages
        if m["kind"] == "notice" and m["text"].startswith("Started")
    ]


async def router_reply(app: App, router: str, mark: int, timeout: float) -> str:
    """The Router's visible answer to the owner after `mark`."""

    async def answered():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        replies = [
            m["text"]
            for m in messages
            if m["kind"] == "assistant" and not m["meta"].get("internal")
        ]
        row = await app.get(f"/api/sessions/{router}")
        return "\n".join(replies) if replies and row["status"] != "working" else None

    return await wait_for("the Router answers", answered, timeout, app)


async def task_of(app: App, worker_id: str) -> dict:
    tasks = (await app.get("/api/state"))["tasks"]
    return max((t for t in tasks if t["session_id"] == worker_id), key=lambda t: t["created_at"])


async def scenario_owner_flow(app: App, root: Path):
    """What a day with several workers looks like: a data analysis explained in full, a long
    step and progress questions, a message to one worker, stop and cancel, a session open in a
    terminal, and the owner profile. Every worker has a plugin hook that fails."""
    state = await app.get("/api/state")
    router = state["router_id"]
    await app.post("/api/settings", {**state["settings"], "role_efforts": {"worker": "high"}})
    csv = root / "sales.csv"
    csv.write_text(
        "region,month,revenue\n"
        + "".join(
            f"{region},2026-{month:02d},{base + month * step}\n"
            for region, base, step in [("east", 100, 10), ("west", 80, 25), ("north", 60, -5)]
            for month in range(1, 5)
        )
    )

    # 1. Data analysis, explained step by step: the whole answer in the report, no branch, no push.
    mark = await router_mark(app, router)
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": f"用 {csv} 的数据做个分析: 每个 region 的总收入, 以及每个月的环比增长. "
            "一步一步给我讲清楚你是怎么算的, 最后用一个 markdown 表格总结. "
            "这是数据分析, 不用改代码."
        },
    )

    async def analysed():
        for worker in await started_since(app, router, mark):
            task = await task_of(app, worker["id"])
            if task["status"] in {"review", "done"}:
                return worker, task
        return None

    analyst, task = await wait_for("the analysis is reported", analysed, 600, app)
    result = task["result"] or ""
    assert "|---" in result.replace(" ", "") or "|:--" in result.replace(" ", ""), result[:800]
    assert "east" in result.lower() and len(result) > 400, "the whole explanation, in the report"
    assert not analyst["branch"], "an analysis needs no branch"
    commands = (task.get("receipts") or {}).get("commands", [])
    assert not any("git push" in c["command"] for c in commands), commands
    await asyncio.sleep(15)  # The Router's turn about the report.
    after = await app.get(f"/api/sessions/{router}/messages?after={mark}")
    restated = [m for m in after if m["kind"] == "update" and not m["meta"].get("needs_action")]
    assert not restated, f"the report card is the message, not restated: {restated}"
    log(f"analysis reported whole ({len(result)} chars, with a table); not restated")
    notes = await app.get(f"/api/sessions/{analyst['id']}/messages")
    hooks = [m for m in notes if "plugin hook failed" in m["text"]]
    # Copilot reports some hook failures (one the sandbox stops) and carries on past others.
    assert len(hooks) <= 1 and analyst["status"] != "failed", "a failing hook is at most a note"
    log_file = Path.home() / ".copilot" / "session-state" / analyst["id"] / "events.jsonl"
    assert '"reasoningEffort":"high"' in log_file.read_text(), "the worker effort applies"
    log(f"a failing plugin hook did not stop the worker ({len(hooks)} note); effort high applied")

    # 2. Acting on the analysis goes to the worker that did it.
    mark = await router_mark(app, router)
    await app.post(
        f"/api/sessions/{router}/messages",
        {"text": "根据刚才的分析, 把每个 region 的总收入写进 totals.csv (两列: region,total)."},
    )

    async def acted():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        started = [m for m in messages if m["kind"] == "notice" and m["text"].startswith("Started")]
        same = [
            m
            for m in messages
            if m["kind"] == "notice" and m["meta"].get("session_id") == analyst["id"]
        ]
        task = await task_of(app, analyst["id"])
        return (
            (started, same) if (started or same) and task["status"] in {"review", "done"} else None
        )

    started, same = await wait_for("the follow-up is done", acted, 600, app)
    assert same and not started, "the worker that did the analysis does the follow-up"
    log("the follow-up went to the same worker")

    # 3. A long step: progress answered at once, the worker is not called stopped.
    mark = await router_mark(app, router)
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": "Start two workers in parallel. Worker 'Slow job': in its workspace run "
            "exactly `sleep 150 && echo finished > slow.txt` as one command, then report "
            "done. Worker 'Notes': write notes.txt in its workspace with the single line "
            "`notes v1`, then report done."
        },
    )

    async def both():
        workers = await started_since(app, router, mark)
        slow = next((w for w in workers if "slow" in w["title"].lower()), None)
        notes = next((w for w in workers if "note" in w["title"].lower()), None)
        if slow and notes and "sleep" in (slow.get("step") or ""):
            return slow, notes
        return None

    slow, notes_worker = await wait_for("the slow step is running", both, 300, app)
    await asyncio.sleep(25)
    await app.post(f"/api/sessions/{slow['id']}/messages", {"text": "progress?"})
    timeline = await app.get(f"/api/sessions/{slow['id']}/messages")
    assert timeline[-1]["text"].startswith("It reads this when its current step ends"), timeline[-1]
    log(f"a message to the busy worker says so at once: {timeline[-1]['text'][:160]!r}")
    mark = await router_mark(app, router)
    asked = time.monotonic()
    await app.post(f"/api/sessions/{router}/messages", {"text": "进展怎么样了?"})
    answer = await router_reply(app, router, mark, 120)
    took = time.monotonic() - asked
    assert "sleep" in answer.lower() or "150" in answer or "slow" in answer.lower(), answer
    lowered = answer.lower()
    assert not any(word in lowered for word in ("stopped", "已停止", "停掉了")), answer
    log(f"progress answered in {took:.0f}s: {answer[:300]!r}")

    # 4. A message to one worker from the Router chat goes to that worker only.
    async def noted():
        task = await task_of(app, notes_worker["id"])
        return task if task["status"] in {"review", "done"} else None

    await wait_for("the notes worker is done", noted, 300, app)
    mark = await router_mark(app, router)
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": "Add a second line `notes v2` to notes.txt, then report done.",
            "to": notes_worker["id"],
        },
    )
    record = (await app.get(f"/api/sessions/{router}/messages?after={mark}"))[0]
    assert record["kind"] == "owner" and record["meta"]["to"] == notes_worker["id"]
    notes_file = Path(notes_worker["cwd"]) / "notes.txt"

    async def second_line():
        task = await task_of(app, notes_worker["id"])
        text = notes_file.read_text() if notes_file.exists() else ""
        return task["status"] in {"review", "done"} and "notes v2" in text

    await wait_for("the addressed worker did it", second_line, 300, app)
    slow_file = Path(slow["cwd"]) / "slow.txt"
    log("the message from the Router chat reached only that worker")

    # 5. Stop and cancel the slow job: it stays cancelled.
    slow_task = await task_of(app, slow["id"])
    await app.post(f"/api/sessions/{slow['id']}/stop")
    await app.post(f"/api/tasks/{slow_task['id']}/cancel", {"text": "not needed any more"})
    await asyncio.sleep(30)
    assert (await task_of(app, slow["id"]))["status"] == "cancelled"
    log(f"stopped and cancelled: stays cancelled (slow.txt written: {slow_file.exists()})")

    # 6. A session open in a terminal: the Router knows what it does but does not write to it.
    term = root / "terminal"
    term.mkdir()
    helper = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).parent / "fake_terminal.py"),
            str(term),
            "Remember the codeword PELICAN for later. Reply only with OK.",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = await asyncio.wait_for(asyncio.to_thread(helper.stdout.readline), 200)
        assert line.startswith("READY"), line
        mark = await router_mark(app, router)
        await app.post(
            f"/api/sessions/{router}/messages",
            {
                "text": "我在终端里还开着另一个 Copilot session (关于 codeword 的). "
                "它记住的 codeword 是什么?"
            },
        )
        answer = await router_reply(app, router, mark, 180)
        assert "PELICAN" in answer.upper(), answer
        mark = await router_mark(app, router)
        await app.post(
            f"/api/sessions/{router}/messages",
            {"text": "让那个终端里的 session 再记住 OSPREY."},
        )
        answer = await router_reply(app, router, mark, 180)
        assert any(word in answer for word in ("终端", "terminal", "Terminal")), answer
        session_id = line.split()[1]
        assert not (await app.get("/api/state"))["sessions"] or all(
            s["id"] != session_id for s in (await app.get("/api/state"))["sessions"]
        ), "not taken over while open there"
        log(f"terminal session: read, not written: {answer[:200]!r}")
    finally:
        helper.kill()

    # 7. The owner profile is reviewed on a card in the inbox.
    await app.post("/api/profile/distill")

    async def profile_card():
        drafts = (await app.get("/api/state"))["drafts"]
        return next((d for d in drafts if d["kind"] == "profile"), None)

    card = await wait_for("the profile card", profile_card, 600, app)
    assert card["text"].count("##") >= 2, card["text"][:400]
    await app.post(f"/api/drafts/{card['id']}", {"action": "answer", "text": card["text"]})
    assert (await app.get("/api/state"))["profile"] == card["text"].strip()
    log(f"profile reviewed on its card ({len(card['text'])} chars) and in use")
    workers = [s for s in (await app.get("/api/state"))["sessions"] if s["role"] == "worker"]
    failed = [s["title"] for s in workers if s["status"] == "failed"]
    assert not failed, f"no worker failed: {failed}"


async def gone(path: Path) -> bool:
    return not path.exists()


async def scenario_decisions(app: App, project: Path):
    """The owner states a decision a task relies on, then changes it: the Router records the
    change, the affected worker is told and confirms, and its finished result reopens."""
    state = await app.get("/api/state")
    router = state["router_id"]
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": "Decision: our API uses snake_case JSON keys. Record that decision, then in "
            f"the project {project} (isolated worktree) have a worker write api.md with one "
            "example JSON response for GET /users that follows it, and commit it."
        },
    )

    async def recorded_and_done():
        state = await app.get("/api/state")
        tasks = [t for t in state["tasks"] if t["decisions"] and t["status"] == "review"]
        return tasks[0] if tasks else None

    task = await wait_for("a task that relies on the decision is done", recorded_and_done, 600)
    log(f"“{task['title']}” relies on {[d['text'] for d in task['decisions']]}")
    await app.post(
        f"/api/sessions/{router}/messages",
        {"text": "Change of plan: the API uses camelCase JSON keys instead of snake_case."},
    )

    async def confirmed():
        state = await app.get("/api/state")
        current = await app.get(f"/api/tasks/{task['id']}")
        changed = [
            d
            for d in current["decisions"]
            if d["status"] == "active" and d["ack"] and "camel" in d["text"].lower()
        ]
        pending = [d for d in state["drafts"] if d["kind"] == "change"]
        return (
            (changed[0], current)
            if changed and not pending and current["status"] == "review"
            else None
        )

    decision, current = await wait_for("the worker confirmed and redid it", confirmed, 600)
    assert "camel" in decision["text"].lower(), decision
    assert current["runs"] >= 2, "its finished result was reopened and redone"
    tree = Path((await app.get(f"/api/sessions/{task['session_id']}"))["cwd"])
    keys = re.findall(r'"(\w+)"\s*:', (tree / "api.md").read_text())
    assert keys and not [key for key in keys if "_" in key], keys
    log(f"decision changed; the worker confirmed: {decision['ack']!r}; keys now {keys}")


FEEDBACK = [
    ("decision", "Orders status migration: add a nullable column, or backfill first?",
     "Nullable column: fastest", "Backfill first, then make it NOT NULL", "", 0),
    ("follow-up", "Add orders.status: add the column and use it in the API",
     "Added the column with a migration.",
     "Add a rollback migration too; we always need a way back.", "", 0),
    ("retry", "Add an index on orders.created_at", "",
     "Write the rollback before the migration this time.", "", None),
    ("draft-edited", "A Slack reply to Maya Chen",
     "Hi Maya! Thanks so much for reaching out. I wanted to let you know that the fix is going "
     "out today, and I'll be sure to keep you posted on any updates!",
     "Fix ships today. Will ping you when it's out.", "slack", 0),
    ("draft-rejected", "A Slack reply to #deploys",
     "Hey team, just a friendly reminder that deploys are frozen this week. Thanks a ton!",
     "Too long and too chatty. Two sentences max, no exclamation marks.", "slack", 0),
    ("follow-up", "Fix the flaky login test", "Fixed it; all tests pass.",
     "Put the exact test command you ran in the report.", "", 0),
]  # fmt: skip


async def scenario_learning(app: App):
    """The owner's feedback becomes lessons a real model proposes and the owner reviews."""
    import sqlite3

    database = sqlite3.connect(app.data / "chiefofstaff.db")
    with database:
        for kind, context, proposed, owner, scope, agreed in FEEDBACK:
            database.execute(
                """INSERT INTO feedback(kind, context, proposed, owner, scope, agreed, learned,
                created_at) VALUES (?,?,?,?,?,?,0,strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))""",
                (kind, context, proposed, owner, scope, agreed),
            )
    database.close()
    await app.post("/api/lessons/learn", {})

    async def proposed():
        drafts = (await app.get("/api/state"))["drafts"]
        return next((d for d in drafts if d["kind"] == "lessons"), None)

    card = await wait_for("lessons proposed from the feedback", proposed, 600)
    lessons = card["target"]["lessons"]
    for lesson in lessons:
        log(f"lesson ({lesson['scope'] or 'everywhere'}): {lesson['text']}")
    assert 2 <= len(lessons) <= 8, lessons
    assert all(lesson["because"] for lesson in lessons), "each rests on feedback"
    assert any(lesson["scope"] == "slack" for lesson in lessons), "how Slack replies are written"
    assert any("rollback" in lesson["text"].lower() for lesson in lessons), lessons
    keep = {lesson["id"]: lesson["text"] for lesson in lessons}
    await app.post(f"/api/drafts/{card['id']}", {"action": "answer", "content": keep})
    learned = (await app.get("/api/state"))["learning"]
    assert len(learned["lessons"]) == len(lessons) and learned["new_feedback"] == 0, learned
    log(f"the owner kept {len(lessons)} lessons; agreement {learned['agreement']['now']}")


async def scenario_code_folder(app: App, root: Path):
    """The code folder: a repository the owner names that is not on this machine is cloned into
    it; the worker changes it in its own worktree in .chief, removed when the worker closes."""
    code = root / "code"
    code.mkdir()
    origin, _ = published_repo(root, "billing")
    state = await app.get("/api/state")
    settings = {**state["settings"], "code_folder": ""}
    await app.post("/api/settings", settings)  # Another scenario may have set one.
    added = await app.post("/api/projects", {"path": str(code)})
    assert added == {"path": str(code), "kind": "code_folder", "repositories": 0}, added
    router = state["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(
        f"/api/sessions/{router}/messages",
        {
            "text": f"In the billing repository at file://{origin} (not on this machine yet), "
            "add a file CODE.md containing exactly `from the code folder test` and commit it "
            "with the message 'Add code'. Use an isolated worktree."
        },
    )

    async def done():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        for message in messages:
            if message["kind"] == "notice" and message["text"].startswith("Started"):
                worker = await app.get(f"/api/sessions/{message['meta']['session_id']}")
                if worker["status"] == "done" and worker["checkouts"]:
                    return worker
        return None

    worker = await wait_for("the worker commits in a repository it cloned", done, 900, app)
    clone = code / "billing"
    assert (clone / ".git").is_dir(), "cloned into the code folder, to stay"
    [checkout] = [c for c in worker["checkouts"] if Path(c.get("repo", "")) == clone]
    tree = Path(checkout["path"])
    assert tree.is_relative_to(code / ".chief") and (tree / "CODE.md").exists()
    assert "Add code" in git(tree, "log", "--format=%s", "-1")
    assert not (clone / "CODE.md").exists(), "the clone in the code folder is left as it is"
    assert (await app.get("/api/state"))["settings"]["code_folder"] == str(code)
    assert checkout["branch"].startswith("chief/"), checkout
    assert git(tree, "branch", "--show-current").strip() == checkout["branch"]
    await app.post(f"/api/sessions/{worker['id']}/close")
    await wait_for("its clean worktree is removed", lambda: gone(tree), 60)
    assert checkout["branch"] in git(clone, "branch", "--format=%(refname:short)").split()
    log(f"cloned into {clone}, changed in {tree} on {checkout['branch']}; worktree removed")


async def scenario_models_usage_digest(app: App, started: float):
    state = await app.get("/api/state")
    router = state["router_id"]
    models = [m["id"] for m in await app.get("/api/models") if m["id"] != "auto"]
    chosen = next((m for m in models if "haiku" in m or "mini" in m or "fast" in m), models[-1])
    await app.post(f"/api/sessions/{router}/model", {"model": chosen})
    calls_before = (await app.get(f"/api/usage?since={started}"))["total"]["calls"]
    await app.post(f"/api/sessions/{router}/messages", {"text": "Reply with just OK."})

    async def used_new_model():
        usage = await app.get(f"/api/usage?since={started}")
        models_used = {row["model"] for row in usage["by_model"]}
        return usage if usage["total"]["calls"] > calls_before and chosen in models_used else None

    usage = await wait_for(f"Router answers with {chosen}", used_new_model, 180, app)
    roles = {row["role"] for row in usage["by_role"]}
    assert {"router", "worker", "slack"} <= roles, roles
    assert usage["total"]["credits"] > 0 and usage["by_session"][0]["credits"] > 0
    router_view = await app.get(f"/api/sessions/{router}")
    assert router_view["credits"] > 0 and router_view["model"] == chosen
    log(
        f"usage: {usage['total']['credits']:.1f} credits, {usage['total']['calls']} calls, "
        f"models {sorted(row['model'] for row in usage['by_model'])}"
    )
    await app.post("/api/settings", {**state["settings"], "digest_time": "00:00"})

    async def summary():
        messages = await app.get(f"/api/sessions/{router}/messages")
        found = [m for m in messages if m["meta"].get("digest")]
        return found[0] if found else None

    digest = await wait_for("daily summary", summary, 60)
    log("daily summary:\n" + digest["text"])


async def scenario_profile(app: App):
    await app.post("/api/profile/distill")

    async def drafted():
        state = await app.get("/api/state")
        return state["profile_draft"] or None

    draft = await wait_for("profile draft proposed", drafted, 900, app)
    assert "##" in draft and len(draft) < 8000, draft[:500]
    state = await app.post("/api/profile", {"text": draft})
    assert state["profile"] == draft and state["profile_draft"] == ""
    log("profile draft (first lines):\n" + "\n".join(draft.splitlines()[:8]))


async def scenario_restart(app: App, router_before: str):
    await app.stop()
    log("restarted Chief of Staff")
    await app.start()

    async def ready():
        state = await app.get("/api/state")
        return state if state["runtime"]["state"] == "ready" else None

    state = await wait_for("runtime ready after restart", ready, 120)
    assert state["router_id"] == router_before, "the Router must survive a restart"
    before = len(await app.get(f"/api/sessions/{router_before}/messages"))
    await app.post(
        f"/api/sessions/{router_before}/messages",
        {"text": "Without starting anything new: what did the hello.txt worker do? One sentence."},
    )

    async def answered():
        messages = (await app.get(f"/api/sessions/{router_before}/messages"))[before:]
        replies = [m["text"] for m in messages if m["kind"] == "assistant"]
        return replies[-1] if replies else None

    reply = await wait_for("Router answers from memory", answered, 300, app)
    log(f"router after restart: {reply!r}")
    assert "hello" in reply.lower(), reply


async def scenario_external(app: App):
    external = await app.get("/api/external?refresh=true")
    assert isinstance(external, list)
    log(f"{len(external)} other Copilot sessions listed")


def sessions_in(database: Path) -> list[str]:
    import sqlite3

    with sqlite3.connect(database) as db:
        ids = [row[0] for row in db.execute("SELECT id FROM sessions")]
        # A rotated Router left earlier Copilot sessions behind (and has a new one).
        ids += [
            row[0][len("alias:") :]
            for row in db.execute("SELECT key FROM kv WHERE key LIKE 'alias:%'")
        ]
    return list(dict.fromkeys(ids))


async def cleanup(data: Path):
    database = data / "chiefofstaff.db"
    ids: list[str] = []
    if database.exists():
        ids = sessions_in(database)
    path = subprocess.run(["which", "copilot"], capture_output=True, text=True).stdout.strip()
    client = CopilotClient(connection=RuntimeConnection.for_stdio(path=path))
    await client.start()
    # Also whatever the database does not list (a crash, a delete that failed): every session
    # that worked in this run's temporary folders.
    root = str(data.resolve().parent) + "/"
    for meta in await client.list_sessions():
        folder = (meta.context.working_directory if meta.context else "") or ""
        if folder.startswith(root) and meta.session_id not in ids:
            ids.append(meta.session_id)
    for session_id in ids:
        try:
            await client.delete_session(session_id)
        except Exception as exc:
            log(f"could not delete {session_id}: {exc}")
    await client.stop()
    log(f"deleted {len(ids)} test sessions")


async def main():
    workspace = Workspace()
    serve_in_thread(workspace, SLACK_TOKEN, SLACK_PORT)
    serve_docs(DOCS_PORT)
    forget_sign_ins(DOCS_PORT)
    signed_in_before = keychain_accounts()
    with tempfile.TemporaryDirectory(prefix="wb-live-") as temporary:
        root = Path(temporary).resolve()
        project = root / "project"
        project.mkdir()
        git(project, "init", "-q", "-b", "main")
        (project / "README.md").write_text("# Demo project\n")
        git(project, "add", ".")
        git(project, "-c", "user.email=e2e@test", "-c", "user.name=e2e", "commit", "-qm", "init")
        data = root / "data"
        # Like a company plugin's hook whose service is down: it fails on every turn.
        hooks = root / "hooks"
        hooks.mkdir()
        probe = 'echo "journal service unavailable" >&2; exit 3'
        (hooks / "journal.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "hooks": {
                        name: [{"type": "command", "bash": probe, "timeoutSec": 20}]
                        for name in ("sessionStart", "agentStop")
                    },
                }
            )
        )
        app = App(
            data,
            root / "chiefofstaff.log",
            {**DOCS_ENV, "CHIEFOFSTAFF_WORKER_HOOKS": str(hooks)},
        )
        failed = False
        started = time.time()
        try:
            await app.start()
            state = await app.get("/api/state")
            assert state["runtime"]["state"] == "ready", state["runtime"]
            await app.post(
                "/api/settings",
                {
                    "projects": [str(project)],
                    "max_workers": 2,
                    "mac_notifications": False,
                    "slack_enabled": True,
                    "slack_poll_seconds": 20,
                    "digest_time": "",  # Scenario I turns it on; at startup it would fire at once.
                },
            )
            router = state["router_id"]
            await scenario_setup(app)
            if sys.argv[1:] == ["setup"]:
                log("SETUP SCENARIO PASSED")
                return
            if sys.argv[1:] == ["delegate"]:
                await asyncio.sleep(5)  # The watcher's first check starts from now.
                await scenario_delegate(app, workspace)
                log("DELEGATE SCENARIO PASSED")
                return
            if sys.argv[1:] == ["code-folder"]:
                await scenario_code_folder(app, root)
                log("CODE FOLDER SCENARIO PASSED")
                return
            if sys.argv[1:] == ["worker"]:
                await scenario_worker(app, project)
                log("WORKER SCENARIO PASSED")
                return
            if sys.argv[1:] == ["learning"]:
                await scenario_learning(app)
                log("LEARNING SCENARIO PASSED")
                return
            if sys.argv[1:] == ["decisions"]:
                await scenario_decisions(app, project)
                log("DECISIONS SCENARIO PASSED")
                return
            if sys.argv[1:] == ["profile"]:
                await scenario_profile(app)  # Twice: the second asks for a profile again.
                await scenario_profile(app)
                log("PROFILE SCENARIO PASSED")
                return
            if sys.argv[1:] == ["owner-flow"]:
                await scenario_owner_flow(app, root)
                log("OWNER FLOW SCENARIO PASSED")
                return
            if sys.argv[1:] == ["added-repo"]:
                await scenario_added_repo_sandboxed(app, root)
                log("ADDED REPO SCENARIO PASSED")
                return
            if sys.argv[1:] == ["company-review"]:
                await scenario_company_review(app)
                log("COMPANY REVIEW SCENARIO PASSED")
                return
            if sys.argv[1:] == ["repos"]:
                await scenario_repos(app, root)
                log("REPOS SCENARIO PASSED")
                return
            worker = await scenario_worker(app, project)
            await scenario_reopen(app, worker)
            await scenario_slack(app, workspace)
            await scenario_thread(app, workspace)
            await scenario_delegate(app, workspace)
            await scenario_restart(app, router)
            await scenario_external(app)
            await scenario_watchdog(app, project)
            await scenario_dependencies(app, project)
            await scenario_decisions(app, project)
            await scenario_learning(app)
            await scenario_workspace(app, root)
            await scenario_ask_user(app)
            await scenario_ask_session(app)
            await scenario_repos(app, root)
            await scenario_added_repo_sandboxed(app, root)
            await scenario_owner_flow(app, root)
            await scenario_company_review(app)
            await scenario_code_folder(app, root)
            await scenario_models_usage_digest(app, started)
            await scenario_profile(app)
            router_messages = await app.get(f"/api/sessions/{router}/messages")
            rotations = sum("fresh Router session took over" in m["text"] for m in router_messages)
            log(f"the Router was rotated {rotations} time(s) during the run")
            log("ALL LIVE SCENARIOS PASSED")
        except BaseException:
            failed = True
            raise
        finally:
            try:
                state = await app.get("/api/state")
                for session in state["sessions"]:
                    messages = await app.get(f"/api/sessions/{session['id']}/messages")
                    print(f"--- {session['role']} {session['title']} [{session['status']}]")
                    print(transcript(messages))
            except Exception:
                pass
            await app.stop()
            if failed:
                print("--- chiefofstaff.log (tail)")
                print("\n".join((root / "chiefofstaff.log").read_text().splitlines()[-60:]))
            await cleanup(data)
            log(f"removed {forget_sign_ins(DOCS_PORT, signed_in_before)} test sign-in entries")


if __name__ == "__main__":
    asyncio.run(main())
