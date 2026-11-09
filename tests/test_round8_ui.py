"""Bug bash 8 (0.1.32), the page: paths in a line, inline code on cards, a result whose PR wants
changes, a reviewed PR's chip, Slack's token from the environment and Settings' errors."""

import json

from test_round5_settings import run_ui
from test_ui_text import STATIC, board, run_js, task


def test_a_line_shows_a_path_as_a_chip_does():
    """U3: a review's line on Home and in the chat read "Saved … at /Users/alice/.copilot/…"
    in full, while the worker's chat showed the same path short."""
    found = run_js(
        """
        s.state.snapshot = { home: "/Users/alice", data_directory: "/Users/alice/.cos",
          settings: { scratch_directory: "/Users/alice/.copilot/state/files" } };
        return {
          gist: t.gist("## Summary\\nSaved a review at "
            + "/Users/alice/.copilot/state/files/review--01208b/textkit-review.md."),
          code: t.gist("Script: `/Users/alice/work/app/scripts/analyze.py`, see "
            + "https://github.com/o/r/pull/3."),
          first: t.firstLine("Wrote /Users/alice/notes/r8.md"),
          other: t.gist("Kept /usr/local/bin and /tmp/r8/a.txt"),
          chip: t.markdown("At /Users/alice/notes/r8.md."),
        };
        """
    )
    assert found["gist"] == "Saved a review at review--01208b/textkit-review.md."
    assert found["code"] == "Script: ~/work/app/scripts/analyze.py, see r#3."
    assert found["first"] == "Wrote ~/notes/r8.md"
    # Only paths of the kinds a chip shows: not every word with a slash.
    assert found["other"] == "Kept /usr/local/bin and /tmp/r8/a.txt"
    # A message keeps its chip, with the whole path to open.
    assert 'data-path="/Users/alice/notes/r8.md"' in found["chip"]
    assert ">~/notes/r8.md</span>." in found["chip"]


def test_a_card_shows_inline_code_as_code_and_escapes_the_rest():
    """U7: a decision card read "words() is currently `text.split()`." with its backticks."""
    found = run_js(
        """
        return [
          t.inlineHtml("words() is currently `text.split()`. <b>Keep</b> it?"),
          t.codeHtml("Keep `split()` & <i>go</i>"),
        ];
        """
    )
    assert found[0] == (
        "words() is currently <code>text.split()</code>. &lt;b&gt;Keep&lt;/b&gt; it?"
    )
    assert found[1] == "Keep <code>split()</code> &amp; &lt;i&gt;go&lt;/i&gt;"


def test_a_result_to_review_whose_pr_wants_changes_says_so_under_needs_you():
    """R13: just delivered, with its PR's changes requested or CI failing, a task said only
    "To review"; "Ask it to fix…" came after Accept."""
    workers = [{"id": "w1", "status": "idle"}]

    def own(name, number, status="review", **pr):
        label = f"o/r#{number}"
        prs = [{"label": label, "own": True, "state": "OPEN", **pr}]
        return task(name, "w1", status, watching=[f"github:{label}"], watch_kind="own",
                    receipts={"prs": prs})  # fmt: skip

    tasks = [
        own("t1", 1, review="CHANGES_REQUESTED"),
        own("t2", 2, checks="failing", failing=["lint"]),
        own("t3", 3, checks="passing", merge_state="CLEAN"),
        own("t4", 4, checks="pending"),
        # Accepted earlier, on the same PR: it does not take the result's place, nor it its.
        own("t5", 1, "done", review="CHANGES_REQUESTED"),
    ]
    tasks[4]["updated_at"] = "2026-11-07T11:55:00Z"
    found = board(tasks, workers)
    assert found["phases"]["t1"] == "needs/pr-changes"
    assert found["whys"]["t1"] == "Changes requested on r#1"
    assert found["phases"]["t2"] == "needs/pr-failing"
    assert found["whys"]["t2"] == "CI failing on r#2 (lint)"
    # Ready to merge or CI still running: a result to review, as before.
    assert found["phases"]["t3"] == "needs/review"
    assert found["phases"]["t4"] == "needs/review"
    assert found["phases"]["t5"] == "needs/pr-changes"


def test_a_reviewed_prs_chip_says_whether_the_review_was_sent():
    """A7: "You reviewed it" while the review still waited on its card, or was rejected."""
    words = run_js(
        f"const k = await import({json.dumps((STATIC / 'tasks.js').as_uri())});\n"
        "return ['sent', 'drafted', ''].map((state) =>"
        " k.reviewedWords({ reviewed: true, review_state: state }))"
        ".concat([k.reviewedWords({ own: true, reviewed: true, review_state: 'sent' })]);"
    )
    assert words == ["You reviewed it", "Its review waits on its card", "", ""]


def test_a_slack_token_from_the_environment_is_changed_there():
    """S10: with CHIEFOFSTAFF_SLACK_USER_TOKEN set, Remove and Save token did nothing."""
    said = run_ui(
        """
        s.state.snapshot = { slack: { token_saved: false, token_source: "env", connected: true,
          can_send: false } };
        const env = settings.slackStatusText();
        s.state.snapshot.slack = { token_saved: true, token_source: "keychain" };
        return [env, settings.slackStatusText()];
        """
    )
    assert said == [
        "Set by CHIEFOFSTAFF_SLACK_USER_TOKEN: change it there. Watching. "
        "This token cannot send messages (chat:write).",
        "Token saved.",
    ]


def test_a_servers_error_names_the_fields_it_is_about():
    """S14: an error on Save did not show the tab with the field: the page knows which now."""
    fields = run_ui(
        """
        globalThis.fetch = async () => ({ status: 422, ok: false, json: async () => ({ detail: [
          { type: "value_error", loc: ["body", "digest_time"], msg: "Value error, Use HH:MM" },
        ] }) });
        try {
          await d.api("/api/settings", { method: "POST", body: {} });
        } catch (error) {
          return [error.message, error.fields];
        }
        """
    )
    assert fields == ["Use HH:MM", ["digest_time"]]
