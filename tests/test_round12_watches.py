# ruff: noqa: RUF001  (quotes and brackets of every script are what E47 is about)
"""QA round 12: watches an older version scanned keep their place (E35), a gap that grows is
told again (E42), and a gist never ends inside a quote (E47)."""

import json
import re
import time
from datetime import datetime

from conftest import Harness
from test_round11_away import notices, scan, watch_key

from chiefofstaff.connector import (
    LOOKBACK_HOURS,
    OVERLAP_SECONDS,
    RECOVER_DAYS,
    clock_of,
    gist,
    unclosed,
)
from chiefofstaff.models import Settings, Watch


def starts(job: str) -> dict[str, float]:
    """Each watch of a scan's job, by what it watches: where it is searched from."""
    return {
        what: datetime.fromisoformat(at).timestamp()
        for what, at in re.findall(r"^\d+\. (.+?): new since (\S+)$", job, re.MULTILINE)
    }


async def upgraded(tmp_path, done_hours_ago: float):
    """A store as 0.1.35 left it: "DMs to me" covered in full by its own last scan, "#deploys"
    never (each of its scans covered it in part: no place of its own), and the last scan that
    covered both `done_hours_ago`. "PR mentions" was added since, never scanned."""
    watches = [Watch(what=what) for what in ("DMs to me", "#deploys", "PR mentions")]
    harness = Harness(tmp_path / "data", Settings(watches=watches, watch_after_hours_minutes=0))
    runtime = await harness.connect()
    done = time.time() - done_hours_ago * 3600
    covered = time.time() - 3600
    harness.store.set("watch_done_at", str(done))
    harness.store.set("watchdone:" + watch_key("DMs to me"), str(covered))
    harness.store.set("watch_scan", json.dumps([watch_key("DMs to me"), watch_key("#deploys")]))
    partial = {"reason": "the search returns the newest 20", "at": "2026-11-12T08:00:00+00:00"}
    harness.store.set("watchpartial:" + watch_key("#deploys"), json.dumps(partial | {"tries": 1}))
    return harness, runtime, done, covered


async def test_a_watch_an_older_version_never_covered_goes_on_from_where_it_left_it(tmp_path):
    """E35: "#deploys" had no place of its own, so the upgrade took it for a new watch: searched
    from a day back, the 14 hours before that neither searched nor told."""
    harness, runtime, done, covered = await upgraded(tmp_path, 38)
    job = await scan(harness, runtime)
    found = starts(job)
    assert abs(found["#deploys"] - (done - OVERLAP_SECONDS)) < 61, job
    assert abs(found["DMs to me"] - (covered - OVERLAP_SECONDS)) < 61, job
    # A watch added since is new: a day back, as before.
    day = time.time() - LOOKBACK_HOURS * 3600 - OVERLAP_SECONDS
    assert abs(found["PR mentions"] - day) < 120, job
    assert not notices(harness) and runtime.connector.view()["gaps"] == []
    # Its place is its own now: the next scan reads on from it, whatever it was.
    assert float(harness.store.get("watchfrom:" + watch_key("#deploys"))) == done


async def test_what_an_older_version_left_beyond_scans_reach_is_a_gap_told(tmp_path):
    """E35: left 9 days back, what is older than scans reach (a week) is a coverage gap, kept
    and told; the scan searches the week."""
    harness, runtime, done, _covered = await upgraded(tmp_path, 9 * 24)
    job = await scan(harness, runtime)
    horizon = time.time() - RECOVER_DAYS * 86400
    assert abs(starts(job)["#deploys"] - horizon) < 120, job
    [told] = notices(harness)
    assert told.startswith("Could not check \u201c#deploys\u201d between "), told
    [gap] = runtime.connector.view()["gaps"]
    assert gap["watch"] == "#deploys" and gap["text"] in told
    assert abs(datetime.fromisoformat(gap["from"]).timestamp() - done) < 1, gap
    assert abs(datetime.fromisoformat(gap["to"]).timestamp() - horizon) < 120, gap


async def test_a_watch_known_only_by_its_partial_scans_keeps_its_place(tmp_path):
    """E35: left out of the last scan (switched off then), its partial scans say it was one."""
    harness, runtime, done, _covered = await upgraded(tmp_path, 30)
    harness.store.set("watch_scan", json.dumps([watch_key("DMs to me")]))
    job = await scan(harness, runtime)
    assert abs(starts(job)["#deploys"] - (done - OVERLAP_SECONDS)) < 61, job


def told(harness) -> list[str]:
    router = harness.store.messages(harness.router_id)
    return [m["text"] for m in router if m["kind"] == "notice" and "ould not check" in m["text"]]


