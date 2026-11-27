"""Prepared for you in the page (static/prepared.js, board.js), run with node: a kept PR's row
marker and words, its one action and its menu, and Home's section of PRs found."""

import json

from test_round9_ui import DOM
from test_ui_text import STATIC, run_js

BOARD = f"const b = await import({json.dumps((STATIC / 'board.js').as_uri())});\n"
TASKS = f"const k = await import({json.dumps((STATIC / 'tasks.js').as_uri())});\n"
PREPARED = f"const p = await import({json.dumps((STATIC / 'prepared.js').as_uri())});\n" + DOM
# A node's words, all of them, in order.
TEXT = (
    "const text = (node) => typeof node === 'string' ? node : [node.textContent,"
    " ...node.children.map(text)].join(' ').replace(/\\s+/g, ' ').trim();\n"
)
KEPT = {
    "target": "github:acme/api#12",
    "label": "api#12",
    "url": "https://github.com/acme/api/pull/12",
    "line": "A fix for the failing CI is ready: test_timeout passes now.",
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
        // Its PR merged or closed (owner report 0.1.61): it waits on it no more.
        const over = (how, monitoring) => ({ id: "t", status: "done", attention: "", monitoring,
          watching: [kept.target], shepherd: { ...kept, line: `api#12 ${how}.`, action: null,
            ended: how, ended_at: "2026-11-16T09:00:00+00:00" } });
        return [place(done("review", "A fix is ready")),
          place(done("resume", "api#12 paused: commit 9f8e7d6 wasn't pushed by its worker.")),
          place(done("look", "Stopped: two attempts in a row made no progress")),
          place({ id: "t", status: "review", shepherd: kept }),
          place({ id: "t", status: "review", shepherd: { ...kept, action: null } }),
          place(over("merged", [])), place(over("closed", [])),
          place(over("merged", [{ key: "slack:C1:1.2", kind: "thread", waiting_for: "a reply" }]))];
        """
    )
    assert found[0] == ["needs", "prepared", "A fix is ready", "Prepared for you"]
    assert found[1][1:] == [
        "kept-paused",
        "api#12 paused: commit 9f8e7d6 wasn't pushed by its worker.",
        "Paused",
    ]
    assert found[2][1] == "kept-stopped" and found[2][3] == "Stopped"
    # Awaiting acceptance: what it prepared says more than the PR's standing.
    assert found[3] == ["needs", "review", KEPT["line"], "Awaiting acceptance"]
    assert found[4][2] == ""
    # Merged or closed: out of Needs you, saying so, not "Waiting for review" (owner report
    # 0.1.61); still monitored for something else, there.
    assert found[5] == ["finished", "kept-merged", "api#12 merged.", "Merged"]
    assert found[6] == ["finished", "kept-closed", "api#12 closed.", "Closed"]
    assert found[7][0] == "monitoring"


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
          ended: menu(task({ ended: "merged", action: null })),
          line: text(p.keptLine(task({
            accept_note: "Accepted, but the PR changed after what you reviewed" }))),
          noLine: p.keptLine(task({ line: "" })),
          described: ["fix", "review", "resume", "look"].map((kind) => p.keptButton(
            task({ action: { kind, label: "X" } }), act).attrs["aria-description"]),
        };
        """
    )
    assert found["labels"] == ["Fix it", "Review & post", "Resume", "Look"]
    assert found["refused"] is None and found["none"] is None
    assert found["menu"] == [
        "Stop looking after this PR",
        "Merge when ready",
        "Accept this commit…",
        "What it did",
    ]
    assert found["enrolled"][1] == "Don't merge it by itself"
    assert found["off"] == ["Look after this PR", "Accept this commit…", "What it did"]
    assert "Accept this commit…" not in found["accepted"]
    assert found["running"] == []
    assert found["ended"] == ["What it did"]  # Merged: nothing to change on it.
    # Its PR named; what its one action does said in words, not only on hover.
    assert found["line"] == (
        "api#12 · A fix for the failing CI is ready: test_timeout passes now. Review fix to see "
        "exactly what would be published, and approve it. Accepted, but the PR changed after "
        "what you reviewed Nothing is published until you approve it."
    )
    assert found["noLine"] is None
    # (Focus and screen readers too.)
    assert all(words and words.endswith("(api#12)") for words in found["described"])
    assert "Look after it again" in found["described"][2]


