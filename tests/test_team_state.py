"""Teams lite in the page (static/teamstate.js): where a team stands is Home's own placement of
its tasks (board.js), its pending cards and questions; run with node."""

import json

from test_ui_text import STATIC, run_js

# Every place and marker board.js's taskPhase gives a task, and the part state it is.
PHASES = {
    "needs/blocked": "needs",
    "needs/settings": "needs",
    "needs/stopped": "needs",
    "needs/question": "needs",
    "needs/failed": "needs",
    "needs/check-failed": "needs",
    "needs/review": "needs",
    "needs/update": "needs",
    "needs/prepared": "needs",
    "needs/kept-paused": "needs",
    "needs/kept-stopped": "needs",
    "needs/pr-changes": "needs",
    "needs/pr-conflicts": "needs",
    "needs/pr-failing": "needs",
    "needs/runs-unknown": "needs",
    "needs/stalled": "needs",
    "/": "needs",
    "/review": "needs",
    "/update": "needs",
    "running/": "working",
    "running/following": "working",
    "running/terminal": "working",
    "running/background": "working",
    "monitoring/resets": "watching",
    "monitoring/queued": "next",
    "monitoring/check-back": "watching",
    "monitoring/turn": "next",
    "monitoring/watching": "watching",
    "monitoring/paused": "watching",
    "monitoring/waiting": "watching",
    "monitoring/overdue": "watching",
    "monitoring/idle": "watching",
    "monitoring/delivered": "watching",
    "monitoring/mergeable": "watching",
    "monitoring/pr-failing": "watching",
    "finished/done": "accepted",
    "finished/reviewed": "accepted",
    "finished/still-following": "accepted",
    "finished/failed": "finished",
    "finished/cancelled": "finished",
    "finished/set-aside": "finished",
}


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


def test_a_teams_words_are_the_same_on_its_row_the_sidebar_and_its_page():
    """Home's row and the sidebar's line say a team's words; its page's header starts with
    their headline: the same words for every state, a part waiting on work that went on
    outside the team too (said plainly, not "Watching")."""
    found = lite(
        "const clock = () => '9:12';\n"
        "const base = { id: 'team-1-t1', request: { at: '2026-11-24T09:12:00Z' }, "
        "waiting: [], edges: [] };\n"
        "const out = member('a', { moved_out: { task: 'x', title: 'Sales report export', "
        "session: 'sx' } });\n"
        "const after = [{ kind: 'after', from: 'a', to: 'b', state: 'pending' }];\n"
        "const run = { section: 'running', marker: '' };\n"
        "const next = { section: 'monitoring', marker: 'queued' };\n"
        "const watch = { section: 'monitoring', marker: 'watching' };\n"
        "const done = { section: 'finished', marker: 'done' };\n"
        "const cases = {\n"
        "  needs: [[member('a'), member('b')], {}, { a: { section: 'needs', marker: 'review' },"
        " b: run }],\n"
        "  held: [[member('a'), member('b')], { waiting: [{ id: 'q1', from: 'a', to: 'b', "
        "blocks: true, minutes: 34 }] }, { a: { section: 'monitoring', marker: 'waiting' }, "
        "b: run }],\n"
        "  mixed: [[member('a'), member('b'), member('c')], {}, { a: run, b: next, c: watch }],\n"
        "  next: [[member('a'), member('b')], {}, { a: done, b: next }],\n"
        "  watching: [[member('a'), member('b')], {}, { a: done, b: watch }],\n"
        "  outside: [[out, member('b')], { edges: after }, { b: next }],\n"
        "  outsideMixed: [[out, member('b'), member('c')], { edges: after }, { b: next, c: run }"
        "],\n"
        "  done: [[member('a'), member('b')], {}, { a: done, b: done }],\n"
        "  aside: [[member('a'), member('b')], {}, { a: { section: 'finished', marker: "
        "'set-aside' }, b: { section: 'finished', marker: 'cancelled' } }],\n"
        "};\n"
        "const found = {};\n"
        "for (const [name, [members, extra, phases]] of Object.entries(cases)) {\n"
        "  const team = { ...base, ...extra, members };\n"
        "  const st = ts.teamState(team, ctx(phases));\n"
        "  found[name] = [st.words, st.headline, ts.headerWords(team, st, clock, "
        "Date.parse('2026-11-24T12:00:00Z'))];\n"
        "}\n"
        "return found;"
    )
    for name, (words, headline, header) in found.items():
        assert header.startswith(headline), (name, header, headline)
        assert headline.casefold() in words.casefold(), (name, words, headline)
    assert found["outside"] == [
        "Waiting on “Sales report export” (outside this team)",
        "Waiting on “Sales report export” (outside this team)",
        "Waiting on “Sales report export” (outside this team) · 1 of 2 done · 2 h 48 min",
    ]
    assert found["outsideMixed"][:2] == [
        "1 working · 1 waiting on “Sales report export” (outside this team)",
        "Working",
    ]
    assert found["next"][:2] == ["1 up next", "Up next"], "never Watching for up next"
    assert found["watching"][:2] == ["1 watching", "Watching"]
    assert found["mixed"][:2] == ["1 working · 1 up next · 1 watching", "Working"]
    assert found["needs"][1] == "Waiting on you" and found["held"][1] == "Pa waits on Pb's answer"
    assert found["aside"][0] == "Done · set aside" and found["done"][2].startswith("Done")


