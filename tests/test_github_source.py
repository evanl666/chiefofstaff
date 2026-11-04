import json
from datetime import datetime

import pytest

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


async def test_each_repository_is_reached_with_the_account_that_can_see_it(tmp_path):
    """gh's active account is personal; the company's repositories need the company account
    that is also signed in. Chief of Staff finds it and remembers it per owner."""
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    calls = []

    async def gh(*arguments, payload=None, token=""):
        calls.append((arguments, token))
        if arguments[:2] == ("auth", "status"):
            accounts = [
                {"login": "evanl666", "active": True, "state": "success"},
                {"login": "evan-corp", "active": False, "state": "success"},
                {"login": "expired", "active": False, "state": "error"},
            ]
            return json.dumps({"hosts": {"github.com": accounts}})
        if arguments[:2] == ("auth", "token"):
            return f"token-of-{arguments[-1]}\n"
        if arguments[1] == "repos/corp/svc/pulls/404":
            raise RuntimeError("GitHub request failed: gh: Not Found (HTTP 404)")
        if arguments[1].startswith("repos/corp/") and token != "token-of-evan-corp":
            raise RuntimeError("GitHub request failed: gh: Not Found (HTTP 404)")
        if arguments[1] == "repos/corp/svc/pulls/9/reviews":
            raise RuntimeError("GitHub request timed out.")
        return json.dumps({"number": 1, "by": token or "active"})

    source = GitHubSource(store, Host(store), gh_command=gh)
    assert (await source.pull("corp/svc", 1))["by"] == "token-of-evan-corp"
    assert store.get("ghaccount:corp") == "evan-corp"
    assert (await source.pull("acme/app", 1))["by"] == "active", "the active one where it can"
    assert not any(token for arguments, token in calls if "acme/app" in " ".join(arguments))

    calls.clear()
    assert (await source.pull("corp/svc", 2))["by"] == "token-of-evan-corp"
    assert [arguments[1] for arguments, _ in calls if arguments[0] == "api"] == [
        "repos/corp/svc/pulls/2"
    ], "remembered: straight to the company account"

    try:
        await source.pull("corp/svc", 404)
        raise AssertionError("a missing PR is still missing")
    except RuntimeError as exc:
        assert "404" in str(exc)
    assert store.get("ghaccount:corp") == "evan-corp", "a missing PR does not forget the account"

    calls.clear()
    try:
        await source.gh(
            "api", "repos/corp/svc/pulls/9/reviews", "--method=POST", "--input=-", payload={}
        )
        raise AssertionError("a timeout is not an access problem")
    except RuntimeError as exc:
        assert "timed out" in str(exc)
    posts = [arguments for arguments, _ in calls if "--method=POST" in arguments]
    assert len(posts) == 1, "a write that may have happened is never sent again"
    assert await source.logins() == {"evanl666", "evan-corp"}


