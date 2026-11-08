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
        "#1068 https://github.com/o/ad-targeting-model/pull/1068：head `5e8adc1b`": (
            "https://github.com/o/ad-targeting-model/pull/1068"
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
    """Where each task is on the board (board.js): its section and marker, the boxes per
    section, and the finished ones in order."""
    return run_js(
        f"const b = await import({json.dumps((STATIC / 'board.js').as_uri())});\n"
        f"const workers = new Map({json.dumps(workers)}.map((w) => [w.id, w]));\n"
        f"const options = {json.dumps(options)};\n"
        "options.onCards = new Set(options.onCards || []);\n"
        f"const found = b.boardOf({json.dumps(tasks)}, workers, "
        "{ now: Date.parse('2026-11-07T12:00:00Z'), stallHours: 4, ...options });\n"
        "return { phases: Object.fromEntries([...found.phases].map(([id, p]) =>"
        " [id, [p.section, p.marker].filter(Boolean).join('/')])),"
        " boxes: Object.fromEntries(Object.entries(found.sections).map(([k, v]) =>"
        " [k, v.map((e) => [e.sessionId, e.tasks.map((t) => t.id)])])),"
        " finished: found.finished.map((t) => t.id),"
        " pages: b.finishedBoxes(found.finished, options.limit || 30)"
        ".map((box) => [box.sessionId, box.tasks.map((t) => t.id)]),"
        " sidebar: Object.fromEntries([...workers.keys()].map((id) => [id, b.workerSection("
        "found.phases, " + json.dumps(tasks) + ".filter((t) => t.session_id === id))])),"
        " whys: Object.fromEntries([...found.phases].map(([id, p]) => [id, p.why])),"
        " followUps: b.followingUp([...workers.values()], " + json.dumps(tasks) + ","
        " new Set(found.sections.running.map((e) => e.sessionId))).map((w) => w.id) };"
    )


def task(id, session, status="running", at="2026-11-07T11:50:00Z", **extra):
    return {"id": id, "session_id": session, "status": status, "title": id, "created_at": at,
            "updated_at": at, "active_at": at, **extra}  # fmt: skip


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
        task("f", "w3", "review"),  # A report that only informs: read, not accepted.
        task("v", "w3", "review", receipts=CHANGED),  # Changed code: to review.
        task("r", "w4", "review", follow_up=True),  # The owner asked more; its worker is at it.
        task("g", "w3", "done", ended_at="2026-11-07T10:00:00Z"),
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
        "e": "monitoring/overdue", "f": "finished/report", "v": "needs/review",
        "r": "running/following", "g": "finished/done", "h": "monitoring/delivered",
        "n": "needs/update", "x": "needs/failed", "y": "needs/check-failed",
    }  # fmt: skip
    boxes = found["boxes"]
    assert sorted(boxes["running"]) == [["w1", ["a"]], ["w4", ["r"]]]
    assert sorted(entry[0] for entry in boxes["monitoring"]) == ["w1", "w2", "w3"]
    assert sorted(entry[0] for entry in boxes["needs"]) == ["w1", "w2", "w3"]
    assert set(boxes) == {"needs", "running", "monitoring"}, "four places, finished apart"
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
    assert found["phases"]["r"] == "", "on its card, not again"
    assert found["boxes"]["needs"] == []
    assert board(tasks, workers, stallHours=1)["phases"]["a"] == "monitoring/overdue"


def test_no_answer_to_a_nudge_brings_a_stalled_task_to_the_owner():
    workers = [{"id": "w1", "status": "idle"}]
    quiet = "2026-11-07T06:00:00Z"
    asked = task("a", "w1", at=quiet, nudged_at="2026-11-07T10:30:00Z")
    just = task("b", "w1", at=quiet, nudged_at="2026-11-07T11:30:00Z")
    found = board([asked, just], workers)
    assert found["phases"] == {"a": "needs/stalled", "b": "monitoring/overdue"}


