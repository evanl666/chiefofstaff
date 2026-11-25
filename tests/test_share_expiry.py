"""Share links end on time: the payload's own expiry refuses them (sharing.expired, in
test_share_crypto), and Chief deletes the gist at expiry, at its next start after an expiry
while it was off, and again an hour later when a delete failed. With no live link there is no
timer and nothing reads the table or GitHub."""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import Harness, settle
from share_world import FakeGitHub, finished_task

from chiefofstaff.shares import Shares
from chiefofstaff.sources.github import GitHubSource


@pytest.fixture
async def world(tmp_path):
    harness = Harness(tmp_path / "data")
    harness.fake = FakeGitHub()
    harness.runtime.github = GitHubSource(harness.store, harness.runtime, gh_command=harness.fake)
    await harness.connect()
    harness.shares = harness.runtime.shares
    return harness


async def share(world, days: int = 1) -> dict:
    task = await finished_task(world)
    preview = await world.shares.preview(task["id"])
    decisions = {hit["id"]: "remove" for hit in preview["hits"]}
    return (await world.shares.create(task["id"], decisions, preview["digest"], days, "jordan"))[
        "share"
    ]


def later(world, **delta):
    moment = datetime.now(UTC) + timedelta(**delta)
    world.shares.clock = lambda: moment


async def test_the_timer_deletes_the_gist_at_expiry(world):
    made = await share(world, days=1)
    assert world.shares.timer is not None and len(world.fake.gists) == 1
    later(world, days=1, seconds=1)
    world.shares.arm()
    await settle(world.runtime)
    row = world.shares.row(made["id"])
    assert (
        row["state"] == "expired" and row["error"] == "" and row["ended_at"] == made["expires_at"]
    )
    assert world.fake.gists == {}
    assert world.shares.timer is None, "nothing waits once none is live"
    assert [item["state"] for item in world.shares.listed()] == ["expired"]


async def test_one_that_expired_while_chief_was_off_is_deleted_at_start(world):
    made = await share(world, days=1)
    world.shares.timer.cancel()
    later(world, days=2)
    await world.shares.resume()
    assert world.shares.row(made["id"])["state"] == "expired" and world.fake.gists == {}


async def test_a_failed_delete_is_shown_and_tried_again_an_hour_later(world):
    made = await share(world, days=1)
    world.fake.sign_out("jordan")
    later(world, days=1, seconds=5)
    await world.shares.sweep()
    [row] = world.shares.listed()
    assert (
        row["state"] == "expired"
        and row["error"] == "@jordan isn't signed in to gh on this computer"
    )
    assert len(world.fake.gists) == 1
    world.shares.arm()
    wake = world.shares.next_wake()
    assert wake is not None and wake > world.shares.clock() + timedelta(minutes=59)
    world.fake.accounts["jordan"]["in"] = True
    await world.shares.sweep()
    assert len(world.fake.gists) == 1, "not before the hour"
    later(world, days=1, hours=1, seconds=10)
    await world.shares.sweep()
    assert world.fake.gists == {} and world.shares.row(made["id"])["error"] == ""
    world.shares.arm()
    assert world.shares.timer is None


async def test_ended_links_leave_the_list_after_a_week(world):
    await share(world, days=1)
    later(world, days=1, seconds=1)
    await world.shares.sweep()
    assert len(world.shares.listed()) == 1
    later(world, days=9)
    assert world.shares.listed() == []


async def test_no_live_link_no_timer_no_query(world):
    calls = []
    store = world.store
    real_one = store.one

    def one(sql, params=()):
        calls.append(sql)
        return real_one(sql, params)

    store.one = one
    before = len(world.fake.calls)
    await world.runtime.shares.resume()
    assert world.shares.timer is None and world.shares.next_wake() is None
    assert all("shares" not in sql or "sqlite_master" in sql for sql in calls)
    assert world.fake.calls[before:] == []


def test_chief_started_after_an_expiry_deletes_the_gist(tmp_path):
    """Runtime.start: a link that expired while Chief of Staff was off is deleted then."""
    import time

    from conftest import FakeClient, FakeSlackMcp, FakeTokens
    from fastapi.testclient import TestClient

    from chiefofstaff.runtime import Runtime
    from chiefofstaff.sources.slack import SlackSource
    from chiefofstaff.store import Store
    from chiefofstaff.web import create_app

    fake = FakeGitHub()
    gist = fake.made("jordan", 4242001, "x", "Chief of Staff share sh_12345678 · encrypted")
    data = tmp_path / "data"
    store = Store(data)
    store.db.executescript(Shares.TABLE)
    store.execute(
        "INSERT INTO shares(id, task_id, title, login, account_id, gist_id, link, created_at, "
        "expires_at, state) VALUES('sh_12345678', 't1', 'Old', 'jordan', 4242001, ?, 'l', "
        "'2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', 'live')",
        (gist,),
    )
    store.close()

    def factory(store, bus):
        async def notifier(title, text):
            pass

        runtime = Runtime(
            store, bus, backend_factory=lambda settings: FakeClient(), notifier=notifier
        )
        runtime.slack = SlackSource(store, runtime, tokens=FakeTokens(""), mcp=FakeSlackMcp())
        runtime.github = GitHubSource(store, runtime, gh_command=fake)
        return runtime

    app = create_app(data, "key", 8765, runtime_factory=factory)
    with TestClient(app, base_url="http://127.0.0.1:8765"):
        deadline = time.monotonic() + 15  # (A slow machine's start.)
        while fake.gists and time.monotonic() < deadline:
            time.sleep(0.02)
        assert fake.gists == {}
        row = app.state.runtime.shares.row("sh_12345678")
        assert row["state"] == "expired" and row["ended_at"] == "2026-01-02T00:00:00Z"
