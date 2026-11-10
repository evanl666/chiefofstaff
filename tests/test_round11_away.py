"""Bug bash round 11: what an away notification says of each item (E31), answer codes only
where the owner's replies are read (E36), and watches that keep what they could not cover
(E35, C9)."""

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from conftest import Harness, call_tool, settle
from test_round6_drafts import away, spawn

from chiefofstaff.connector import GAP_TRIES, OVERLAP_SECONDS, RECOVER_DAYS, gap_text, gist
from chiefofstaff.models import Settings, Watch

TITLE = "Fix deployment compatibility issue"
NEEDS = "The worker found two possible solutions and needs you to select one before continuing."


def files(harness, messenger: dict) -> tuple[str, str]:
    """The messenger's last job's message with codes ("" when it has none) and without."""
    job = harness.session(messenger["id"]).sent[-1]
    codes = re.search(r"A: (/\S+-codes\.txt)", job)
    plain = re.search(r"B: (/\S+-plain\.txt)", job)
    assert plain, job
    return (Path(codes.group(1)).read_text() if codes else ""), Path(plain.group(1)).read_text()


def section(body: str, words: str) -> list[str]:
    return next(part for part in body.split("\n\n") if words in part).splitlines()


# E31: an item says which task it is, what it needs in its first sentences that say something,
# and where to answer it.


def test_a_gist_leaves_labels_out_and_ends_on_a_whole_sentence():
    labelled = "Issue:\nThere are two reasons:\n1. The base image moved.\n2. The lock pins a wheel."
    assert gist(labelled) == "The base image moved. The lock pins a wheel."
    assert gist(f"## Summary\n**Options:**\n- \n{NEEDS}") == NEEDS
    assert gist("**Options**\n**It waits for your pick.**") == "*It waits for your pick.*"
    sentences = [
        "The deploy fails on the new runtime image.",
        "Pinning the old image is quick but leaves the cipher problem for later.",
        "Upgrading the client is safer, but it touches every service that uses it and needs a "
        "release of each.",
    ]
    assert gist(" ".join(sentences)) == " ".join(sentences[:2]), "whole sentences, not cut"
    long = "word " * 70 + "end."
    said = gist(long)
    assert len(said) <= 200 and said.endswith("\u2026"), said
    assert long[len(said) - 1] == " ", "cut at a word"
    asked = gist(" ".join([*sentences, "Which one should I use?"]), ask=True)
    assert asked.startswith(sentences[0]) and asked.endswith("Which one should I use?"), asked
    assert len(asked) <= 200


async def test_an_away_item_names_its_task_what_it_needs_and_where_to_answer(tmp_path):
    harness = Harness(tmp_path / "data", Settings(away_notify=True, away_how="DM me"))
    runtime = await away(harness)
    worker, _task = await spawn(runtime, harness, TITLE)
    progress = f"**Issue:**\nThere are two reasons:\n\n{NEEDS}"
    await call_tool(runtime, "report", worker, status="question", summary=progress)
    harness.session(worker).emit("session.idle")
    await settle(runtime)
    await runtime.connector.notify_away()
    await settle(runtime)
    codes, plain = files(harness, runtime.toolset.last_special("messenger"))
    lines = section(codes, TITLE)
    code = re.search(r"\[([A-Z2-7]{5})\]", lines[0]).group(1)
    assert lines == [
        f"*[{code}]* Needs your input",
        f"Task: {TITLE}",
        NEEDS,
        f"Action: Open Needs you \u2192 {TITLE}",
        f"Reply: `{code} <your answer>`",
    ], lines
    assert section(plain, TITLE) == [
        "*Needs your input*",
        f"Task: {TITLE}",
        NEEDS,
        f"Action: Open Chief of Staff \u2192 Needs you \u2192 {TITLE}",
    ]


