import json
from datetime import datetime

from chiefofstaff.models import Settings
from chiefofstaff.sources.github import GitHubSource
from chiefofstaff.store import Store

THREAD = "github:acme/app#7"


class Host:
    def __init__(self, store):
        self.store = store
        self.deliveries = []
        self.unfollowed = []
        self.subscriptions = [{"thread": THREAD, "since": 1793500000.0}]

    async def on_trigger(self, *args, **kwargs):
        raise AssertionError("followed PR activity must not go to the Router")

    async def on_thread(self, source, thread, keys, text, display=None):
        self.deliveries.append((thread, keys, text))
        for key in keys:
            self.store.add_trigger(source, key, text)
        return True

    def followed(self, prefix):
        return [s for s in self.subscriptions if s["thread"].startswith(prefix)]

    def unfollow(self, thread, finished=False, merged=False):
        self.subscriptions = [s for s in self.subscriptions if s["thread"] != thread]
        self.unfollowed.append((thread, finished, merged))


def fake_gh(state):
    async def gh(*arguments, payload=None):
        path = arguments[1]
        if path == "repos/acme/app/pulls/7":
            pull = {"state": state["state"], "merged": state["merged"], "head": {"sha": "abc1234"}}
            if state["state"] != "open":
                pull["closed_at"] = pull["merged_at"] = state.get("ended", "2026-01-01T00:00:00Z")
            return json.dumps(pull)
        if path.startswith("repos/acme/app/issues/7/comments"):
            return json.dumps(state["comments"])
        if path.startswith("repos/acme/app/pulls/7/comments"):
            return json.dumps(
                [
                    {
                        "id": 21,
                        "user": {"login": "carol"},
                        "path": "app.py",
                        "line": 12,
                        "body": "Off by one?",
                        "html_url": "u21",
                    }
                ]
            )
        if path.startswith("repos/acme/app/pulls/7/reviews"):
            return json.dumps(
                [
                    {
                        "id": 31,
                        "user": {"login": "me"},
                        "state": "COMMENTED",
                        "body": "our own review",
                        "html_url": "u31",
                        "submitted_at": "2026-11-01T03:00:00Z",
                    },
                    {
                        "id": 32,
                        "user": {"login": "carol"},
                        "state": "CHANGES_REQUESTED",
                        "body": "Please add a test",
                        "html_url": "u32",
                        "submitted_at": "2026-11-01T03:00:00Z",
                    },
                ]
            )
        if path.startswith("repos/acme/app/commits/abc1234/check-runs"):
            return json.dumps(
                {
                    "check_runs": [
                        {"name": "tests", "status": "completed", "conclusion": "failure"},
                        {"name": "lint", "status": "completed", "conclusion": "success"},
                    ]
                }
            )
        raise AssertionError(path)

    return gh


async def test_followed_pr_activity_reaches_its_followers(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    store.add_trigger("github-sent", "github:review:31", "our own review")
    state = {
        "state": "open",
        "merged": False,
        "comments": [
            {"id": 11, "user": {"login": "bob"}, "body": "LGTM after CI", "html_url": "u11"}
        ],
    }
    host = Host(store)
    source = GitHubSource(store, host, gh_command=fake_gh(state))
    await source.poll_once()

    [(thread, keys, text)] = host.deliveries
    assert thread == THREAD
    assert keys == [
        "github:issuecomment:11",
        "github:reviewcomment:21",
        "github:review:32",
        "github:ci:acme/app#7@abc1234",
    ]
    assert "carol on app.py:12: Off by one?" in text
    assert "carol changes requested: Please add a test" in text
    assert "CI on abc1234: failed: tests" in text
    assert "our own review" not in text

    await source.poll_once()
    assert len(host.deliveries) == 1, "nothing new, nothing delivered"

    state.update(state="closed", merged=True, ended="2026-11-03T00:00:00Z")
    await source.poll_once()
    assert host.deliveries[-1][1] == ["github:state:github:acme/app#7:merged"]
    assert host.subscriptions == [], "a merged PR is no longer followed"
    assert host.unfollowed == [(THREAD, True, True)], "it was merged while followed"


async def test_one_broken_pr_does_not_stop_the_others(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    state = {"state": "open", "merged": False, "comments": []}
    host = Host(store)
    host.subscriptions.insert(0, {"thread": "github:acme/gone#1", "since": 1793500000.0})
    gh = fake_gh(state)

    async def with_missing(*arguments, payload=None):
        if "acme/gone" in arguments[1]:
            raise RuntimeError("HTTP 404")
        return await gh(*arguments, payload=payload)

    calls = []

    async def counted(*arguments, payload=None):
        calls.append(arguments[1])
        return await with_missing(*arguments, payload=payload)

    source = GitHubSource(store, host, gh_command=counted)
    for _ in range(8):
        await source.poll_once()
    assert host.deliveries and host.deliveries[0][0] == THREAD
    assert len(host.subscriptions) == 2, "failures never drop a follow (or close a worker)"
    assert sum("acme/gone" in call for call in calls) == 5, "then it backs off"


async def test_a_re_requested_review_goes_to_the_follower(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings(github_enabled=True, github_repos=["acme/app"]))
    state = {"state": "open", "merged": False, "comments": []}
    host = Host(store)
    gh = fake_gh(state)

    async def with_search(*arguments, payload=None):
        if arguments[0] == "search":
            return json.dumps(
                [
                    {
                        "number": 7,
                        "title": "Fix",
                        "repository": {"nameWithOwner": "acme/app"},
                        "url": "u",
                        "author": {"login": "bob"},
                        "isDraft": False,
                    }
                ]
            )
        return await gh(*arguments, payload=payload)

    source = GitHubSource(store, host, gh_command=with_search)
    await source.poll_once()
    _first_thread, first_keys, first_text = host.deliveries[0]
    assert first_keys == ["github:acme/app#7@abc1234"]
    assert "review is requested again" in first_text


async def test_a_pr_cited_after_it_finished_is_just_unfollowed(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    state = {"state": "closed", "merged": True, "comments": []}
    host = Host(store)
    await GitHubSource(store, host, gh_command=fake_gh(state)).poll_once()
    assert host.deliveries == [], "no news about a PR that was already merged"
    assert host.unfollowed == [(THREAD, False, False)], "and its follower is not closed"


async def test_a_pr_is_done_by_when_it_ended_not_by_what_a_poll_saw(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    host = Host(store)
    followed_at = 1793500000.0
    state = {"state": "closed", "merged": True, "comments": [], "ended": "2026-11-03T00:00:00Z"}
    assert datetime.fromisoformat("2026-11-03T00:00:00+00:00").timestamp() > followed_at
    source = GitHubSource(store, host, gh_command=fake_gh(state))
    await source.poll_once()  # Merged before any poll saw it open.
    assert host.unfollowed == [(THREAD, True, True)]
    assert "github:state:github:acme/app#7:merged" in host.deliveries[-1][1]

    host.subscriptions = [{"thread": THREAD, "since": followed_at + 10**6}]  # Cited later.
    await source.poll_once()
    assert host.unfollowed[-1] == (THREAD, False, False), "already finished: just unfollowed"
