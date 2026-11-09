"""Bug bash 9 (0.1.33), the page: stopping to follow a PR, PRs with merge conflicts and a
review's summary with `code` on its card."""

import json

from test_ui_text import STATIC, board, run_js, task

# Just enough of a page for cards to be made: nodes that keep their children.
DOM = """
const walk = (node) =>
  typeof node === "string" || !node ? [] : [node, ...node.children.flatMap(walk)];
document.createElement = (tag) => {
  const node = { tag, className: "", textContent: "", innerHTML: "", hidden: false,
    children: [], attrs: {}, style: {}, dataset: {}, addEventListener() {},
    setAttribute(key, value) { this.attrs[key] = value; },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = [...nodes]; },
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    querySelector(selector) {
      return walk(this).find((found) => found !== this
        && found.className.split(" ").includes(selector.slice(1))) || null;
    },
    querySelectorAll: () => [] };
  return node;
};
"""


def module(name: str) -> str:
    return f"const m = await import({json.dumps((STATIC / name).as_uri())});\n" + DOM


def test_stopping_to_follow_a_pr_says_nobody_reads_it_now():
    """H6: "Stopped following app#89: new messages there go to the Router." Only followed PRs
    are read: with nobody following it, its news reaches no one. Slack's go to the Router."""
    found = run_js(
        module("tasks.js")
        + """
        s.state.snapshot = { tasks: [{ id: "t2", session_id: "w2",
          watching: ["github:acme/app#90"] }] };
        const task = { id: "t1", session_id: "w1" };
        return [["github:acme/app#89", true], ["github:acme/App#90", true],
          ["slack:C0123ABCD:1700000000.000100", true], ["github:acme/app#89", false]]
          .map(([key, all]) => m.unfollowedWords(key, "it", all, task));
        """
    )
    assert found == [
        "Stopped following it. Nobody follows it now: its news reaches no one.",
        "Stopped following it; another worker still follows it.",
        "Stopped following it: new messages there go to the Router.",
        "Stopped following it for this task; its worker still follows it for another task.",
    ]


def pr(number, **extra):
    return {"label": f"o/app#{number}", "url": f"https://github.com/o/app/pull/{number}",
            "own": True, "state": "OPEN", "checks": "passing", **extra}  # fmt: skip


def test_a_pr_with_merge_conflicts_says_so():
    """H5: GitHub's DIRTY (merge conflicts) was nowhere: Artifacts said "#94 · in review" and
    #92's chip "approved"."""
    prs = [
        pr(92, review="APPROVED", merge_state="DIRTY"),
        pr(93, review="CHANGES_REQUESTED", merge_state="DIRTY"),
        pr(94, merge_state="DIRTY", checks="failing"),
    ]
    found = run_js(
        module("tasks.js")
        + f"const a = await import({json.dumps((STATIC / 'artifacts.js').as_uri())});\n"
        + f"const prs = {json.dumps(prs)};\n"
        + """
        s.state.snapshot = { sessions: [], tasks: [], pr_titles: {} };
        const chips = walk(m.chips({ id: "t1", session_id: "w1", status: "review",
          receipts: { prs, flags: [], changed: {} } }))
          .filter((node) => node.className === "pr-state").map((node) => node.textContent);
        return { status: prs.map((pr) => a.prStatus(pr)), chips };
        """
    )
    assert found["status"] == [
        ["merge conflicts", "bad"],
        ["changes requested", "bad"],  # What the reviewer asked comes first.
        ["merge conflicts", "bad"],  # Before its CI: of little use until it merges cleanly.
    ]
    assert found["chips"] == [" · ✓ CI · merge conflicts", " · ✓ CI · changes requested",
                              " · ✗ CI · merge conflicts"]  # fmt: skip


def test_a_task_whose_pr_has_merge_conflicts_needs_you():
    workers = [{"id": "w1", "status": "idle"}]

    def own(name, number, status="review", **extra):
        return task(name, "w1", status, watching=[f"github:o/app#{number}"], watch_kind="own",
                    receipts={"prs": [pr(number, **extra)]})  # fmt: skip

    tasks = [
        own("t1", 92, merge_state="DIRTY", review="APPROVED"),
        own("t2", 94, "done", merge_state="DIRTY"),
        own("t3", 95, merge_state="DIRTY", checks="failing", failing=["docs"]),
    ]
    found = board(tasks, workers)
    assert found["phases"] == {"t1": "needs/pr-conflicts", "t2": "needs/pr-conflicts",
                               "t3": "needs/pr-conflicts"}  # fmt: skip
    assert found["whys"]["t1"] == "Merge conflicts on app#92"


def test_a_reviews_summary_shows_its_code_as_code():
    """Its review's summary on the card showed `words()` with its backticks (set as text)."""
    found = run_js(
        module("drafts.js")
        + """
        s.state.snapshot = { sessions: [], tasks: [], drafts: [] };
        const card = m.draftCard({ id: "d1", kind: "github", session_id: "w1",
          text: "Use `words()` here; <b>not</b> split.",
          target: { repo: "o/app", number: 7, url: "https://github.com/o/app/pull/7",
            comments: [], verdict: "comment" } });
        const preview = card.querySelector(".summary-preview");
        return [preview.innerHTML, preview.textContent];
        """
    )
    assert found == ["Use <code>words()</code> here; &lt;b&gt;not&lt;/b&gt; split.", ""]