async def test_every_kind_of_item_names_its_task_or_what_it_is_about(tmp_path):
    harness = Harness(tmp_path / "data", Settings(away_notify=True))
    runtime = await harness.connect()
    connector = runtime.connector
    worker, task = await spawn(runtime, harness, "Answer Bob about the release")
    await call_tool(runtime, "draft_reply", worker, text="Friday works.", to="Bob", where="email")
    reply = harness.store.drafts()[-1]
    loose = harness.store.create_draft(
        harness.router_id, "reply", {"to": "Carol", "task_id": ""}, "Sure."
    )
    harness.store.set(f"stopped:{task}", "1")
    decision = {
        "kind": "decision",
        "target": {
            "question": "Which database?",
            "askers": [{"title": "Add the storage layer"}],
            "options": [{"choice": "SQLite"}],
        },
        "text": "",
    }
    items = [
        {"draft": reply},
        {"draft": loose},
        {"draft": decision},
        {"task": {"id": "T1", "status": "failed", "title": "Build the release",
                  "progress": "## Error:\nThe build broke."}},
        {"task": runtime.tasks.get(task) | {"status": "waiting"}},
        {"ask": {"kind": "update", "text": "Prod is down.", "meta": {"urgent": True}}},
        {"ask": {"kind": "assistant", "text": "Which port should it use?", "meta": {}}},
    ]  # fmt: skip
    for number, item in enumerate(items):
        item |= {"id": f"item{number}", "code": f"COD{chr(65 + number)}2"}
    body, _blocks = connector.render(items, 0, True)
    sections = [part.splitlines() for part in body.split("\n\n")[1:-1]]
    assert [lines[1] for lines in sections] == [
        "Task: Answer Bob about the release",
        "About: your reply to Carol",
        "Task: Add the storage layer",
        "Task: Build the release",
        "Task: Answer Bob about the release",
        "About: a note from the Router",
        "About: a question from the Router",
    ], body
    assert [lines[-2] for lines in sections] == [
        "Action: Open Needs you \u2192 Answer Bob about the release",
        "Action: Open Needs you \u2192 your reply to Carol",
        "Action: Open Needs you \u2192 Add the storage layer",
        "Action: Open Needs you \u2192 Build the release",
        "Action: Open Needs you \u2192 Answer Bob about the release",
        "Action: Open Needs you \u2192 The Router",
        "Action: Open the chat with the Router",
    ], body
    assert [lines[0] for lines in sections][3:5] == ["*[CODD2]* Failed", "*[CODE2]* Stopped"]
    assert sections[3][2] == "The build broke.", "not its heading"
    harness.store.set("owner_language", "Chinese")
    body, _blocks = connector.render(items[3:4], 0, False)
    assert "\u4efb\u52a1\uff1aBuild the release" in body, body
    assert "\u64cd\u4f5c\uff1a\u6253\u5f00 Chief of Staff \u2192 Needs you \u2192 Build" in body


# E36: answer codes are made and kept only where the owner's replies are read.


async def test_codes_are_made_and_kept_only_where_replies_are_read(tmp_path):
    harness = Harness(tmp_path / "data", Settings(away_notify=True, away_how="DM me"))
    runtime = await away(harness)
    connector = runtime.connector
    worker, _task = await spawn(runtime, harness, "Answer Bob about the release")

    def codes() -> tuple[dict, dict]:
        return dict(harness.store.items("awaycode:")), dict(harness.store.items("awayitem:"))

    async def notify(text: str, replies: str) -> tuple[dict, str, tuple]:
        """A notification of a new draft, as the messenger reports it went; and the codes
        there were while it was being sent."""
        await call_tool(runtime, "draft_reply", worker, text=text, to="Bob", where="email")
        await connector.notify_away()
        await settle(runtime)
        sending = codes()
        messenger = runtime.toolset.last_special("messenger")
        job = harness.session(messenger["id"]).sent[-1]
        await call_tool(runtime, "report_notified", messenger["id"], ok=True, where="Slack DM",
                        method="bot send", replies=replies)  # fmt: skip
        harness.session(messenger["id"]).emit("session.idle")
        await settle(runtime)
        return messenger, job, sending

    # Its replies are read there: codes as before, kept once it went out.
    await notify("Friday works.", "bot read_dm_replies")
    kept = connector.open_codes()
    assert len(kept) == 1 and harness.store.get(f"awaycode:{kept[0]}")
    # The version without codes went (no way to read replies was found): its new codes go.
    _messenger, job, _sending = await notify("Monday then.", "")
    assert "A: " in job, "whether replies can be read there was not known yet"
    assert sorted(harness.store.items("awaycode:")) == kept, "only the code the owner got"
    assert connector.capabilities()["replies"]["state"] == "no"
    # Replies there are not read: none is made; it says where to answer instead.
    before = codes()
    messenger, job, sending = await notify("Tuesday, final answer.", "")
    assert sending == codes() == before, "no code made for it, nor kept"
    assert "A: " not in job and "-codes." not in job, job
    written = re.search(r"B: (/\S+)-plain\.txt", job).group(1)
    assert not list(Path(written).parent.glob(f"{Path(written).name}-codes.*")), "none written"
    _codes, plain = files(harness, messenger)
    lines = section(plain, "Tuesday, final answer.")
    assert lines[-1] == (
        "Action: Open Chief of Staff \u2192 Needs you \u2192 Answer Bob about the release"
    ), lines
    assert not re.search(r"\b[A-Z2-7]{5}\b", plain), plain


