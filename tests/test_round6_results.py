"""Bug bash round 6: a finished task's result when its worker goes on with it (a question
answered, separate work asked for in its chat), closing after a merge, and Create PR."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import Harness, call_tool, settle

from chiefofstaff.models import Settings


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


async def results(runtime, task_id: str, count: int):
    """Until its result's end was recorded `count` times (in the background, after a report)."""
    for _ in range(100):
        await settle(runtime)
        if len(json.loads(runtime.store.get(f"taskends:{task_id}") or "[]")) >= count:
            return
        await asyncio.sleep(0.03)
    raise AssertionError(f"No end recorded for {task_id}")


RESULT = (
    "Branch `r6reg-div`, 1 commit: `div(a, b)` in `calc.py`, raising `ValueError` for 0, "
    "with `test_div` and `test_div_by_zero`. `pytest -q` gives 4 passed."
)


@pytest.mark.parametrize("reviewed", [True, False])
async def test_fixes_for_review_comments_then_a_question_are_a_new_result(tmp_path, reviewed):
    """G1: its worker pushed fixes for its PR's review comments and asked "re-review and
    merge yourself?"; once answered, its report came as news, the result still the old one.
    `reviewed`: its PR's review came first (else it changed its code in its own turn)."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Div", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    commit(tree, "calc.py", "def div(a, b):\n    return a / b\n")
    await call_tool(runtime, "report", session_id, status="done", summary=RESULT)
    await results(runtime, task, 1)
    if reviewed:
        runtime.took_up(session_id, [task])  # Its PR's review comments, on this task.
        await settle(runtime)
    commit(tree, "calc.py", "def _check(b):\n    pass\n\n\ndef div(a, b):\n    return a / b\n")
    asked = "Review addressed in 8b61885 (pushed). Re-review and merge yourself?"
    await call_tool(runtime, "report", session_id, status="question", summary=asked)
    assert runtime.tasks.get(task)["status"] == "waiting"
    await runtime.task_actions.follow_up(task, "OK, I will ask the reviewer to look again.")
    runtime.took_up(session_id, [task])  # Its conversation takes the answer up.
    await settle(runtime)
    whole = f"{RESULT} Review addressed in 8b61885: the zero check is `_check(b)` now."
    told = await call_tool(runtime, "report", session_id, status="done", summary=whole)
    assert told.result_type != "failure", told.text_result_for_llm
    shown = runtime.task_actions.view(runtime.tasks.get(task))
    assert shown["status"] == "review" and shown["result"] == whole, shown["result"]
    assert not shown["news"], "a new result to review, not news"
    await results(runtime, task, 2)
    found = runtime.task_actions.view(runtime.tasks.get(task))["receipts"]
    assert found["changed"]["commits"] == 2, found["changed"]
    assert not found.get("stale"), "its own commits are its new result, not out of date"


def shell(harness, session_id: str, call: str, command: str, output: str = ""):
    session = harness.session(session_id)
    session.emit("tool.execution_start", tool_name="bash", tool_call_id=call,
                 arguments={"command": command})  # fmt: skip
    done = SimpleNamespace(content=f"{output}\n<exited with exit code 0>")
    session.emit("tool.execution_complete", tool_call_id=call, success=True, result=done)


async def test_create_pr_text_and_the_result_it_leaves_on_the_card(tmp_path):
    """G7: after Create PR the card still said "not pushed, no PR" above its PR. G8: the PR's
    text said "This is unchanged from before." and "(new)", and cited a here-document as the
    command that checked "pytest passes" (its blank lines broke the Markdown)."""
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
    worker = await spawn(runtime, harness.router_id, title="Div", instructions="x",
                         project=str(app), isolate=True, done_when=["pytest passes"])  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    commit(tree, "calc.py", "def div(a, b):\n    return a / b\n", "Add div")
    heredoc = "cat >> calc.py <<'EOF'\n\n\ndef floordiv(a, b):\n    return a // b\nEOF\n"
    heredoc += "pytest -q | tail -1"
    shell(harness, session_id, "p1", heredoc, "7 passed in 0.01s")
    claims = [{"criterion": "pytest passes", "result": "met",
               "evidence": "`pytest -q | tail -1` \u2192 7 passed"}]  # fmt: skip
    hook = "- The pre-push hook fails on Windows, so I disabled it there in .githooks/pre-push."
    summary = (
        "Branch `r6reg-div` (from main), 2 commits, not pushed, no PR:\n"
        "- ccddd63: `div(a, b)` in calc.py plus tests. This is unchanged from before.\n"
        f"- 57c5e01 (new): `floordiv(a, b)` with the same zero check.\n{hook}"
    )
    await call_tool(runtime, "report", session_id, status="done", summary=summary,
                    criteria=claims)  # fmt: skip
    await settle(runtime)
    await runtime.task_actions.create_pr(task)
    created = next(call for call in calls if call[:2] == ("pr", "create"))
    body = created[created.index("--body") + 1]
    assert "- ccddd63: `div(a, b)` in calc.py plus tests.\n" in body, body
    assert "- 57c5e01: `floordiv(a, b)` with the same zero check." in body, body
    assert "unchanged" not in body and "(new)" not in body, body
    assert "- pytest passes: `pytest -q | tail -1`\n" in body, body
    assert "EOF" not in body and "def floordiv" not in body, body
    assert hook in body, "not about pushing its branch: it stays"
    assert runtime.tasks.get(task)["result"] == summary, "the result stays as reported"
    shown = runtime.task_actions.view(runtime.tasks.get(task))["result"]
    assert shown.startswith("Branch `r6reg-div` (from main), 2 commits:\n- ccddd63"), shown
    assert "not pushed" not in shown and "no PR" not in shown and hook in shown, shown


async def test_a_merged_pr_is_not_open_in_its_task_s_result(harness):
    """N11: merged from its card, the done task still read "Opened …#72 (not merged). … It is
    open and not merged.\""""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Merge note", instructions="x")
    session_id, task = worker["session_id"], worker["task_id"]
    url = "https://github.com/acme/app/pull/72"
    kept = (
        "- Root cause: the file handle stays open after an error \u2014 fixed by using a "
        "context manager.\n- Issue #40 is still open; it needs a separate fix in the parser."
    )
    summary = (
        f'Opened {url} (not merged).\n\nOpened {url}, titled "Merge note", against `main`. '
        f"It is open and not merged.\n\n{kept}"
    )
    await call_tool(runtime, "report", session_id, status="done", summary=summary)
    await settle(runtime)
    for _ in range(2):  # From its card, then seen merged: once.
        runtime.task_actions.pr_merged(session_id, "github:acme/app#72", task)
    found = runtime.tasks.get(task)
    assert found["status"] == "done"
    assert found["result"] == summary, "the result stays as reported"
    shown = runtime.task_actions.view(found)["result"]
    assert shown == (
        f'acme/app#72 is merged.\n\nOpened {url}.\n\nOpened {url}, titled "Merge note", '
        f"against `main`.\n\n{kept}"
    ), shown


def owner_messages(harness, session_id: str) -> list[dict]:
    return [m for m in harness.store.messages(session_id) if m["kind"] == "owner"]


def commands_of(runtime, task_id: str) -> list[str]:
    return [
        row["command"]
        for row in runtime.store.all(
            "SELECT c.command FROM commands c JOIN runs r ON r.id=c.run_id WHERE r.task_id=? "
            "ORDER BY c.id",
            (task_id,),
        )
    ]


async def test_separate_work_asked_of_a_worker_straight_is_a_task_of_its_own(harness):
    """G2: "New, separate task for this worker: … add mod()" @-sent to a worker whose PR waited
    to merge was filed under that PR's task, and its result replaced that task's. Its worker,
    which reads the message, is told what it is about and says it is another deliverable."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Div", instructions="x")
    session_id, task = worker["session_id"], worker["task_id"]
    await call_tool(runtime, "report", session_id, status="done", summary=RESULT)
    asked = (
        "New, separate task for this worker: make a new branch r6reg-mod from main and add "
        "mod(a, b) with a test. Do not push."
    )
    await runtime.owner_send_to(harness.router_id, session_id, asked)
    assert [t["id"] for t in runtime.tasks.for_session(session_id)] == [task], "its worker says"
    [message] = owner_messages(harness, session_id)
    assert message["meta"]["task_id"] == task
    prompt = message["meta"]["prompt"]
    assert f"about task {task} \u201cDiv\u201d unless it asks" in prompt, prompt
    assert "report(new_task_title=" in prompt and f"task {task} keeps its result" in prompt
    done = "Branch `r6reg-mod`: `mod(a, b)` with `test_mod`; `pytest -q` gives 5 passed."
    told = await call_tool(runtime, "report", session_id, status="done", summary=done,
                           new_task_title="Add mod() on r6reg-mod")  # fmt: skip
    assert "a task of its own" in told.text_result_for_llm, told.text_result_for_llm
    [made] = [t for t in runtime.tasks.for_session(session_id) if t["id"] != task]
    assert (made["title"], made["status"], made["result"]) == ("Add mod() on r6reg-mod",
                                                               "review", done)  # fmt: skip
    assert runtime.tasks.get(task)["result"] == RESULT
    [message] = owner_messages(harness, session_id)
    assert message["meta"]["task_id"] == made["id"], "what asked for it goes with it"
    inbound = [m for m in harness.store.messages(harness.router_id) if m["kind"] == "inbound"]
    assert inbound[-1]["meta"]["task_id"] == made["id"], "the Router hears of a task of its own"


