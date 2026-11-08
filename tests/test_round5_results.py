"""Bug bash round 5: what becomes of a finished task's result when its worker goes on with it
(an answer, its PR's review comments), its receipts after a follow-up, and Create PR's text."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from conftest import Harness, call_tool, settle

from chiefofstaff.envelope import wrap
from chiefofstaff.models import Settings
from chiefofstaff.receipts import criteria_results


def run(*command, cwd=None):
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True).stdout


def commit(repo: Path, name: str, text: str, message: str = ""):
    (repo / name).write_text(text)
    run("git", "add", ".", cwd=repo)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm",
        message or f"add {name}", cwd=repo)  # fmt: skip


def published(tmp: Path, name: str) -> tuple[Path, Path]:
    origin = tmp / f"{name}.git"
    run("git", "init", "-q", "--bare", "-b", "main", str(origin))
    local = tmp / name
    run("git", "clone", "-q", str(origin), str(local))
    run("git", "checkout", "-q", "-b", "main", cwd=local)
    commit(local, "README.md", f"# {name}\n")
    run("git", "push", "-q", "origin", "main", cwd=local)
    return origin, local


async def spawn(runtime, router, **arguments):
    result = await call_tool(runtime, "spawn_worker", router, why_new="test", **arguments)
    return json.loads(result.text_result_for_llm)


def shell(harness, session_id: str, call: str, command: str, output: str = ""):
    session = harness.session(session_id)
    session.emit("tool.execution_start", tool_name="bash", tool_call_id=call,
                 arguments={"command": command})  # fmt: skip
    done = SimpleNamespace(content=f"{output}\n<exited with exit code 0>")
    session.emit("tool.execution_complete", tool_call_id=call, success=True, result=done)


async def ended(runtime, task_id: str):
    """Until its result's end was recorded (in the background, after its report)."""
    for _ in range(100):
        await settle(runtime)
        if runtime.store.get(f"taskend:{task_id}"):
            return
        await asyncio.sleep(0.03)
    raise AssertionError(f"No end recorded for {task_id}")


# A result with names and versions enough for left_out to tell what a report leaves out.
RESULT = (
    "Committed `notes/r5ws-cpr.md` on branch `r5ws-cpr` (1d5d1b9). It covers `ReceiptCard`, "
    "`TaskBoard`, `NewsBanner` and `DiffView`, checked with Python 3.11 and Node 22.4.1."
)


async def test_an_answer_in_chat_on_finished_work_keeps_the_whole_result(tmp_path):
    """S4: a finished task asked "push it or will you?"; answered "Yes, push it" in its chat,
    its result became only "Pushed r5ws-cpr (1d5d1b9..77c9b15) to origin"."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Notes", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    commit(tree, "notes.md", "notes\n")
    await call_tool(runtime, "report", session_id, status="done", summary=RESULT)
    await ended(runtime, task)
    await call_tool(runtime, "report", session_id, status="question",
                    summary="Push it, or will you?")  # fmt: skip
    await runtime.owner_send(session_id, "Yes, push it")
    await settle(runtime)
    assert runtime.tasks.get(task)["status"] == "review"
    prompts = [m["meta"].get("prompt", "") for m in harness.store.messages(session_id)]
    assert "replaces its result" in prompts[-1], "told to restate the whole result"
    assert "replaces its result" in harness.session(session_id).sent[-1], "in the same turn"
    commit(tree, "notes.md", "notes\nmore\n")  # What it did meanwhile (a rebase, a commit).
    refused = await call_tool(runtime, "report", session_id, status="done",
                              summary="Pushed r5ws-cpr (1d5d1b9..77c9b15) to origin.")  # fmt: skip
    assert refused.result_type == "failure"
    assert "leaves out most of the earlier one" in refused.text_result_for_llm
    whole = f"{RESULT} Pushed r5ws-cpr (1d5d1b9..77c9b15) to origin."
    await call_tool(runtime, "report", session_id, status="done", summary=whole)
    assert runtime.tasks.get(task)["result"] == whole


async def test_news_after_an_answer_on_finished_work_leaves_its_result_alone(harness):
    """S4: answered, a worker that changed nothing reports news on its result: not asked to
    restate it (its result stays as it is)."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Notes", instructions="x")
    session_id, task = worker["session_id"], worker["task_id"]
    await call_tool(runtime, "report", session_id, status="done", summary=RESULT)
    await call_tool(runtime, "report", session_id, status="question",
                    summary="Push it, or will you?")  # fmt: skip
    await runtime.owner_send(session_id, "Yes, push it")
    told = await call_tool(runtime, "report", session_id, status="done",
                           summary="Pushed r5ws-cpr to origin.")  # fmt: skip
    assert told.result_type != "failure", told.text_result_for_llm
    shown = runtime.task_actions.view(runtime.tasks.get(task))
    assert shown["result"] == RESULT and shown["news"]["text"] == "Pushed r5ws-cpr to origin."


