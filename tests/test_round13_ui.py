"""QA round 13 on Home (E48, E53, E55, E56, E58, E59, E62): a task's runs under their line
are one card with it, each a line in the list's columns; a run no longer tracked is not said
to run; a dev server's page is a link of its task, not what it delivered; a finished task
waiting only on runs of unknown status is under Needs you; a task's deliverables are ordered
by what needs a look first, and a waiting run is never folded away; Monitoring's words come
from the last word on a run and say each state once."""

import json

from test_round9_ui import DOM
from test_ui_text import STATIC, run_js

ARTIFACTS = f"const a = await import({json.dumps((STATIC / 'artifacts.js').as_uri())});\n"
BOARD = f"const b = await import({json.dumps((STATIC / 'board.js').as_uri())});\n"
TASKS = f"const k = await import({json.dumps((STATIC / 'tasks.js').as_uri())});\n"
AGO = "const at = (h) => new Date(Date.now() - h * 3600000).toISOString();\n"
# A task with its PR and nine runs (one failed, one waiting for approval, an older one).
NINE = (
    AGO + "const states = ['running', 'running', 'succeeded', 'failed', 'running', 'succeeded',"
    " 'waiting for approval', 'succeeded', 'succeeded'];\n"
    "const runs = states.map((status, i) => ({ kind: 'flow', url: `https://flyte.x/e/${i}`,"
    " title: `Export ${i}`, status, at: at(i + 1) }));\n"
    "const task = (id, title, ...items) => ({ id, title, status: 'done', session_id: 'w',"
    " updated_at: at(0), deliverables: items });\n"
    "const pr = (n, status, h) => ({ kind: 'pr', url: `https://github.com/o/app/pull/${n}`,"
    " title: `app#${n} · Change ${n}`, role: 'created', status, at: at(h) });\n"
    "s.state.snapshot = { settings: { max_workers: 3 }, sessions: [], tasks: [],"
    " artifacts_hidden: [], pr_titles: {} };\n"
)


def test_a_tasks_runs_are_one_card_with_their_line():
    """E48: each run under "9 runs" was indented whole (its kind column too, FLOW again), two
    lines high with only "35m ago" on the second, 2px apart from its line."""
    found = run_js(
        DOM
        + ARTIFACTS
        + NINE
        + "const shipping = task('a', 'Ship the export', pr(7, 'in review', 0), ...runs);\n"
        "const { rows } = a.artifactItems([shipping], () => {});\n"
        "return rows.map((row) => { const node = row.build(); const parts = walk(node);\n"
        "  const of = (name) => parts.find((p) => p !== node"
        " && String(p.className).split(' ').includes(name));\n"
        "  return { cls: node.className, kind: of('a-kind')?.attrs['aria-hidden'] || '',"
        " branch: of('a-branch')?.textContent || '', name: of('a-name')?.textContent,"
        " status: of('a-status')?.textContent, tip: of('a-status')?.attrs['data-tip'],"
        " meta: Boolean(of('a-meta')) }; });"
    )
    header = next(row for row in found if "runs" in row["cls"].split())
    assert "has-children" in header["cls"].split() and header["meta"], header
    children = [row for row in found if "in-runs" in row["cls"].split()]
    assert [row["name"] for row in children] == ["Export 0", "Export 3", "Export 6"]
    for row in children:
        # One line: its time with its status, its kind kept for the columns but not said.
        assert not row["meta"] and row["branch"] == "\u2514" and row["kind"] == "true", row
    # Review: a status in a word or two, so its name is read first ("waiting for approval"
    # squeezed it); the whole of it on hover.
    assert [row["status"] for row in children] == [
        "running · 1h ago",
        "failed · 4h ago",
        "waiting · 7h ago",
    ]
    assert children[2]["tip"].startswith("waiting for approval · 7h ago\n"), children[2]
    assert ["last-in-runs" in row["cls"].split() for row in children] == [False, False, True]
    others = [row for row in found if "in-runs" not in row["cls"].split()]
    assert all(row["meta"] and not row["branch"] and not row["kind"] for row in others)