async def test_codes_sent_to_the_own_slack_dm_are_kept_only_when_the_owner_got_them(tmp_path):
    from test_round10_ops import own_dm_harness, send_to_own_dm

    harness, runtime = await own_dm_harness(tmp_path)
    worker, _task = await spawn(runtime, harness, "Answer Bob about the release")
    await call_tool(runtime, "draft_reply", worker, text="Friday works.", to="Bob", where="email")
    await runtime.connector.notify_away()
    await send_to_own_dm(runtime, harness, version="B")
    assert not harness.store.items("awaycode:"), "it went without them"
    await call_tool(runtime, "draft_reply", worker, text="Monday then.", to="Bob", where="email")
    await runtime.connector.notify_away()
    await send_to_own_dm(runtime, harness, version="A")
    assert len(harness.store.items("awaycode:")) == len(runtime.connector.open_codes()) == 1


async def test_codes_the_owner_got_outlive_a_send_that_never_reported(tmp_path):
    """Review: a notification sent to the owner's own Slack DM with its codes, whose messenger
    then ended without report_notified, timed out: its codes were deleted, and the next try
    gave new ones; the owner's answers by the first were lost."""
    from test_round10_ops import DM, OWNER, entry, own_dm_harness, stamp

    harness, runtime = await own_dm_harness(tmp_path)
    worker, _task = await spawn(runtime, harness, "Answer Bob about the release")
    await call_tool(runtime, "draft_reply", worker, text="Friday works.", to="Bob", where="email")
    await runtime.connector.notify_away()
    await settle(runtime)
    messenger = runtime.toolset.last_special("messenger")["id"]
    sent = stamp(1)
    harness.mcp.sent_link = f"https://acme.slack.com/archives/{DM}/p{sent.replace('.', '')}"
    harness.mcp.reads[DM] = entry(DM, OWNER, "Me", sent, "Chief of Staff")
    said = await call_tool(runtime, "send_owner_slack_dm", messenger, version="A")
    assert 'replies="read_owner_slack_dm"' in said.text_result_for_llm, said.text_result_for_llm
    [code] = harness.store.items("awaycode:")
    harness.session(messenger).emit("session.idle")  # No report_notified.
    await settle(runtime)
    pending = json.loads(harness.store.get("away_pending") or "{}")
    harness.store.set("away_pending", json.dumps(pending | {"at": time.time() - 3600}))
    assert runtime.connector.pending_send() is None, "timed out"
    assert harness.store.get(f"awaycode:{code}"), "the code it may have given is kept"
    harness.store.delete("away_retry_at")
    await runtime.connector.notify_away()
    await settle(runtime)
    body, _plain = files(harness, runtime.toolset.last_special("messenger"))
    assert f"*[{code}]*" in body, body


# E35, C9: a watch's window that scans covered only in part is searched again from where it
# was last covered, however long ago; what no scan can cover is a gap the owner is told of.


async def watched(tmp_path, done_hours_ago: float):
    settings = Settings(watches=[Watch(what="DMs to me")], watch_after_hours_minutes=0)
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    done = time.time() - done_hours_ago * 3600
    harness.store.set("watchdone:" + watch_key("DMs to me"), str(done))
    return harness, runtime, done


def watch_key(what: str) -> str:
    from chiefofstaff.connector import watch_key as key

    return key(what)


async def scan(harness, runtime, *calls: dict, **report) -> str:
    """One scan: its job, then its report (none when `report` is empty), or its several
    report_found `calls`."""
    connector = runtime.connector
    harness.store.set("watch_scanned_at", "0")
    await connector.scan()
    await settle(runtime)
    row = connector.session()["id"]
    job = harness.session(row).sent[-1]
    for call in calls or ([report] if report else []):
        await call_tool(runtime, "report_found", row, **({"items": []} | call))
    harness.session(row).emit("session.idle")
    await settle(runtime)
    return job