# The team page (team-page-v4): the brief, the plan's stages and words, Since you looked, So
# far's Watch out, Tell the team's recipients, Home's row and Copy summary.

NEEDS = {key: value for key, value in PHASES.items() if value == "needs"}


def v4(body: str):
    return lite(
        "const team = (members, extra = {}) => ({ id: 'team-1-ta', members, waiting: [], edges: [],"
        " request: { at: '2026-11-24T10:00:00Z' }, seen: null, looked: '', ...extra });\n"
        "const after = (from, to, state = 'pending') => ({ kind: 'after', from, to, state });\n"
        "const run = { section: 'running', marker: '' };\n"
        "const next = { section: 'monitoring', marker: 'queued' };\n"
        "const done = { section: 'finished', marker: 'done' };\n"
        "const at = (minutes) => new Date(Date.parse('2026-11-24T12:00:00Z') - minutes * 60000)"
        ".toISOString();\n" + body
    )


def test_the_brief_chooses_the_one_need_on_every_home_place_and_marker():
    """Whatever Home's marker for a part that needs the owner, the brief puts that part first,
    says what kind of need it is, and what waits on it; with no need, it is calm (or done)."""
    found = v4(
        f"const phases = {json.dumps(sorted(NEEDS))};\n"
        "const out = {};\n"
        "for (const key of phases) {\n"
        "  const [section, marker] = key.split('/');\n"
        "  const t = team([member('a'), member('b'), member('c')], { edges: [after('a', 'c')] });\n"
        "  const st = ts.teamState(t, ctx({ a: { section, marker, why: 'Its CI fails' }, b: run, "
        "c: next }));\n"
        "  const brief = ts.briefOf(t, st, Date.parse('2026-11-24T12:00:00Z'));\n"
        "  out[key] = [brief.kind, brief.part && brief.part.id, brief.need, brief.unblocks];\n"
        "}\n"
        "const calm = team([member('a'), member('b'), member('c')], { edges: [after('a', 'c'),"
        " after('b', 'c')] });\n"
        "out.calm = ts.briefOf(calm, ts.teamState(calm, ctx({ a: done, b: run, c: next })));\n"
        "out.calm = [out.calm.kind, out.calm.working, out.calm.next.id, out.calm.nextWords];\n"
        "const over = team([member('a'), member('b')]);\n"
        "out.done = ts.briefOf(over, ts.teamState(over, ctx({ a: done, b: done }))).kind;\n"
        "const held = team([member('a'), member('b')], { waiting: [{ id: 'q1', from: 'a', to: 'b',"
        " blocks: true, minutes: 34 }] });\n"
        "const h = ts.briefOf(held, ts.teamState(held, ctx({ a: { section: 'monitoring', marker:"
        " 'waiting' }, b: run })));\n"
        "out.held = [h.kind, h.part.id, h.to, h.waited];\n"
        "return out;"
    )
    kinds = {"review": "review", "": "ask", "question": "ask", "settings": "ask"}
    stuck = {"failed", "stopped", "blocked", "stalled", "check-failed", "kept-paused"}
    for key in NEEDS:
        marker = key.split("/")[1]
        kind, part, need, unblocks = found[key]
        expected = kinds.get(marker) or (
            "stuck" if marker in stuck or marker == "kept-stopped" else "look"
        )
        assert (kind, part, need) == ("need", "a", expected), (key, found[key])
        assert unblocks == "Pc can't start until Pa is done.", (key, unblocks)
    assert found["calm"] == ["calm", ["Pb"], "c", "starts after Pa and Pb"]
    assert found["done"] == "done"
    assert found["held"] == ["held", "a", "Pb", 34]