def test_a_run_no_longer_tracked_is_neither_running_nor_needing_a_look():
    """E53: after Stop tracking, Artifacts still said "running" in blue, counted in "N
    running", "As its worker last said"."""
    found = run_js(
        ARTIFACTS + NINE + "const off = { ...runs[0], said_by: 'you', untracked: true };\n"
        "const shipping = task('a', 'Ship the export', off, ...runs.slice(1, 3));\n"
        "const [item] = a.deliverablesOf(shipping).filter((i) => i.untracked);\n"
        "const [group] = a.groupArtifacts(a.collectArtifacts([shipping]));\n"
        "return { item: [item.status, item.tone, item.by, item.help],"
        " words: group.folded.words, tone: group.folded.tone };"
    )
    assert found["item"] == [
        "not tracked",
        "neutral",
        "you",
        "Not tracked · last reported running: you stopped tracking it",
    ]
    assert (found["words"], found["tone"]) == ("1 running", "active"), found


def test_runs_waiting_are_never_folded_away_and_needing_a_look_comes_first():
    """E58: a run waiting for approval was behind "+N" ("N more, not failed") while its line
    was amber with no word why; a task's merged PR was above another's PR with failing CI;
    the line's tooltip left out what the owner marked."""
    found = run_js(
        ARTIFACTS
        + NINE
        + "const ended = runs.map((r, i) => i === 2 ? { ...r, status: 'ended', said_by: 'you' }"
        " : r);\n"
        "const shipping = task('a', 'Ship the export', pr(7, 'merged', 0), ...ended);\n"
        "const quiet = task('c', 'Tidy the docs', pr(9, 'merged', 0.5),"
        " ...runs.filter((r) => r.status === 'succeeded').map((r, i) => ({ ...r,"
        " url: `https://flyte.x/c/${i}` })));\n"
        "const cache = { ...task('b', 'Tune the cache', pr(8, 'CI failing', 5)),"
        " updated_at: at(5) };\n"
        "const groups = a.groupArtifacts(a.collectArtifacts([shipping, quiet, cache]));\n"
        "return groups.map((g) => ({ task: g.task.id, members: g.members.map((m) => m.block"
        " ? 'runs' : m.item.kind), shown: g.runs.map((r) => r.status),"
        " folded: g.folded && [g.folded.words, g.folded.tone, g.folded.hidden,"
        " g.folded.hiddenWords, g.folded.by] }));"
    )
    shipping, cache, quiet = found
    assert [shipping["task"], cache["task"], quiet["task"]] == ["a", "b", "c"]
    # Its run that failed since (as urgent as the other's failing CI, newer): its group first,
    # that run before its merged PR (which used to come first, above the other's failing CI).
    assert shipping["members"] == ["runs", "PR"]
    # A task with nothing urgent (a merged PR, succeeded runs) is below one with failing CI.
    assert quiet["members"] == ["PR", "runs"]
    assert shipping["shown"] == ["running", "failed", "waiting for approval"]
    assert shipping["folded"] == [
        # Review: what needs a look first first (a narrow list cuts the end off).
        "1 failed · 1 waiting · 3 running",
        "bad",
        6,
        "3 succeeded, 2 running, 1 ended",
        "As their worker last said, or as you marked them",
    ]


def test_a_rows_version_changes_with_who_said_where_it_stands():
    """E58: one marked by the owner since (its worker's word before) kept its old tooltip."""
    found = run_js(
        DOM
        + ARTIFACTS
        + NINE
        + "const one = (by) => a.artifactItems([task('a', 'Ship', { ...runs[2], said_by: by })],"
        " () => {}).rows[0].version;\n"
        "return [one(''), one('you'), one('GitHub')];"
    )
    assert len(set(found)) == 3, found