async def test_separate_work_asked_while_a_task_is_at_work_is_a_task_of_its_own(harness):
    """Asked in its chat for something apart from the task it is still at, its worker reports
    that with new_task_title: a task of its own, with that message and the commands run since.
    The task it is on goes on, and its own report (without new_task_title) is its result."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Div", instructions="x")
    session_id, task = worker["session_id"], worker["task_id"]
    shell(harness, session_id, "c1", "pytest -q tests/test_div.py", "1 passed")
    await settle(runtime)
    asked = "Separately: write the 0.2 release notes into NOTES.md."
    await runtime.owner_send(session_id, asked)
    [message] = owner_messages(harness, session_id)
    prompt = message["meta"]["prompt"]
    assert f"about task {task} \u201cDiv\u201d unless it asks" in prompt, prompt
    assert f"task {task} goes on" in prompt, prompt
    shell(harness, session_id, "c2", "pytest -q tests/test_notes.py", "1 passed")
    await settle(runtime)
    notes = "Wrote the 0.2 release notes into `NOTES.md`."
    told = await call_tool(runtime, "report", session_id, status="done", summary=notes,
                           new_task_title="Write the 0.2 release notes")  # fmt: skip
    assert f"{task} goes on" in told.text_result_for_llm, told.text_result_for_llm
    [made] = [t for t in runtime.tasks.for_session(session_id) if t["id"] != task]
    assert (made["title"], made["status"], made["result"]) == ("Write the 0.2 release notes",
                                                               "review", notes)  # fmt: skip
    still = runtime.tasks.get(task)
    assert (still["status"], still["result"]) == ("running", ""), "it goes on"
    assert runtime.tasks.current(session_id)["id"] == task
    [message] = owner_messages(harness, session_id)
    assert message["meta"]["task_id"] == made["id"], "what asked for it goes with it"
    assert commands_of(runtime, made["id"]) == ["pytest -q tests/test_notes.py"]
    assert commands_of(runtime, task) == ["pytest -q tests/test_div.py"]
    shell(harness, session_id, "c3", "pytest -q", "2 passed")
    await settle(runtime)
    assert commands_of(runtime, task)[-1] == "pytest -q", "what it runs now is the task's"
    done = "Added `div(a, b)` in `calc.py`; `pytest -q` gives 2 passed."
    reported = await call_tool(runtime, "report", session_id, status="done", summary=done)
    assert reported.result_type != "failure", reported.text_result_for_llm
    assert (runtime.tasks.get(task)["status"], runtime.tasks.get(task)["result"]) == (
        "review",
        done,
    )
    assert runtime.tasks.get(made["id"])["result"] == notes
    assert len(runtime.tasks.for_session(session_id)) == 2


async def test_work_on_another_branch_than_its_open_pr_is_another_task(tmp_path):
    """G2: work the owner asked for on a new branch, while the task's PR waited to merge,
    replaced that task's result (its merge card then showed the other branch's work)."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Div", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    commit(tree, "calc.py", "def div(a, b):\n    return a / b\n")
    await call_tool(runtime, "report", session_id, status="done", summary=RESULT)
    await results(runtime, task, 1)
    branch = run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=tree).strip()
    pr = {"label": "acme/app#70", "own": True, "state": "OPEN", "branch": branch, "about": True}
    run_id = runtime.store.value("SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC",
                                 (task,))  # fmt: skip
    runtime.store.execute("UPDATE runs SET evidence=? WHERE id=?",
                          (json.dumps({"checkouts": [], "prs": [pr]}), run_id))  # fmt: skip
    await runtime.owner_send(session_id, "Now add mod() on a branch r6reg-mod made from main.")
    await asyncio.sleep(1.1)  # Its commit is after it was asked (git counts seconds).
    run("git", "checkout", "-q", "-b", "r6reg-mod", "origin/main", cwd=tree)
    commit(tree, "mod.py", "def mod(a, b):\n    return a % b\n")
    done = "Branch `r6reg-mod`: `mod(a, b)` in `mod.py`, 1 commit."
    refused = await call_tool(runtime, "report", session_id, status="done", summary=done)
    assert refused.result_type == "failure"
    assert "another deliverable" in refused.text_result_for_llm, refused.text_result_for_llm
    told = await call_tool(runtime, "report", session_id, status="done", summary=done,
                           new_task_title="Add mod() on r6reg-mod")  # fmt: skip
    assert "a task of its own" in told.text_result_for_llm, told.text_result_for_llm
    [made] = [t for t in runtime.tasks.for_session(session_id) if t["id"] != task]
    assert (made["title"], made["status"], made["result"]) == ("Add mod() on r6reg-mod",
                                                               "review", done)  # fmt: skip
    assert runtime.tasks.get(task)["result"] == RESULT
    assert runtime.tasks.get(task)["status"] == "review"
    [message] = [m for m in harness.store.messages(session_id) if m["kind"] == "owner"]
    assert message["meta"]["task_id"] == made["id"], "what asked for it goes with it"
    await results(runtime, made["id"], 1)
    found = runtime.task_actions.view(runtime.tasks.get(made["id"]))["receipts"]
    assert found["changed"]["commits"] == 1 and found["changed"]["files"] == 1, found


