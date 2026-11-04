import json
import os
import subprocess
from datetime import datetime
from types import SimpleNamespace

import pytest
from conftest import Harness, call_tool, settle

from chiefofstaff import checks as checks_module
from chiefofstaff.models import DraftDecision, Settings

GIT = ["git", "-c", "user.email=owner@example.com", "-c", "user.name=Owner"]


def git(repo, *arguments, env=None):
    return subprocess.run(
        [*GIT, "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    ).stdout


def slack_result(*messages):
    return "\n\n".join(
        f"Channel: <#{channel}> (x) | From: <@{user}> (Owner) | ts: {ts}\nText: {text}\n"
        f"Permalink: https://acme.slack.com/archives/{channel}/p{ts.replace('.', '')}"
        for channel, user, ts, text in messages
    )


def learner_of(harness):
    return next(
        row for row in harness.store.open_sessions() if harness.store.get(f"learner:{row['id']}")
    )


async def spawn(runtime, router, title, **extra):
    extra.setdefault("instructions", title)
    extra.setdefault("why_new", "test")
    result = await call_tool(runtime, "spawn_worker", router, title=title, **extra)
    return json.loads(result.text_result_for_llm)


async def test_the_owners_own_words_are_collected_where_they_wrote_them(tmp_path):
    project = tmp_path / "api"
    project.mkdir()
    git(project, "init", "-q", "-b", "main")
    git(project, "config", "user.email", "owner@example.com")
    for message in (
        "Fix the retry loop\n\nIt retried forever on 404.",
        "Add the export\n\nCo-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>",
        "Worker's commit",
    ):
        (project / "f.txt").write_text(message)
        git(project, "add", "-A")
        git(project, "commit", "-q", "-m", message)
    worker_sha = git(project, "rev-parse", "--short", "HEAD").strip()
    harness = Harness(tmp_path / "data", Settings(projects=[str(project)], slack_enabled=True))
    runtime = await harness.connect()
    harness.store.execute(
        """INSERT INTO commands(session_id, call_id, command, kind, exit_code, output, started_at)
        VALUES ('w', 'c', 'git commit -m x', 'commit', 0, ?, 'now')""",
        (f"[main {worker_sha}] Worker's commit\n 1 file changed",),
    )
    await runtime.slack.discover()
    draft = harness.store.create_draft(harness.router_id, "slack", {}, "Sure, on it")
    harness.store.update_draft(draft["id"], status="sent", final_text="Sure, on it")
    harness.mcp.results["from:me"] = slack_result(
        ("D0ALICE01", "U0OWNER1", "1790000001.000100", "hey! can u send me the deck? thx"),
        ("C0DEPLOYS", "U0OWNER1", "1790000002.000100", "Deploying now. Ping me if it breaks"),
        ("D0BOB0001", "U0OWNER1", "1790000003.000100", "Sure, on it"),
        ("C0DEPLOYS", "U0ALICE1", "1790000004.000100", "not the owner's"),
    )
    harness.gh_replies["auth status"] = json.dumps(
        {"hosts": {"github.com": [{"login": "owner", "state": "success", "active": True}]}}
    )
    harness.gh_replies["auth token"] = "tok"
    reviews = [
        {
            "id": "R1",
            "url": "https://github.com/a/b/pull/1#pullrequestreview-111111",
            "body": "Nice. Two nits, then ship it.",
            "comments": {
                "nodes": [
                    {"id": "C1", "url": "https://github.com/a/b/pull/1#discussion_r222222",
                     "body": "nit: name it retries", "path": "r.py"},
                ]
            },
        },
        {  # Published by Chief of Staff (a draft the owner approved): learned as feedback.
            "id": "R2",
            "url": "https://github.com/a/b/pull/1#pullrequestreview-333333",
            "body": "Drafted by a worker",
            "comments": {"nodes": []},
        },
    ]  # fmt: skip
    harness.store.add_trigger("github-sent", "github:review:333333", "Drafted by a worker")
    pull = {"title": "Retry", "repository": {"nameWithOwner": "a/b"}, "reviews": {"nodes": reviews}}
    harness.gh_replies["api graphql"] = json.dumps({"data": {"search": {"nodes": [pull]}}})
    harness.client.metas = [
        SimpleNamespace(
            session_id="mine-1",
            summary="Tune the ranker",
            is_remote=False,
            modified_time=datetime.now(),
            context=SimpleNamespace(working_directory=str(project), repository="", branch=""),
        )
    ]
    harness.client.logs["mine-1"] = [
        {
            "type": "user.message",
            "id": "e1",
            "data": {"content": "先跑一个小的 smoke test 再跑全量"},
        },
        {"type": "user.message", "id": "e2", "data": {"content": "progress?"}},
        {"type": "assistant.message", "id": "e3", "data": {"content": "Done: the long answer"}},
    ]
    runtime.add_message(harness.router_id, "owner", "Always answer me in Chinese, briefly.")

    added = await runtime.habits.collect()

    found = {
        (row["kind"], row["text"])
        for row in harness.store.all("SELECT kind, text FROM observations")
    }
    assert found == {
        ("slack_dm", "hey! can u send me the deck? thx"),
        ("slack_channel", "Deploying now. Ping me if it breaks"),
        ("pr_review", "Nice. Two nits, then ship it."),
        ("pr_review", "r.py: nit: name it retries"),
        ("commit", "Fix the retry loop\n\nIt retried forever on 404."),
        ("instruction", "先跑一个小的 smoke test 再跑全量"),
        ("instruction", "Always answer me in Chinese, briefly."),
    }, "only the owner's own words; not sent drafts, Copilot's commits or a worker's"
    assert added == 7
    assert await runtime.habits.collect() == 0, "read once"
    assert runtime.habits.view()["observed"]["pr_review"] == 2


async def test_the_owners_style_is_learned_applies_at_once_and_can_be_undone(tmp_path):
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    habits = runtime.habits
    for index, text in enumerate(["hey, quick q", "thx!!", "lgtm, ship it", "on it 👍", "ok"]):
        habits.add("slack", "slack_dm", f"s{index}", text)
    await runtime.learning.maybe_learn()
    await settle(runtime)
    learner = learner_of(harness)
    session = harness.session(learner["id"])
    assert [tool.name for tool in session.options["tools"]] == ["update_style", "propose_lessons"]
    assert session.options["available_tools"] == ["update_style", "propose_lessons"], "no shell"
    assert '"text": "thx!!"' in session.sent[0]

    ids = [row["id"] for row in harness.store.all("SELECT id FROM observations")]
    style = "Lowercase, very short, casual; thanks as thx!!. E.g. “on it 👍”."
    few = await call_tool(
        runtime, "update_style", learner["id"], section="slack_dm", text=style, evidence=ids[:2]
    )
    assert few.result_type == "failure" and "at least 3" in few.text_result_for_llm
    done = await call_tool(
        runtime, "update_style", learner["id"], section="slack_dm", text=style, evidence=ids
    )
    assert "version 1" in done.text_result_for_llm
    assert any("Slack direct messages" in text for _, text in harness.kinds(harness.router_id))

    worker = await spawn(runtime, harness.router_id, "Write notes")
    slack_worker = await spawn(runtime, harness.router_id, "Answer Bob", slack=True)

    def prompt(session_id):
        return runtime.options(harness.store.session(session_id))["system_message"]["content"]

    [slack] = [row for row in harness.store.open_sessions() if row["role"] == "slack"]
    for session_id in (slack["id"], slack_worker["session_id"]):
        assert "<owner_style>" in prompt(session_id) and style in prompt(session_id)
    assert "<owner_style>" not in prompt(worker["session_id"]), "no Slack style without Slack"
    assert "<owner_style>" not in prompt(harness.router_id)

    await runtime.habits.undo("slack_dm")
    assert runtime.habits.styles() == {}
    with pytest.raises(ValueError):
        await runtime.habits.undo("slack_dm")


async def test_habits_join_the_lessons_card_the_owner_has_not_reviewed_yet(harness):
    runtime = await harness.connect()
    runtime.habits.add("copilot", "instruction", "o1", "先跑一个小的 smoke test 再跑全量")
    runtime.habits.add("copilot", "instruction", "o2", "smoke test first, then the full run")
    runtime.learning.record("follow-up", context="Train", owner="Run a smoke job first")
    await runtime.learning.learn()
    learner = learner_of(harness)
    observed = [row["id"] for row in harness.store.all("SELECT id FROM observations")]
    lesson = {"lesson": "Run a small smoke test before the full run", "observations": observed}
    await call_tool(runtime, "propose_lessons", learner["id"], lessons=[lesson])
    [card] = [d for d in harness.store.drafts() if d["kind"] == "lessons"]
    assert card["target"]["lessons"][0]["because"][0]["owner"] == "先跑一个小的 smoke test 再跑全量"
    harness.session(learner["id"]).emit("session.idle")
    await settle(runtime)
    assert harness.store.session(learner["id"])["closed_at"]

    runtime.learning.record("follow-up", context="Docs", owner="Docs in English please")
    await runtime.learning.learn()  # Not blocked by the card waiting for the owner.
    second = learner_of(harness)
    assert "waiting for the owner's review" in harness.session(second["id"]).sent[0]
    later = {"lesson": "Write docs in English"}
    await call_tool(runtime, "propose_lessons", second["id"], lessons=[later])
    [card] = [d for d in harness.store.drafts() if d["kind"] == "lessons"]
    assert [item["text"] for item in card["target"]["lessons"]] == [
        "Run a small smoke test before the full run",
        "Write docs in English",
    ]

    # The owner reviewed only the first (their card had not updated yet): the other comes back.
    first_id = card["target"]["lessons"][0]["id"]
    seen = {first_id: "Run a small smoke test first"}
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer", content=seen))
    assert [lesson["text"] for lesson in runtime.learning.lessons()] == [
        "Run a small smoke test first"
    ]
    [again] = [d for d in harness.store.drafts() if d["kind"] == "lessons"]
    assert [item["text"] for item in again["target"]["lessons"]] == ["Write docs in English"]


async def test_a_helper_that_stops_short_is_closed(harness):
    runtime = await harness.connect()
    runtime.learning.record("follow-up", context="t", owner="Shorter reports")
    started = await runtime.learning.learn()
    await settle(runtime)
    harness.session(started["session_id"]).emit("session.idle")
    await settle(runtime)
    row = harness.store.session(started["session_id"])
    assert row["closed_at"], "nothing to report: closed, not asked to report"


needs_sandbox = pytest.mark.skipif(not checks_module.sandbox_kind(), reason="no sandbox here")


def command_ran(harness, session_id, call_id, text, code, output=""):
    harness.store.execute(
        """INSERT INTO commands(session_id, call_id, command, kind, exit_code, output, started_at,
        ended_at) VALUES (?,?,?,?,?,?,'now','now')""",
        (session_id, call_id, text, "test", code, output),
    )


GOOD = "import asyncio\n\nasync def fetch():\n    await asyncio.sleep(1)\n"
BAD = "import time\n\nasync def fetch():\n    time.sleep(1)\n"


@needs_sandbox
async def test_a_fixed_mistake_becomes_a_check_that_proves_itself_then_runs(tmp_path):
    project = tmp_path / "svc"
    (project / "src").mkdir(parents=True)
    git(project, "init", "-q", "-b", "main")
    (project / "src" / "fetch.py").write_text(GOOD)
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "start")
    harness = Harness(tmp_path / "data", Settings(projects=[str(project)]))
    runtime = await harness.connect()
    worker = await spawn(runtime, harness.router_id, "Make fetch async", project=str(project))
    session_id = worker["session_id"]

    # It made the mistake, the test failed; it fixed it, the test passed.
    (project / "src" / "fetch.py").write_text(BAD)
    command_ran(harness, session_id, "c1", "pytest tests/test_fetch.py", 1, "blocked the loop")
    runtime.checks.on_command(session_id, "c1")
    await settle(runtime)
    (project / "src" / "fetch.py").write_text(GOOD)
    command_ran(harness, session_id, "c2", "pytest", 0, "1 passed")
    runtime.checks.on_command(session_id, "c2")
    await settle(runtime)
    assert git(project, "status", "--porcelain") == "", "the worker's checkout is untouched"
    [case] = harness.store.all("SELECT * FROM failure_cases")
    refs = git(project, "for-each-ref", "--format=%(refname)", "refs/chiefofstaff/")
    assert f"refs/chiefofstaff/{case['id']}/failing" in refs

    [checker] = [
        row for row in harness.store.open_sessions() if harness.store.get(f"checker:{row['id']}")
    ]
    session = harness.session(checker["id"])
    assert session.options["available_tools"] == ["propose_checks"]
    assert "+    await asyncio.sleep(1)" in session.sent[0] and case["id"] in session.sent[0]

    proposal = {
        "case": case["id"],
        "name": "No time.sleep in async code",
        "why": "time.sleep blocks the event loop.",
        "applies_to": ["src/**/*.py"],
    }
    weak = await call_tool(
        runtime, "propose_checks", checker["id"], checks=[{**proposal, "command": "true"}]
    )
    assert "passes on the failing code" in weak.text_result_for_llm
    good = "! git grep -nE '^[[:space:]]+time[.]sleep' -- '*.py'"
    proven = await call_tool(
        runtime, "propose_checks", checker["id"], checks=[{**proposal, "command": good}]
    )
    assert "You are done" in proven.text_result_for_llm
    [card] = [d for d in harness.store.drafts() if d["kind"] == "check"]
    assert (
        card["target"]["failing"]["exit"] == 1
        and "time.sleep" in (card["target"]["failing"]["output"])
    )
    assert card["target"]["fixed"]["exit"] == 0
    await runtime.drafts.resolve(card["id"], DraftDecision(action="answer"))
    assert runtime.checks.view()["checks"][0]["status"] == "active"

    # Another worker makes the same mistake and ends its turn with its answer: the answer is
    # not filed, and the worker hears why. Its next report goes through, flagged.
    other = await spawn(runtime, harness.router_id, "Add a poller", project=str(project))
    (project / "src" / "poll.py").write_text(BAD)
    session = harness.session(other["session_id"])
    await settle(runtime)
    session.emit("assistant.message", content="Added the poller in src/poll.py. " * 8)
    session.emit("session.idle")
    await settle(runtime)
    assert "No time.sleep in async code" in session.sent[-1]
    assert runtime.tasks.get(other["task_id"])["status"] == "running"
    await call_tool(runtime, "report", other["session_id"], status="done", summary="It is fine.")
    task = runtime.tasks.get(other["task_id"])
    assert task["status"] == "review"
    flags = runtime.task_actions.receipts(task)["flags"]
    assert any("No time.sleep in async code" in flag for flag in flags)
    # In a shared folder, what the worker committed during its task counts as changed too.
    (project / "src" / "poll.py").write_text(GOOD)
    git(project, "add", "-A")
    later = datetime.fromisoformat(task["created_at"]).timestamp() + 2  # The worker's commit.
    git(project, "commit", "-q", "-m", "Poll", env={"GIT_COMMITTER_DATE": f"@{int(later)} +0000"})
    shared = {"path": str(project), "shared": True}
    assert await runtime.checks.changed_files(shared) == []
    assert await runtime.checks.changed_files(shared, task["created_at"]) == ["src/poll.py"]


@needs_sandbox
async def test_a_check_cannot_reach_the_network_or_write(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "a.txt").write_text("x")
    _, output = await checks_module.run_sandboxed(
        "echo y > a.txt; curl -s -m 3 https://example.com >/dev/null; echo $?",
        repo,
        [repo],
        tmp_path / "scratch",
    )
    assert (repo / "a.txt").read_text() == "x"
    assert output.strip().splitlines()[-1] != "0", "no network"


async def test_what_accepted_results_cost_by_model(harness):
    runtime = await harness.connect()
    store = harness.store
    for task, status, session, model, credits in [
        ("t1", "done", "w1", "smart", 4.0),
        ("t2", "review", "w2", "smart", 2.0),
        ("t3", "done", "w3", "fast", 1.0),
        ("t4", "done", "helper", "fast", 9.0),
    ]:
        store.execute(
            "INSERT INTO tasks(id, session_id, title, status, created_at, updated_at) "
            "VALUES (?,?,?,?,'now','now')",
            (task, session, task, status),
        )
        store.execute(
            "INSERT INTO runs(id, task_id, session_id, status, started_at) "
            "VALUES (?,?,?,'done','now')",
            ("r" + task, task, session),
        )
        store.record_usage(session, "worker", model, credits, 0, 1, 1, run_id="r" + task)
    store.record_usage("w1", "worker", "tiny", 0.1, 0, 1, 1, run_id="rt1")  # Naming it, say.
    store.set("learner:helper", "1")
    found = {row["model"]: row for row in store.usage(0, 2e10)["outcomes"]}
    assert set(found) == {"smart", "fast"}, "by the model that did most; no helpers"
    assert (found["smart"]["tasks"], found["smart"]["accepted"]) == (2, 1)
    assert found["smart"]["per_accepted"] == pytest.approx(6.1)
    assert found["fast"]["per_accepted"] == pytest.approx(1.0)
    del runtime


async def test_with_learning_from_work_off_nothing_read_before_is_sent(harness):
    runtime = await harness.connect()
    for index in range(30):
        runtime.habits.add("slack", "slack_dm", f"s{index}", f"message {index}")
    harness.store.save_settings(Settings(learn_from_work=False))
    await runtime.learning.maybe_learn()
    await settle(runtime)
    assert not any(harness.store.get(f"learner:{r['id']}") for r in harness.store.open_sessions())
    with pytest.raises(ValueError, match="Nothing new"):
        await runtime.habits.learn_now()


@pytest.mark.parametrize(
    ("failing", "passing", "same"),
    [
        (
            "uv run --with pytest pytest -q 2>&1 | tail -25",
            "uv run pytest -q | tail -3; git status",
            True,
        ),
        ("pytest tests/test_a.py -q", "cd svc && python -m pytest -q", True),
        ("pytest tests/test_a.py", "pytest tests/test_b.py", False),
        ("npm test", "pytest", False),
    ],
)
def test_a_pass_after_a_failure_is_the_same_test_run(failing, passing, same):
    assert checks_module.related(failing, passing) == same


async def test_a_style_never_sets_the_language(tmp_path):
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    for index, text in enumerate(["先说结论", "三行以内", "表格先给"]):
        runtime.habits.add("copilot", "instruction", f"o{index}", text)
    await runtime.learning.learn()
    learner = learner_of(harness)
    ids = [row["id"] for row in harness.store.all("SELECT id FROM observations")]
    for section, text in [
        ("chat", "Writes in short Chinese sentences."),
        ("slack_dm", "Short, 用中文, lowercase."),
    ]:
        refused = await call_tool(
            runtime, "update_style", learner["id"], section=section, text=text, evidence=ids
        )
        assert refused.result_type == "failure" and "Leave the language out" in (
            refused.text_result_for_llm
        )
    # Its lessons came before it wrote the refused style again: its task waits for that.
    lessons = await call_tool(runtime, "propose_lessons", learner["id"], lessons=[])
    assert "call it again for each" in lessons.text_result_for_llm
    assert runtime.tasks.current(learner["id"])["status"] == "running"
    quoted = await call_tool(runtime, "update_style", learner["id"], section="chat",
                             text="Very short (“三行以内”).", evidence=ids)  # fmt: skip
    assert "without quoting" in quoted.text_result_for_llm, "quotes pull answers into a language"
    slack = "lowercase, ends with 🙏 (“on it 🙏”, “好的 🙏”)."
    done = await call_tool(
        runtime, "update_style", learner["id"], section="slack_dm", text=slack, evidence=ids
    )
    assert "version 1" in done.text_result_for_llm, "a Slack style may quote either language"
    assert runtime.tasks.current(learner["id"])["status"] == "running", "chat is still to write"
    kept = "Conclusion first, then details; three lines at most."
    done = await call_tool(
        runtime, "update_style", learner["id"], section="chat", text=kept, evidence=ids
    )
    assert "version 1" in done.text_result_for_llm
    assert runtime.tasks.current(learner["id"])["status"] == "done"
    assert runtime.states[learner["id"]].close_after_turn