async def test_a_follow_up_keeps_the_criteria_an_earlier_result_verified(harness):
    """S5: every follow-up turned a verified card into "Not verified: did not report on its
    acceptance criteria"."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Sum", instructions="x",
                         done_when=["`pytest -q` passes"])  # fmt: skip
    session_id, task = worker["session_id"], worker["task_id"]
    shell(harness, session_id, "p1", "pytest -q", "1 passed in 0.01s")
    met = {"criterion": "`pytest -q` passes", "result": "met", "evidence": "`pytest -q`: 1 passed"}
    await call_tool(runtime, "report", session_id, status="done", summary="Sum added.",
                    criteria=[met])  # fmt: skip
    await settle(runtime)
    assert runtime.task_actions.view(runtime.tasks.get(task))["receipts"]["verdict"] == "verified"

    await runtime.task_actions.follow_up(task, "Also say what it returns for no numbers")
    sent = harness.session(session_id).sent[-1]
    assert "Done when criteria again" in sent and "`pytest -q` passes" in sent
    await call_tool(runtime, "report", session_id, status="done",
                    summary="Sum added; it returns 0 for no numbers.")  # fmt: skip
    await settle(runtime)
    found = runtime.task_actions.view(runtime.tasks.get(task))["receipts"]
    assert found["verdict"] == "verified", found
    assert found["criteria"][0]["earlier"], "marked as the earlier result's"
    assert not any("acceptance criteria" in flag for flag in found["flags"]), found["flags"]

    # Verified only while what backs it holds: not when the code changed after it ran.
    ran = [{"command": "pytest -q", "exit_code": 0, "ended_at": "2026-01-01T10:00:00+00:00"}]
    [before] = criteria_results(["`pytest -q` passes"], [], ran, [met], "2026-01-01T11:00:00+00:00")
    assert before["state"] == "claimed" and before["earlier"]


async def test_code_changed_after_answering_a_question_on_finished_work_is_a_new_result(tmp_path):
    """W3: a task that only read was asked "fix it?" and answered on the question card; its
    worker added a worktree, fixed and committed, and reported: that came as news, its result
    still said nothing changed, and there was nothing to accept."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Check calc", instructions="x")
    session_id, task = worker["session_id"], worker["task_id"]
    await call_tool(runtime, "report", session_id, status="done",
                    summary="calc.py divides by zero on empty input. No code changed.")  # fmt: skip
    await settle(runtime)
    await call_tool(runtime, "report", session_id, status="question", summary="Fix it?")
    [question] = runtime.asks.session_questions(session_id)
    await runtime.task_actions.answer_question(question["id"], "Yes, fix it")
    await settle(runtime)
    await call_tool(runtime, "add_repo", session_id, repo=str(app))
    [checkout] = json.loads(harness.store.session(session_id)["repos"])
    tree = Path(checkout["path"])
    edited = {"path": str(tree / "calc.py")}
    harness.session(session_id).emit("tool.execution_start", tool_name="edit",
                                     tool_call_id="e1", arguments=edited)  # fmt: skip
    commit(tree, "calc.py", "def mean(xs):\n    return sum(xs) / len(xs) if xs else 0\n")
    await settle(runtime)
    fixed = "Fixed calc.py: an empty input gives 0 now, committed on its branch."
    await call_tool(runtime, "report", session_id, status="done", summary=fixed)
    await settle(runtime)
    shown = runtime.task_actions.view(runtime.tasks.get(task))
    assert shown["status"] == "review" and shown["result"] == fixed, shown["status"]
    assert not shown["news"], "a new result to review, not news"