def test_the_brief_puts_first_what_holds_most_of_the_request_up():
    found = v4(
        "const now = Date.parse('2026-11-24T12:00:00Z');\n"
        "const pick = (members, phases, extra = {}, tasks = {}) => { "
        "const t = team(members, extra); "
        "return ts.chooseNeed(t, ts.teamState(t, ctx(phases, { tasks })), now).part.id; };\n"
        "const review = { section: 'needs', marker: 'review' };\n"
        "const ask = { section: 'needs', marker: 'question' };\n"
        "const failed = { section: 'needs', marker: 'failed' };\n"
        "const m = [member('a'), member('b'), member('c')];\n"
        "return {\n"
        # A question (its part stopped on it, and the tests wait on it) before a result that
        # holds nothing up (its result is handed on already).
        "  askBeforeReady: pick(m, { a: review, b: ask, c: next }, { edges: [after('a', 'c', "
        "'met'), after('b', 'c')] }),\n"
        # A result the next part waits on the owner's OK for holds that part up: before a
        # question nothing else waits on? Two each: the plan's order decides no more.
        "  gatedReview: pick(m, { a: ask, b: review, c: next }, { edges: [after('b', 'c', 'owner')]"
        " }),\n"
        "  moreWaiting: pick(m, { a: ask, b: ask, c: next }, { edges: [after('b', 'c')] }),\n"
        "  stuckBeforeReview: pick([member('a'), member('b')], { a: review, b: failed }),\n"
        "  longerWait: pick([member('a'), member('b')], { a: ask, b: ask }, {}, { a: { status: "
        "'waiting', updated_at: at(5) }, b: { status: 'waiting', updated_at: at(40) } }),\n"
        "  planOrder: pick([member('a'), member('b')], { a: review, b: review }),\n"
        "};"
    )
    assert found == {
        "askBeforeReady": "b",
        "gatedReview": "a",
        "moreWaiting": "b",
        "stuckBeforeReview": "b",
        "longerWait": "b",
        "planOrder": "a",
    }


def test_what_a_need_unblocks_on_chains_fans_and_waits_for_the_owners_ok():
    found = v4(
        "const ask = { section: 'needs', marker: 'question' };\n"
        "const review = { section: 'needs', marker: 'review' };\n"
        "const words = (members, phases, edges) => { const t = team(members, { edges });"
        " const b = ts.briefOf(t, ts.teamState(t, ctx(phases))); return [b.unblocks, b.also]; };\n"
        "const m4 = [member('a'), member('b'), member('c'), member('d')];\n"
        "return {\n"
        "  chain: words(m4.slice(0, 3), { a: ask, b: next, c: next }, [after('a', 'b'), "
        "after('b', 'c')]),\n"
        "  fan: words(m4, { a: ask, b: next, c: next, d: run }, [after('a', 'b'), after('a', 'c')"
        "]),\n"
        "  gated: words(m4.slice(0, 2), { a: review, b: next }, [after('a', 'b', 'owner')]),\n"
        "  handed: words(m4.slice(0, 2), { a: review, b: next }, [after('a', 'b', 'met')]),\n"
        "  also: words(m4.slice(0, 3), { a: ask, b: review, c: run }, []),\n"
        "  starts: [\n"
        "    ts.startsWords(team(m4.slice(0, 3), { edges: [after('a', 'c', 'met'), after('b', 'c'"
        ")] }), ts.teamState(team(m4.slice(0, 3), { edges: [after('a', 'c', 'met'), "
        "after('b', 'c')] }), ctx({ a: done, b: run, c: next })), { id: 'c' }),\n"
        "    ts.startsWords(team(m4.slice(0, 2), { edges: [after('a', 'b', 'owner')] }), "
        "ts.teamState(team(m4.slice(0, 2)), ctx({ a: review, b: next })), { id: 'b' }),\n"
        "  ],\n"
        "};"
    )
    assert found["chain"] == ["Pb and Pc can't start until Pa is done.", ""]
    assert found["fan"] == ["Pb and Pc can't start until Pa is done.", ""]
    assert found["gated"] == ["Pb starts once you accept it.", ""]
    assert found["handed"] == ["", ""], "its result is handed on already: nothing waits"
    assert found["also"] == ["", "Pb's result is ready too (below)."]
    assert found["starts"] == [
        "Has Pa's result ✓ · starts after Pb",
        "Starts after your OK on Pa",
    ]


