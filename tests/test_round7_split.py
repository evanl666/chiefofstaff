"""Bug bash round 7: separate work asked of a worker while a task is at work (reported on its
own, counted from the message that asked for it, in a worktree it made itself), what the owner
reads about a new task, and news on a PR whose maker stopped following it."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from conftest import Harness, call_tool, settle

from chiefofstaff.models import Settings


def run(*command, cwd=None):
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True).stdout


def commit(repo: Path, name: str, text: str):
    (repo / name).write_text(text)
    run("git", "add", ".", cwd=repo)
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", f"add {name}",
        cwd=repo)  # fmt: skip


def published(tmp: Path, name: str) -> Path:
    origin = tmp / f"{name}.git"
    run("git", "init", "-q", "--bare", "-b", "main", str(origin))
    local = tmp / name
    run("git", "clone", "-q", str(origin), str(local))
    run("git", "checkout", "-q", "-b", "main", cwd=local)
    commit(local, "README.md", f"# {name}\n")
    run("git", "push", "-q", "origin", "main", cwd=local)
    return local


async def spawn(runtime, router, **arguments):
    arguments.setdefault("why_new", "test")
    result = await call_tool(runtime, "spawn_worker", router, **arguments)
    return json.loads(result.text_result_for_llm)


def shell(harness, session_id: str, call: str, command: str, output: str = ""):
    session = harness.session(session_id)
    session.emit("tool.execution_start", tool_name="bash", tool_call_id=call,
                 arguments={"command": command})  # fmt: skip
    done = SimpleNamespace(content=f"{output}\n<exited with exit code 0>")
    session.emit("tool.execution_complete", tool_call_id=call, success=True, result=done)


async def based(runtime, task_id: str):
    """Until its start was recorded (in the background, as its worker takes it up)."""
    for _ in range(100):
        await settle(runtime)
        if runtime.store.get(f"taskbase:{task_id}"):
            return
        await asyncio.sleep(0.03)
    raise AssertionError(f"No base recorded for {task_id}")


async def receipts(runtime, task_id: str) -> dict:
    """Its receipts once its result's end was recorded (in the background, after a report)."""
    for _ in range(100):
        await settle(runtime)
        found = runtime.task_actions.view(runtime.tasks.get(task_id))["receipts"]
        if runtime.store.get(f"taskend:{task_id}") and found and found["version"]:
            return found
        await asyncio.sleep(0.03)
    raise AssertionError(f"No receipts for {task_id}")


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


async def test_separate_work_asked_as_a_worker_finishes_is_reported_on_its_own(harness):
    """J2: asked in its chat for a separate task as it finished, its report was held back with
    "handle it, then report once, covering both": one report then held both, and the separate
    task got the other's result and changes."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Fix the parser", instructions="x")
    session_id = worker["session_id"]
    session = harness.session(session_id)
    session.emit("user.message", content=session.sent[0])
    session.emit("assistant.turn_start")
    await settle(runtime)
    runtime.update_session(session_id, status="working")
    await runtime.owner_send(session_id, "Also open a separate task: count the TODOs, no edits.")
    assert runtime.states[session_id].unread, "sent within its turn, not seen yet"
    held = await call_tool(runtime, "report", session_id, status="done", summary="Fixed it.")
    text = held.text_result_for_llm
    assert held.result_type == "failure", text
    assert "each deliverable on its own" in text and "report(new_task_title=" in text, text
    assert "covering both" not in text, text


async def test_separate_work_split_off_a_task_at_work_counts_from_the_message_asking(tmp_path):
    """J2: split off a task still at work, the separate task had no start of its own and
    counted the whole checkout (the task's fix). Code review: everything the owner wrote since
    the task was taken up moved with it, and the task's own test runs."""
    app = published(tmp_path, "app")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Fix div", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    await based(runtime, task)
    await runtime.owner_send(session_id, "Use pytest.approx in its test.")
    commit(tree, "calc.py", "def div(a, b):\n    return a / b\n")
    shell(harness, session_id, "c1", "pytest -q", "1 passed")
    await settle(runtime)
    asked = "Separately, as a task of its own: count the Python files. Change no files."
    await runtime.owner_send(session_id, asked)
    shell(harness, session_id, "c2", "git ls-files '*.py' | wc -l", "1")
    await settle(runtime)
    told = await call_tool(runtime, "report", session_id, status="done", changed_code=False,
                           summary="One Python file: `calc.py`.",
                           new_task_title="Count the Python files")  # fmt: skip
    assert told.result_type != "failure", told.text_result_for_llm
    [made] = [t for t in runtime.tasks.for_session(session_id) if t["id"] != task]
    assert made["goal"] == asked, made["goal"]
    first, second = owner_messages(harness, session_id)
    assert (first["meta"]["task_id"], second["meta"]["task_id"]) == (task, made["id"])
    assert commands_of(runtime, made["id"]) == ["git ls-files '*.py' | wc -l"]
    assert commands_of(runtime, task) == ["pytest -q"], "its own test run stays"
    changed = (await receipts(runtime, made["id"]))["changed"]
    assert not changed.get("commits") and not changed.get("files"), changed
    done = "Added `div(a, b)` in `calc.py`; `pytest -q` gives 1 passed."
    await call_tool(runtime, "report", session_id, status="done", summary=done, task_id=task,
                    changed_code=True)  # fmt: skip
    changed = (await receipts(runtime, task))["changed"]
    assert changed["commits"] == 1 and changed["files"] == 1, changed


async def test_work_in_a_worktree_the_worker_made_itself_is_its_tasks_change(tmp_path):
    """J3: separate work in a worktree its worker made with `git worktree add` showed no
    changes and "Says it made changes, but its checkouts have none." A worktree of a
    repository it does not work in is not taken."""
    app = published(tmp_path, "app")
    other = published(tmp_path, "other")
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Count", instructions="x",
                         project=str(app), isolate=True)  # fmt: skip
    tree, session_id, task = Path(worker["cwd"]), worker["session_id"], worker["task_id"]
    await based(runtime, task)
    folder = runtime.workspaces.workspace(harness.store.session(session_id))
    made = folder / "app-count"
    run("git", "worktree", "add", "-q", "-b", "count", str(made), "origin/main", cwd=tree)
    commit(made, "COUNT.md", "1 Python file\n")
    run("git", "worktree", "add", "-q", "-b", "elsewhere", str(folder / "other-x"), cwd=other)
    await call_tool(runtime, "report", session_id, status="done", summary="Counted them.",
                    changed_code=True)  # fmt: skip
    found = await receipts(runtime, task)
    assert found["changed"]["commits"] == 1 and found["changed"]["files"] == 1, found["changed"]
    assert "Says it made changes, but its checkouts have none." not in found["flags"]
    assert "count" in [checkout["branch"] for checkout in found["checkouts"]]
    paths = [c["path"] for c in runtime.workspaces.checkouts(harness.store.session(session_id))]
    assert str(made) in paths and str(folder / "other-x") not in paths, paths