async def test_a_split_takes_the_news_it_split_off_with_it(harness):
    """W3: after Split, the change was the new task's result, and the same news stayed on the
    task it came from too."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Alpha file", instructions="x")
    session_id, task = worker["session_id"], worker["task_id"]
    await call_tool(runtime, "report", session_id, status="done", summary="Wrote `alpha.txt`.")
    await runtime.owner_send(session_id, "Also write beta.txt")
    await call_tool(runtime, "report", session_id, status="done", summary="Wrote `beta.txt`.")
    assert runtime.task_actions.view(runtime.tasks.get(task))["news"]
    made = runtime.task_actions.split(task, "Write the beta file")
    assert made["result"] == "Wrote `beta.txt`."
    assert not runtime.task_actions.view(runtime.tasks.get(task))["news"]


async def test_fixes_for_review_comments_on_finished_work_are_a_new_result(tmp_path):
    """S18: its worker pushed fixes for its PR's review comments; they came as news, and the
    card said its files changed since it was checked while its diff stayed the old one."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Calc", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    commit(tree, "calc.py", "def add(a, b):\n    return a + b\n")
    await call_tool(runtime, "report", session_id, status="done", summary=RESULT)
    await ended(runtime, task)
    runtime.took_up(session_id, [task])  # Its PR's review comments, on this task.
    commit(tree, "calc.py", "def add(a: int, b: int) -> int:\n    return a + b\n")
    refused = await call_tool(runtime, "report", session_id, status="progress",
                              summary="Pushed fixes for the review comments.")  # fmt: skip
    assert refused.result_type == "failure"
    assert "new result for the owner to review" in refused.text_result_for_llm
    whole = f"{RESULT} Review comments addressed: `add` has type hints now."
    await call_tool(runtime, "report", session_id, status="done", summary=whole)
    shown = runtime.task_actions.view(runtime.tasks.get(task))
    assert shown["status"] == "review" and shown["result"] == whole
    assert not shown["news"]

    # Its PR merged, a pull in its checkout is not more work on it.
    await settle(runtime)
    run_id = runtime.store.value("SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC",
                                 (task,))  # fmt: skip
    merged = [{"label": "acme/app#3", "own": True, "state": "MERGED"}]
    runtime.store.execute("UPDATE runs SET evidence=? WHERE id=?",
                          (json.dumps({"checkouts": [], "prs": merged}), run_id))  # fmt: skip
    runtime.tasks.move(task, "done")
    runtime.took_up(session_id, [task])
    commit(tree, "LATEST.md", "from main\n")
    told = await call_tool(runtime, "report", session_id, status="done",
                           summary="Merged; pulled the branch.")  # fmt: skip
    assert told.result_type != "failure", told.text_result_for_llm
    assert runtime.tasks.get(task)["status"] == "done"