def test_a_dev_servers_page_is_a_link_of_its_task():
    """E55: a page on this computer or its network is neither a document nor a run: a link of
    its task's, declared (made) or only mentioned in a report (referenced). Review: those its
    reports only mention (localhost:3000, Grafana on :3000, Jenkins on :8443) had vanished
    from its card; a run on a port of its own elsewhere (Airflow on :8080) is a run."""
    found = run_js(
        "const urls = ['http://localhost:3000/app', 'https://devbox.local/report',"
        " 'http://127.0.0.1/x', 'http://192.168.1.4/x', 'http://[::1]:8080/x',"
        " 'http://127.0.0.1:8080/dags/nightly/grid', 'https://grafana.acme.dev:3000/d/abc',"
        " 'https://ci.acme.dev:8443/job/deploy/42/', 'https://airflow.acme.dev:8080/dags/x/grid',"
        " 'https://docs.google.com/document/d/abc123/edit'];\n"
        "const kinds = urls.map((u) => [t.linkKind(u)?.kind, t.linkKind(u)?.role]);\n"
        "s.state.snapshot = { pr_titles: {} };\n"
        "const links = t.taskLinks({ title: 'Preview', artifacts: [{ url: urls[0],"
        " name: 'Preview', status: 'running' }], result: `Opened ${urls[5]}; dashboards at"
        " ${urls[6]} and ${urls[7]}.` });\n"
        "return { kinds, links: links.map((l) => [l.found.kind, l.role, l.name]) };"
    )
    assert found["kinds"] == [["link", "reference"]] * 8 + [["run", "run"], ["doc", "output"]]
    assert found["links"] == [
        ["link", "output", "Preview"],
        ["link", "reference", ""],
        ["link", "reference", ""],
        ["link", "reference", ""],
    ]


def test_a_run_of_unknown_status_says_so_on_artifacts_and_its_chip():
    """Screenshot review: "Run f3a9c1 · running" in blue on Artifacts and on its chip under
    Needs you, beside "Status unknown"; counted as running on its task's runs line."""
    found = run_js(
        ARTIFACTS + NINE + "const said = [{ url: runs[0].url, at: at(47) },"
        " { url: runs[1].url, at: at(60), checked_at: at(30) }];\n"
        "const lost = [{ ...runs[0], unknown: true }, { ...runs[1], status: 'in progress',"
        " said_by: 'GitHub', unknown: true }, ...runs.slice(2, 4)];\n"
        "const shipping = { ...task('a', 'Ship the export', ...lost), artifacts: said };\n"
        "const items = a.deliverablesOf(shipping).filter((i) => i.unknown);\n"
        "const [group] = a.groupArtifacts(a.collectArtifacts([shipping]));\n"
        "return { items: items.map((i) => [i.status, i.tone, i.help]),"
        " words: group.folded.words, shown: group.runs.map((r) => r.name) };"
    )
    assert found["items"] == [
        ["status unknown", "wait", "Status unknown: reported running 47h ago, not checked since"],
        [
            "status unknown",
            "wait",
            "Status unknown: GitHub said in progress 30h ago, not read since",
        ],
    ]
    assert found["words"] == "1 failed · 2 unknown"
    # Never folded away: they wait for the owner's word.
    assert found["shown"] == ["Export 0", "Export 1", "Export 3"]


def test_a_needs_you_row_for_unknown_runs_does_not_repeat_its_result():
    """Screenshot review: its second line was the run's state, then its accepted result (here,
    as often, its own title again)."""
    found = run_js(
        TASKS + "s.state.snapshot = { settings: { max_workers: 3 }, sessions: [], tasks: [] };\n"
        "const task = { id: 't', title: 'Backfill', status: 'done', result: 'Backfill: done.' };\n"
        "return [k.rowLatest(task, null, { section: 'needs', marker: 'runs-unknown' }).text,"
        " k.rowLatest(task, null, { section: 'needs', marker: 'pr-failing' }).text];"
    )
    assert found == ["", "Backfill: done."]