def test_a_kept_rows_words_name_its_pr_and_say_what_its_action_does():
    """Owner report 0.1.61: "A decision card asked me to Resume. I didn't understand what it
    was for." Its row names its PR (linked: where its line says it, else first) and says what
    happened and what its button does, in words on the page."""
    found = run_js(
        PREPARED
        + f"const kept = {json.dumps(KEPT)};\n"
        + """
        const shape = (nodes) => nodes.map((node) => typeof node === "string" ? node
          : { tag: node.tag, text: node.textContent, href: node.attrs.href });
        const paused = { ...kept, line: "api#12 paused: its worker was closed, then reopened.",
          action: { kind: "resume", label: "Resume" } };
        return {
          paused: shape(p.keptSays(paused)),
          folded: shape(p.keptSays(paused, paused.line, false)),
          review: shape(p.keptSays(kept)),
          fix: shape(p.keptSays({ ...kept, line: "CI failing on api#12",
            action: { kind: "fix", label: "Fix it" } })),
          look: shape(p.keptSays({ ...kept, line: "Stopped: two attempts made no progress.",
            action: { kind: "look", label: "Look" } })),
          merged: shape(p.keptSays({ ...kept, line: "api#12 merged.", action: null })),
        };
        """
    )
    link = {"tag": "a", "text": "api#12", "href": "https://github.com/acme/api/pull/12"}
    assert found["paused"] == [
        link,
        " paused: its worker was closed, then reopened.",
        " Resume to keep it moving.",
    ]
    assert found["folded"] == [link, " paused: its worker was closed, then reopened."]
    assert found["review"] == [
        link,
        " · A fix for the failing CI is ready: test_timeout passes now.",
        " Review fix to see exactly what would be published, and approve it.",
    ]
    assert found["fix"] == [
        "CI failing on ",
        link,
        " Fix it to have its worker prepare a fix for your approval.",
    ]
    assert found["look"][-1] == " Look to see what was tried, and why it stopped."
    assert found["merged"] == [link, " merged."]


def test_a_finished_row_whose_kept_pr_merged_says_so_newest_first():
    found = run_js(
        BOARD
        + TASKS
        + f"const kept = {json.dumps(KEPT)};\n"
        + """
        s.state.snapshot = { settings: { max_workers: 3 }, sessions: [], tasks: [] };
        const task = { id: "t", status: "done", result: "Fixed it: PR acme/api#12.",
          ended_at: "2026-01-01T00:00:00+00:00", updated_at: "2026-01-01T00:00:00+00:00",
          monitoring: [], shepherd: { ...kept, line: "api#12 merged.", action: null,
            ended: "merged", ended_at: "2026-11-16T09:00:00+00:00" } };
        const phase = b.taskPhase(task, null);
        return [k.rowWhy(task, null, phase).text, b.endedAt(task),
          b.endedAt({ ...task, shepherd: undefined })];
        """
    )
    assert found == ["api#12 merged.", "2026-11-16T09:00:00+00:00", "2026-01-01T00:00:00+00:00"]


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


def test_a_kept_rows_line_is_its_one_line():
    """Its words, once: not with the task's result beside them, and no warning mark."""
    found = run_js(
        BOARD
        + TASKS
        + f"const kept = {json.dumps(KEPT)};\n"
        + """
        s.state.snapshot = { settings: { max_workers: 3 }, sessions: [], tasks: [] };
        const line = (task) => { const p = b.taskPhase(task, null);
          return [k.rowWhy(task, null, p), k.rowLatest(task, null, p).text]; };
        const result = "Fixed it: PR acme/api#12.";
        return [line({ id: "t", status: "review", result, shepherd: kept }),
          line({ id: "t", status: "done", result, attention: "prepared",
            attention_why: "api#12 paused: commit 9f8e7d6 wasn't pushed by its worker.",
            shepherd: { ...kept, action: { kind: "resume" } } }),
          line({ id: "t", status: "review", result, shepherd: { ...kept, action: null } })];
        """
    )
    review, paused, plain = found
    assert review == [{"text": KEPT["line"], "help": ""}, ""]
    assert paused == [
        {"text": "api#12 paused: commit 9f8e7d6 wasn't pushed by its worker.", "help": ""},
        "",
    ]
    assert plain[1] == "Fixed it: PR acme/api#12."  # Not kept moving: as today.