async def test_news_after_a_result_made_in_chat_is_not_a_change(tmp_path):
    """S18: the base of an earlier chat (before its latest result) made news on that result
    count as a change, a new result."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Calc", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    commit(tree, "calc.py", "def add(a, b):\n    return a + b\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Calc added.")
    await ended(runtime, task)
    await runtime.owner_send(session_id, "Add type hints")
    commit(tree, "calc.py", "def add(a: int, b: int) -> int:\n    return a + b\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Calc typed.")
    assert runtime.tasks.get(task)["result"] == "Calc typed."
    for _ in range(100):
        await settle(runtime)
        if len(json.loads(runtime.store.get(f"taskends:{task}") or "[]")) == 2:
            break
        await asyncio.sleep(0.03)
    runtime.took_up(session_id, [task])  # Its PR's review: approved.
    told = await call_tool(runtime, "report", session_id, status="progress",
                           summary="The reviewer approved the PR.")  # fmt: skip
    assert told.result_type != "failure", told.text_result_for_llm
    assert runtime.task_actions.view(runtime.tasks.get(task))["news"]


async def test_another_tasks_work_in_the_same_worktree_is_no_change_of_a_finished_one(tmp_path):
    """Code review: back on an accepted task (news on its PR) after another one in the same
    worktree (a branch from main), its answer counted the other task's work as its own change:
    a progress report was refused as changing its code, and done sent it back to review."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    router = harness.router_id
    worker = await spawn(runtime, router, title="Calc", instructions="x", project=str(app),
                         isolate=True)  # fmt: skip
    tree, session_id, first = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    commit(tree, "calc.py", "def add(a, b):\n    return a + b\n")
    await call_tool(runtime, "report", session_id, status="done", summary=RESULT)
    await ended(runtime, first)
    runtime.task_actions.record_opened(first, ["https://github.com/acme/app/pull/7"])
    runtime.follow(session_id, ["github:acme/app#7"], task_id=first)
    runtime.task_actions.accept(first)
    await call_tool(runtime, "message_session", router, session_id=session_id, task="new",
                    why_new="test",
                    title="Add the units module", text="Add units.py from main")  # fmt: skip
    second = runtime.tasks.current(session_id)["id"]
    assert second != first
    await settle(runtime)
    run("git", "checkout", "-q", "-b", "units", "origin/main", cwd=tree)
    commit(tree, "units.py", "UNITS = {}\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Units added.",
                    task_id=second)  # fmt: skip
    await ended(runtime, second)
    await runtime.on_thread("github", "github:acme/app#7", ["k1"], "Bob: how many tests?")
    await settle(runtime)
    assert runtime.turn_task(session_id) == first
    told = await call_tool(runtime, "report", session_id, status="progress",
                           summary="Answered Bob: it has 12 tests.", task_id=first)  # fmt: skip
    assert told.result_type != "failure", told.text_result_for_llm
    await call_tool(runtime, "report", session_id, status="done",
                    summary=f"{RESULT} It has 12 tests.", task_id=first)  # fmt: skip
    assert runtime.tasks.get(first)["status"] == "done", "not a new result to review"


async def test_news_on_a_finished_tasks_pr_puts_its_worker_on_that_task(tmp_path):
    """Code review: a PR's news for a finished task left its worker's turn on its later task,
    so fixes for the review comments were not that finished task's new result."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Calc", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, first = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    commit(tree, "calc.py", "def add(a, b):\n    return a + b\n")
    await call_tool(runtime, "report", session_id, status="done", summary=RESULT)
    await ended(runtime, first)
    runtime.task_actions.record_opened(first, ["https://github.com/acme/app/pull/7"])
    runtime.follow(session_id, ["github:acme/app#7"], task_id=first)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", title="Write the notes", text="Write notes",
                    why_new="test")  # fmt: skip
    second = runtime.tasks.current(session_id)["id"]
    await call_tool(runtime, "report", session_id, status="done", summary="Notes written.",
                    task_id=second)  # fmt: skip
    assert runtime.turn_task(session_id) == second
    await runtime.on_thread("github", "github:acme/app#7", ["k1"], "Bob: add type hints to add")
    await settle(runtime)
    assert runtime.turn_task(session_id) == first, "its turn is on the task the news is for"
    commit(tree, "calc.py", "def add(a: int, b: int) -> int:\n    return a + b\n")
    refused = await call_tool(runtime, "report", session_id, status="progress",
                              summary="Type hints added.", task_id=first)  # fmt: skip
    assert refused.result_type == "failure"
    assert "new result for the owner to review" in refused.text_result_for_llm


async def test_news_seen_mid_turn_puts_its_worker_on_that_task(harness):
    """Code review: news on a followed thread, seen while its worker was at another task, left
    its turn on that other task. A task the owner stopped stays stopped for news."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Calc", instructions="x")
    session_id, first = worker["session_id"], worker["task_id"]
    await call_tool(runtime, "report", session_id, status="done", summary="Calc added.")
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", title="Write the notes", text="Write notes",
                    why_new="test")  # fmt: skip
    assert runtime.turn_task(session_id) != first
    runtime.store.set(f"stopped:{first}", "you")
    news = wrap("github", "thread", "Bob: add type hints", thread="github:acme/app#7",
                trust="untrusted", task=first)  # fmt: skip
    harness.session(session_id).emit("user.message", content=news)
    await settle(runtime)
    assert runtime.turn_task(session_id) == first
    assert not runtime.store.get(f"goingon:{first}"), "news does not resume a stopped task"