def test_a_finished_task_waiting_only_on_runs_of_unknown_status_needs_the_owner():
    """E56: in Monitoring for days, "Status unknown · reported running 2d ago", its way out
    behind ⋯."""
    found = run_js(
        BOARD + AGO + "const flow = (extra) => ({ key: 'f', kind: 'flow', label: 'Nightly export',"
        " url: 'https://x/f', waiting_for: '', status: 'running', unknown: true,"
        " reported_at: at(49), last_check: null, next_check: null, trigger: '',"
        " changed_at: at(49), ...extra });\n"
        "const pr = { key: 'github:o/app#7', kind: 'pr', label: 'app#7', url: '',"
        " waiting_for: 'approval', last_check: null, next_check: null, trigger: '',"
        " changed_at: at(5) };\n"
        "const task = (...items) => ({ id: 't', status: 'done', monitoring: items });\n"
        "const place = (t) => { const p = b.taskPhase(t, null); return [p.section, p.marker,"
        " p.why, b.statusWord(p)]; };\n"
        "return [place(task(flow({}))), place(task(flow({ unlisted: true }), { ...flow({}),"
        " key: 'g', label: 'Backfill' })), place(task(flow({}), pr))];"
    )
    alone, two, waiting = found
    assert alone == [
        "needs",
        "runs-unknown",
        "Nightly export: reported running 2d ago",
        "Status unknown",
    ]
    assert two[2] == (
        "Nightly export and 1 more: reported running 2d ago · its later report no longer lists it"
    )
    # Something still waited on: Monitoring, as before.
    assert waiting[:2] == ["monitoring", "delivered"]


def test_unknown_runs_words_go_by_the_last_word_and_say_each_state_once():
    """E59: "reported … ago" was its worker's word, though Chief of Staff read it on GitHub
    since; one whose reads fail did not say when it was last read. E62: "status unknown" twice
    in its tooltip."""
    found = run_js(
        BOARD
        + TASKS
        + AGO
        + "s.state.snapshot = { settings: { max_workers: 3 }, sessions: [], tasks: [] };\n"
        "const run = { key: 'f', kind: 'flow', label: 'Deploy', url: 'https://x/f',"
        " waiting_for: '', status: 'in progress', unknown: true, reported_at: at(72),"
        " last_check: at(30), next_check: null, read_error: 'HTTP 502',"
        " trigger: 'Chief of Staff could not read it on GitHub (HTTP 502)', changed_at: at(72) };\n"
        "const task = { id: 't', status: 'done', monitoring: [run] };\n"
        "const phase = b.taskPhase(task, null);\n"
        "return [b.monitorWhy(task), k.waitWords(task, null, phase).help];"
    )
    why, tip = found
    assert why == "Status unknown · reported in progress 30h ago"
    assert tip.count("status unknown") == 1, tip
    assert tip.endswith("could not read it on GitHub (HTTP 502) (last read 1d ago)"), tip


def test_a_tasks_runs_are_taken_off_artifacts_together(tmp_path):
    """E58: hiding a task's nine runs took unfolding them and nine clicks; their line's ✕
    takes them all off (each comes back with Bring back, as one taken off alone)."""
    from conftest import FakeClient, FakeSlackMcp, FakeTokens
    from fastapi.testclient import TestClient
    from test_web import ORIGIN, TOKEN, sign_in

    from chiefofstaff.runtime import Runtime
    from chiefofstaff.sources.slack import SlackSource
    from chiefofstaff.web import create_app

    def factory(store, bus):
        async def notifier(title, text):
            pass

        runtime = Runtime(store, bus, backend_factory=lambda _: FakeClient(), notifier=notifier)
        runtime.slack = SlackSource(store, runtime, tokens=FakeTokens(""), mcp=FakeSlackMcp())
        return runtime

    app = create_app(tmp_path / "data", TOKEN, 8765, runtime_factory=factory)
    with TestClient(app, base_url="http://127.0.0.1:8765") as http:
        sign_in(http)
        # Review: as many as a task can list (20 declared, 60 its reports link); 40 was the cap.
        runs = [{"key": f"flyte.x/e/{index}", "tone": "good"} for index in range(80)]
        first, *rest = runs
        hidden = http.post(
            "/api/artifacts/hide",
            json={"key": first["key"], "tone": "good", "together": rest},
            headers=ORIGIN,
        ).json()["hidden"]
        assert hidden == runs
        back = http.post(
            "/api/artifacts/hide", json={"key": rest[0]["key"], "hidden": False}, headers=ORIGIN
        ).json()["hidden"]
        assert [item["key"] for item in back] == [first["key"]] + [r["key"] for r in rest[1:]]
        # Undo: all of them back at once.
        undone = http.post(
            "/api/artifacts/hide",
            json={"key": first["key"], "hidden": False, "tone": "good", "together": rest},
            headers=ORIGIN,
        ).json()["hidden"]
        assert undone == []
