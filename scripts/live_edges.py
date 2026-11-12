"""Live edge cases against real Copilot sessions: what goes wrong in real use.

A. A review asked for in Chinese, by the repository's short name, on a repository only the
   non-active gh account can see; the owner drops every line comment and edits the text.
B. The PR moves after the review was drafted: nothing is published, the worker drafts again.
C. A PR no signed-in account can see: the worker says so instead of hanging.
D. Stopped mid-command: nothing is filed or asked; "go on" finishes it.
E. Chief of Staff restarts mid-task: the worker still has the plugin's, the repository's and
   the owner's MCP servers.

    uv run python scripts/live_edges.py [A B C D E]
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
import live_parity  # noqa: F401  (it sets its own port: this one is set after)

live_e2e.PORT = 18769
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import (  # noqa: E402
    COMPANY_REPO,
    App,
    cleanup,
    log,
    second_account_token,
    transcript,
    wait_for,
)
from live_parity import setup  # noqa: E402

MODEL = os.environ.get("EDGES_MODEL", "claude-sonnet-5.5")


async def say(app: App, text: str) -> int:
    router = (await app.get("/api/state"))["router_id"]
    mark = max((m["id"] for m in await app.get(f"/api/sessions/{router}/messages")), default=0)
    await app.post(f"/api/sessions/{router}/messages", {"text": text})
    return mark


async def assigned(app: App, mark: int) -> str:
    router = (await app.get("/api/state"))["router_id"]

    async def found():
        messages = await app.get(f"/api/sessions/{router}/messages?after={mark}")
        return next(
            (m["meta"]["session_id"] for m in messages
             if m["kind"] == "notice"
             and m["text"].startswith(("Started", "New task", "Continued in"))),
            None,
        )  # fmt: skip

    return await wait_for("the Router assigns it", found, 300)


async def new_draft(app: App, before: set[str], kind: str = "github") -> dict:
    async def found():
        drafts = (await app.get("/api/state"))["drafts"]
        return next((d for d in drafts if d["id"] not in before and d["kind"] == kind), None)

    return await wait_for(f"a {kind} draft", found, 900, app)


async def settled_draft(app: App, worker: str, before: set[str], kind: str = "github") -> dict:
    """The worker's latest pending draft once its turn is over: asked again (a report without
    a draft), it drafts anew, and the newer draft supersedes the first."""

    async def idle():
        session = await app.get(f"/api/sessions/{worker}")
        return session["status"] not in {"queued", "starting", "working"} or None

    await wait_for("the drafting turn ends", idle, 600, app)
    drafts = [
        d for d in (await app.get("/api/state"))["drafts"]
        if d["id"] not in before and d["kind"] == kind
    ]  # fmt: skip
    assert drafts, "a pending draft"
    return drafts[-1]


def gh_as_second(*arguments: str) -> str:
    env = {**os.environ, "GH_TOKEN": second_account_token()}
    return subprocess.run(
        ["gh", *arguments], env=env, capture_output=True, text=True, check=True
    ).stdout


def push_unrelated_commit():
    """A new commit on the PR's branch (a README line), so its head moves."""
    with tempfile.TemporaryDirectory() as folder:
        token = second_account_token()
        url = f"https://x-access-token:{token}@github.com/{COMPANY_REPO}.git"
        run = lambda *a: subprocess.run(a, cwd=folder, check=True, capture_output=True)  # noqa: E731
        run("git", "clone", "-q", "-b", "feature", url, ".")
        Path(folder, "NOTES.md").write_text(f"Moved at {time.time()}\n")
        run("git", "add", "NOTES.md")
        run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "Move the head")
        run("git", "push", "-q", "origin", "feature")