async def test_why_a_task_is_new_reads_as_part_of_the_notice(harness):
    """J10: "· new because Different question … at the owner's request." pasted as written."""
    runtime = await harness.connect()
    router = harness.router_id
    worker = await spawn(runtime, router, title="Fix the parser", instructions="x",
                         why_new="Different subject than the other workers.")  # fmt: skip
    await call_tool(runtime, "message_session", router, session_id=worker["session_id"],
                    task="new", earlier="keep", title="Docs PR", text="Write the docs",
                    why_new="PR of its own, as you asked.")  # fmt: skip
    notices = [m["text"] for m in harness.store.messages(router) if m["kind"] == "notice"]
    assert notices[0].endswith(" · new because different subject than the other workers")
    assert notices[-1].endswith(" · new because PR of its own, as you asked"), notices[-1]


async def test_a_quick_question_to_a_worker_is_answered_not_reported(harness):
    """K2: "Quick one: what is 17*3?" to the worker of an accepted task was filed as a report
    of a new task: its note called a question a deliverable of its own."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Fix the parser", instructions="x")
    session_id, task = worker["session_id"], worker["task_id"]
    await call_tool(runtime, "report", session_id, status="done", summary="Fixed it.")
    runtime.task_actions.accept(task)
    await runtime.owner_send(session_id, "Quick one: what is 17*3? Answer in one line.")
    [message] = owner_messages(harness, session_id)
    prompt = message["meta"]["prompt"]
    assert "you answer in your reply, without a report" in prompt, prompt
    assert "PR, question" not in prompt, prompt


async def test_news_on_a_pr_its_maker_stopped_following_goes_to_the_task_still_following(harness):
    """Code review: ✕ on the PR chip of the task that made it, the PR's news still went to it."""
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, title="Div", instructions="x")
    session_id, maker = worker["session_id"], worker["task_id"]
    thread = "github:acme/app#70"
    url = "https://github.com/acme/app/pull/70"
    await call_tool(runtime, "report", session_id, status="done", summary=f"Opened {url}.")
    runtime.follow(session_id, [thread], task_id=maker)
    await call_tool(runtime, "message_session", harness.router_id, session_id=session_id,
                    task="new", title="Fix its CI", text="Fix the CI of #70",
                    why_new="another fix")  # fmt: skip
    later = runtime.tasks.current(session_id)
    runtime.follow(session_id, [thread], task_id=later["id"])
    assert runtime.thread_task(session_id, thread)["id"] == maker
    runtime.unfollow_for(session_id, thread, task_id=maker)
    assert runtime.thread_task(session_id, thread)["id"] == later["id"]


async def test_a_task_waiting_for_an_accepted_result_is_queued(harness):
    """Code review: after a task in review, with wait_for="accepted", said "Started"."""
    runtime = await harness.connect()
    router = harness.router_id
    first = await spawn(runtime, router, title="Design the API", instructions="x")
    await call_tool(runtime, "report", first["session_id"], status="done",
                    summary="The API: `GET /items`.")  # fmt: skip
    assert runtime.tasks.get(first["task_id"])["status"] == "review"
    await spawn(runtime, router, title="Build the client", instructions="y",
                after=[first["task_id"]], wait_for="accepted")  # fmt: skip
    notices = [m["text"] for m in harness.store.messages(router) if m["kind"] == "notice"]
    assert notices[-1].startswith("Queued “Build the client” · after “Design the API”"), notices