def test_the_plan_in_stages_and_its_rail():
    found = v4(
        "const t = team([member('a'), member('b'), member('c'), member('d')], { edges: "
        "[after('a', 'b', 'met'), after('a', 'c', 'met'), after('b', 'd'), after('c', 'd')] });\n"
        "const st = ts.teamState(t, ctx({ a: done, b: run, c: run, d: next }));\n"
        "const stages = ts.stagesOf(t, st);\n"
        "return { stages: stages.map((s) => [s.label, s.parts.map((p) => p.id)]),\n"
        "  rails: [ts.railState(t, st, stages[0], stages[1]), ts.railState(t, st, stages[1], "
        "stages[2])],\n"
        "  side: ts.stagesOf(team([member('a'), member('b')]), ts.teamState(team([member('a'), "
        "member('b')]), ctx({ a: run, b: run }))).map((s) => s.label) };"
    )
    assert found["stages"] == [
        ["", ["a"]],
        ["Then, side by side", ["b", "c"]],
        ["Then", ["d"]],
    ]
    assert found["rails"] == ["met", "pending"]
    assert found["side"] == ["Side by side"]


def test_since_you_looked_is_what_changed_after_the_look_only():
    found = v4(
        "const looked = at(30);\n"
        "const t = team([member('a'), member('b'), member('c'), member('d')], { edges: "
        "[after('a', 'd')] });\n"
        "const tasks = { a: { status: 'done', ended_at: at(10) }, b: { status: 'review', "
        "updated_at: at(5) }, c: { status: 'done', ended_at: at(50) }, d: { status: 'waiting', "
        "updated_at: at(20) } };\n"
        "const st = ts.teamState(t, ctx({ a: done, b: { section: 'needs', marker: 'review' }, "
        "c: done, d: { section: 'needs', marker: 'question' } }, { tasks }));\n"
        "const brief = ts.briefOf(t, st);\n"
        "const made = [{ key: 'pr:1', part: 'a', short: 'PR #412', status: 'merged', at: at(12) "
        "}, { key: 'pr:2', part: 'c', short: 'PR #9', status: 'merged', at: at(45) }];\n"
        "const decided = [{ key: 'a:q1', kind: 'between', from: 'b', to: 'd', at: at(3) }, "
        "{ key: 'a:q0', kind: 'between', from: 'c', to: 'd', at: at(40) }, { key: 'd:1', kind: "
        "'yours', at: at(2) }];\n"
        "return { items: ts.sinceOf(t, st, { looked, decided, made, brief }).map((i) => [i.text,"
        " i.target]), never: ts.sinceOf(t, st, { looked: '', decided, made, brief }).length };"
    )
    assert found["items"] == [
        ["Pd asked you", "brief"],
        ["Pa's PR #412 merged", "line:pr:1"],
        ["Pa is done", "part:a"],
        ["Pb's result is ready", "part:b"],
        ["Pb answered Pd", "line:a:q1"],
    ]
    assert found["never"] == 0, "never looked: nothing to say since when"