def test_recently_finished_is_newest_first_across_workers_a_page_at_a_time():
    def ended(id, session, hour):
        return task(id, session, "done", ended_at=f"2026-11-07T{hour:02d}:00:00Z")

    workers = [{"id": "w1", "status": "idle"}, {"id": "w2", "status": "idle"}]
    # A busy worker's old results do not hide another's newer ones.
    tasks = [ended(f"a{h}", "w1", h) for h in range(1, 6)] + [ended("b9", "w2", 9)]
    tasks += [ended("b10", "w2", 10), ended("a11", "w1", 11)]
    found = board(tasks, workers, limit=4)
    assert found["finished"] == ["a11", "b10", "b9", "a5", "a4", "a3", "a2", "a1"]
    assert found["pages"] == [["w1", ["a11"]], ["w2", ["b10", "b9"]], ["w1", ["a5"]]]


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
          "https://docs.google.com/document/d/abc/edit",
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
        "pr", "output", "run", "diagnostic", "diagnostic", "run", "diagnostic", "diagnostic",
        "reference",
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
            receipts: { prs: [pr(1, { own: true, checks: "failing" }), pr(2, { own: false })] },
            artifacts: [
              { url: "https://flyte.example.com/console/projects/p/domains/d/executions/abc",
                name: "ICP flow", status: "running", at: "2026-11-07T11:20:00Z" },
              { url: sheet, name: "Weekly sheet", status: "draft", at: "2026-11-07T11:00:00Z" },
            ],
            // A doc an earlier report gave (the latest does not repeat it), the same document
            // again with a query, a Spark application under the run, a link in code.
            links: ["https://docs.google.com/document/d/1DocMadeEarly/edit"],
            progress: "Doc: https://docs.google.com/document/d/1DocMadeEarly/edit?usp=sharing "
              + "Spark https://shs.example.com/history/application_1700000000000_0042/jobs/\\n"
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
        ];
        return a.collectArtifacts(tasks).map((i) => [i.kind, i.name, i.status, i.tone, i.task.id]);
        """
    )
    assert found == [
        ["PR", "app#1", "CI failing", "bad", "t1"],
        ["Doc", "1DocMade", "in progress", "active", "t1"],
        ["Flyte", "ICP flow", "running", "active", "t1"],
        ["PR", "app#3", "draft", "active", "t2"],
        ["Sheet", "Weekly sheet", "final", "good", "t2"],
        ["PR", "app#2", "merged", "good", "t2"],
    ], "worst first, then newest; own PRs only; one row per document, the newer word on it"


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
            t.linkKey("https://airflow.corp/graph?dag_id=a&utm_source=x"),
            t.linkKey("https://airflow.corp/graph?dag_id=b"),
            t.linkKey("https://acme.cloud.databricks.com/?o=1#job/2/run/3"),
            t.linkKey("https://acme.cloud.databricks.com/?o=1#job/2/run/4"),
          ],
          flyte: t.linkKind("https://example.com/console/projects/p/domains/d/executions/abc123"),
          docs: [
            t.linkKind("https://docs.google.com/document/d/ABCDEFGH123/edit").label,
            t.linkKind("https://docs.google.com/document/d/ZYXWVUT9876/edit").label,
          ],
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
    assert found["keys"][3] == "airflow.corp/graph?dag_id=a" != found["keys"][4]
    assert found["keys"][5] != found["keys"][6]
    assert found["flyte"]["label"] == "Flyte · abc123" and found["flyte"]["role"] == "run"
    assert found["docs"] == ["Google Doc · ABCDEFGH", "Google Doc · ZYXWVUT9"]


def test_a_stopped_task_waits_for_the_owner():
    found = board(
        [task("s", "w1", "waiting", stopped="owner"), task("o", "w1", started=True)],
        [{"id": "w1", "status": "working", "turn_task": "o"}],
    )
    assert found["phases"] == {"s": "needs/stopped", "o": "running"}
