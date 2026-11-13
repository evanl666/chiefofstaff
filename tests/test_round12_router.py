# ruff: noqa: RUF001  (Chinese titles' brackets and commas are what E44 is about)
"""QA round 12, the Router: a task's title says what it delivers, not how to write it (E44)."""

import json

import pytest
from conftest import call_tool

from chiefofstaff.task_actions import style_note


@pytest.mark.parametrize(
    ("title", "note"),
    [
        ("Review app#7 (in English, human-sounding)", "(in English, human-sounding)"),
        ("Review app#7 — in English", "— in English"),
        ("Draft a reply to Bob: casual", ": casual"),
        ("审查 app#7（用英文，像人写的评论）", "（用英文，像人写的评论）"),
        ("回复 Bob（简洁一点）", "（简洁一点）"),
    ],
)  # fmt: skip
def test_a_titles_note_on_how_to_write_it_is_found(title, note):
    assert style_note(title) == note


@pytest.mark.parametrize(
    "title",
    [
        "Review app#7 (comments only)",
        "Review app#7 (with comments)",
        "Review app#7 (security)",
        "Fix login (Python 3.12)",
        "Translate the README (Chinese)",  # The language is what it delivers.
        "Review app#7 (Chinese tests)",
        "Update the docs (tone and wording)",
        "Review embedding schema · ranking-data#589",
        "Answer Jingtao: why two parallel gets are slow",
        "Fix the tone detector",
    ],
)
def test_a_titles_note_on_anything_else_stays(title):
    assert style_note(title) == ""


async def test_the_router_is_told_once_to_leave_how_to_write_out_of_a_title(harness):
    """E44: "Review <PR> (in English, human-sounding comments)": how to write it is the
    instructions'. Refused once, with why; the same title again stands (it may be the owner's)."""
    runtime = await harness.connect()
    title = "Review app#7 (in English, human-sounding comments)"
    start = {"instructions": "Review it.", "why_new": "test"}
    refused = await call_tool(runtime, "spawn_worker", harness.router_id, title=title, **start)
    said = refused.text_result_for_llm
    assert refused.result_type == "failure" and "says how to write the work" in said, said
    assert "\u201cReview app#7\u201d" in said and not runtime.tasks.listed()
    started = await call_tool(runtime, "spawn_worker", harness.router_id, title=title, **start)
    worker = json.loads(started.text_result_for_llm)["session_id"]
    [task] = runtime.tasks.for_session(worker)
    assert task["title"] == title
    new = {"session_id": worker, "task": "new", "why_new": "test", "earlier": "keep"}
    again = await call_tool(runtime, "message_session", harness.router_id, **new,
                            title="Answer Bob — concise", text="Answer Bob.")  # fmt: skip
    assert "Not sent" in again.text_result_for_llm and "\u201cAnswer Bob\u201d" in (
        again.text_result_for_llm
    )
    assert len(runtime.tasks.for_session(worker)) == 1
    await call_tool(runtime, "message_session", harness.router_id, **new,
                    title="Answer Bob about the deploy", text="Answer Bob.")  # fmt: skip
    assert len(runtime.tasks.for_session(worker)) == 2