def test_since_you_looked_has_a_question_once_it_holds_a_part_up():
    """A question between parts is news once it holds its asker up (after the look); one that
    holds nothing up, like a note, is not."""
    found = v4(
        "const looked = at(30);\n"
        "const t = team([member('a'), member('b'), member('c'), member('d')], { waiting: ["
        "{ id: 'q1', from: 'b', to: 'a', blocks: true, minutes: 12, asked: at(12) }, "
        "{ id: 'q2', from: 'c', to: 'a', blocks: false, minutes: 5, asked: at(5) }, "
        "{ id: 'q3', from: 'd', to: 'a', blocks: true, minutes: 50, asked: at(50) }] });\n"
        "const st = ts.teamState(t, ctx({ a: run, b: run, c: run, d: run }));\n"
        "return ts.sinceOf(t, st, { looked, brief: ts.briefOf(t, st) })"
        ".map((i) => [i.key, i.text, i.target]);"
    )
    assert found == [["q:q1", "Pb waits on Pa's answer", "part:b"]]


def test_watch_out_is_what_could_go_wrong_across_the_parts():
    found = v4(
        "const out = member('x', { moved_out: { task: 'y', title: 'Sales export', session: 'sy' }"
        " });\n"
        "const t = team([member('a'), member('b'), out, member('c'), member('d')], { edges: "
        "[after('x', 'c'), after('a', 'd', 'failed')], waiting: [{ id: 'q1', from: 'a', to: 'b', "
        "blocks: true, minutes: 34 }] });\n"
        "const st = ts.teamState(t, ctx({ a: run, b: run, c: next, d: next }));\n"
        "const decided = [{ key: 'd:1', kind: 'yours', text: 'SMS too', unconfirmed: ['b', 'zz'] "
        "}, { key: 'd:2', kind: 'yours', text: 'Email', unconfirmed: [] }];\n"
        "return ts.watchOf(t, st, decided).map((w) => [w.key, w.text]);"
    )
    assert found == [
        ["w:q:q1", "Pa waits on Pb's answer"],
        ["w:o:c", "Pc waits on “Sales export”"],
        ["w:f:a:d", "Pa's result won't come to Pd"],
        ["w:d:d:1", "Pb hasn't confirmed your change yet"],
    ]


def test_tell_the_team_goes_to_the_parts_not_finished_and_says_how_each_takes_it():
    found = v4(
        "const out = member('x', { moved_out: { task: 'y', title: 'Y', session: 'sy' } });\n"
        "const t = team([member('a'), member('b'), member('c'), member('d'), member('e'), out, "
        "member('f')], { waiting: [{ id: 'q', from: 'e', to: 'a', blocks: true, minutes: 3 }] });\n"
        "const st = ts.teamState(t, ctx({ a: run, b: { section: 'needs', marker: 'review' }, c: "
        "next, d: done, e: run, f: { section: 'finished', marker: 'cancelled' } }, { tasks: { f: "
        "{ status: 'cancelled' }, d: { status: 'done' } } }));\n"
        "return { how: Object.fromEntries(st.parts.map((p) => [p.id, ts.tellHow(t, st, p)])), "
        "to: ts.defaultRecipients(t, st) };"
    )
    assert found["how"] == {
        "a": ["now", "At work: reads it at its next step"],
        "b": ["now", "Result ready: reads it now, before you accept"],
        "c": ["start", "Not started: reads it when it starts (nothing spent till then)"],
        "d": ["reopen", "Done: this reopens it as a follow-up"],
        "e": ["now", "Waits on Pa's answer: reads it now"],
        "x": ["", "Went on outside this team: tell it in its own chat"],
        "f": ["", "Cancelled: retry it first"],
    }
    assert found["to"] == ["a", "b", "c", "e"], "a done part only when ticked"


