"""Bug bash round 7: away replies the messenger reads (J1, J4-J9)."""

import pytest
from conftest import Harness, call_tool, settle
from test_round6_drafts import away, spawn, told

from chiefofstaff.connector import holds_send, own_part
from chiefofstaff.models import Settings


def code_of(body: str, words: str) -> str:
    from test_connector import code_in

    return code_in(body, words)


async def drafted(tmp_path, *texts):
    harness = Harness(tmp_path / "data", Settings(away_notify=True, away_how="DM me"))
    runtime = await away(harness)
    worker, _task = await spawn(runtime, harness, "Answer the team")
    for text in texts:
        await call_tool(runtime, "draft_reply", worker, text=text, to="Bob", where="email")
    messenger, body = await told(runtime, harness)
    return harness, runtime, messenger, body


def status(harness, words: str) -> str:
    row = harness.store.one("SELECT id FROM drafts WHERE text LIKE ?", (f"%{words}%",))
    return harness.store.draft(row["id"])["status"]


async def reply(runtime, messenger, *replies):
    result = await call_tool(runtime, "report_owner_replies", messenger["id"],
                             replies=list(replies))  # fmt: skip
    return result.text_result_for_llm


async def test_a_reply_is_judged_once_and_the_newest_counts(tmp_path):
    """J1: a joke ("sure, send it… 🙄") read as unclear five times was read as send the sixth
    time and sent a draft with blanks, over a newer "no"."""
    harness, runtime, messenger, body = await drafted(tmp_path, "Monday works for me.")
    code = code_of(body, "Monday works")
    joke = {"code": code, "text": f"{code} sure, send it, what could go wrong", "intent": "unclear"}
    for _read in range(3):
        await reply(runtime, messenger, joke)
    assert status(harness, "Monday works") == "pending"
    # Read again, the same joke now looks like a send: it was judged already.
    assert await reply(runtime, messenger, {**joke, "intent": "send"}) == "Nothing to apply."
    assert status(harness, "Monday works") == "pending"
    # Its newest reply is what counts, whatever order they come in.
    no = {"code": code, "text": f"{code} no", "intent": "no", "at": "2026-11-08T10:05:00Z"}
    late_joke = {
        **joke,
        "text": f"{code} ship it lol",
        "intent": "send",
        "at": "2026-11-08T10:01:00Z",
    }
    said = await reply(runtime, messenger, no, late_joke)
    assert f"{code}: dropped" in said, said
    assert status(harness, "Monday works") == "rejected"
    notes = [
        m["text"] for m in harness.store.messages(harness.router_id) if f"[{code}]" in m["text"]
    ]
    assert len(notes) == 2, notes  # The unclear joke once, the no once.


async def test_two_codes_in_one_message_each_get_their_own_part(tmp_path):
    """J6: "UELNW ship it, B4XJT no": UELNW was refused for the other code's "no"."""
    harness, runtime, messenger, body = await drafted(tmp_path, "Friday it is.", "Thursday then.")
    first, second = code_of(body, "Friday it is"), code_of(body, "Thursday then")
    text = f"{first} ship it, {second} no"
    said = await reply(runtime, messenger, {"code": first, "text": text, "intent": "send"},
                       {"code": second, "text": text, "intent": "no"})  # fmt: skip
    assert f"{first}: marked as sent" in said and f"{second}: dropped" in said, said
    notes = [
        m["text"] for m in harness.store.messages(harness.router_id) if f"[{second}]" in m["text"]
    ]
    assert notes and first not in notes[0], notes


async def test_a_draft_with_blanks_is_not_sent_and_the_owner_is_asked_again(tmp_path):
    """J5: "yes" sent "Hey Alice, [yes, I'll review it / can't, ___]" as it was. J4: a reply
    that was not applied was said only in the Router chat, never where the owner is."""
    harness, runtime, messenger, body = await drafted(
        tmp_path, "Hey Alice, [yes, I can review it / sorry, can't this week]."
    )
    code = code_of(body, "Hey Alice")
    said = await reply(runtime, messenger, {"code": code, "text": f"{code} yes", "intent": "send"})
    assert "blank to fill" in said, said
    assert status(harness, "Hey Alice") == "pending"
    # The next notification asks again, where the owner is, saying why.
    harness.session(messenger["id"]).emit("session.idle")
    await settle(runtime)
    _messenger, again = await told(runtime, harness)
    assert "Not sent yet" in again and "blank to fill" in again, again
    assert code in again, "the same code still answers it"


@pytest.mark.parametrize(
    ("text", "held"),
    [
        ("\u4e0d\u9519\uff0c\u53d1\u5427", False),  # "Not bad, send it."
        ("\u6ca1\u4ec0\u4e48\u95ee\u9898\uff0c\u53d1\u5427", False),  # "No problem, send."
        ("\u5148\u53d1\u5427", False),  # "Send it first."
        ("send it, no changes needed", False),
        ("lgtm, no notes", False),
        ("ship it", False),
        ("send tmrw", True),
        ("\u660e\u5929\u53d1", True),  # "Send tomorrow."
        ("\u4e0b\u5348\u53d1", True),  # "Send in the afternoon."
        ("send it Monday", True),
        ("send at 3pm", True),
        ("send in an hour", True),
        ("send it EOD", True),
        ("ship it jk", True),
        ("\u5f00\u73a9\u7b11\u7684\uff0c\u53d1\u5427", True),  # "Just kidding, send it."
        ("sure send it \U0001f644", True),
        # Code review: more ways to say later.
        ("send it in the morning", True),
        ("sure, send at 3", True),
        ("send it next", True),
        ("\u5f00\u5b8c\u4f1a\u53d1", True),  # "Send after the meeting."
        ("\u8fc7\u4f1a\u513f\u53d1", True),  # "Send in a while."
        ("\u4e0b\u73ed\u540e\u53d1", True),  # "Send after work."
        ("\u597d\u7684\uff0c\u660e\u65e9\u53d1", True),  # "OK, send tomorrow morning."
    ],
)
def test_the_floor_holds_later_and_jokes_but_not_approval(text, held):
    """J7: clear sends refused for a "no" or a "not" in them; holds and jokes not caught."""
    assert bool(holds_send(text)) is held, (text, holds_send(text))


def test_a_message_s_part_for_each_code():
    codes = {"UELNW", "B4XJT"}
    assert own_part("UELNW ship it, B4XJT no", "UELNW", codes).strip() == "ship it,"
    assert own_part("UELNW ship it, B4XJT no", "B4XJT", codes).strip() == "no"
    assert own_part("ship it", "UELNW", codes) == "ship it"


async def test_an_unclear_reply_says_what_held_it_back(tmp_path):
    """Live r31: "send it after lunch" read as unclear said only 'answer "send"…', not why."""
    _harness, runtime, messenger, body = await drafted(tmp_path, "Lunch on me.")
    code = code_of(body, "Lunch on me")
    said = await reply(
        runtime,
        messenger,
        {"code": code, "text": f"{code} send it after lunch", "intent": "unclear"},
    )
    assert "\u201cafter\u201d sounds like not now" in said, said