async def scenario_a_chinese_company_review(app: App):
    before = {d["id"] for d in (await app.get("/api/state"))["drafts"]}
    name = COMPANY_REPO.split("/")[1]
    # The owner writes as they do, full-width commas included.
    await say(app, f"帮我review一下 {name} 的 PR #1\uff0c有问题的行直接评论\uff0c起草好给我看")
    draft = await new_draft(app, before)
    assert draft["target"]["repo"] == COMPANY_REPO, draft["target"]
    comments = draft["target"].get("comments") or []
    log(f"drafted with {len(comments)} line comment(s)")
    content = {f"comment:{index}": "" for index in range(len(comments))}
    edited = "Owner's edit: please handle empty input in mean()."
    sent = await app.post(
        f"/api/drafts/{draft['id']}", {"action": "send", "text": edited, "content": content}
    )
    assert sent["status"] == "sent", sent
    review = sent["outcome"].rsplit("pullrequestreview-", 1)[1]
    published = json.loads(gh_as_second("api", f"repos/{COMPANY_REPO}/pulls/1/reviews/{review}"))
    # What Chief of Staff posts ends with its mark (sources/github.py, POSTED_MARK).
    assert published["body"].removesuffix("\n\n<!-- chief-of-staff -->") == edited, published
    mine = json.loads(
        gh_as_second("api", "--paginate", f"repos/{COMPANY_REPO}/pulls/1/reviews/{review}/comments")
    )
    assert mine == [], "every line comment was dropped"
    log(f"A: published the owner's text only, no line comments: {sent['outcome']}")


async def scenario_b_pr_moves(app: App):
    before = {d["id"] for d in (await app.get("/api/state"))["drafts"]}
    name = COMPANY_REPO.split("/")[1]
    mark = await say(app, f"Review {name} PR #1 again and draft a short review.")
    worker = await assigned(app, mark)
    await new_draft(app, before)
    draft = await settled_draft(app, worker, before)
    push_unrelated_commit()

    async def moved():
        pull = json.loads(gh_as_second("api", f"repos/{COMPANY_REPO}/pulls/1"))
        return pull["head"]["sha"] != draft["target"]["head_sha"]

    # GitHub shows a pushed commit on the PR a few seconds later.
    await wait_for("GitHub shows the PR's new head", moved, 120)
    response = await app.http.post(f"/api/drafts/{draft['id']}", json={"action": "send"})
    assert response.status_code >= 400 and "changed" in response.text, response.text
    log("B: a review of an older head is not published")
    before.add(draft["id"])
    again = await new_draft(app, before)
    assert again["target"]["head_sha"] != draft["target"]["head_sha"], "drafted on the new head"
    sent = await app.post(f"/api/drafts/{again['id']}", {"action": "send"})
    assert sent["status"] == "sent"
    messages = await app.get(f"/api/sessions/{worker}/messages")
    assert any("changed" in m["text"] for m in messages), "the worker was told why"
    log("B: the worker drafted again on the new head; that one was published")


async def scenario_c_no_access(app: App):
    mark = await say(app, "Review evanl666/this-repo-does-not-exist-7f3a PR #3 and draft a review.")
    worker = await assigned(app, mark)

    async def settled():
        tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] == worker]
        # A task it cannot do is failed, under Needs you with why (or it asks, or reports).
        settled = {"waiting", "review", "done", "failed"}
        return next((t for t in tasks if t["status"] in settled), None)

    task = await wait_for("the worker says it cannot", settled, 600, app)
    text = (task.get("progress") or "") + (task.get("result") or "")
    assert "evanl666" in text or "signed in" in text or "access" in text.lower(), text
    assert not (await app.get("/api/state"))["drafts"] or all(
        d["target"].get("repo") != "evanl666/this-repo-does-not-exist-7f3a"
        for d in (await app.get("/api/state"))["drafts"]
    )
    log(f"C: [{task['status']}] {text[:200]!r}")


