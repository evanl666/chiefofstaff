# ruff: noqa: RUF001  (Chinese titles' brackets are what E61 is about)
"""QA round 13, E61: a language in a title is what is written, not how ("Add a Chinese README
(中文)" was refused once); a rename or a split is held to the same rule as a new title."""

import json

import pytest
from conftest import call_tool
from test_round10_lifecycle import spawn

from chiefofstaff.task_actions import style_note


@pytest.mark.parametrize(
    "title",
    [
        "Add a Chinese README (中文)",
        "Update the locale strings: French",
        "Write the onboarding guide (English)",
        "日文版文档（日文）",
    ],
)
def test_a_language_alone_is_what_a_title_delivers(title):
    assert style_note(title) == ""


@pytest.mark.parametrize(
    ("title", "note"),
    [
        ("Review app#7 (in English)", "(in English)"),
        ("Reply to Bob: in plain English", ": in plain English"),
        ("回复 PR 评论（英文写）", "（英文写）"),
        ("回复 PR 评论（用中文）", "（用中文）"),
        ("Review app#7 (human-sounding)", "(human-sounding)"),
        ("审查 app#7（像人写）", "（像人写）"),
    ],
)  # fmt: skip
def test_how_to_write_it_is_still_found(title, note):
    assert style_note(title) == note


async def test_a_rename_and_a_split_are_held_to_the_same_title_rule(harness):
    runtime = await harness.connect()
    worker, task = await spawn(runtime, harness.router_id, "Review app#7")
    styled = "Review app#7 (in English, human-sounding comments)"
    refused = await call_tool(runtime, "rename_task", harness.router_id, task_id=task["id"],
                              title=styled)  # fmt: skip
    assert refused.result_type == "failure", refused.text_result_for_llm
    assert "says how to write the work" in refused.text_result_for_llm
    assert runtime.tasks.get(task["id"])["title"] == "Review app#7"
    await call_tool(runtime, "rename_task", harness.router_id, task_id=task["id"], title=styled)
    assert runtime.tasks.get(task["id"])["title"] == styled, "the same title again stands"
    plain = await call_tool(runtime, "rename_task", harness.router_id, task_id=task["id"],
                            title="Add a Chinese README (中文)")  # fmt: skip
    assert plain.result_type == "success", plain.text_result_for_llm
    split = await call_tool(runtime, "split_task", harness.router_id, task_id=task["id"],
                            title="Fix the login copy (concise)")  # fmt: skip
    assert split.result_type == "failure" and "Not split" in split.text_result_for_llm
    assert json.dumps(runtime.tasks.for_session(worker)).count("Fix the login copy") == 0
