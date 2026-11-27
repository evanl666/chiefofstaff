"""Autopilot's chat commands (autopilot/grammar.py): the whole message, in English or Chinese,
always answered with the end; near misses are not commands (they go to the Router)."""

from datetime import datetime, timedelta, timezone

import pytest

from chiefofstaff.autopilot.grammar import (
    chinese_number,
    clock,
    parse,
    said_off,
    said_on,
)

PT = timezone(timedelta(hours=-8))
NOW = datetime(2026, 11, 24, 14, 12, tzinfo=PT)  # A Tuesday, 2:12 PM.
NIGHT = datetime(2026, 11, 24, 23, 30, tzinfo=PT)


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=PT)


@pytest.mark.parametrize(
    ("text", "until"),
    [
        ("autopilot on", at(24, 16, 12)),
        ("Autopilot on.", at(24, 16, 12)),
        ("  autopilot   ON ", at(24, 16, 12)),
        ("autopilot until 5pm", at(24, 17)),
        ("autopilot until 5:30 pm", at(24, 17, 30)),
        ("autopilot until 1pm", at(25, 13)),  # Already past today: tomorrow.
        ("autopilot until 17:00", at(24, 17)),
        ("autopilot until 5", at(24, 17)),  # No AM or PM: the next 5 o'clock.
        ("autopilot until tomorrow 9am", at(25, 9)),
        ("autopilot until tomorrow 9", at(25, 9)),
        ("autopilot for 3h", at(24, 17, 12)),
        ("autopilot for 90 minutes", at(24, 15, 42)),
        ("autopilot for 1h 30m", at(24, 15, 42)),
        ("autopilot for 2 hours", at(24, 16, 12)),
        ("autopilot until I turn it off", None),
        ("开 autopilot", at(24, 16, 12)),
        ("开autopilot", at(24, 16, 12)),
        ("开 autopilot 到下午5点", at(24, 17)),
        ("开 autopilot 到下午5点半", at(24, 17, 30)),
        ("开 autopilot 到17:00", at(24, 17)),
        ("开 autopilot 到明天上午9点", at(25, 9)),
        ("开 autopilot 到明早九点", at(25, 9)),
        ("开 autopilot 到晚上十一点", at(24, 23)),
        ("autopilot 开两个小时", at(24, 16, 12)),
        ("autopilot 开一个半小时", at(24, 15, 42)),
        ("开 autopilot 30分钟", at(24, 14, 42)),
        ("开 autopilot 直到我关掉", None),
        ("打开 autopilot。", at(24, 16, 12)),
    ],
)
def test_on_commands_and_their_end(text, until):
    command = parse(text, NOW, "09:00")
    assert command is not None and command.action == "on"
    assert command.until == until
    assert command.language == ("zh" if any("\u4e00" <= c <= "\u9fff" for c in text) else "en")
    assert command.bare == (until is not None and "until" not in text and "for" not in text
                            and not any(c in text for c in "到小分直"))  # fmt: skip


def test_a_bare_on_ends_at_the_morning_time_if_that_comes_first():
    assert parse("autopilot on", NIGHT, "09:00").until == NIGHT + timedelta(hours=2)
    early = datetime(2026, 11, 25, 8, 0, tzinfo=PT)
    assert parse("autopilot on", early, "09:00").until == at(25, 9)
    assert parse("autopilot on", early, "").until == at(25, 9), "9:00 AM with no summary"
    assert parse("autopilot on", early, "08:30").until == at(25, 8, 30)


@pytest.mark.parametrize(
    "text",
    ["autopilot off", "stop autopilot", "Stop Autopilot!", "关 autopilot", "停止 autopilot",
     "关闭autopilot", "autopilot 关"],
)  # fmt: skip
def test_off_commands(text):
    assert parse(text, NOW).action == "off"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "autopilot",
        "autopilot on please",
        "turn on autopilot",
        "is autopilot on?",
        "what did autopilot do last night?",
        "autopilot until",
        "autopilot until 25pm",
        "autopilot until 13pm",
        "autopilot until today 1pm",  # Past.
        "autopilot until next week",
        "autopilot for 0h",
        "autopilot for 30 days",  # Longer than a week: not a timed run.
        "autopilot for ever",
        "autopilot 是什么",
        "开 autopilot 到昨天",
        "开 autopilot 一会儿",
        "please stop autopilot now and tell me what it did",
        "autopilot on\nand fix the tests",
    ],
)
def test_near_misses_are_not_commands(text):
    assert parse(text, NOW, "09:00") is None


def test_chinese_numbers():
    assert [chinese_number(t) for t in ("两", "十", "十二", "二十三", "9", "三十")] == [
        2,
        10,
        12,
        23,
        9,
        30,
    ]
    assert chinese_number("十十") is None and chinese_number("几") is None


def test_the_end_is_said_back_in_the_messages_language():
    assert clock(at(24, 16, 12), NOW, "en") == "4:12 PM"
    assert clock(at(25, 9), NOW, "en") == "tomorrow 9:00 AM"
    assert clock(at(27, 18), NOW, "en") == "Fri 6:00 PM"
    assert clock(at(24, 16, 12), NOW, "zh") == "下午 4:12"
    assert clock(at(25, 9), NOW, "zh") == "明天上午 9:00"
    assert said_on(at(24, 16, 12), NOW, "en") == "Autopilot on until 4:12 PM"
    assert said_on(None, NOW, "en") == "Autopilot on until you turn it off"
    assert said_on(at(24, 16, 12), NOW, "zh") == "Autopilot 已开启\uff0c到下午 4:12"
    assert said_on(None, NOW, "zh") == "Autopilot 已开启\uff0c直到你关闭"
    assert said_off(True, "en") == "Autopilot stopped." and said_off(False, "en") == (
        "Autopilot is off."
    )
    assert said_off(True, "zh") == "Autopilot 已停止\u3002"
