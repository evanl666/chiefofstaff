"""The UI's text helpers (static/text.js), run with node: links in reports and on chips."""

# ruff: noqa: RUF001  (Chinese punctuation is what these tests are about)

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "src" / "chiefofstaff" / "static"
# Just enough of a browser for the modules to load.
STUBS = """
const store = {};
globalThis.localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; },
  removeItem: (k) => { delete store[k]; } };
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });
globalThis.document = { addEventListener() {}, querySelector() { return null; },
  querySelectorAll() { return []; }, getElementById() { return null; },
  documentElement: { dataset: {} }, body: { classList: { toggle() {}, add() {}, remove() {} } } };
"""


def run_js(body: str):
    """Run `body` (an async function body that returns a JSON-able value) against text.js."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = (
        STUBS
        + f"const t = await import({json.dumps((STATIC / 'text.js').as_uri())});\n"
        + f"const s = await import({json.dumps((STATIC / 'state.js').as_uri())});\n"
        + f"const result = await (async () => {{ {body} }})();\n"
        + "console.log(JSON.stringify(result)); process.exit(0);\n"
    )
    done = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_url_ends_where_the_sentence_goes_on():
    cases = {
        "#1068 https://github.com/o/ranking-model/pull/1068：head `5e8adc1b`": (
            "https://github.com/o/ranking-model/pull/1068"
        ),
        "见 https://github.com/o/r/pull/79”。": "https://github.com/o/r/pull/79",
        "https://github.com/o/r/pull/3608（叠在": "https://github.com/o/r/pull/3608",
        "**https://github.com/evanl666/chiefofstaff**：公开": "https://github.com/evanl666/chiefofstaff",
        "see https://x.com/a/b.": "https://x.com/a/b",
        "(https://x.com/q?a=1&b=2)": "https://x.com/q?a=1&b=2",
    }
    found = run_js(f"return Object.keys({json.dumps(cases)}).map((text) => t.findUrls(text)[0]);")
    assert found == list(cases.values())
    html = run_js("return t.markdown('Opened https://github.com/o/r/pull/1068：head 5e8adc1b');")
    assert 'href="https://github.com/o/r/pull/1068"' in html


def test_links_say_what_they_are():
    kinds = run_js(
        """
        s.state.snapshot = { pr_titles: { "o/r#72": "Schedule weekly embeddings" } };
        return [
          "https://github.com/o/r/pull/72",
          "https://shs-ltx1.grid.example.com/history/application_1700000000000_0042/jobs/",
          "https://flyte.example.com/console/projects/p/domains/d/executions/abc123",
          "https://airflow.example.com/dags/daily_export/grid",
        ].map((url) => [t.linkKind(url).label, t.linkKind(url).title].filter(Boolean).join(" · "))
          .concat([t.markdown("See https://github.com/o/r/pull/72.")]);
        """
    )
    assert kinds[0] == "r#72 · Schedule weekly embeddings"
    assert kinds[1] == "Spark · application_1700000000000_0042"
    assert kinds[2] == "Flyte · abc123"
    assert kinds[3] == "Airflow · daily_export"
    assert "r#72 · Schedule weekly embeddings</a>." in kinds[4]


def test_links_in_angle_brackets_and_with_slack_ampersands():
    html = run_js(
        "return ["
        "t.markdown('See <https://x.com/a?b=1&c=2|the doc> and <https://y.com/z>.'),"
        "t.markdown('Run https://x.com/q?a=1&amp;b=2 now'),"
        "t.findUrls('https://x.com/q?a=1&amp;b=2')[0]];"
    )
    assert 'href="https://x.com/a?b=1&amp;c=2"' in html[0] and ">the doc</a>" in html[0]
    assert 'href="https://y.com/z"' in html[0] and "&gt;" not in html[0].split("y.com/z")[1][:3]
    assert 'href="https://x.com/q?a=1&amp;b=2"' in html[1], "one & in the link, not &amp;"
    assert html[2] == "https://x.com/q?a=1&b=2"


def test_fences_with_any_info_string_and_first_lines_keep_numbers():
    found = run_js(
        "return [t.markdown('```c#\\nvar x = 1;\\n```'), t.markdown('```shell title=x\\nls\\n```'),"
        "t.firstLine('3 tests failed in auth'), t.firstLine('1. Fixed the bug'),"
        "t.firstLine('## Results\\nfix_login works')];"
    )
    assert "<pre" in found[0] and "c#" not in found[0].split("<pre")[1][:40]
    assert "<pre" in found[1]
    assert found[2:] == ["3 tests failed in auth", "Fixed the bug", "Results"]


def board(tasks: list, workers: list, **options):
    """Where each task is on the board (board.js): its section and marker, each section's rows
    (task ids, in order), the reasons, the sidebar's place per worker and the workers at work
    with no row of a task in Running."""
    return run_js(
        f"const b = await import({json.dumps((STATIC / 'board.js').as_uri())});\n"
        f"const workers = new Map({json.dumps(workers)}.map((w) => [w.id, w]));\n"
        f"const options = {json.dumps(options)};\n"
        "options.onCards = new Set(options.onCards || []);\n"
        f"const found = b.boardOf({json.dumps(tasks)}, workers, "
        "{ now: Date.parse('2026-11-07T12:00:00Z'), stallHours: 4, ...options });\n"
        "return { phases: Object.fromEntries([...found.phases].map(([id, p]) =>"
        " [id, [p.section, p.marker].filter(Boolean).join('/')])),"
        " sections: Object.fromEntries(Object.entries(found.sections).map(([k, v]) =>"
        " [k, v.map((t) => t.id)])),"
        " sidebar: Object.fromEntries([...workers.keys()].map((id) => [id, b.workerSection("
        "found.phases, " + json.dumps(tasks) + ".filter((t) => t.session_id === id))])),"
        " whys: Object.fromEntries([...found.phases].map(([id, p]) => [id, p.why])),"
        " followUps: b.followingUp([...workers.values()], " + json.dumps(tasks) + ","
        " new Set(found.sections.running.map((t) => t.session_id))).map((w) => w.id) };"
    )


def task(task_id, session, status="running", at="2026-11-07T11:50:00Z", **extra):
    return {"id": task_id, "session_id": session, "status": status, "title": task_id,
            "created_at": at, "updated_at": at, "active_at": at, **extra}  # fmt: skip


CHANGED = {"changed": {"files": 2, "insertions": 5, "deletions": 1}}


def test_the_board_places_each_task_by_where_it_stands():
    workers = [
        {"id": "w1", "status": "working", "turn_task": "a", "task": {"task_id": "b", "title": "b"}},
        {"id": "w2", "status": "idle", "task": {"task_id": "d"}},
        {"id": "w3", "status": "idle"},
        {"id": "w4", "status": "working", "turn_task": "r"},
    ]
    tasks = [
        task("a", "w1", started=True),  # Its worker's turn is on it.
        task("b", "w1", started=False),  # Same worker, not taken up yet: waits its turn.
        task("p", "w1", started=True),  # Taken up and left for "a": paused, not up next.
        # Same worker, its own check-back: waiting on that, not on its turn.
        task("k", "w1", check_back={"at": "2026-11-07T13:00:00Z", "what": "the run"}),
        task("q", "w1", "waiting"),
        task("c", "w2", check_back={"at": "2026-11-07T13:00:00Z", "what": "the Flyte run"}),
        task("d", "w2", watching=["github:o/r#7"]),
        task("e", "w3", at="2026-11-07T06:00:00Z"),  # 6h quiet, waits on nothing.
        task("f", "w3", "review"),  # A report that only informs: accepted all the same.
        task("v", "w3", "review", receipts=CHANGED),  # Changed code: to accept.
        task("r", "w4", "review", follow_up=True),  # The owner asked more; its worker is at it.
        task("g", "w3", "done", ended_at="2026-11-07T10:00:00Z"),
        task("s", "w3", "done", set_aside=True),  # Cancelled after it finished: not accepted.
        task("h", "w2", "done", watching=["github:o/r#8"]),  # Delivered, still followed.
        task("n", "w2", "done", news={"at": "2026-11-07T11:55:00Z", "text": "merged"}),
        task("x", "w2", "failed"),
        task("y", "w2", check_back={"at": "2026-11-07T11:00:00Z", "what": "x", "attempts": 3}),
    ]
    found = board(tasks, workers)
    assert found["phases"] == {
        "a": "running", "b": "monitoring/turn", "p": "monitoring/paused",
        "k": "monitoring/check-back",
        "q": "needs/question", "c": "monitoring/check-back", "d": "monitoring/watching",
        "e": "monitoring/overdue", "f": "needs/review", "v": "needs/review",
        "r": "running/following", "g": "finished/done", "s": "finished/set-aside",
        "h": "monitoring/delivered",
        "n": "needs/update", "x": "needs/failed", "y": "needs/check-failed",
    }  # fmt: skip
    # A row per task in each place (not a box per worker), each task in one place.
    sections = found["sections"]
    assert set(sections) == {"needs", "running", "monitoring", "finished"}
    assert sorted(sections["running"]) == ["a", "r"]
    assert sorted(sections["needs"]) == ["f", "n", "q", "v", "x", "y"]
    assert sorted(sections["finished"]) == ["g", "s"]
    placed = [task_id for ids in sections.values() for task_id in ids]
    assert sorted(placed) == sorted(t["id"] for t in tasks), "each task in one place"
    assert found["sidebar"] == {"w1": "needs", "w2": "needs", "w3": "needs", "w4": "running"}
    assert found["whys"]["b"] == "Up next: its worker is on “a” now"
    assert found["whys"]["p"] == "Paused: its worker is on “a” now"


def test_a_stale_turn_on_a_reported_task_does_not_hide_the_one_at_work():
    """D2/D3: its turn was last named for A (a note about it, say), A was reported; the worker
    is at work on B. B runs; A is a result, not "following up"; no follow-up card."""
    workers = [{"id": "w1", "status": "working", "turn_task": "a", "task": {"task_id": "b"}}]
    tasks = [
        task("a", "w1", "review", receipts=CHANGED, started=True),
        task("b", "w1", started=True),
    ]
    found = board(tasks, workers)
    assert found["phases"] == {"a": "needs/review", "b": "running"}
    assert found["followUps"] == []
    # Accepted since: still B at work, and no card for the worker beside it.
    tasks[0] = task("a", "w1", "done", started=True)
    found = board(tasks, workers)
    assert found["phases"] == {"a": "finished/done", "b": "running"}
    assert found["followUps"] == []
    # The owner did ask about A since: following up on it, B up next... or paused.
    tasks[0] = task("a", "w1", "review", receipts=CHANGED, started=True, follow_up=True)
    found = board(tasks, workers)
    assert found["phases"] == {"a": "running/following", "b": "monitoring/paused"}


def test_a_worker_at_work_with_nothing_open_gets_a_card_of_its_own():
    workers = [
        {"id": "w1", "status": "working", "turn_task": ""},  # News on a finished task's PR.
        {"id": "w2", "status": "working", "turn_task": ""},  # A running task: shown there.
        {"id": "w3", "status": "idle"},
    ]
    tasks = [task("a", "w1", "done"), task("b", "w2", check_back={"at": "x", "what": "y"})]
    tasks.append(task("c", "w3", "done"))
    found = board(tasks, workers)
    assert found["phases"]["b"] == "running", "its worker is at work: on its only open task"
    assert found["followUps"] == ["w1"]


def test_a_task_on_a_card_or_quiet_briefly_stays_put():
    workers = [{"id": "w1", "status": "idle"}]
    tasks = [
        task("a", "w1", at="2026-11-07T10:00:00Z"),
        task("r", "w1", "review", receipts=CHANGED),
    ]
    found = board(tasks, workers, onCards=["r"])
    assert found["phases"]["a"] == "monitoring/idle", "2h quiet: says it waits on nothing"
    assert found["phases"]["r"] == "review", "on its card, not again: a result to accept there"
    assert found["sections"]["needs"] == []
    assert board(tasks, workers, stallHours=1)["phases"]["a"] == "monitoring/overdue"


def test_news_on_a_result_with_a_draft_card_shows_on_that_card():
    """W3: its worker answered a follow-up with news while a draft for it waits: the news shows
    as an Update on that card (the task's card goes in it), not hidden and not twice."""
    workers = [{"id": "w1", "status": "idle"}]
    news = {"at": "2026-11-07T11:55:00Z", "text": "Answered: the flag is on"}
    tasks = [
        task("r", "w1", "review", receipts=CHANGED, news=news),
        task("p", "w1", "review"),  # A report that only informs, with a reply drafted.
    ]
    found = board(tasks, workers, onCards=["r", "p"])
    # Still a result to accept (QA round 10); its news shows in it (newsBlock, from task.news).
    assert found["phases"] == {"r": "review", "p": "review"}, "a report waits for Accept too"
    assert found["sections"]["needs"] == [], "on the draft's card only"


def test_a_question_about_its_own_draft_is_decided_on_that_card():
    """Bug bash 6 (T6): "approve the draft, or hold it" showed as a Question card beside the
    draft's card: two cards for one decision."""
    workers = [{"id": "w1", "status": "idle"}]
    tasks = [
        task("q", "w1", "waiting", question_draft="d1"),
        task("o", "w1", "waiting"),  # Another question: under Needs you as it was.
    ]
    found = board(tasks, workers, onCards=["q"])
    assert found["phases"] == {"q": "", "o": "needs/question"}
    assert found["sections"]["needs"] == ["o"]


def test_a_follow_up_behind_another_task_is_up_next_not_waiting_on_its_pr():
    """L5: the owner followed up on finished A (it follows PR #59) while its worker runs B: A
    waits its turn after B, not on its PR. The worker's current task may say A already: its
    turn is what counts."""
    workers = [{"id": "w1", "status": "working", "turn_task": "b", "task": {"task_id": "a"}}]
    tasks = [
        task("a", "w1", result="Opened #59", started=True, watching=["github:o/app#59"]),
        task("b", "w1", started=True, at="2026-11-07T11:40:00Z"),
    ]
    found = board(tasks, workers)
    assert found["phases"] == {"a": "monitoring/turn", "b": "running"}
    assert found["whys"]["a"] == "Up next: following up after “b”"


def test_no_answer_to_a_nudge_brings_a_stalled_task_to_the_owner():
    workers = [{"id": "w1", "status": "idle"}]
    quiet = "2026-11-07T06:00:00Z"
    asked = task("a", "w1", at=quiet, nudged_at="2026-11-07T10:30:00Z")
    just = task("b", "w1", at=quiet, nudged_at="2026-11-07T11:30:00Z")
    found = board([asked, just], workers)
    assert found["phases"] == {"a": "needs/stalled", "b": "monitoring/overdue"}


def test_recently_finished_is_newest_first_across_workers():
    def ended(task_id, session, hour):
        return task(task_id, session, "done", ended_at=f"2026-11-07T{hour:02d}:00:00Z")

    workers = [{"id": "w1", "status": "idle"}, {"id": "w2", "status": "idle"}]
    # A busy worker's old results do not hide another's newer ones; a row each.
    tasks = [ended(f"a{h}", "w1", h) for h in range(1, 6)] + [ended("b9", "w2", 9)]
    tasks += [ended("b10", "w2", 10), ended("a11", "w1", 11)]
    found = board(tasks, workers)
    assert found["sections"]["finished"] == ["a11", "b10", "b9", "a5", "a4", "a3", "a2", "a1"]


def test_monitoring_is_one_list_and_a_quiet_one_stays_monitored():
    """QA E12/E16/E18: Monitoring is one list (no "delivered" and "reviewed" groups); an
    accepted task still waiting on others stays there however quiet ("No updates for 26h"),
    not with the finished ones; someone else's PR the owner reviewed is no obligation of theirs:
    finished (news there comes as an update); one PR a worker follows for two tasks is one
    row; overdue comes first."""
    workers = [{"id": "w1", "status": "idle"}, {"id": "w2", "status": "idle"}]
    day_ago = "2026-11-06T10:00:00Z"
    awaiting = {"prs": [{"label": "o/r#3", "own": True, "state": "OPEN", "awaiting": ["lead"]}]}
    tasks = [
        task("own", "w1", "done", at="2026-11-07T11:40:00Z", watching=["github:o/r#1"],
             watch_kind="own", watch_changed_at="2026-11-07T11:30:00Z"),
        task("rev", "w2", "done", watching=["github:o/r#2"], watch_kind="reviewed"),
        # Not accepted yet: under Needs you, whatever it follows.
        task("old", "w2", "review", at="2026-11-07T08:00:00Z", watching=["github:o/r#2"],
             watch_kind="reviewed"),
        task("quiet", "w1", "done", at="2026-11-07T10:00:00Z", watching=["github:o/r#3"],
             watch_kind="own", watch_changed_at=day_ago, receipts=awaiting),
        # The same PR for the same worker (split off, say): the newer one has the row.
        task("split", "w1", "done", at="2026-11-07T09:00:00Z", watching=["github:o/r#3"],
             watch_kind="own", watch_changed_at=day_ago),
        task("news", "w1", "done", watching=["github:o/r#4"], watch_changed_at=day_ago,
             news={"at": "2026-11-07T11:00:00Z", "text": "merged"}),
        task("wait", "w1", watching=["github:o/r#5"], at="2026-11-07T11:58:00Z"),
        task("late", "w2", at="2026-11-07T06:00:00Z"),
    ]  # fmt: skip
    found = board(tasks, workers)
    assert found["phases"] == {
        "own": "monitoring/delivered", "rev": "finished/reviewed", "old": "needs/review",
        "quiet": "monitoring/delivered", "split": "finished/still-following",
        "news": "needs/update", "wait": "monitoring/watching", "late": "monitoring/overdue",
    }  # fmt: skip
    assert found["sections"]["monitoring"] == ["late", "wait", "own", "quiet"]
    assert found["whys"]["quiet"] == "Waiting for approval · No updates for 26h"
    assert found["whys"]["own"] == "Following o/r#1", "quiet for less than an hour: not said"
    assert found["whys"]["split"] == "Still following, for “quiet”"
    assert found["sidebar"] == {"w1": "needs", "w2": "needs"}


def test_monitoring_follows_the_servers_obligations():
    """QA E16/E18: what a task waits on is the server's `monitoring` when it has it: an empty
    list is nothing to wait on (finished), whatever it still follows; one there is monitored,
    said by what it waits for."""
    workers = [{"id": "w1", "status": "idle"}]
    pr = {"key": "github:o/r#9", "kind": "pr", "label": "o/r#9",
          "url": "https://github.com/o/r/pull/9", "waiting_for": "CI",
          "last_check": "2026-11-07T11:55:00Z", "next_check": "2026-11-07T12:10:00Z",
          "trigger": "CI finishing wakes it", "changed_at": "2026-11-07T09:00:00Z"}  # fmt: skip
    tasks = [
        task("ci", "w1", "done", monitoring=[pr]),
        # Followed, but the server says it waits on nothing of the owner's.
        task("none", "w1", "done", watching=["github:o/r#8"], watch_kind="own", monitoring=[]),
        # A check-back is said apart: what it waits for is its PR (one it waits on alone, that).
        task("back", "w1", "done", monitoring=[
            {**pr, "key": "checkback:back", "kind": "check_back", "waiting_for": "the rerun"},
            {**pr, "key": "github:o/r#10", "waiting_for": "review", "changed_at": None}]),
        task("alone", "w1", "done", monitoring=[
            {**pr, "key": "checkback:alone", "kind": "check_back", "waiting_for": "the rerun",
             "changed_at": None}]),
        # Open and following a PR for itself: it waits on it, whatever the server counts.
        task("open", "w1", "running", started=True, watching=["github:o/r#5"], monitoring=[]),
        # Running, its worker idle: waiting on a flow it started.
        task("flow", "w1", "running", started=True, monitoring=[{
            "key": "flow:abc", "kind": "flow", "label": "Nightly", "url": "", "waiting_for":
            "the nightly run", "last_check": None, "next_check": None, "trigger": "",
            "changed_at": None}]),
    ]  # fmt: skip
    found = board(tasks, workers)
    assert found["phases"] == {
        "ci": "monitoring/delivered", "none": "finished/done", "flow": "monitoring/watching",
        "open": "monitoring/watching", "back": "monitoring/delivered",
        "alone": "monitoring/delivered",
    }  # fmt: skip
    assert found["whys"]["back"] == "Waiting for review"
    assert found["whys"]["alone"] == "Waiting for the rerun"
    assert found["whys"]["open"] == "Following o/r#5"
    assert found["whys"]["ci"] == "Waiting for CI · No updates for 3h"
    assert found["whys"]["flow"] == "Waiting for the nightly run"


def test_every_result_waits_for_accept_and_is_never_finished_before_it():
    """QA E1/E2: a report that only informs went to Recently finished on its own, with a Done
    button; every result is under Needs you as "Awaiting acceptance" until Accept, then it is
    finished (or monitored while it still waits on others). Cancelled or set aside is over,
    never done."""
    workers = [{"id": "w1", "status": "idle"}]
    tasks = [
        task("report", "w1", "review", result="The flag is on in prod."),
        task("asked", "w1", "review", review_required=True, attention="acceptance",
             attention_why="You asked to see it first"),
        task("accepted", "w1", "done", result="The flag is on in prod."),
        task("cancelled", "w1", "cancelled", result="Cancelled by you."),
        task("aside", "w1", "done", set_aside=True, watching=["github:o/r#1"]),
    ]  # fmt: skip
    found = board(tasks, workers)
    assert found["phases"] == {
        "report": "needs/review", "asked": "needs/review", "accepted": "finished/done",
        "cancelled": "finished/cancelled", "aside": "finished/set-aside",
    }  # fmt: skip
    assert found["whys"]["asked"] == "You asked to see it first"
    words = tasks_js(
        "const phase = (section, marker) => ({ section, marker, why: '' });\n"
        "const b = await import('" + (STATIC / "board.js").as_uri() + "');\n"
        "return [b.statusWord(phase('needs', 'review')), b.statusWord(phase('running', '')),"
        " b.statusWord(phase('finished', 'cancelled')),"
        " b.statusWord(phase('finished', 'set-aside')),"
        " k.finishedWords({ status: 'done', set_aside: true, result: 'Wrote it.' },"
        " phase('finished', 'set-aside')),"
        " k.finishedWords({ status: 'done', result: '' }, phase('finished', 'done'))];"
    )
    assert words == ["Awaiting acceptance", "Running", "Cancelled", "Set aside",
                     "Set aside, not accepted: Wrote it.", "Accepted"]  # fmt: skip


def test_running_is_a_row_per_task_and_a_background_process_is_said():
    """QA E16/E19/E20, E13: Running has a row per task (two workers, two rows; not a box per
    worker), in the order they came, not by their latest step; a worker whose turn is over
    while a command it left running goes on is waiting for that process, not idle."""
    workers = [
        {"id": "w1", "status": "working", "turn_task": "a"},
        {"id": "w2", "status": "idle", "background": True, "turn_task": "b"},
        {"id": "w3", "status": "working", "turn_task": "c"},
    ]
    tasks = [
        task("a", "w1", started=True, at="2026-11-07T11:00:00Z"),
        task("b", "w2", started=True, at="2026-11-07T11:30:00Z"),
        task("c", "w3", started=True, at="2026-11-07T11:58:00Z"),
        # Another task of a worker whose process runs: not what that process is for.
        task("d", "w2", started=True, at="2026-11-07T11:20:00Z"),
    ]
    tasks[0]["updated_at"] = "2026-11-07T11:59:00Z"  # Its latest step: it does not move up.
    found = board(tasks, workers)
    assert found["phases"] == {
        "a": "running", "b": "running/background", "c": "running", "d": "monitoring/idle",
    }  # fmt: skip
    assert found["sections"]["running"] == ["c", "b", "a"]
    assert found["whys"]["b"] == "Waiting for background process"


def test_a_delivered_task_is_placed_by_where_its_own_pr_stands():
    """E12: with its PR's CI failing, changes requested or ready to merge, a delivered task
    stayed in Monitoring as "Delivered" while Artifacts said otherwise: both use prStatus now."""
    workers = [{"id": "w1", "status": "idle"}]

    def own(number, **pr):
        label = f"o/r#{number}"
        prs = [{"label": label, "own": True, "state": "OPEN", **pr}]
        return task(f"t{number}", "w1", "done", watching=[f"github:{label}"], watch_kind="own",
                    watch_changed_at="2026-11-07T11:00:00Z", receipts={"prs": prs})  # fmt: skip

    tasks = [
        own(1, checks="failing", failing=["lint"]),
        own(2, review="CHANGES_REQUESTED", checks="failing"),
        own(3, checks="passing", merge_state="CLEAN"),
        own(4, checks="pending"),
        own(5, checks="failing", draft=True),  # A draft: not done yet, whatever its CI says.
        # Someone else's PR it reviewed: what the author does about it is theirs (finished).
        task(
            "t6",
            "w1",
            "done",
            watching=["github:o/r#6"],
            watch_kind="reviewed",
            receipts={
                "prs": [{"label": "o/r#6", "reviewed": True, "state": "OPEN", "checks": "failing"}]
            },
        ),
    ]
    found = board(tasks, workers)
    assert found["phases"] == {
        "t1": "needs/pr-failing", "t2": "needs/pr-changes", "t3": "monitoring/mergeable",
        "t4": "monitoring/delivered", "t5": "monitoring/delivered", "t6": "finished/reviewed",
    }  # fmt: skip
    assert found["whys"]["t1"] == "CI failing on r#1 (lint)"
    assert found["whys"]["t2"] == "Changes requested on r#2"
    assert found["whys"]["t3"] == "Ready to merge: r#3"
    assert found["whys"]["t4"] == "Waiting for CI · No updates for 1h"
    assert found["whys"]["t5"] == "Waiting for review · No updates for 1h"
    assert sorted(found["sections"]["monitoring"]) == ["t3", "t4", "t5"]
    # Its status in Artifacts is the same word.
    words = run_js(
        f"const a = await import({json.dumps((STATIC / 'board.js').as_uri())});\n"
        f"return {json.dumps([t['receipts']['prs'][0] for t in tasks[:3]])}"
        ".map((pr) => a.prStatus(pr)[0]);"
    )
    assert words == ["CI failing", "changes requested", "ready to merge"]


def test_a_report_the_router_wants_looked_at_and_a_task_after_a_stopped_one():
    """E10: review_required is a result to review, report or not. #4: a task its worker has not
    taken up, behind one the owner stopped, is up next after Go on, not "nothing to wait on"."""
    workers = [{"id": "w1", "status": "idle"}]
    tasks = [
        task("mock", "w1", "review", review_required=True),
        task("s", "w1", "waiting", stopped="you"),
        task("next", "w1", started=False),
    ]
    found = board(tasks, workers)
    assert found["phases"] == {
        "mock": "needs/review",
        "s": "needs/stopped",
        "next": "monitoring/turn",
    }
    assert found["whys"]["next"] == "Up next: after you say Go on to “s”"


def test_urls_keep_their_underscores_and_parentheses_and_bad_escapes_do_not_break():
    found = run_js(
        """
        return [
          t.findUrls("see https://en.wikipedia.org/wiki/Foo_(bar) now")[0],
          t.findUrls("(https://x.com/a/b_)")[0],
          t.findUrls("path https://x.com/run_ ok")[0],
          t.findUrls("[doc](https://x.com/a_(b)).")[0],
          t.linkKind("https://example.com/report%zz").label,
          t.markdown("[x](https://x.com/a_(b))"),
          t.markdown("bad https://example.com/report%zz link"),
        ];
        """
    )
    assert found[:4] == [
        "https://en.wikipedia.org/wiki/Foo_(bar)",
        "https://x.com/a/b_",
        "https://x.com/run_",
        "https://x.com/a_(b)",
    ]
    assert found[4] == "example.com · report%zz"
    assert 'href="https://x.com/a_(b)"' in found[5]
    assert 'href="https://example.com/report%zz"' in found[6]


def test_links_have_a_role_on_a_card():
    roles = run_js(
        """
        const urls = [
          "https://github.com/o/r/pull/7",
          "https://docs.google.com/document/d/abc123/edit",
          "https://docs.google.com/",
          "https://flyte.example.com/console/projects/p/domains/d/executions/abc123",
          "https://flyte.example.com/console/projects/p/domains/d/executions/abc123/logs",
          "https://shs.example.com/history/application_1700000000000_0042/jobs/",
          "https://box-rdev-1.example.com/admin/airflow/dags/daily_export/grid",
          "https://box-rdev-1.example.com/debug",
          "https://github.com/o/r/actions/runs/12",
          "https://github.com/o/r/issues/3",
        ];
        return urls.map((url) => t.linkKind(url).role);
        """
    )
    assert roles == [
        "pr", "output", "reference", "run", "diagnostic", "diagnostic", "run", "diagnostic",
        "diagnostic", "reference",
    ]  # fmt: skip


def test_artifacts_in_one_list_with_where_each_stands():
    found = run_js(
        f"const a = await import({json.dumps((STATIC / 'artifacts.js').as_uri())});\n"
        + """
        s.state.snapshot = { pr_titles: {} };
        const pr = (n, extra) => ({ label: `acme/app#${n}`, url: `https://github.com/acme/app/pull/${n}`,
          state: "OPEN", checks: "passing", at: "2026-11-07T11:00:00Z", ...extra });
        const sheet = "https://docs.google.com/spreadsheets/d/1SheetABC_x/edit";
        const tasks = [
          { id: "t1", status: "running", title: "Run the ICP flow",
            updated_at: "2026-11-07T11:30:00Z",
            receipts: { prs: [pr(1, { own: true, checks: "failing" }), pr(2, { own: false }),
              pr(4, { role: "referenced" }),
              pr(5, { role: "reviewed", at: "2026-11-07T11:25:00Z" })] },
            artifacts: [
              { url: "https://flyte.example.com/console/projects/p/domains/d/executions/abc",
                name: "ICP flow", status: "running", at: "2026-11-07T11:20:00Z" },
              { url: sheet, name: "Weekly sheet", status: "draft", at: "2026-11-07T11:00:00Z" },
              // Not deliverables: a mockup file, a plain link, a CI run.
              { url: "file:///work/mock5.html", path: "/work/mock5.html", name: "mock5.html",
                status: "draft", at: "2026-11-07T11:00:00Z" },
              { url: "https://example.com/dash", name: "Dashboard", status: "live",
                at: "2026-11-07T11:00:00Z" },
              { url: "https://github.com/acme/app/actions/runs/77", name: "CI", status: "failed",
                at: "2026-11-07T11:00:00Z" },
            ],
            files: [{ path: "/work/out.csv", name: "out.csv", size: 2048 }],
            // A doc an earlier report said it made (the latest does not repeat it), named as
            // that report linked it, the same document again with a query; one it only
            // mentions, docs.google.com itself, a Spark application under the run, links in
            // code and one cut short.
            links: ["https://docs.google.com/document/d/1DocMadeEarly/edit"],
            made_links: ["https://docs.google.com/document/d/1DocMadeEarly/edit"],
            link_names: { "https://docs.google.com/document/d/1DocMadeEarly/edit": "Early doc" },
            progress: "Doc: https://docs.google.com/document/d/1DocMadeEarly/edit?usp=sharing "
              + "see [the spec](https://docs.google.com/document/d/1OnlyMentioned/edit) and "
              + "https://docs.google.com/ "
              + "Spark https://shs.example.com/history/application_1700000000000_0042/jobs/\\n"
              + "`[x](https://docs.google.com/document/d/1InCodeDoc/edit)` "
              + "https://docs.google.com/document/d/1CutShort… "
              + "```\\nhttps://not-a-link.example.com/x\\n```" },
          { id: "t2", status: "done", title: "Review #2", updated_at: "2026-11-07T10:00:00Z",
            receipts: { prs: [pr(2, { own: true, state: "MERGED" }),
              pr(3, { draft: true, own: true })] },
            artifacts: [{ url: sheet + "?usp=sharing", name: "Weekly sheet", status: "final",
              at: "2026-11-07T11:10:00Z" }],
            // Given to it to work from: not its output.
            given_links: ["https://docs.google.com/document/d/1GivenDocC"],
            result: "Read https://docs.google.com/document/d/1GivenDocC/edit?usp=sharing",
            goal: "See https://docs.google.com/document/d/1GivenDocC" },
          // A report that only informs: accepted all the same.
          { id: "t3", status: "review", title: "Explain the flow",
            updated_at: "2026-11-07T09:00:00Z",
            result: "Wrote [the plan](https://docs.google.com/document/d/1PlanDoc/edit) and "
              + "https://docs.google.com/spreadsheets/d/1NumbersSheet/edit",
            made_links: ["https://docs.google.com/document/d/1PlanDoc/edit?usp=sharing",
              "https://docs.google.com/spreadsheets/d/1NumbersSheet/edit"] },
        ];
        const phases = new Map([["t3", { section: "needs", marker: "review" }]]);
        return a.collectArtifacts(tasks, phases)
          .map((i) => [i.kind, i.role, i.name, i.status, i.tone, i.task.id]);
        """
    )
    assert found == [
        ["PR", "created", "app#1", "CI failing", "bad", "t1"],
        ["Doc", "", "the plan", "to accept", "wait", "t3"],
        ["Doc", "", "Sheet", "to accept", "wait", "t3"],
        ["Doc", "", "Early doc", "in progress", "active", "t1"],
        ["PR", "reviewed", "app#5", "in review", "active", "t1"],
        ["Flow", "", "ICP flow", "running", "active", "t1"],
        ["PR", "created", "app#3", "draft", "active", "t2"],
        ["Doc", "", "Weekly sheet", "final", "good", "t2"],
        ["PR", "created", "app#2", "merged", "good", "t2"],
    ], (
        "PRs it made, changed or reviewed, docs and flows only (no file, link, log or CI run); "
        "worst first, then newest; one row per document, the newer word on it"
    )


def test_a_pr_is_ready_to_merge_when_github_would_merge_it():
    found = run_js(
        f"const a = await import({json.dumps((STATIC / 'artifacts.js').as_uri())});\n"
        """
        const pr = (extra) => a.prStatus({ state: "OPEN", checks: "passing", ...extra });
        return [
          pr({ merge_state: "CLEAN" }), pr({ merge_state: "CLEAN", review: "CHANGES_REQUESTED" }),
          pr({ merge_state: "CLEAN", checks: "pending" }),
          pr({ merge_state: "HAS_HOOKS", review: "APPROVED" }),
          pr({ merge_state: "BLOCKED", review: "APPROVED" }), pr({}),
          pr({ merge_state: "CLEAN", draft: true }),
        ].map(([word]) => word);
        """
    )
    assert found == ["ready to merge", "changes requested", "CI running", "ready to merge",
                     "approved", "in review", "draft"]  # fmt: skip


def test_what_a_task_made_is_what_it_declared_or_says_it_made():
    """E1/E9: a link is its output when its worker declared it or a report says it made it;
    what it only mentions, docs.google.com itself and links in code are not. A document is
    named as its worker or a report named it, else by its kind: never by its id."""
    found = run_js(
        """
        const doc = (id) => `https://docs.google.com/document/d/${id}/edit`;
        const links = t.taskLinks({
          title: "Eval SLM", goal: "",
          artifacts: [{ url: doc("1Declared"), name: "", status: "draft" },
            { url: "file:///work/scratch/mock5.html", path: "/work/scratch/mock5.html",
              name: "mock5.html", status: "draft" }],
          files: [{ path: "/work/scratch/notes.md", name: "notes.md", size: 2048 },
            { path: "/work/scratch/mock5.html", name: "mock5.html", size: 10 }],
          made_links: [doc("1SPkOjC0abc") + "?usp=sharing"],
          given_links: [doc("1Given")],
          result: "Made [qwen3_8b SFT 11-05](" + doc("1SPkOjC0abc") + "), read " + doc("1Given")
            + ", see [the bug report](" + doc("1Mentioned") + ") and https://docs.google.com/ "
            + "`" + doc("1InCode") + "` https://flyte.example.com/console/projects/p/domains/d/executions/run1",
        });
        return links.map((l) => [l.found.kind, l.role, t.linkName(l), l.path]);
        """
    )
    assert found == [
        ["doc", "output", "Google Doc", ""],
        ["file", "output", "mock5.html", "/work/scratch/mock5.html"],
        ["doc", "output", "qwen3_8b SFT 11-05", ""],
        ["doc", "reference", "Google Doc", ""],
        ["doc", "reference", "the bug report", ""],
        ["link", "reference", "docs.google.com", ""],
        ["run", "run", "Flyte · run1", ""],
        ["file", "output", "notes.md", "/work/scratch/notes.md"],
    ]


def test_slack_ids_read_as_names():
    found = run_js(
        """
        s.state.snapshot = { slack_names: { U0OWNER1: "Evan", D0BOB0001: "Bob", C0ENG: "eng" } };
        return [
          t.slackText("Channel: <#C0DEPLOYS> (deploys) | From: <@U0BOB001> (Bob) | "
            + "ts: 1712345678.000100\\nText: hi <@U0OWNER1>"),
          t.slackText("DM <#D0BOB0001> ts: 1712345700.000400 <#C0ENG|eng> (see above) <@U0NOBODY>"),
          t.threadLabel("slack:D0BOB0001"), t.threadLabel("slack:C0ENG:1712345678.000100"),
          t.threadLabel("slack:D0UNKNOWN"),
        ];
        """
    )
    assert found == [
        "Channel: #deploys | From: @Bob\nText: hi @Evan",
        "DM with Bob #eng (see above) @U0NOBODY",
        "Slack DM with Bob", "Slack thread in #eng", "Slack DM",
    ]  # fmt: skip


def test_what_a_slack_session_writes_reads_without_ids_and_stamps():
    found = run_js(
        """
        s.state.snapshot = { slack_names: { D0ALICE01: "DM with Alice", U0ALICE1: "Alice Chen",
          U0OWNER1: "you", C0ENG: "eng" } };
        return [
          t.slackText('Slack event (DM with Alice Chen, thread slack:D0ALICE01:1794141072.949527)'
            + ': "hey <@U0OWNER1>, ping <@U0BOB001>" (ts 1794141072.949527). Please look.'),
          t.slackText("Alice Chen (DM D0ALICE01, ts 1794141072.949527) asked her and Bob "
            + "(U0BOB001). Alice Chen (U0ALICE1) and Alice (U0ALICE1) in C0ENG."),
          t.slackText("https://acme.slack.com/archives/D0ALICE01/p1794141072949527 (UTF16LE) "
            + "W3C U2 C0FFEE1 parts 1794141072.949527"),
        ];
        """
    )
    assert found == [
        'Slack event (DM with Alice Chen, thread in DM with Alice): "hey @you, ping @U0BOB001". '
        "Please look.",
        "Alice Chen (DM with Alice) asked her and Bob. Alice Chen and Alice (@Alice Chen) in #eng.",
        "https://acme.slack.com/archives/D0ALICE01/p1794141072949527 (UTF16LE) W3C U2 C0FFEE1 "
        "parts 1794141072.949527",
    ], "links, words and ids no name is known for stay"


def test_a_status_in_words_has_its_tone_negations_too():
    tones = run_js(
        f"const a = await import({json.dumps((STATIC / 'artifacts.js').as_uri())});\n"
        """
        return ["no errors", "0 failures", "error-free", "incomplete", "not ready",
          "unpublished", "partially complete", "ready for review", "cancelled", "aborted",
          "failed", "succeeded", "running", "final", "paused", "awaiting approval",
          "timed out after 2h", "Timed_out", "fails on CI", "crash", "OOM killed", "not failed",
          "did not fail", "failed: not found", "0 errors, 2 failures", "no errors; 3 tests failed",
          "Not failed yet, still running", "didn't fail", "no errors but 2 warnings"]
          .map((s) => [s, a.toneOf(s)]);
        """
    )
    assert dict(tones) == {
        "no errors": "good", "0 failures": "good", "error-free": "good",
        "incomplete": "active", "not ready": "active", "unpublished": "active",
        "partially complete": "active", "ready for review": "wait", "cancelled": "muted",
        "aborted": "muted", "failed": "bad", "succeeded": "good", "running": "active",
        "final": "good", "paused": "active", "awaiting approval": "wait",
        "timed out after 2h": "bad", "Timed_out": "bad", "fails on CI": "bad", "crash": "bad",
        "OOM killed": "bad", "not failed": "good", "did not fail": "good",
        "failed: not found": "bad", "0 errors, 2 failures": "bad",
        "no errors; 3 tests failed": "bad", "Not failed yet, still running": "active",
        "didn't fail": "good", "no errors but 2 warnings": "neutral",
    }  # fmt: skip


def test_lines_keep_code_and_underscores_as_the_message_shows_them():
    """Bug bash #18-#21: a card's line said "init.py" for `__init__.py`; images showed as "!";
    a bare link took the "'s" after it; two Google docs had the same label."""
    found = run_js(
        """
        return {
          first: t.firstLine("**Fixed** `__init__.py` and __init__.py"),
          plain: t.plainText("Edit `__init__.py`, format_money; **bold** ![chart](https://x.com/a.png)"),
          image: t.markdown("![chart](https://x.com/a.png)"),
          apostrophe: t.markdown("see https://x.com/abc's page"),
          urls: t.findUrls("it's https://x.com/a'b here"),
          keys: [
            t.linkKey("https://docs.google.com/document/d/ABCDEFGH123/edit?usp=sharing"),
            t.linkKey("https://docs.google.com/document/d/ABCDEFGH123/view#h"),
            t.linkKey("https://www.X.com/a/b/?q=1#f"),
            t.linkKey("https://airflow.example.com/graph?dag_id=a&utm_source=x"),
            t.linkKey("https://airflow.example.com/graph?dag_id=b"),
            t.linkKey("https://acme.cloud.databricks.com/?o=1#job/2/run/3"),
            t.linkKey("https://acme.cloud.databricks.com/?o=1#job/2/run/4"),
          ],
          flyte: t.linkKind("https://example.com/console/projects/p/domains/d/executions/abc123"),
          docs: [
            t.linkKind("https://docs.google.com/document/d/ABCDEFGH123/edit"),
            t.linkKind("https://docs.google.com/document/d/ZYXWVUT9876/edit"),
          ].map((found) => [found.label, found.id]),
        };
        """
    )
    assert found["first"] == "Fixed __init__.py and __init__.py"
    assert found["plain"] == "Edit __init__.py, format_money; bold chart"
    assert ">\U0001f5bc chart</a>" in found["image"] and "<img" not in found["image"]
    assert (
        'href="https://x.com/abc"' in found["apostrophe"]
        and "</a>&#39;s page" in found["apostrophe"]
    )
    assert found["urls"] == ["https://x.com/a"]
    assert found["keys"][0] == found["keys"][1] == "gdoc:ABCDEFGH123"
    assert found["keys"][2] == "x.com/a/b?q=1", "what tells it apart stays; a place in it not"
    assert found["keys"][3] == "airflow.example.com/graph?dag_id=a" != found["keys"][4]
    assert found["keys"][5] != found["keys"][6]
    assert found["flyte"]["label"] == "Flyte · abc123" and found["flyte"]["role"] == "run"
    # Named by what they are; their ids tell them apart (linkKey) and are on hover.
    assert found["docs"] == [["Google Doc", "ABCDEFGH123"], ["Google Doc", "ZYXWVUT9876"]]


def test_a_stopped_task_waits_for_the_owner():
    found = board(
        [task("s", "w1", "waiting", stopped="owner"), task("o", "w1", started=True)],
        [{"id": "w1", "status": "working", "turn_task": "o"}],
    )
    assert found["phases"] == {"s": "needs/stopped", "o": "running"}


def test_a_link_it_was_given_is_not_its_output_and_cut_or_generic_links_say_nothing():
    """G8b: a link the task was given stays a reference though a report says it made it
    (declared, or a file it wrote, it is output). G9b: a link cut with "..." is no link. G10b:
    "here", "this doc", "链接" are not a doc's name: it is named by its kind."""
    found = run_js(
        """
        const doc = (id) => `https://docs.google.com/document/d/${id}/edit`;
        const links = t.taskLinks({
          title: "Summarize the spec", goal: "",
          given_links: [doc("1GivenSpec"), doc("1GivenDeclared")],
          made_links: [doc("1GivenSpec"), doc("1OwnDoc")],
          artifacts: [{ url: doc("1GivenDeclared"), name: "", status: "final" }],
          result: "Wrote [here](" + doc("1OwnDoc") + "), from [this doc](" + doc("1GivenSpec")
            + ") and [链接](" + doc("1GivenDeclared") + "). See "
            + "https://docs.google.com/document/d/1CutShort... "
            + "and https://docs.google.com/document/d/1CutUni…",
        });
        return links.map((l) => [l.key, l.role, t.linkName(l)]);
        """
    )
    assert found == [
        ["gdoc:1GivenDeclared", "output", "Google Doc"],
        ["gdoc:1OwnDoc", "output", "Google Doc"],
        ["gdoc:1GivenSpec", "reference", "Google Doc"],
    ]


def test_a_line_keeps_what_follows_a_link_and_a_notice_keeps_its_whole_title():
    """H10: "#44 (https://…/pull/44) is open" kept its ")"; U7: a title with " · " in it is
    the title's, not where the notice's aside starts. U16b: a file too large to count is said
    so, not "+0 −0"."""
    found = run_js(
        """
        return {
          gist: [
            t.gist("Opened #44 (https://github.com/o/cos-approve-test/pull/44) is open."),
            t.gist("Doc: https://docs.google.com/document/d/abc/edit. Done (see https://x.com/a/b)."),
          ],
          heads: [
            t.headOf("Started “Benchmark words() · textkit” · new because the owner asked"),
            t.headOf("New task “Fix A · B” for “Worker · two”"),
            t.headOf("Told “Fix it”"),
          ],
          changes: [
            t.changesText({ files: 1, insertions: 0, deletions: 0, large: 1 }),
            t.changesText({ files: 3, insertions: 5, deletions: 1, large: 2 }),
            t.changesText({ files: 2, insertions: 5, deletions: 1 }),
          ],
          others: [t.notCountedText({ files: 1, not_counted: 2 }), t.notCountedText({ files: 1 })],
        };
        """
    )
    assert found["gist"] == [
        "Opened #44 (cos-approve-test#44) is open.",
        "Doc: docs.google.com… Done (see x.com…).",
    ]
    assert found["heads"] == [
        "Started “Benchmark words() · textkit”",
        "New task “Fix A · B” for “Worker · two”",
        "Told “Fix it”",
    ]
    assert found["changes"] == [
        "1 large new file",
        "+5 −1 in 1 file · 2 large new files",
        "+5 −1 in 2 files",
    ]
    assert found["others"] == ["not counted: 2 files", ""]


def test_changes_requested_wins_over_failing_ci():
    """H9: one word for a PR everywhere: changes requested before CI failing."""
    found = run_js(
        f"const a = await import({json.dumps((STATIC / 'artifacts.js').as_uri())});\n"
        """
        return [
          a.prStatus({ state: "OPEN", checks: "failing", review: "CHANGES_REQUESTED" }),
          a.prStatus({ state: "OPEN", checks: "failing", review: "" }),
        ];
        """
    )
    assert found == [["changes requested", "bad"], ["CI failing", "bad"]]


def test_queued_rows_in_start_order_and_a_follow_up_is_up_next():
    """L6: queued rows in the order they start (the oldest first), after the rest. L7: a
    finished task the owner followed up on runs again at once, but its worker is on another
    task: it is up next, not paused."""
    workers = [
        {"id": "w1", "status": "working", "turn_task": "on"},
        {"id": "w2", "status": "queued"},
    ]
    tasks = [
        task("on", "w1", started=True),
        task("again", "w1", started=True, result="Fixed it.", at="2026-11-07T11:40:00Z"),
        task("left", "w1", started=True, at="2026-11-07T11:30:00Z"),
        task("q2", "w2", "queued", at="2026-11-07T11:55:00Z"),
        task("q1", "w2", "queued", at="2026-11-07T11:10:00Z"),
        task("q3", "w2", "queued", at="2026-11-07T11:58:00Z"),
    ]
    tasks[4]["updated_at"] = "2026-11-07T11:59:00Z"  # Updated last, created first.
    found = board(tasks, workers)
    assert found["phases"]["again"] == "monitoring/turn"
    assert found["whys"]["again"] == "Up next: following up after “on”"
    assert found["phases"]["left"] == "monitoring/paused"
    assert found["sections"]["monitoring"] == ["again", "left", "q1", "q2", "q3"]


def tasks_js(body: str):
    """Run `body` against tasks.js (`k`), text.js (`t`) and state.js (`s`)."""
    return run_js(f"const k = await import({json.dumps((STATIC / 'tasks.js').as_uri())});\n{body}")


def test_slack_conversations_it_follows_are_named_once():
    """L6/A7: a worker following two DMs showed "Watching a Slack thread" twice, and a DM with a
    thread in it read "a Slack thread; a Slack thread": each by its name, once."""
    found = tasks_js(
        """
        s.state.snapshot = { slack_names: { D0BOB0001: "Bob", D0ANN0001: "Ann",
          C0DEPLOYS: "deploys" } };
        const keys = ["slack:D0BOB0001:1712345678.000100", "slack:D0BOB0001", "slack:D0ANN0001",
          "slack:C0DEPLOYS:1712345600.000100", "slack:C0DEPLOYS:1712345700.000100",
          "github:o/app#7", "slack:D0NONAME1"];
        return k.followedOnce(keys).map(k.threadWords);
        """
    )
    assert found == ["DM with Bob", "DM with Ann", "a thread in #deploys", "app#7", "a Slack DM"]


def test_artifacts_are_the_deliverables_the_server_names():
    """QA E17/E25/E26: Artifacts and a card's chips are a task's `deliverables`: its PRs with
    their part in it (created, modified, reviewed; never one it only referenced), the docs it
    wrote and the flows it ran, each by its title; a PR two tasks share is the one that made
    it; a set-aside task's doc reads so, not "delivered"."""
    found = run_js(
        f"const a = await import({json.dumps((STATIC / 'artifacts.js').as_uri())});\n"
        """
        s.state.snapshot = { pr_titles: {} };
        const url = (n) => `https://github.com/o/app/pull/${n}`;
        const doc = "https://docs.google.com/document/d/1SetAsideDoc/edit";
        const receipt = { label: "o/app#7", url: url(7), state: "OPEN", checks: "passing",
          review: "APPROVED", own: true, at: "2026-11-07T11:00:00Z" };
        const tasks = [
          { id: "t1", status: "done", title: "Fix it", updated_at: "2026-11-07T11:30:00Z",
            receipts: { prs: [receipt] },
            deliverables: [
              { kind: "pr", url: url(7), title: "Fix the exporter", role: "created" },
              { kind: "pr", url: url(8), title: "Their PR", role: "referenced" },
              // Named as the server names it (its number first): not "app#9 · app#9 · …".
              { kind: "pr", url: url(9), title: "app#9 · Bob's PR", role: "reviewed",
                status: "approved" },
              { kind: "flow", url: "https://airflow.example.com/dags/nightly/grid",
                title: "Nightly export", status: "succeeded", at: "2026-11-07T11:10:00Z" },
              { kind: "file", url: "file:///work/mock.html", title: "mock.html" },
              { kind: "link", url: "https://example.com/x", title: "A link" },
            ] },
          // Pushed to the PR t1 opened: t1's row (it made it).
          { id: "t2", status: "review", title: "Push a fix", updated_at: "2026-11-07T11:40:00Z",
            deliverables: [{ kind: "pr", url: url(7), title: "Fix the exporter",
              role: "modified" }] },
          { id: "t3", status: "done", set_aside: true, title: "Not wanted",
            updated_at: "2026-11-07T11:05:00Z",
            deliverables: [{ kind: "doc", url: doc, title: "The plan" }] },
        ];
        return a.collectArtifacts(tasks, new Map())
          .map((i) => [i.kind, i.role, i.name, i.status, i.tone, i.task.id]);
        """
    )
    assert found == [
        ["PR", "reviewed", "app#9 · Bob's PR", "approved", "good", "t1"],
        ["Flow", "", "Nightly export", "succeeded", "good", "t1"],
        ["PR", "created", "app#7 · Fix the exporter", "approved", "good", "t1"],
        ["Doc", "", "The plan", "set aside", "muted", "t3"],
    ]


def test_the_diff_reads_names_git_quotes_and_says_large_new_files():
    """S8: a name git quotes ("it's \\"quoted\\" ü") read as the quoted pair in the diff's
    header. S9: a new file too large to show was missing from the diff: said with its size."""
    found = run_js(
        f"const d = await import({json.dumps((STATIC / 'diff.js').as_uri())});\n"
        r"""
        const quoted = '"b/docs/it\'s \\"quoted\\" (draft) \\303\\274.md"';
        const diff = [
          `diff --git ${quoted.replace('"b/', '"a/')} ${quoted}`,
          "new file mode 100644", "--- /dev/null",
          `+++ ${quoted}`, "@@ -0,0 +1 @@", "+hi",
          'diff --git a/tab\there.txt "b/tab\\there.txt"', "deleted file mode 100644",
          "diff --git a/plain name.txt b/plain name.txt", "--- a/plain name.txt",
          "+++ b/plain name.txt", "@@ -1 +1 @@", "-a", "+b",
          'diff --git a/old.md "b/new \\"one\\".md"', "rename from old.md",
          'rename to "new \\"one\\".md"',
        ].join("\n");
        return [d.parseDiff(diff).map((f) => [f.name, f.note, f.added, f.removed]),
          d.largeWords({ name: "data/huge.bin", size: 21 * 1024 * 1024 }),
          d.largeWords({ name: "x.csv", size: 300 * 1024 })];
        """
    )
    assert found[0] == [
        ['docs/it\'s "quoted" (draft) ü.md', "", 1, 0],
        ["tab\there.txt", "Deleted", 0, 0],
        ["plain name.txt", "", 1, 1],
        ['new "one".md', "Renamed from old.md", 0, 0],
    ]
    assert found[1:] == ["New file, 21 MB, not shown", "New file, 300 KB, not shown"]


def test_a_draft_on_the_owners_own_pr_is_a_reply_not_a_review():
    """S16: comments answering the reviewer on the owner's own PR read "Review of …#58"."""
    found = run_js(
        f"const d = await import({json.dumps((STATIC / 'drafts.js').as_uri())});\n"
        "return [d.reviewWords({ own: true }), d.reviewWords({ own: false })];"
    )
    assert found[0] == {"label": "Reply on ", "open": "Reply…", "send": "Send reply"}
    assert found[1]["label"] == "Review of " and found[1]["open"] == "Review…"


def test_what_cancelling_came_to_and_a_cancelled_line_said_once():
    """A2: cancelling a task that had finished sets it aside: the toast says so, not "Task
    cancelled.". A6: "Cancelled: Cancelled by you." said "Cancelled" twice."""
    found = tasks_js(
        """
        const cancelled = (progress) => k.finishedWords(
          { status: "cancelled", result: "", progress }, { section: "finished" });
        return [k.cancelledWords({ status: "done", set_aside: true }),
          k.cancelledWords({ status: "cancelled" }),
          cancelled("Cancelled by you."), cancelled("The bucket is gone."), cancelled("")];
        """
    )
    assert found == [
        "It had finished: set aside, its result is kept.",
        "Task cancelled.",
        "Cancelled by you.",
        "Cancelled: The bucket is gone.",
        "Cancelled",
    ]


def test_an_unreadable_follow_says_why():
    """E11: "Can't read app#72" whatever the reason; it reads the PR when only its CI checks
    could not be read, and no access is not GitHub being slow. QA E10: by the server's kind,
    with its sentence for the owner; GitHub slow or its rate limit passes by itself (Retry),
    no access or not found does not (stop following)."""
    found = tasks_js(
        "const key = 'github:acme/app#72';\n"
        "const kinds = [{kind: 'access', step: 'pull request', error: 'HTTP 404'},"
        " {kind: 'access', step: 'CI checks', error: 'HTTP 403'},"
        " {kind: 'transient', step: 'reviews', error: 'GitHub request timed out.'}, {},"
        " {kind: 'permission', error: 'Your accounts cannot see acme/app.'},"
        " {kind: 'not_found', error: 'acme/app#72 is gone.'},"
        " {kind: 'rate_limited', error: 'Too many requests.', retry_at: '2026-11-07T12:30:00Z'},"
        " {kind: 'permission', step: 'CI checks', error: 'Its checks are private.'}];\n"
        "return [kinds.map((why) => k.failingWords(key, why)[0]),"
        " k.failingWords(key, kinds[4])[1], k.failingWords(key, kinds[6])[1].includes('Too many'),"
        " kinds.map((why) => k.PASSING.has(why.kind)), k.failingWords(key, kinds[7])[1]];"
    )
    assert found[0] == [
        "No access to app#72", "Can't read app#72's CI checks",
        "GitHub not answering for app#72", "Can't read app#72", "No access to app#72",
        "app#72 not found", "GitHub's rate limit for app#72", "Can't read app#72's CI checks",
    ]  # fmt: skip
    assert found[1].endswith("Your accounts cannot see acme/app."), "the server's own words"
    assert "GitHub said" not in found[1]
    assert found[2]
    assert found[3] == [False, False, True, False, False, False, True, False]
    assert found[4].endswith(" Its checks are private."), "the server's words, not GitHub's"


def test_one_row_for_a_followed_pr_keeps_the_task_that_needs_you():
    """Bug bash 7 (R2, R4): a newer task of the same worker following the same PR pushed the
    "Changes requested" one to the finished; a PR that could not be read lately took its task
    off Home (placed as if it followed nothing)."""
    workers = [{"id": "w1", "status": "idle"}]
    prs = [{"label": "o/r#9", "own": True, "state": "OPEN", "review": "CHANGES_REQUESTED"}]
    tasks = [
        task("older", "w1", "done", at="2026-11-07T08:00:00Z", watching=["github:o/r#9"],
             watch_changed_at="2026-11-07T11:00:00Z", receipts={"prs": prs}),
        task("newer", "w1", "done", at="2026-11-07T11:30:00Z", watching=["github:o/r#9"],
             watch_changed_at="2026-11-07T11:00:00Z"),
        task("unread", "w1", "done", watch_unreadable=["github:o/r#5"],
             watch_changed_at="2026-11-07T11:00:00Z",
             receipts={"prs": [{"label": "o/r#5", "own": True, "state": "OPEN",
                                "checks": "failing"}]}),
    ]  # fmt: skip
    found = board(tasks, workers)
    assert found["phases"]["older"] == "needs/pr-changes"
    assert found["phases"]["newer"] == "finished/still-following"
    assert found["phases"]["unread"] == "needs/pr-failing"


def test_python_and_the_page_say_where_a_pr_stands_the_same_way():
    """Bug bash 7 (R1): Home placed a task by its PR, the Router and the daily summary did not:
    one rule, in both."""
    from chiefofstaff.task_actions import pr_status

    prs = [
        {"state": "MERGED"}, {"state": "CLOSED"}, {"state": "OPEN", "draft": True},
        {"state": "OPEN", "review": "CHANGES_REQUESTED", "checks": "failing"},
        {"state": "OPEN", "checks": "failing"}, {"state": "OPEN", "awaiting": ["lead"]},
        {"state": "OPEN", "checks": "pending"}, {"state": "OPEN", "merge_state": "CLEAN"},
        {"state": "OPEN", "review": "APPROVED", "merge_state": "BEHIND"}, {"state": "OPEN"},
    ]  # fmt: skip
    page = run_js(
        f"const b = await import({json.dumps((STATIC / 'board.js').as_uri())});\n"
        f"return {json.dumps(prs)}.map((pr) => b.prStatus(pr)[0]);"
    )
    assert page == [pr_status(pr) for pr in prs]


def test_a_result_to_accept_with_news_or_its_pr_to_fix_is_still_awaiting_acceptance():
    """QA round 10: a review task with news, or whose own PR has changes requested, showed as
    "Update" or "Changes requested", with no Request changes and Accept hidden."""
    workers = [{"id": "w1", "status": "idle"}]
    news = {"at": "2026-11-07T11:55:00Z", "text": "CI passes now"}
    label = "o/r#1"
    prs = [{"label": label, "own": True, "state": "OPEN", "review": "CHANGES_REQUESTED"}]
    tasks = [
        task("n", "w1", "review", news=news),
        task("p", "w1", "review", watching=[f"github:{label}"], watch_kind="own",
             receipts={"prs": prs}),
        task("d", "w1", "done", news=news),
    ]  # fmt: skip
    found = board(tasks, workers)
    assert found["phases"] == {"n": "needs/review", "p": "needs/review", "d": "needs/update"}
    assert found["whys"]["n"] == "News: CI passes now"
    assert found["whys"]["p"] == "Changes requested on r#1"


def test_a_monitored_task_checked_a_moment_ago_says_just_now():
    """QA round 10 review: "checked just ago"."""
    words = run_js(
        f"const k = await import({json.dumps((STATIC / 'tasks.js').as_uri())});\n"
        "const at = (minutes) => new Date(Date.now() + minutes * 60000).toISOString();\n"
        "const row = (last) => ({ id: 't', status: 'done', monitoring: [{ key: 'github:o/app#7',"
        " kind: 'pr', label: 'o/app#7', url: '', waiting_for: 'approval', last_check: last,"
        " next_check: at(10.5), trigger: 'A review', changed_at: at(-60) }] });\n"
        "return [k.monitorBits(row(at(0))).checks, k.monitorBits(row(at(-5))).checks];"
    )
    assert words == ["checked just now · next in 10 min", "checked 5 min ago · next in 10 min"]


def test_a_rows_delivery_line_follows_the_owners_message_only():
    """QA round 10 review: the time was taken after the request (a slow one left "Sending…"
    for ten minutes), a Router message took the row's place, and "Handled" never showed."""
    found = run_js(
        f"const k = await import({json.dumps((STATIC / 'tasks.js').as_uri())});\n"
        f"const s = await import({json.dumps((STATIC / 'state.js').as_uri())});\n"
        "const task = { id: 't1', session_id: 'w1' };\n"
        "const at = Date.now();\n"
        "k.noteSent(task, at);\n"
        "const out = [k.sentState(task)];\n"
        "const msg = (id, kind, delivery, ms) => s.noteDelivery({ id, kind, delivery,"
        " session_id: 'w1', created_at: new Date(at + ms).toISOString() });\n"
        "msg(5, 'owner', 'received', 7000);  // Stored 7 s into a slow request.\n"
        "out.push(k.sentState(task));\n"
        "msg(6, 'router', 'sent', 8000);\n"
        "out.push(k.sentState(task));\n"
        "msg(5, 'owner', 'processed', 7000);\n"
        "out.push(k.sentState(task), k.sentState(task, Date.now() + 61000));\n"
        "return out;"
    )
    assert found == ["sending", "received", "received", "processed", ""]
    gone = run_js(
        f"const k = await import({json.dumps((STATIC / 'tasks.js').as_uri())});\n"
        "const task = { id: 't2', session_id: 'w2' };\n"
        "k.noteSent(task, Date.now());  // Nothing sent after all (a Go on clicked twice).\n"
        "return [k.sentState(task), k.sentState(task, Date.now() + 31000)];"
    )
    assert gone == ["sending", ""]