async def scenario_d_stop_and_go_on(app: App, project: Path):
    mark = await say(
        app,
        f"In {project}: run `sleep 45 && echo slept > slept.txt` in the shell (do not shorten "
        "it), then tell me what slept.txt contains.",
    )
    worker = await assigned(app, mark)

    async def sleeping():
        row = await app.get(f"/api/sessions/{worker}")
        return row if "sleep 45" in (row.get("step_command") or "") else None

    await wait_for("the worker is in the long command", sleeping, 300, app)
    await app.post(f"/api/sessions/{worker}/stop")
    await asyncio.sleep(8)
    messages = await app.get(f"/api/sessions/{worker}/messages")
    assert not [m for m in messages if m["kind"] == "report"], "nothing filed"
    assert not [m for m in messages if "report-missing" in json.dumps(m["meta"])], "not asked"
    task = next(t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] == worker)
    assert task["status"] == "waiting", task["status"]
    log("D: stopped mid-command: waiting, nothing filed, nothing asked")
    await app.post(f"/api/sessions/{worker}/messages", {"text": "Go on."})

    async def finished():
        tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] == worker]
        return next((t for t in tasks if t["status"] in {"review", "done"}), None)

    task = await wait_for("it finishes after go on", finished, 600, app)
    assert "slept" in (task.get("result") or ""), task.get("result")
    log("D: after 'go on' it finished and reported what the file contains")


async def scenario_e_restart_keeps_mcp(app: App, project: Path) -> App:
    ask = (
        "Call the probe_ping tool of every MCP server whose name starts with 'probe' and report "
        "each server's answer."
    )
    mark = await say(app, f"In {project}, in an isolated worktree: {ask}")
    worker = await assigned(app, mark)

    async def answered(after: str = ""):
        tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] == worker]
        done = [t for t in tasks if t["status"] in {"review", "done"} and t["updated_at"] > after]
        return done[-1] if done else None

    first = await wait_for("the probes answer", answered, 600, app)
    for server in ("probe-user", "probe-plugin", "probe-repo"):
        assert f"pong from {server}" in first["result"], first["result"]
    await app.stop()
    await app.start()
    log("E: Chief of Staff restarted")
    await app.post(f"/api/sessions/{worker}/messages", {"text": f"Again: {ask}"})
    later = await wait_for(
        "the probes answer after the restart",
        lambda: answered(first["updated_at"]),
        600,
        app,
    )
    for server in ("probe-user", "probe-plugin", "probe-repo"):
        assert f"pong from {server}" in later["result"], later["result"]
    log("E: after a restart the worker still has the owner's, the plugin's and the repo's MCP")
    return app


async def main():
    wanted = set(sys.argv[1:]) or set("ABCDE")
    with tempfile.TemporaryDirectory(prefix="cos-edges-") as temporary:
        root = Path(temporary).resolve()
        home, project, _ = setup(root)
        app = App(root / "data", root / "chiefofstaff.log", {"COPILOT_HOME": str(home)})
        await app.start()
        try:
            await app.post(
                "/api/settings",
                {"projects": [str(project)], "model": MODEL, "max_workers": 3,
                 "mac_notifications": False, "digest_time": ""},
            )  # fmt: skip
            if not second_account_token():
                log("SKIPPED A-C: no second gh account signed in")
                wanted -= set("ABC")
            if "A" in wanted:
                await scenario_a_chinese_company_review(app)
            if "B" in wanted:
                await scenario_b_pr_moves(app)
            if "C" in wanted:
                await scenario_c_no_access(app)
            if "D" in wanted:
                await scenario_d_stop_and_go_on(app, project)
            if "E" in wanted:
                await scenario_e_restart_keeps_mcp(app, project)
            log("LIVE EDGE CASES PASSED")
        except BaseException:
            state = await app.get("/api/state")
            for session in state["sessions"]:
                print(f"--- {session['role']} {session['title']} [{session['status']}]")
                print(transcript(await app.get(f"/api/sessions/{session['id']}/messages")))
            print("\n".join((root / "chiefofstaff.log").read_text().splitlines()[-40:]))
            raise
        finally:
            await app.stop()
            await cleanup(root / "data")


if __name__ == "__main__":
    asyncio.run(main())
