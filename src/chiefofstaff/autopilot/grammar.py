"""Autopilot's chat commands and durations. A message to the Router that is exactly one of its
commands, in English or Chinese, turns it on or off; Chief of Staff answers it itself, in the
message's language, always saying when it ends. Anything else, near misses included, goes to the
Router as before.

| You write | It does |
| --- | --- |
| `autopilot on` · `开 autopilot` | On for 2 hours (until the next morning time, if sooner) |
| `autopilot until 5pm` · `autopilot until tomorrow 9am` · `开 autopilot 到下午5点` | Until then |
| `autopilot for 3h` · `autopilot 开两个小时` · `开 autopilot 30分钟` | On for that long |
| `autopilot until I turn it off` · `开 autopilot 直到我关掉` | On until turned off |
| `autopilot off` · `stop autopilot` · `关 autopilot` · `停止 autopilot` | Stop |

Pure functions of the text and the time: no store, no sessions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

# A bare "on" lasts this long, or until the next morning time if that comes first.
BARE = timedelta(hours=2)
# The longest a timed run may be asked for (longer: until turned off, said as such).
LONGEST = timedelta(days=7)
# The morning time when the daily summary is off.
MORNING = "09:00"


@dataclass(frozen=True)
class Command:
    """`on` with its end (`until` None: until turned off), or `off`; in the language it was
    written in ("en" or "zh"). `bare`: on with no time said."""

    action: str
    language: str
    until: datetime | None = None
    bare: bool = False


DIGITS = {"零": 0, "\u3007": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
          "七": 7, "八": 8, "九": 9}  # fmt: skip


def chinese_number(text: str) -> int | None:
    """0 to 99 in digits or Chinese numerals (十, 十二, 二十三, 两)."""
    if text.isdigit():
        return int(text)
    if not text or any(char not in DIGITS and char != "十" for char in text):
        return None
    if "十" not in text:
        return DIGITS[text] if len(text) == 1 else None
    tens, _, ones = text.partition("十")
    if len(tens) > 1 or len(ones) > 1 or "十" in ones:
        return None
    return (DIGITS[tens] if tens else 1) * 10 + (DIGITS[ones] if ones else 0)


def at_time(moment: datetime, hour: int, minute: int, day: int | None) -> datetime | None:
    """That time on a day (0 today, 1 tomorrow), or with no day said, the next one after now."""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    when = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day is not None:
        return when + timedelta(days=day)
    return when if when > moment else when + timedelta(days=1)


def either_half(moment: datetime, hour: int, minute: int, day: int | None) -> datetime | None:
    """A time said without morning or afternoon (5, 5:30, 5点): whichever of h:mm and h+12:mm
    comes next after now (on that day, when one is said)."""
    if not 1 <= hour <= 12:
        return at_time(moment, hour, minute, day)
    found = [
        when
        for candidate in (hour % 12, hour % 12 + 12)
        if (when := at_time(moment, candidate, minute, day)) is not None and when > moment
    ]
    return min(found) if found else None


def morning(moment: datetime, morning_time: str) -> datetime:
    """The next morning time (the daily summary's, or 9:00 AM) after now."""
    hour, minute = (int(part) for part in (morning_time or MORNING).split(":"))
    return at_time(moment, hour, minute, None) or moment + timedelta(days=1)


def bare_until(moment: datetime, morning_time: str) -> datetime:
    """Two hours, or until the next morning time if that comes first."""
    return min(moment + BARE, morning(moment, morning_time))


def timed(moment: datetime, length: timedelta) -> datetime | None:
    return moment + length if timedelta(minutes=1) <= length <= LONGEST else None


EN_TIME = re.compile(
    r"(?:(?P<day>tomorrow|today)\s+)?(?:at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*"
    r"(?P<half>am|pm|a\.m\.|p\.m\.)?"
)
EN_LENGTH = re.compile(
    r"(?:(?P<h>\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours))?\s*"
    r"(?:(?P<m>\d+)\s*(?:m|min|mins|minute|minutes))?"
)
EN_OFF = frozenset({"autopilot off", "stop autopilot"})
EN_FOREVER = "i turn it off"


def english_time(rest: str, moment: datetime) -> datetime | None:
    found = EN_TIME.fullmatch(rest)
    if not found:
        return None
    hour, minute = int(found["h"]), int(found["m"] or 0)
    day = {"tomorrow": 1, "today": 0}.get(found["day"] or "")
    if found["half"]:
        if not 1 <= hour <= 12:
            return None
        when = at_time(moment, hour % 12 + (12 if found["half"][0] == "p" else 0), minute, day)
    else:
        when = either_half(moment, hour, minute, day)
    if when is None or when <= moment or when - moment > LONGEST:
        return None
    return when


def english(text: str, moment: datetime, morning_time: str) -> Command | None:
    if text in EN_OFF:
        return Command("off", "en")
    if text == "autopilot on":
        return Command("on", "en", bare_until(moment, morning_time), bare=True)
    if text.startswith("autopilot until "):
        rest = text.removeprefix("autopilot until ")
        if rest == EN_FOREVER:
            return Command("on", "en", None)
        when = english_time(rest, moment)
        return Command("on", "en", when) if when else None
    if text.startswith("autopilot for "):
        found = EN_LENGTH.fullmatch(text.removeprefix("autopilot for ").strip())
        if not found or not (found["h"] or found["m"]):
            return None
        length = timedelta(hours=float(found["h"] or 0), minutes=int(found["m"] or 0))
        when = timed(moment, length)
        return Command("on", "en", when) if when else None
    return None


ZH_ON = "(?:开|开启|打开)"
ZH_OFF = re.compile(r"(?:关|关闭|关掉|停|停止|停掉)\s*autopilot|autopilot\s*(?:关|关闭|关掉|停止)")
ZH_NUMBER = r"\d{1,2}|[零\u3007一二两三四五六七八九十]{1,3}"
ZH_TIME = re.compile(
    rf"(?P<day>今天|明天|明早|今晚|明晚)?(?P<part>凌晨|早上|上午|中午|下午|傍晚|晚上)?"
    rf"(?:(?P<h>{ZH_NUMBER})(?:点|點)(?:(?P<half>半)|(?P<m>{ZH_NUMBER})分?)?"
    rf"|(?P<hh>\d{{1,2}}):(?P<mm>\d{{2}}))"
)
ZH_LENGTH = re.compile(
    rf"(?:(?P<h>{ZH_NUMBER}|半)\s*个?\s*(?P<half>半)?\s*(?:小时|钟头))?\s*"
    rf"(?:(?P<m>{ZH_NUMBER})\s*分钟)?"
)
ZH_FOREVER = re.compile(r"直到我(?:关掉|关闭|关上|关)(?:它)?")
EVENING = {"下午", "傍晚", "晚上"}


def chinese_time(rest: str, moment: datetime) -> datetime | None:
    found = ZH_TIME.fullmatch(rest)
    if not found:
        return None
    day = {"今天": 0, "今晚": 0, "明天": 1, "明早": 1, "明晚": 1}.get(found["day"] or "")
    part = found["part"] or {"今晚": "晚上", "明晚": "晚上", "明早": "上午"}.get(found["day"] or "")
    if found["hh"]:
        hour, minute = int(found["hh"]), int(found["mm"])
    else:
        hour = chinese_number(found["h"])
        minute = 30 if found["half"] else chinese_number(found["m"] or "0")
        if hour is None or minute is None:
            return None
    if part in EVENING or (part == "中午" and hour < 11):
        hour = hour + 12 if hour < 12 else hour
    elif part == "凌晨" and hour == 12:
        hour = 0
    when = at_time(moment, hour, minute, day) if part else either_half(moment, hour, minute, day)
    if when is None or when <= moment or when - moment > LONGEST:
        return None
    return when


def chinese_length(rest: str) -> timedelta | None:
    found = ZH_LENGTH.fullmatch(rest.strip())
    if not found or not (found["h"] or found["m"]):
        return None
    if found["h"] == "半":
        hours = 0.5 if not found["half"] else None
    else:
        whole = chinese_number(found["h"]) if found["h"] else 0
        hours = None if whole is None else whole + (0.5 if found["half"] else 0)
    minutes = chinese_number(found["m"]) if found["m"] else 0
    if hours is None or minutes is None:
        return None
    return timedelta(hours=hours, minutes=minutes)


def chinese(text: str, moment: datetime, morning_time: str) -> Command | None:
    if ZH_OFF.fullmatch(text):
        return Command("off", "zh")
    # 开 autopilot …, or autopilot 开 … (a length: autopilot 开两个小时).
    found = re.fullmatch(rf"{ZH_ON}\s*autopilot\s*(?P<rest>.*)", text) or re.fullmatch(
        rf"autopilot\s*{ZH_ON}\s*(?P<rest>.*)", text
    )
    if not found:
        return None
    rest = found["rest"].strip()
    if not rest:
        return Command("on", "zh", bare_until(moment, morning_time), bare=True)
    if ZH_FOREVER.fullmatch(rest):
        return Command("on", "zh", None)
    if rest.startswith(("到", "直到")):
        when = chinese_time(rest.removeprefix("直到").removeprefix("到").strip(), moment)
        return Command("on", "zh", when) if when else None
    if (length := chinese_length(rest)) and (when := timed(moment, length)):
        return Command("on", "zh", when)
    return None


def parse(text: str, moment: datetime, morning_time: str = "") -> Command | None:
    """The command a whole message is, or None (it goes to the Router). `moment`: now, in the
    owner's time zone; `morning_time`: the daily summary's time ("" when it is off)."""
    flat = re.sub(r"\s+", " ", text.strip()).rstrip(".!\u3002\uff01").strip()
    if not flat or len(flat) > 60 or "autopilot" not in flat.lower():
        return None
    flat = flat.lower()
    if re.search(r"[\u4e00-\u9fff]", flat):
        return chinese(flat, moment, morning_time)
    return english(flat, moment, morning_time)


def clock(when: datetime, moment: datetime, language: str) -> str:
    """When a run ends, as the owner reads it: 4:12 PM, tomorrow 9:00 AM, Mon 6:00 PM;
    下午 4:12, 明天上午 9:00."""
    local = when.astimezone(moment.tzinfo)
    days = (local.date() - moment.date()).days
    if language == "zh":
        hour = local.hour
        part = ("凌晨" if hour < 6 else "上午" if hour < 12 else "中午" if hour < 13
                else "下午" if hour < 18 else "晚上")  # fmt: skip
        shown = f"{part} {hour % 12 or 12}:{local.minute:02d}"
        if days == 0:
            return shown
        if days == 1:
            return f"明天{shown}"
        return f"{local.month}月{local.day}日{shown}"
    shown = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    if days == 0:
        return shown
    if days == 1:
        return f"tomorrow {shown}"
    if days < 7:
        return f"{local.strftime('%a')} {shown}"
    return f"{local.strftime('%b')} {local.day} {shown}"


def said_on(until: datetime | None, moment: datetime, language: str) -> str:
    """The one line a command is answered with."""
    if language == "zh":
        end = "直到你关闭" if until is None else "到" + clock(until, moment, "zh")
        return f"Autopilot 已开启\uff0c{end}"
    if until is None:
        return "Autopilot on until you turn it off"
    return f"Autopilot on until {clock(until, moment, 'en')}"


def said_off(was_on: bool, language: str) -> str:
    if language == "zh":
        return "Autopilot 已停止。" if was_on else "Autopilot 没有开着。"
    return "Autopilot stopped." if was_on else "Autopilot is off."
