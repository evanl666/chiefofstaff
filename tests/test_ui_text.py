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
    """Where each task is on the board (board.js): its section and marker, the boxes per
    section, Monitoring's rows by group, and the finished ones in order."""
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
        " monitoring: Object.fromEntries(Object.entries(found.monitoring).map(([k, v]) =>"
        " [k, v.map((t) => t.id)])),"
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


def test_recently_finished_is_newest_first_across_workers():
    def ended(id, session, hour):
        return task(id, session, "done", ended_at=f"2026-11-07T{hour:02d}:00:00Z")

    workers = [{"id": "w1", "status": "idle"}, {"id": "w2", "status": "idle"}]
    # A busy worker's old results do not hide another's newer ones; a row each, no boxes.
    tasks = [ended(f"a{h}", "w1", h) for h in range(1, 6)] + [ended("b9", "w2", 9)]
    tasks += [ended("b10", "w2", 10), ended("a11", "w1", 11)]
    found = board(tasks, workers)
    assert found["finished"] == ["a11", "b10", "b9", "a5", "a4", "a3", "a2", "a1"]


def test_monitoring_keeps_deliveries_and_reviewed_prs_apart_and_lets_quiet_ones_go():
    """E6: the owner's deliveries waiting on others and PRs they reviewed are groups of their
    own; one PR a worker follows for two tasks is one row; a day with nothing new there and it
    is with the finished ones, still followed (news brings it back); overdue comes first."""
    workers = [{"id": "w1", "status": "idle"}, {"id": "w2", "status": "idle"}]
    day_ago = "2026-11-06T10:00:00Z"
    tasks = [
        task("own", "w1", "done", watching=["github:o/r#1"], watch_kind="own",
             watch_changed_at="2026-11-07T09:00:00Z"),
        task("rev", "w2", "done", watching=["github:o/r#2"], watch_kind="reviewed"),
        # The same PR for the same worker: one split off the other, say; the newer shows.
        task("old", "w2", "review", at="2026-11-07T08:00:00Z", watching=["github:o/r#2"],
             watch_kind="reviewed"),
        task("quiet", "w1", "done", watching=["github:o/r#3"], watch_changed_at=day_ago),
        task("news", "w1", "done", watching=["github:o/r#4"], watch_changed_at=day_ago,
             news={"at": "2026-11-07T11:00:00Z", "text": "merged"}),
        task("wait", "w1", watching=["github:o/r#5"], at="2026-11-07T11:58:00Z"),
        task("late", "w2", at="2026-11-07T06:00:00Z"),
    ]  # fmt: skip
    found = board(tasks, workers)
    assert found["phases"] == {
        "own": "monitoring/delivered", "rev": "monitoring/reviewed",
        "old": "finished/still-following", "quiet": "finished/still-following",
        "news": "needs/update", "wait": "monitoring/watching", "late": "monitoring/overdue",
    }  # fmt: skip
    assert found["monitoring"] == {"waiting": ["late", "wait"], "delivered": ["own"],
                                   "reviewed": ["rev"]}  # fmt: skip
    assert found["whys"]["quiet"] == "Still following; nothing new there for 26h"
    assert found["whys"]["old"] == "Still following, for “rev”"
    assert found["sidebar"] == {"w1": "needs", "w2": "monitoring"}


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
            receipts: { prs: [pr(1, { own: true, checks: "failing" }), pr(2, { own: false })] },
            artifacts: [
              { url: "https://flyte.example.com/console/projects/p/domains/d/executions/abc",
                name: "ICP flow", status: "running", at: "2026-11-07T11:20:00Z" },
              { url: sheet, name: "Weekly sheet", status: "draft", at: "2026-11-07T11:00:00Z" },
            ],
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
          // A report that only informs (Home says so): "reported", not "to review".
          { id: "t3", status: "review", title: "Explain the flow",
            updated_at: "2026-11-07T09:00:00Z",
            result: "Wrote [the plan](https://docs.google.com/document/d/1PlanDoc/edit) and "
              + "https://docs.google.com/spreadsheets/d/1NumbersSheet/edit",
            made_links: ["https://docs.google.com/document/d/1PlanDoc/edit?usp=sharing",
              "https://docs.google.com/spreadsheets/d/1NumbersSheet/edit"] },
        ];
        const phases = new Map([["t3", { section: "finished", marker: "report" }]]);
        return a.collectArtifacts(tasks, phases)
          .map((i) => [i.kind, i.name, i.status, i.tone, i.task.id]);
        """
    )
    assert found == [
        ["PR", "app#1", "CI failing", "bad", "t1"],
        ["Doc", "Early doc", "in progress", "active", "t1"],
        ["Flyte", "ICP flow", "running", "active", "t1"],
        ["PR", "app#3", "draft", "active", "t2"],
        ["Doc", "the plan", "reported", "neutral", "t3"],
        ["Sheet", "Sheet", "reported", "neutral", "t3"],
        ["Sheet", "Weekly sheet", "final", "good", "t2"],
        ["PR", "app#2", "merged", "good", "t2"],
    ], "worst first, then newest; own PRs only; one row per document, the newer word on it"


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
        "DM DM with Bob #eng (see above) @U0NOBODY",
        "Slack DM with Bob", "Slack thread in #eng", "Slack DM",
    ]  # fmt: skip


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
    assert found["monitoring"]["waiting"] == ["again", "left", "q1", "q2", "q3"]