def since(job: str) -> str:
    return re.search(r"new since (\S+)$", job, re.MULTILINE).group(1)


def minutes(moment: float) -> str:
    return datetime.fromtimestamp(moment).astimezone().isoformat(timespec="minutes")


def notices(harness) -> list[str]:
    router = harness.store.messages(harness.router_id)
    return [m["text"] for m in router if m["kind"] == "notice" and "Could not check" in m["text"]]


CAPPED = {"incomplete": [{"watch": 1, "reason": "the DM search returns only the newest 3 DMs"}]}


async def test_a_window_covered_in_part_twice_is_searched_again_in_full(tmp_path):
    harness, runtime, done = await watched(tmp_path, 30)
    start = minutes(done - OVERLAP_SECONDS)
    first = await scan(harness, runtime, **CAPPED)
    assert since(first) == start
    day = (datetime.fromtimestamp(done - OVERLAP_SECONDS).astimezone() - timedelta(days=1)).date()
    assert f"after:{day.isoformat()}" in first and "page through" in first, first
    second = await scan(harness, runtime, **CAPPED)
    assert since(second) == start, "still from where it was covered, not the last 24 hours"
    assert "Not covered in full since then (1 scan in a row" in second, second
    third = await scan(harness, runtime, items=[])
    assert since(third) == start and "(2 scans in a row" in third, third
    assert runtime.connector.watch_since("DMs to me") > time.time() - 3600, "covered: reads on"
    assert not notices(harness) and runtime.connector.view()["gaps"] == []


async def test_what_no_scan_can_cover_is_a_gap_the_owner_is_told_of(tmp_path):
    harness, runtime, done = await watched(tmp_path, 5)
    for _scan in range(GAP_TRIES):
        await scan(harness, runtime, **CAPPED)
    [told] = notices(harness)
    assert told.startswith("Could not check \u201cDMs to me\u201d between "), told
    assert "only the newest 3 DMs" in told and "look there yourself" in told
    [gap] = runtime.connector.view()["gaps"]
    assert gap["text"] in told and abs(datetime.fromisoformat(gap["from"]).timestamp() - done) < 1
    assert runtime.connector.watch_since("DMs to me") > done, "the next scans read on from it"
    key = "watchdone:" + watch_key("DMs to me")
    # As if an hour went by since it was given up on.
    harness.store.set(key, str(float(harness.store.get(key) or 0) - 3600))
    for _scan in range(GAP_TRIES):  # Still capped: the same gap, longer, told once.
        await scan(harness, runtime, **CAPPED)
    assert len(notices(harness)) == 1 and len(runtime.connector.view()["gaps"]) == 1
    assert runtime.connector.view()["gaps"][0]["to"] > gap["to"]


async def test_what_is_older_than_the_source_or_scans_reach_is_a_gap(tmp_path):
    harness, runtime, done = await watched(tmp_path, 30)
    reach = time.time() - 6 * 3600
    when = datetime.fromtimestamp(reach).astimezone().isoformat()
    capped = {"watch": 1, "reason": "Slack search keeps 6 hours", "reaches_back_to": when}
    await scan(harness, runtime, incomplete=[capped])
    [told] = notices(harness)
    assert "Slack search keeps 6 hours" in told, told
    [gap] = runtime.connector.view()["gaps"]
    stretch = [datetime.fromisoformat(gap[end]).timestamp() for end in ("from", "to")]
    assert abs(stretch[0] - done) < 1 and abs(stretch[1] - reach) < 1, gap
    assert abs(runtime.connector.watch_since("DMs to me") - (reach - OVERLAP_SECONDS)) < 1
    # Not covered for longer than scans reach back: what is older is a gap, from there on.
    harness.store.set("watchdone:" + watch_key("DMs to me"), str(time.time() - 9 * 86400))
    job = await scan(harness, runtime)
    reached = datetime.fromisoformat(since(job)).timestamp()
    assert abs(reached - (time.time() - RECOVER_DAYS * 86400)) < 120, job
    assert len(notices(harness)) == 2 and f"only {RECOVER_DAYS} days" in notices(harness)[-1]


def test_a_gap_says_when_in_the_owners_clock():
    today = datetime.now().astimezone().replace(hour=15, minute=0, second=0, microsecond=0)
    gap = {"watch": "#deploys", "from": today.timestamp(), "to": today.timestamp() + 6 * 3600}
    assert gap_text(gap) == "Could not check \u201c#deploys\u201d between 3:00 PM and 9:00 PM"


