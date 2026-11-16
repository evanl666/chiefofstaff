"""Prepared for you in the page (static/prepared.js, board.js), run with node: a kept PR's row
marker and words, its one action and its menu, and Home's section of PRs found."""

import json

from test_round9_ui import DOM
from test_ui_text import STATIC, run_js

BOARD = f"const b = await import({json.dumps((STATIC / 'board.js').as_uri())});\n"
PREPARED = f"const p = await import({json.dumps((STATIC / 'prepared.js').as_uri())});\n" + DOM
# A node's words, all of them, in order.
TEXT = (
    "const text = (node) => typeof node === 'string' ? node : [node.textContent,"
    " ...node.children.map(text)].join(' ').replace(/\\s+/g, ' ').trim();\n"
)
KEPT = {
    "target": "github:acme/api#12",
    "label": "api#12",
    "line": "A fix is ready: test_timeout passes now",
    "action": {"kind": "review", "label": "Review fix", "step": "s1"},
    "keep": True,
    "merge_when_ready": False,
    "head": "b" * 40,
    "accepted": "a" * 40,
    "accept_note": "",
    "journal": [{"id": "s1"}],
}


def test_a_kept_prs_row_says_what_it_needs_by_its_one_action():
    found = run_js(
        BOARD
        + f"const kept = {json.dumps(KEPT)};\n"
        + """
        const place = (task) => { const p = b.taskPhase(task, null); return [p.section, p.marker,
          p.why, b.MARKERS[p.marker]]; };
        const done = (kind, line) => ({ id: "t", status: "done", attention: "prepared",
          attention_why: line, shepherd: { ...kept, line, action: { kind } } });
        return [place(done("review", "A fix is ready")),
          place(done("resume", "Paused: new commits it didn't push (9f8e7d6)")),
          place(done("look", "Stopped: two attempts in a row made no progress")),
          place({ id: "t", status: "review", shepherd: kept }),
          place({ id: "t", status: "review", shepherd: { ...kept, action: null } })];
        """
    )
    assert found[0] == ["needs", "prepared", "A fix is ready", "Prepared for you"]
    assert found[1][1:] == ["kept-paused", "Paused: new commits it didn't push (9f8e7d6)", "Paused"]
    assert found[2][1] == "kept-stopped" and found[2][3] == "Stopped"
    # Awaiting acceptance: what it prepared says more than the PR's standing.
    assert found[3] == ["needs", "review", KEPT["line"], "Awaiting acceptance"]
    assert found[4][2] == ""


def test_its_one_action_and_its_menu():
    found = run_js(
        PREPARED
        + TEXT
        + f"const kept = {json.dumps(KEPT)};\n"
        + """
        const act = () => {};
        const label = (task) => p.keptButton(task, act)?.textContent ?? null;
        const menu = (task) => p.keptMenu(task, act).map((item) => item.text);
        const task = (extra, status = "done") =>
          ({ id: "t", status, shepherd: { ...kept, ...extra } });
        return {
          labels: ["fix", "review", "resume", "look"].map((kind) =>
            label(task({ action: { kind, label: { fix: "Fix it", review: "Review & post",
              resume: "Resume", look: "Look" }[kind] } }))),
          refused: label(task({ action: null, refused: true })),
          none: label({ id: "t", status: "done" }),
          menu: menu(task({})),
          enrolled: menu(task({ merge_when_ready: true })),
          off: menu(task({ keep: false })),
          accepted: menu(task({ head: "a".repeat(40) })),
          running: menu(task({}, "running")),
          line: text(p.keptLine(task({
            accept_note: "Accepted; the PR's head is not what you reviewed" }))),
          noLine: p.keptLine(task({ line: "" })),
        };
        """
    )
    assert found["labels"] == ["Fix it", "Review & post", "Resume", "Look"]
    assert found["refused"] is None and found["none"] is None
    assert found["menu"] == [
        "Stop keeping it moving",
        "Merge when ready",
        "Accept this head…",
        "What it did",
    ]
    assert found["enrolled"][1] == "Don't merge it by itself"
    assert found["off"] == ["Keep it moving", "Accept this head…", "What it did"]
    assert "Accept this head…" not in found["accepted"]
    assert found["running"] == []
    assert found["line"] == (
        "A fix is ready: test_timeout passes now · Accepted; the PR's head is not what you reviewed"
    )
    assert found["noLine"] is None


def test_homes_section_shows_three_rows_then_all_found():
    rows = [
        {
            "target": f"github:acme/web#{n}",
            "repo": "acme/web",
            "number": n,
            "label": f"web#{n}",
            "title": f"Change {n}",
            "url": f"https://github.com/acme/web/pull/{n}",
            "kind": "fix-ci",
            "what": "CI failing since Tue",
        }
        for n in (1, 2, 3, 4)
    ]
    found = run_js(
        PREPARED
        + TEXT
        + f"const rows = {json.dumps(rows)};\n"
        + """
        const prepared = { rows: rows.slice(0, 3), more: 1, read: 0,
          all: [...rows, { ...rows[0], target: "github:acme/web#9", label: "web#9", what: undefined,
            why: "from a fork: not yet" }] };
        s.state.snapshot = { shepherd: { on: true, prepared } };
        const items = p.preparedItems();
        const built = items.map((item) => text(item.build()));
        s.state.snapshot = { shepherd: { on: false, prepared } };
        const off = p.preparedItems().length;
        s.state.snapshot = { shepherd: { on: true, prepared: { ...prepared, rows: [], all: [] } } };
        const empty = p.preparedItems().length;
        return { keys: items.map((item) => item.key), built, off, empty };
        """
    )
    assert len(found["keys"]) == 4 and found["keys"][-1] == "prepared-more:2"
    assert found["built"][0] == "Change 1 CI failing since Tue · web#1 Hand to a worker"
    assert found["built"][-1] == "1 more · All found…"
    assert found["off"] == 0 and found["empty"] == 0


def test_the_journal_says_each_operation_in_words():
    found = run_js(
        PREPARED
        + """
        return [["push", "sent"], ["reply", "failed"], ["push", "uncertain"],
          ["request-review", "dispatching"], ["merge", "sent"]]
          .map(([kind, state]) => p.operationWords({ kind, state }));
        """
    )
    assert found == [
        "Pushed the fix",
        "A reply: not done",
        "The push: not known whether it went out",
        "Asking for a review again: on its way",
        "Merged",
    ]
