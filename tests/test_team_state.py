"""Teams lite in the page (static/teamstate.js): where a team stands is Home's own placement of
its tasks (board.js), its pending cards and questions; run with node."""

import json

from test_ui_text import STATIC, run_js

# Every place and marker board.js's taskPhase gives a task, and the part state it is.
PHASES = {
    "needs/blocked": "needs", "needs/settings": "needs", "needs/stopped": "needs",
    "needs/question": "needs", "needs/failed": "needs", "needs/check-failed": "needs",
    "needs/review": "needs", "needs/update": "needs", "needs/prepared": "needs",
    "needs/kept-paused": "needs", "needs/kept-stopped": "needs", "needs/pr-changes": "needs",
    "needs/pr-conflicts": "needs", "needs/pr-failing": "needs", "needs/runs-unknown": "needs",
    "needs/stalled": "needs", "/": "needs", "/review": "needs", "/update": "needs",
    "running/": "working", "running/following": "working", "running/terminal": "working",
    "running/background": "working",
    "monitoring/resets": "watching", "monitoring/queued": "next", "monitoring/check-back": "watching",
    "monitoring/turn": "next", "monitoring/watching": "watching", "monitoring/paused": "watching",
    "monitoring/waiting": "watching", "monitoring/overdue": "watching", "monitoring/idle": "watching",
    "monitoring/delivered": "watching", "monitoring/mergeable": "watching",
    "monitoring/pr-failing": "watching",
    "finished/done": "accepted", "finished/reviewed": "accepted", "finished/still-following": "accepted",
    "finished/failed": "finished", "finished/cancelled": "finished", "finished/set-aside": "finished",
}  # fmt: skip


def lite(body: str):
    return run_js(
        f"const ts = await import({json.dumps((STATIC / 'teamstate.js').as_uri())});\n"
        f"const b = await import({json.dumps((STATIC / 'board.js').as_uri())});\n"
        "const member = (id, extra = {}) => ({ id, task: id, tasks: [id], session: 's' + id, "
        "name: 'P' + id, ...extra });\n"
        "const ctx = (phases, more = {}) => ({ phases: new Map(Object.entries(phases)), "
        "tasks: new Map(Object.entries(more.tasks || {})), needs: { tasks: new Set(more.asked"
        " || []), sessions: new Set(more.sessions || []) }, now: Date.parse('2026-11-24T12:00:00Z')"
        " });\n" + body
    )


def test_every_home_place_and_marker_is_one_part_state():
    found = lite(
        f"const phases = {json.dumps(PHASES)};\n"
        "return Object.fromEntries(Object.keys(phases).map((key) => {\n"
        "  const [section, marker] = key.split('/');\n"
        "  const part = ts.partOf(member('t1'), ctx({ t1: { section, marker, why: '' } }));\n"
        "  const state = part.state === 'watching' && part.next ? 'next'\n"
        "    : part.finished ? (part.accepted ? 'accepted' : 'finished') : part.state;\n"
        "  return [key, state];\n"
        "}));"
    )
    assert found == PHASES


def test_a_team_is_where_its_parts_are_on_home_first_what_needs_you():
    found = lite(
        "const team = (members, waiting = []) => ({ id: 'team-1-t1', members, waiting, "
        "request: { at: '2026-11-24T10:20:00Z' }, seen: null });\n"
        "const run = { section: 'running', marker: '' };\n"
        "const next = { section: 'monitoring', marker: 'queued' };\n"
        "const done = { section: 'finished', marker: 'done' };\n"
        "const review = { section: 'needs', marker: 'review' };\n"
        "const out = {};\n"
        "let s = ts.teamState(team([member('a'), member('b'), member('c')]), "
        "ctx({ a: done, b: run, c: next }));\n"
        "out.working = [s.state, s.words, s.segments, s.finished, s.total];\n"
        "s = ts.teamState(team([member('a'), member('b')]), ctx({ a: review, b: next }));\n"
        "out.review = [s.state, s.words, s.tone];\n"
        "const q = { from: 'b', to: 'a', blocks: true, minutes: 34 };\n"
        "s = ts.teamState(team([member('a'), member('b')], [q]), ctx({ a: run, b: run }));\n"
        "out.held = [s.state, s.words, s.tone];\n"
        "s = ts.teamState(team([member('a'), member('b')], [{ ...q, minutes: 5 }]), "
        "ctx({ a: run, b: run }));\n"
        "out.blocked = [s.state, s.words];\n"
        # An ask_owner card while its turn runs: it names its worker, not a task.
        "s = ts.teamState(team([member('a'), member('b')]), ctx({ a: run, b: run }, "
        "{ sessions: ['sa'], tasks: { a: { status: 'running' } } }));\n"
        "out.asks = [s.state, s.words];\n"
        "s = ts.teamState(team([member('a', { ended_at: '2026-11-24T12:00:00Z' }), member('b')]),"
        " ctx({ a: done, b: { section: 'finished', marker: 'cancelled' } }));\n"
        "out.done = [s.state, s.words, s.tone, s.segments];\n"
        "s = ts.teamState(team([member('a'), member('b')]), "
        "ctx({ a: { section: 'finished', marker: 'set-aside' }, "
        "b: { section: 'finished', marker: 'cancelled' } }));\n"
        "out.aside = [s.state, s.words];\n"
        # Moved out: finished for the team; not listed: as it ended.
        "s = ts.teamState(team([member('a', { moved_out: { task: 'x', title: 'X' } }), "
        "member('b', { status: 'done' })]), ctx({}));\n"
        "out.moved = [s.state, s.accepted, s.parts.map((p) => [p.finished, Boolean(p.moved)])];\n"
        "s = ts.teamState(team([member('a'), member('b')]), ctx({ a: done, "
        "b: { section: 'monitoring', marker: 'delivered' } }));\n"
        "out.watching = [s.state, s.words];\n"
        "return out;"
    )
    assert found["working"] == [
        "working", "1 working · 1 up next", ["done", "working", "next"], 1, 3,
    ]  # fmt: skip
    assert found["review"] == ["needs", "Waiting on you · review Pa", "amber"]
    assert found["held"] == ["blocked", "Pb waits on Pa's answer · 34 min", "amber"]
    assert found["blocked"] == ["blocked", "1 working · 1 waiting on a teammate"]
    assert found["asks"] == ["needs", "Waiting on you · answer Pa"]
    assert found["done"] == ["done", "Done · 2 parts · 1 h 40 min", "green", ["done", "ended"]]
    assert found["aside"] == ["done", "Done · set aside"]
    assert found["moved"] == ["done", True, [[True, True], [True, False]]]
    assert found["watching"] == ["watching", "1 watching"], "accepted, its PR watched: not done"