async def test_a_worker_with_a_result_not_reviewed_stays_after_a_merge(harness):
    """G2: its PR merged from the card, the worker closed itself though another result of it
    was never reviewed."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Div", instructions="x")
    session_id, task = worker["session_id"], worker["task_id"]
    url = "https://github.com/acme/app/pull/70"
    await call_tool(runtime, "report", session_id, status="done", summary=f"Opened {url}.")
    runtime.follow(session_id, ["github:acme/app#70"], task_id=task)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", title="Add mod to calc", text="Add mod()",
                    why_new="another function")  # fmt: skip
    await call_tool(runtime, "report", session_id, status="done", summary="Added mod().")
    runtime.unfollow("github:acme/app#70", finished=True, merged=True)
    harness.session(session_id).emit("session.idle")
    await settle(runtime)
    assert runtime.tasks.get(task)["status"] == "done"
    assert not harness.store.session(session_id)["closed_at"], "its other result waits"


async def test_without_new_task_title_a_report_is_the_task_s_own(harness):
    """Code review: messages that turn a separate task down, ask about one or only name one
    made a task, and left the question they answered open. Now nothing but its worker's
    report makes one."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Div", instructions="x")
    session_id, task = worker["session_id"], worker["task_id"]
    await call_tool(runtime, "report", session_id, status="question",
                    summary="Fix it here, or in a separate task?")  # fmt: skip
    await runtime.owner_send(session_id, "Keep it in this PR rather than in a separate task.")
    assert [t["id"] for t in runtime.tasks.for_session(session_id)] == [task]
    assert not runtime.asks.session_questions(session_id), "answered"
    assert runtime.tasks.get(task)["status"] == "running"
    done = "Fixed it here: `div(a, b)` raises `ValueError` for 0."
    await call_tool(runtime, "report", session_id, status="done", summary=done)
    assert [t["id"] for t in runtime.tasks.for_session(session_id)] == [task]
    assert runtime.tasks.get(task)["result"] == done


