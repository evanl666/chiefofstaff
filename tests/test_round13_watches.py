"""QA round 13, E50: a gap an older version kept was told once, when it began; the update took
it for told at its length then (15 minutes told, 100 hours long) and said nothing more."""

import json
import time

from conftest import Harness
from test_round11_away import scan, watch_key
from test_round12_watches import told as notices

from chiefofstaff.connector import clock_of
from chiefofstaff.models import Settings, Watch

REASON = "the search returns the newest 20"


async def kept_from_before(tmp_path, hours: float = 100):
    """A store an older version left: a "#deploys" gap told when it was 15 minutes long, grown
    to `hours` since, without `told` (that version kept no such thing)."""
    harness = Harness(tmp_path / "data", Settings(watches=[Watch(what="#deploys")]))
    runtime = await harness.connect()
    start = time.time() - (hours + 2) * 3600
    end = start + hours * 3600
    gap = {"key": watch_key("#deploys"), "watch": "#deploys", "from": start, "to": end,
           "reason": REASON, "at": "2026-11-12T08:00:00+00:00"}  # fmt: skip
    harness.store.set("watch_gaps", json.dumps([gap]))
    return harness, runtime, gap


async def test_a_gap_an_older_version_kept_is_told_once_whole_after_the_update(tmp_path):
    harness, runtime, gap = await kept_from_before(tmp_path)
    await scan(harness, runtime, items=[])
    [told] = notices(harness)
    assert told.startswith(
        f"Could not check \u201c#deploys\u201d between {clock_of(gap['from'])} and "
        f"{clock_of(gap['to'])} ({REASON})"
    ), told
    await scan(harness, runtime, items=[])
    assert len(notices(harness)) == 1, "once"
    # Growing a little since: not told again (twice as long, or past a day, would be).
    runtime.connector.gap("#deploys", gap["to"] - 60, gap["to"] + 3600, REASON)
    assert len(notices(harness)) == 1


async def test_an_older_gap_that_grows_at_once_is_told_once(tmp_path):
    """Its first growth after the update (before any scan): told then, all of it, once."""
    harness, runtime, gap = await kept_from_before(tmp_path)
    runtime.connector.gap("#deploys", gap["to"] - 60, gap["to"] + 600, REASON)
    [told] = notices(harness)
    assert f"and {clock_of(gap['to'] + 600)}" in told, told
    await scan(harness, runtime, items=[])
    assert len(notices(harness)) == 1