async def test_review_requests_are_searched_as_each_owners_account(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(
        Settings(github_enabled=True, github_repos=["acme/app", "corp/svc", "corp/web"])
    )
    searches = []

    async def gh(*arguments, payload=None, token=""):
        if arguments[0] == "search":
            searches.append([a for a in arguments if a.startswith("--repo=")])
        return "[]"

    await GitHubSource(store, Host(store), gh_command=gh).poll_review_requests(
        store.settings(), set()
    )
    assert searches == [["--repo=acme/app"], ["--repo=corp/svc", "--repo=corp/web"]]


def test_workers_learn_which_github_accounts_they_can_use():
    from chiefofstaff.prompts import worker_context

    accounts = [{"login": "evanl666", "active": True}, {"login": "evan-corp", "active": False}]
    text = worker_context("/w", "/w", [], False, gh_accounts=accounts)
    assert "GH_TOKEN=$(gh auth token --user evan-corp) gh" in text
    assert "Never run gh auth switch" in text
    assert "gh auth token" not in worker_context("/w", "/w", [], False, gh_accounts=accounts[:1])


async def test_accounts_are_read_on_a_machine_that_just_booted(tmp_path, monkeypatch):
    """The monotonic clock starts at boot: a CI runner or a laptop just started is at 30s."""
    import chiefofstaff.sources.github as module

    monkeypatch.setattr(module.time, "monotonic", lambda: 30.0)
    store = Store(tmp_path / "data")

    async def gh(*arguments, payload=None, token=""):
        accounts = [{"login": "me", "active": True, "state": "success"}]
        return json.dumps({"hosts": {"github.com": accounts}})

    assert await GitHubSource(store, Host(store), gh_command=gh).logins() == {"me"}


def routing_gh(calls, *, rate_limited=False, signed_out=(), corp_sees=True):
    """gh with an active personal account and a company account that alone sees corp/*."""

    async def gh(*arguments, payload=None, token=""):
        calls.append((arguments, token))
        if arguments[:2] == ("auth", "status"):
            accounts = [
                {"login": "me", "active": True, "state": "success"},
                {"login": "corp-me", "active": False, "state": "success"},
            ]
            return json.dumps({"hosts": {"github.com": accounts}})
        if arguments[:2] == ("auth", "token"):
            if arguments[-1] in signed_out:
                raise RuntimeError("GitHub request failed: no oauth token found")
            return f"token-of-{arguments[-1]}\n"
        if rate_limited and not token:
            raise RuntimeError("GitHub request failed: API rate limit exceeded (HTTP 403)")
        if arguments[1].startswith("repos/corp/"):
            if token == "token-of-corp-me" and corp_sees:
                return json.dumps({"ok": True, "as": token})
            raise RuntimeError("GitHub request failed: gh: Not Found (HTTP 404)")
        return json.dumps({"ok": True, "as": token or "active"})

    return gh


async def test_a_rate_limit_is_not_a_reason_to_post_as_another_account(tmp_path):
    store = Store(tmp_path / "data")
    calls = []
    source = GitHubSource(store, Host(store), gh_command=routing_gh(calls, rate_limited=True))
    with pytest.raises(RuntimeError, match="rate limit"):
        await source.gh("api", "repos/acme/app/pulls/1/reviews", "--method=POST", payload={})
    assert not [c for c in calls if c[1]], "no other account tried"
    assert store.get("ghaccount:acme") is None, "and none remembered"


async def test_an_account_signed_out_since_is_forgotten(tmp_path):
    store = Store(tmp_path / "data")
    calls = []
    source = GitHubSource(store, Host(store), gh_command=routing_gh(calls))
    await source.gh("api", "repos/corp/svc/pulls/1")
    assert store.get("ghaccount:corp") == "corp-me"
    source.raw = routing_gh(calls, signed_out={"corp-me"}, corp_sees=False)
    with pytest.raises(RuntimeError, match="404"):
        await source.gh("api", "repos/corp/svc/pulls/1")  # Its cached token fails: dropped.
    assert "corp-me" not in source.tokens
    with pytest.raises(RuntimeError, match="corp-me is no longer signed in to gh"):
        await source.gh("api", "repos/corp/svc/pulls/1")  # Says what to fix.
    store.set("ghaccount:acme", "corp-me")
    assert json.loads(await source.gh("api", "repos/acme/app"))["as"] == "active"
    assert store.get("ghaccount:acme") is None, "the active account answers: nothing to remember"


async def test_a_token_is_asked_of_gh_once_for_many_calls(tmp_path):
    store = Store(tmp_path / "data")
    calls = []
    source = GitHubSource(store, Host(store), gh_command=routing_gh(calls))
    for number in range(5):
        await source.gh("api", f"repos/corp/svc/pulls/{number}")
    assert sum(1 for arguments, _ in calls if arguments[:2] == ("auth", "token")) == 1


async def test_without_gh_the_error_says_so(tmp_path):
    store = Store(tmp_path / "data")

    async def missing(*arguments, payload=None, token=""):
        raise RuntimeError(
            "GitHub CLI not found. Install gh and sign in before connecting reviews."
        )

    with pytest.raises(RuntimeError, match="GitHub CLI not found"):
        await GitHubSource(store, Host(store), gh_command=missing).gh("api", "repos/a/b/pulls/1")


def test_lines_a_review_can_comment_on():
    from chiefofstaff.sources.github import diff_lines, pages
    from chiefofstaff.tools import InlineComment, misplaced

    files = [
        {  # Two hunks, the second with a removal and "\\ No newline".
            "filename": "app.py",
            "additions": 3,
            "deletions": 1,
            "patch": "@@ -1,2 +1,3 @@\n a\n+b\n c\n@@ -20,2 +21,2 @@\n x\n-y\n+z\n"
            "\\ No newline at end of file",
        },
        {
            "filename": "new_name.py",
            "previous_filename": "old_name.py",
            "additions": 1,
            "patch": "@@ -5 +5 @@\n-old\n+new",
        },
        {
            "filename": "gone.py",
            "status": "removed",
            "deletions": 2,
            "patch": "@@ -1,2 +0,0 @@\n-p\n-q",
        },
    ]
    lines = diff_lines(files)
    assert lines["app.py"]["RIGHT"] == {1, 2, 3, 21, 22}
    assert lines["app.py"]["LEFT"] == {1, 2, 20, 21}
    assert lines["new_name.py"] == {"LEFT": {5}, "RIGHT": {5}}, "a hunk header without counts"
    assert lines["gone.py"] == {"LEFT": {1, 2}, "RIGHT": set()}

    def check(**comment):
        return misplaced([InlineComment(body="b", **comment)], lines)

    assert check(path="app.py", line=22) == []
    assert check(path="app.py", start_line=21, line=22) == []
    assert check(path="app.py", start_line=2, line=21), "a range across two hunks"
    assert check(path="app.py", line=21, side="LEFT") == [], "a removed line"
    assert check(path="gone.py", line=2, side="LEFT") == [], "a deleted file, its old side"
    assert check(path="gone.py", line=1), "a deleted file has no new side"
    assert "old_name.py is not changed" in check(path="old_name.py", line=5)[0], "its new name"
    assert check(path="app.py", start_line=3, line=3), "a range must start before it ends"

    assert pages('[{"a": 1}][{"a": 2}]\n[]') == [{"a": 1}, {"a": 2}], "older gh: page by page"
    assert pages('[{"a": 1}, {"a": 2}]') == [{"a": 1}, {"a": 2}]
    assert pages("") == []