async def test_an_answer_about_a_task_while_on_another_branch_is_not_split_off(tmp_path):
    """Code review: asked in chat about a finished task's open PR while its worker was on
    another task's branch, its answer was refused as "another deliverable" and then split off
    into "Work on branch …"."""
    _, app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Fix a", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, first = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    commit(tree, "a.py", "RETRY = 0\n")
    await call_tool(runtime, "report", session_id, status="done", summary="Fixed `a.py`.")
    await results(runtime, first, 1)
    branch = run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=tree).strip()
    pr = {"label": "acme/app#68", "own": True, "state": "OPEN", "branch": branch, "about": True}
    run_id = runtime.store.value("SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC",
                                 (first,))  # fmt: skip
    runtime.store.execute("UPDATE runs SET evidence=? WHERE id=?",
                          (json.dumps({"checkouts": [], "prs": [pr]}), run_id))  # fmt: skip
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", why_new="test", title="Add feature b", text="Add b")  # fmt: skip
    second = runtime.tasks.current(session_id)["id"]
    runtime.took_up(session_id, [second])
    run("git", "checkout", "-q", "-b", "feat-b", "origin/main", cwd=tree)
    commit(tree, "b.py", "B = 1\n")
    await call_tool(runtime, "report", session_id, status="done", task_id=second,
                    summary="Added `b.py` on feat-b.")  # fmt: skip
    await runtime.task_actions.follow_up(first, "Why did you drop the retry in #68?")
    runtime.took_up(session_id, [first])  # Its conversation takes the question up.
    await settle(runtime)
    answer = "Fixed `a.py`. The retry hid the error, so I dropped it."
    told = await call_tool(
        runtime, "report", session_id, status="done", task_id=first, summary=answer
    )
    assert told.result_type != "failure", told.text_result_for_llm
    assert len(runtime.tasks.for_session(session_id)) == 2, "no task split off"
