"""QA round 14, E74: after the update, the first scan told again a gap over ten days ago and told
whole then, and one of a watch since removed, as if they were new ("anything that came in then
may have been missed"). Only a gap still open, or told shorter than it is, is told again, and
said to be told before."""

import json
import time

from conftest import Harness
from test_round11_away import scan, watch_key
from test_round12_watches import told as notices

from chiefofstaff.connector import between
from chiefofstaff.models import Settings, Watch

REASON = "the search returns the newest 20"
DAY = 86400


async def kept(tmp_path, *gaps: dict, covered: float | None = None):
    """A store 0.1.43 or 0.1.44 left (gaps without `told_at`), watching "#deploys" only; it was
    covered in full up to `covered` (epoch) when given."""
    harness = Harness(tmp_path / "data", Settings(watches=[Watch(what="#deploys")]))
    runtime = await harness.connect()
    harness.store.set("watch_gaps", json.dumps(list(gaps)))
    if covered:
        harness.store.set("watchdone:" + watch_key("#deploys"), str(covered))
    return harness, runtime


def gap(watch: str, start: float, end: float, told: float | None = None) -> dict:
    found = {"key": watch_key(watch), "watch": watch, "from": start, "to": end, "reason": REASON,
             "at": "2026-11-04T08:00:00+00:00"}  # fmt: skip
    return found | ({"told": told} if told is not None else {})


async def test_a_gap_over_and_told_whole_or_of_a_removed_watch_is_not_told_again(tmp_path):
    now = time.time()
    over = gap("#deploys", now - 11 * DAY, now - 10 * DAY, told=DAY)
    removed = gap("#incidents", now - 3 * DAY, now - 2 * DAY, told=3600)  # Told short, even.
    harness, runtime = await kept(tmp_path, over, removed, covered=now - 3600)
    await scan(harness, runtime, items=[])
    assert not notices(harness)
    stamped = json.loads(harness.store.get("watch_gaps"))
    assert all(item["told_at"] for item in stamped), "looked at once: never again"


async def test_a_gap_still_open_is_told_again_as_told_before(tmp_path):
    now = time.time()
    open_ = gap("#deploys", now - 5 * DAY, now - 3600, told=5 * DAY - 3600)
    harness, runtime = await kept(tmp_path, open_, covered=now - 3600)  # Not covered past it.
    await scan(harness, runtime, items=[])
    [told] = notices(harness)
    assert told == (
        "Told before; the full range is: could not check \u201c#deploys\u201d for 4 days 23 "
        f"hours, {between(open_['from'], open_['to'])} ({REASON}). Settings \u2192 Connections "
        "\u2192 Watches lists what could not be checked."
    ), told
    assert "may have been missed" not in told
    await scan(harness, runtime, items=[])
    assert len(notices(harness)) == 1


async def test_a_gap_over_but_told_shorter_than_it_is_is_told_again(tmp_path):
    now = time.time()
    short = gap("#deploys", now - 6 * DAY, now - 4 * DAY, told=900)
    whole = gap("#deploys", now - 3 * DAY, now - 2 * DAY, told=DAY)
    harness, runtime = await kept(tmp_path, short, whole, covered=now - DAY)
    await scan(harness, runtime, items=[])
    [told] = notices(harness)
    assert between(short["from"], short["to"]) in told and "for 2 days" in told, told
    assert between(whole["from"], whole["to"]) not in told, told