def test_a_look_applies_only_to_the_key_it_was_given_for():
    found = lite(
        "const done = { section: 'finished', marker: 'done' };\n"
        "const base = { id: 'team-1-t1', members: [member('a'), member('b')], waiting: [], "
        "request: { at: '2026-11-24T10:00:00Z' } };\n"
        "const first = ts.teamState({ ...base, seen: null }, ctx({ a: done, b: done }));\n"
        "const at = '2026-11-24T11:55:00Z';\n"
        "const seen = ts.teamState({ ...base, seen: { key: first.key, at, dismissed: false } },"
        " ctx({ a: done, b: done }));\n"
        "const later = ts.teamState({ ...base, seen: { key: first.key, at: "
        "'2026-11-24T11:40:00Z', dismissed: false } }, ctx({ a: done, b: done }));\n"
        # Reopened and accepted again: news on it changes its latest moment.
        "const again = ts.teamState({ ...base, seen: { key: first.key, at, dismissed: false } },"
        " ctx({ a: done, b: done }, { tasks: { b: { status: 'done', updated_at: "
        "'2026-11-24T11:59:00Z' } } }));\n"
        "const gone = ts.teamState({ ...base, seen: { key: first.key, at, dismissed: true } }, "
        "ctx({ a: done, b: done }));\n"
        "const reopened = ts.teamState({ ...base, seen: { key: first.key, at, dismissed: true } },"
        " ctx({ a: done, b: { section: 'needs', marker: 'review' } }));\n"
        "return { unseen: first.left, seen: [Boolean(seen.seen), seen.left, seen.leavesAt === "
        "Date.parse(at) + 600000], later: later.left, again: [again.key !== first.key, "
        "again.seen, again.left], gone: gone.left, reopened: [reopened.state, reopened.left] };"
    )
    assert found["unseen"] is False, "never seen: it stays, whatever its age"
    assert found["seen"] == [True, False, True], "seen 5 min ago: it leaves in 5"
    assert found["later"] is True, "seen 20 min ago: gone to Past teams"
    assert found["again"] == [True, None, False], "a new key: the old look does not apply"
    assert found["gone"] is True
    assert found["reopened"] == ["needs", False]


def test_home_pending_items_are_attributed_to_a_task_or_its_worker():
    found = lite(
        "const p = ts.pendingFor({ drafts: [{ kind: 'slack', target: { task_id: 't1' }, "
        "session_id: 's1' }, { kind: 'decision', target: { askers: [{ task_id: 't2', "
        "session_id: 's2' }] }, session_id: 'r' }, { kind: 'ask_owner', target: {}, "
        "session_id: 's3' }], ok: [{ kind: 'merge', target: { task_id: 't4' }, session_id: "
        "'s4' }], questions: [{ task_id: '', session_id: 's5' }], tasks: [{ id: 't6' }] });\n"
        "return { tasks: [...p.tasks].sort(), sessions: [...p.sessions].sort() };"
    )
    assert found == {"tasks": ["t1", "t2", "t4", "t6"], "sessions": ["s3", "s5"]}
