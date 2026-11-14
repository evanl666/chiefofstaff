"""QA round 13, E50: a gap an older version kept was told once, when it began; 0.1.43 took it
for told at its length then (15 minutes told, 100 hours "told") and said nothing more. The first
scan after the update tells each such gap again, once, all of it."""

import json
import time
from datetime import datetime

import pytest
from conftest import Harness
from test_round11_away import scan, watch_key
from test_round12_watches import told as notices

from chiefofstaff.connector import between, gap_text
from chiefofstaff.models import Settings, Watch

REASON = "the search returns the newest 20"


async def kept_from_before(tmp_path, version: str, hours: float = 100):
    """A store an older version left: a "#deploys" gap told when it was 15 minutes long, grown
    to `hours` since. 0.1.42 kept no `told`; 0.1.43, touching it, wrote its length then."""
    harness = Harness(tmp_path / "data", Settings(watches=[Watch(what="#deploys")]))
    runtime = await harness.connect()
    start = time.time() - (hours + 2) * 3600
    end = start + hours * 3600
    gap = {"key": watch_key("#deploys"), "watch": "#deploys", "from": start, "to": end,
           "reason": REASON, "at": "2026-11-12T08:00:00+00:00"}  # fmt: skip
    if version == "0.1.43":
        gap["told"] = end - start - 300  # Grown five minutes since it took it for told.
    harness.store.set("watch_gaps", json.dumps([gap]))
    return harness, runtime, gap


@pytest.mark.parametrize("version", ["0.1.42", "0.1.43"])
async def test_a_gap_an_older_version_kept_is_told_once_whole_after_the_update(tmp_path, version):
    harness, runtime, gap = await kept_from_before(tmp_path, version)
    await scan(harness, runtime, items=[])
    [told] = notices(harness)
    assert told.startswith(
        f"Could not check \u201c#deploys\u201d for 4 days 4 hours, "
        f"{between(gap['from'], gap['to'])} ({REASON})"
    ), told
    await scan(harness, runtime, items=[])
    assert len(notices(harness)) == 1, "once"
    # Growing a little since: not told again (twice as long, or past a day, would be).
    runtime.connector.gap("#deploys", gap["to"] - 60, gap["to"] + 3600, REASON)
    assert len(notices(harness)) == 1
    runtime.connector.gap("#deploys", gap["to"] - 60, gap["from"] + 201 * 3600, REASON)
    assert len(notices(harness)) == 2, "twice as long: told again"


async def test_a_gap_told_whole_by_0_1_43_is_told_once_more_as_it_was_open(tmp_path):
    """0.1.43 wrote `told` as long as the gap was: whether it was told then cannot be known, so
    the newest gap of each watch is told once more; an older one told whole is not."""
    harness, runtime, gap = await kept_from_before(tmp_path, "0.1.43")
    closed = gap | {"from": gap["from"] - 86400, "to": gap["from"] - 82800, "told": 3600}
    open_ = gap | {"told": gap["to"] - gap["from"]}
    harness.store.set("watch_gaps", json.dumps([closed, open_]))
    await scan(harness, runtime, items=[])
    [told] = notices(harness)
    assert between(open_["from"], open_["to"]) in told, told
    assert between(closed["from"], closed["to"]) not in told, told


async def test_an_older_gap_that_grows_at_once_is_told_once(tmp_path):
    """Its first growth after the update (before any scan): told then, all of it, once."""
    harness, runtime, gap = await kept_from_before(tmp_path, "0.1.42")
    runtime.connector.gap("#deploys", gap["to"] - 60, gap["to"] + 600, REASON)
    [told] = notices(harness)
    assert between(gap["from"], gap["to"] + 600) in told, told
    await scan(harness, runtime, items=[])
    assert len(notices(harness)) == 1


async def test_a_gap_this_version_told_is_not_told_again_at_a_scan(tmp_path):
    harness = Harness(tmp_path / "data", Settings(watches=[Watch(what="#deploys")]))
    runtime = await harness.connect()
    start = time.time() - 6 * 3600
    runtime.connector.gap("#deploys", start, start + 3600, REASON)
    runtime.connector.gap("#deploys", start + 3500, start + 3900, REASON)  # A little longer.
    await scan(harness, runtime, items=[])
    await scan(harness, runtime, items=[])
    assert len(notices(harness)) == 1


def clock(moment: datetime) -> str:
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def day(moment: datetime) -> str:
    return f"{moment.strftime('%b')} {moment.day}"


def test_a_gap_of_days_reads_as_days():
    """Re-review: a 110-hour gap ending today read "between Nov 9, 6:34 AM and 10:49 AM", as if
    it were hours. Its end's day whenever it is not its start's, and how long it was."""
    end = datetime.now().astimezone().replace(hour=10, minute=49, second=0, microsecond=0)
    start = datetime.fromtimestamp(end.timestamp() - 110 * 3600).astimezone()
    gap = {"watch": "#deploys", "from": start.timestamp(), "to": end.timestamp(), "reason": REASON}
    assert gap_text(gap) == (
        f"Could not check \u201c#deploys\u201d for 4 days 14 hours, between {day(start)}, "
        f"{clock(start)} and {day(end)}, {clock(end)} ({REASON})"
    )
    assert gap_text(gap, still=True).startswith(
        "Still could not check \u201c#deploys\u201d for 4 days"
    )


def test_a_gap_over_midnight_says_both_days_and_a_short_one_its_minutes():
    end = datetime.now().astimezone().replace(hour=2, minute=0, second=0, microsecond=0)
    start = datetime.fromtimestamp(end.timestamp() - 3 * 3600).astimezone()
    gap = {"watch": "#deploys", "from": start.timestamp(), "to": end.timestamp()}
    assert gap_text(gap) == (
        f"Could not check \u201c#deploys\u201d for 3 hours, between {day(start)}, {clock(start)} "
        f"and {day(end)}, {clock(end)}"
    )
    short = {"watch": "#deploys", "from": end.timestamp(), "to": end.timestamp() + 15 * 60}
    assert gap_text(short) == (
        f"Could not check \u201c#deploys\u201d for 15 minutes, between {clock(end)} and "
        f"{clock(datetime.fromtimestamp(short['to']).astimezone())}"
    )
    odd = {"watch": "#deploys", "from": end.timestamp(), "to": end.timestamp() + 6.6 * 3600}
    assert "for about 7 hours, " in gap_text(odd)