def test_homes_row_says_the_newest_change_and_the_outcome_when_done():
    found = v4(
        "const now = Date.parse('2026-11-24T12:00:00Z');\n"
        "const row = (t, phases, tasks = {}, extra = {}) => ts.rowWords(t, ts.teamState(t, "
        "ctx(phases, { tasks })), { now, ...extra });\n"
        "const m = [member('a'), member('b'), member('c')];\n"
        "const news = { kind: 'answered', from: 'b', to: 'a', at: at(2) };\n"
        "const tasks = { a: { status: 'running', progress_updates: [{ text: 'Wiring the queue', "
        "at: at(9) }] } };\n"
        "return {\n"
        "  newest: row(team(m, { news }), { a: run, b: run, c: next }, tasks),\n"
        "  seen: row(team(m, { news, looked: at(1) }), { a: run, b: run, c: next }, tasks),\n"
        "  progress: row(team(m), { a: run, b: run, c: next }, tasks),\n"
        "  quiet: row(team(m), { a: run, b: run, c: next }),\n"
        "  needs: row(team(m, { news }), { a: { section: 'needs', marker: 'review' }, b: run, "
        "c: next }),\n"
        "  done: row(team([member('a', { ended_at: '2026-11-24T11:05:00Z' }), member('b')]), "
        "{ a: done, b: done }, {}, { merged: 2 }),\n"
        "};"
    )
    assert found["newest"] == {"words": "Pb answered Pa · 2 min ago", "tone": "fresh-news"}
    assert found["seen"] == {"words": "Pb answered Pa · 2 min ago", "tone": ""}
    assert found["progress"] == {"words": "Pa: Wiring the queue · 9 min ago", "tone": "fresh-news"}
    assert found["quiet"]["words"] == "2 working · 1 up next", "nothing new: the counts"
    assert found["needs"] == {"words": "Waiting on you · review Pa", "tone": "amber"}
    assert found["done"] == {"words": "Done · 2 PRs merged · 1 h 5 min", "tone": "green"}


def test_the_parts_words_and_times_on_the_plan():
    found = v4(
        "const now = Date.parse('2026-11-24T12:00:00Z');\n"
        "const t = team([member('a'), member('b'), member('c'), member('d')], { edges: "
        "[after('a', 'd', 'met'), after('b', 'd')] });\n"
        "const tasks = { a: { status: 'done', started_at: at(80), ended_at: at(42) }, b: { status:"
        " 'running', started_at: at(24), progress: 'Wiring the queue\\nmore' }, c: { status: "
        "'waiting', updated_at: at(18) } };\n"
        "const st = ts.teamState(t, ctx({ a: done, b: run, c: { section: 'needs', marker: "
        "'question' }, d: next }, { tasks }));\n"
        "return st.parts.map((p) => [ts.partWords(t, st, p, { ask: 'Partial refunds too?', clock:"
        " () => '9:57' }), ts.partTime(p, now)]);"
    )
    assert found == [
        ["Done · you accepted it at 9:57", "38 min"],
        ["Wiring the queue", "24 min"],
        ["Asks you: Partial refunds too?", "waiting 18 min"],
        ["Has Pa's result ✓ · starts after Pb", ""],
    ]


def test_copy_summary_is_the_prs_the_decisions_and_a_line_per_part():
    found = v4(
        "const t = team([member('a', { ended_at: '2026-11-24T11:40:00Z' }), member('b', { "
        "ended_at: '2026-11-24T11:40:00Z' })], { goal: 'Refunds' });\n"
        "const st = ts.teamState(t, ctx({ a: done, b: done }, { tasks: { a: { status: 'done', "
        "lead: 'The refund API is merged' } } }));\n"
        "return ts.summaryMarkdown(t, st, { made: [{ name: 'shop#412 · Refund API', url: "
        "'https://github.com/acme/shop/pull/412', status: 'merged' }], decided: [{ kind: 'yours',"
        " text: 'Partial refunds too' }, { kind: 'between', text: 'Cents', from: 'b', to: 'a' }]"
        " });"
    )
    assert found == (
        "**Refunds**\n\n"
        "Done in 1 h 40 min: 1 PR merged and 2 decisions kept.\n\n"
        "Made:\n- [shop#412 · Refund API](https://github.com/acme/shop/pull/412) (merged)\n\n"
        "Decided:\n- Partial refunds too (you)\n- Cents (Pb answered Pa)\n\n"
        "Parts:\n- Pa: The refund API is merged\n"
        "- Pb: Done · you accepted it at 2026-11-24T11:40:00Z\n"
    )