async def test_a_scan_that_reports_in_several_calls_counts_once_toward_a_gap(tmp_path):
    """Review: a call that left the watch out took it for covered and forgot how many scans in
    a row had covered it in part; the next call, listing it, counted 1 again: never given up
    on, its watermark stuck 30 hours back."""
    harness, runtime, done = await watched(tmp_path, 30)
    jobs = [await scan(harness, runtime, {}, CAPPED) for _scan in range(GAP_TRIES)]
    assert "(1 scan in a row" in jobs[1] and "(2 scans in a row" in jobs[2], jobs[2]
    assert len(notices(harness)) == 1 and len(runtime.connector.view()["gaps"]) == 1
    assert runtime.connector.watch_since("DMs to me") > done + 3600, "it reads on"


async def test_a_new_watch_starts_a_day_back_whatever_other_watches_hold_back(tmp_path):
    """Review: a watch added (or reworded) while another, always partial one held the last
    full scan of all back 5 days was searched 5 days back; 9 days back, it was told a gap from
    before it existed."""
    settings = Settings(watches=[Watch(what="PR mentions")], watch_after_hours_minutes=0)
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    harness.store.set("watch_done_at", str(time.time() - 5 * 86400))
    job = await scan(harness, runtime)
    started = datetime.fromisoformat(since(job)).timestamp()
    assert started >= time.time() - 86400 - OVERLAP_SECONDS - 60, job
    for key in ("watchfrom:", "watchdone:"):
        harness.store.delete(key + watch_key("PR mentions"))
    harness.store.set("watch_done_at", str(time.time() - 9 * 86400))
    await scan(harness, runtime)
    assert not notices(harness) and runtime.connector.view()["gaps"] == []


async def test_a_gap_older_than_scans_reach_is_listed_in_settings(tmp_path):
    """Review: the gap reach_back records ends a week ago: Settings left it out at once."""
    harness, runtime, _done = await watched(tmp_path, 9 * 24)
    await scan(harness, runtime)
    [told] = notices(harness)
    [gap] = runtime.connector.view()["gaps"]
    assert gap["watch"] == "DMs to me" and gap["text"] in told, gap


def test_a_line_wrapped_mid_sentence_is_no_sentence_end():
    """Review: a gist stopped at "…pins a wheel that is", a line wrapped in its sentence."""
    text = (
        "The deploy fails on the new runtime image. The base image moved last week and the lock "
        "file still pins a wheel that is\nno longer published on the index, so every service "
        "build breaks at the install step. Pinning is quick."
    )
    said = gist(text)
    assert said.endswith("\u2026") and "no longer published" in said, said
    assert gist("The deploy fails.\nThe base image moved.") == (
        "The deploy fails. The base image moved."
    )


# What watches could not check stays in sight in Settings (Connections, Watches).

DOM = """
const nodes = {};
const node = () => {
  const made = { textContent: "", hidden: false, disabled: false, value: "", dataset: {},
    children: [], classes: [], append() {}, replaceChildren() {}, setAttribute() {},
    addEventListener() {}, querySelector: () => node(), querySelectorAll: () => [] };
  made.classList = { add() {}, remove() {}, toggle: (name, on) => {
    made.classes = made.classes.filter((other) => other !== name).concat(on ? [name] : []);
  } };
  return made;
};
document.querySelector = (selector) => (nodes[selector] ??= node());
document.querySelectorAll = () => [];
document.createElement = () => node();
nodes["#settings-form"] = { watch_minutes: node(), watch_after_hours_minutes: node() };
"""


def test_settings_shows_what_watches_could_not_check():
    from test_round5_settings import run_ui

    gap = "Could not check \u201cDMs to me\u201d between 3:00 PM and 9:00 PM (capped)"
    found = run_ui(
        DOM
        + f"""
        s.state.snapshot = {{
          settings: {{ github_repos: [], watches: [{{ what: "DMs to me" }}] }},
          abilities: {{ skills: [], plugins: {{}} }}, sessions: [],
          connector: {{ scanned_at: new Date().toISOString(), last: {{ found: 0 }},
                       gaps: [{{ text: {json.dumps(gap)} }}] }},
        }};
        settings.renderConnections();
        return nodes["#watch-state"];
        """
    )
    assert found["textContent"].endswith(f" \u00b7 {gap}"), found
    assert found["classes"] == ["error"], "said as a problem"