async def test_create_pr_describes_the_change_not_the_instruction(tmp_path):
    """S11: the PR's body said "Not pushed, no PR opened…" and listed "Nothing pushed, no PR
    opened" as checked (its command cut at 200 characters); its title was the instruction."""
    origin, app = published(tmp_path, "app")
    run("git", "config", "remote.origin.url", "https://github.com/acme/app.git", cwd=app)
    run("git", "config", f"url.{origin}.insteadOf", "https://github.com/acme/app.git", cwd=app)
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    calls = []

    async def gh(*arguments, payload=None):
        calls.append(arguments)
        if arguments[:2] == ("pr", "list"):
            return "[]"
        if arguments[:2] == ("pr", "create"):
            return "https://github.com/acme/app/pull/3\n"
        return json.dumps({"url": "https://github.com/acme/app/pull/3", "state": "OPEN"})

    runtime.github.gh = gh
    criteria = ["notes.md is committed", "Nothing pushed, no PR opened"]
    worker = await spawn(runtime, harness.router_id, instructions="x", project=str(app),
                         isolate=True, done_when=criteria,
                         title="Commit notes/r5ws-cpr.md on branch r5ws-cpr")  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    commit(tree, "notes.md", "notes\n", "Add the r5ws-cpr notes")
    long = "git log --oneline -1 -- notes.md && git status --porcelain && echo " + "x" * 240
    shell(harness, session_id, "g1", long, "abc123 Add the r5ws-cpr notes")
    shell(harness, session_id, "g2", "git status -sb", "## r5ws-cpr")
    claims = [
        {"criterion": criteria[0], "result": "met",
         "evidence": "`git log --oneline -1 -- notes.md` shows abc123"},
        {"criterion": criteria[1], "result": "met", "evidence": "`git status -sb`: no upstream"},
    ]  # fmt: skip
    summary = (
        "Committed `notes.md` on branch r5ws-cpr (abc123), not pushed.\n\n"
        "Not pushed, no PR opened\u2026\n\n- It lists the five round 5 areas."
    )
    await call_tool(runtime, "report", session_id, status="done", summary=summary,
                    criteria=claims)  # fmt: skip
    await settle(runtime)
    await runtime.task_actions.create_pr(task)
    created = next(call for call in calls if call[:2] == ("pr", "create"))
    assert created[created.index("--title") + 1] == "Add the r5ws-cpr notes"
    body = created[created.index("--body") + 1]
    assert "Committed `notes.md` on branch r5ws-cpr (abc123)." in body, body
    assert "- It lists the five round 5 areas." in body
    assert "push" not in body.lower() and "no PR" not in body, body
    assert "- notes.md is committed: `git log --oneline -1 -- notes.md" in body
    # The command its evidence names, not the chained line around it (bug bash 6, N3).
    assert "`git log --oneline -1 -- notes.md`" in body and "x" * 50 not in body