async def test_a_gap_that_grows_a_lot_is_told_again_with_all_of_it(tmp_path):
    """E42: a gap told at 15 minutes grew to three and a half days without another word."""
    harness = Harness(tmp_path / "data", Settings(watches=[Watch(what="#deploys")]))
    await harness.connect()
    connector = harness.runtime.connector
    start = time.time() - 4 * 86400
    reason = "the search returns the newest 20"

    covered = [start + 15 * 60]

    def grow(minutes: float):
        # Given up on again from where the last gap ended (see Connector.give_up).
        connector.gap("#deploys", covered[0], start + minutes * 60, reason)
        covered[0] = start + minutes * 60

    connector.gap("#deploys", start, covered[0], reason)
    assert len(told(harness)) == 1
    grow(20)
    grow(29)
    assert len(told(harness)) == 1, "a little longer: not told again"
    grow(31)  # Twice as long.
    assert len(told(harness)) == 2
    assert told(harness)[-1].startswith("Still could not check \u201c#deploys\u201d between ")
    grow(50)
    assert len(told(harness)) == 2
    grow(61)  # Past an hour (not twice 31 minutes).
    assert len(told(harness)) == 3
    grow(100)
    grow(121)
    assert len(told(harness)) == 3
    for hours in (4.1, 8.2):  # Twice as long, each.
        grow(hours * 60)
    assert len(told(harness)) == 5
    grow(12 * 60)
    grow(16 * 60)
    assert len(told(harness)) == 5
    grow(24.5 * 60)  # Past a day (not twice 8.2 hours).
    assert len(told(harness)) == 6
    grow(3.5 * 24 * 60)
    assert len(told(harness)) == 7
    latest = told(harness)[-1]
    assert f"between {clock_of(start)} and {clock_of(start + 3.5 * 86400)}" in latest, latest
    grow(3.6 * 24 * 60)
    assert len(told(harness)) == 7, "once per crossing"
    [gap] = connector.view()["gaps"]
    assert abs(datetime.fromisoformat(gap["to"]).timestamp() - (start + 3.6 * 86400)) < 1


async def test_a_gap_an_older_version_kept_is_not_told_again_at_once(tmp_path):
    """E42: one kept before this version (told when it began, its length then unknown) counts
    as told as it is: not told again for growing a little."""
    harness = Harness(tmp_path / "data", Settings(watches=[Watch(what="#deploys")]))
    await harness.connect()
    connector = harness.runtime.connector
    start = time.time() - 4 * 86400
    kept = {"key": watch_key("#deploys"), "watch": "#deploys", "from": start, "to": start + 3600}
    harness.store.set("watch_gaps", json.dumps([kept | {"reason": "x", "at": "2026-11-12"}]))
    connector.gap("#deploys", start + 3000, start + 5400, "x")
    assert not told(harness)
    connector.gap("#deploys", start + 5000, start + 7200, "x")
    assert len(told(harness)) == 1


def test_a_gist_never_ends_inside_a_quote():
    """E47: an away item read …说“先做 A。, cut at a stop inside the quote."""
    text = (
        "老板说“先做 A。再做 B，然后把结果发给我。”另外，部署脚本需要更新，因为新的镜像已经改了"
        "路径，旧的路径已经不能用了，请尽快处理。"
    )
    assert gist(text, limit=40) == "老板说“先做 A。再做 B，然后把结果发给我。”"
    said = gist(text, limit=60)
    assert said.endswith("\u2026\u201d") and not unclosed(said), said
    english = (
        'He wrote: "Ship the fix today. Then tell the release channel what changed and why it '
        'matters for the next build." We should answer him before noon.'
    )
    said = gist(english, limit=60)
    assert said.endswith('\u2026"') and len(said) <= 60, said
    assert gist(english, limit=130) == english[: english.index(" We")]
    bracketed = "Logs say why (see the run. Its second half shows the cause). Restart it now."
    assert gist(bracketed, limit=70) == bracketed[: bracketed.index(" Restart")]
    japanese = "部長は「まず A を直す。次に B を確認する。」と言いました。急ぎです。"
    assert not unclosed(gist(japanese, limit=30))


def test_quotes_and_brackets_left_open():
    assert unclosed('He said "go') == '"'
    assert unclosed("“先做 A。（第一步") == "）”"
    assert unclosed("【注意】「先做") == "」"
    assert unclosed("don't stop, it’s fine, the owners' list") == ""
    assert unclosed("1) first, 2) second") == "", "a closing mark with nothing open"
    assert unclosed('a 5" screen') == ""
    assert unclosed("‘quoted’ and 'this'") == ""
    assert unclosed("‘it’s fine") == "’", "an apostrophe inside a quote does not close it"
    long = "他说“" + "这句话很长而且没有空格" * 10 + "”"
    said = gist(long, limit=30)
    assert len(said) == 30 and said.endswith("\u2026\u201d"), said
