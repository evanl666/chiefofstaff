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
    """Where each task is on the board (board.js), and the boxes per section."""
    return run_js(
        f"const b = await import({json.dumps((STATIC / 'board.js').as_uri())});\n"
        f"const workers = new Map({json.dumps(workers)}.map((w) => [w.id, w]));\n"
        f"const options = {json.dumps(options)};\n"
        "options.onCards = new Set(options.onCards || []);\n"
        f"const found = b.boardOf({json.dumps(tasks)}, workers, "
        "{ now: Date.parse('2026-11-07T12:00:00Z'), stallHours: 4, ...options });\n"
        "return { phases: Object.fromEntries([...found.phases].map(([id, p]) => [id, p.section])),"
        " boxes: Object.fromEntries(Object.entries(found.sections).map(([k, v]) =>"
        " [k, v.map((e) => [e.sessionId, e.tasks.map((t) => t.id)])])) };"
    )


def task(id, session, status="running", at="2026-11-07T11:50:00Z", **extra):
    return {"id": id, "session_id": session, "status": status, "title": id, "created_at": at,
            "updated_at": at, "active_at": at, **extra}  # fmt: skip


def test_the_board_places_each_task_by_where_it_stands():
    workers = [
        {"id": "w1", "status": "working", "turn_task": "a", "task": {"task_id": "b", "title": "b"}},
        {"id": "w2", "status": "idle", "task": {"task_id": "d"}},
        {"id": "w3", "status": "idle"},
        {"id": "w4", "status": "working", "turn_task": "r"},
    ]
    tasks = [
        task("a", "w1"),  # Its worker's turn is on it.
        task("b", "w1"),  # Same worker, waits its turn.
        task("q", "w1", "waiting"),
        task("c", "w2", check_back={"at": "2026-11-07T13:00:00Z", "what": "the Flyte run"}),
        task("d", "w2", watching=["github:o/r#7"]),
        task("e", "w3", at="2026-11-07T06:00:00Z"),  # 6h quiet, waits on nothing.
        task("f", "w3", "review"),
        task("r", "w4", "review"),  # The owner asked more; its worker is at it.
        task("g", "w3", "done", ended_at="2026-11-07T10:00:00Z"),
    ]
    found = board(tasks, workers)
    assert found["phases"] == {
        "a": "running", "b": "queued", "q": "needs", "c": "monitoring", "d": "monitoring",
        "e": "stalled", "f": "review", "r": "running", "g": "finished",
    }  # fmt: skip
    boxes = found["boxes"]
    assert sorted(boxes["running"]) == [["w1", ["a"]], ["w4", ["r"]]]
    assert boxes["monitoring"] == [["w2", ["c", "d"]]] or boxes["monitoring"] == [
        ["w2", ["d", "c"]]
    ]
    assert [entry[0] for entry in boxes["needs"]] == ["w1"], "w1 shows in three sections"
    assert [entry[0] for entry in boxes["queued"]] == ["w1"]


def test_a_task_on_a_card_or_quiet_briefly_stays_put():
    workers = [{"id": "w1", "status": "idle"}]
    tasks = [task("a", "w1", at="2026-11-07T10:00:00Z"), task("r", "w1", "review")]
    found = board(tasks, workers, onCards=["r"])
    assert found["phases"]["a"] == "running", "2h quiet is not stalled yet"
    assert "r" not in [t for _, ids in found["boxes"]["review"] for t in ids], "on its card"
    assert board(tasks, workers, stallHours=1)["phases"]["a"] == "stalled"
