"""QA round 7 on 0.1.46, a task's life: a worker waiting for the owner's word on its folder's
settings is under Needs you; an ended task follows nothing, and its Retry brings back what it
followed and tells the workers that were told it would not come; a replaced task hands its
dependents on; a check-back the owner's close ends, or that failed on accepted work, is theirs
to settle; focus ended early, a merged PR followed on request, the Watch scans that fail, an
undone split, a failed task about to leave Needs you."""

import json
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest
from conftest import Harness, call_tool, settle
from test_progress import busy_worker
from test_round10_lifecycle import spawn, view
from test_round12_checkbacks import checking
from test_ui_text import board, task
from test_web import ORIGIN, app_client, sign_in  # noqa: F401  (app_client: a fixture)

from chiefofstaff.digest import needs
from chiefofstaff.models import Settings, Watch
from chiefofstaff.store import hours_ago

PR = "github:acme/app#7"


def prompts(harness, session_id: str) -> list[str]:
    return [m["meta"]["prompt"] for m in harness.store.prompts(session_id)]


# E105: the owner's final text, empty too.


async def test_a_review_summary_the_owner_removed_stays_removed_after_a_failed_send(harness):
    runtime = await harness.connect()
    worker, _ = await spawn(runtime, harness.router_id, "Review acme/app#7")
    target = {"repo": "acme/app", "number": 7, "verdict": "comment",
              "comments": [{"path": "app.py", "line": 3, "body": "Off by one?"}]}  # fmt: skip
    draft = harness.store.create_draft(worker, "github", target, "Looks good overall.")

    async def down(*arguments, **options):
        raise RuntimeError("GitHub is temporarily unavailable (502)")

    runtime.github.publish_review = down
    with suppress(RuntimeError):
        await runtime.drafts.decide(draft["id"], decision("send", content={"body": ""}))
    after = harness.store.draft(draft["id"])
    assert after["status"] == "pending" and after["final_text"] == ""
    assert after["has_final"], "the owner's own text is empty: not the drafted one"
    assert not harness.store.draft(
        harness.store.create_draft(worker, "github", target, "Other.")["id"]
    )["has_final"]


def decision(action, **fields):
    from chiefofstaff.models import DraftDecision

    return DraftDecision(action=action, **fields)


def test_the_page_shows_and_sends_the_owners_own_text_an_empty_one_too():
    from test_ui_text import run_js

    found = run_js(
        "return [s.finalText({ text: 'Summary', final_text: '', has_final: 1 }),"
        " s.finalText({ text: 'Summary', final_text: '', has_final: 0 })];"
    )
    assert found == ["", "Summary"]
