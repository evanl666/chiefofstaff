import json
from datetime import datetime

import pytest

from chiefofstaff.models import DraftDecision, Settings
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

    async def on_thread(self, source, thread, keys, text, display=None, replies=None):
        self.deliveries.append((thread, keys, text))
        self.replies = replies
        for key in keys:
            self.store.add_trigger(source, key, text)
        return True

    def followed(self, prefix):
        return [s for s in self.subscriptions if s["thread"].startswith(prefix)]

    def unfollow(self, thread, finished=False, merged=False):
        self.subscriptions = [s for s in self.subscriptions if s["thread"] != thread]
        self.unfollowed.append((thread, finished, merged))

    def missing(self, thread, why):
        self.subscriptions = [s for s in self.subscriptions if s["thread"] != thread]
        self.unfollowed.append((thread, "missing", why))

    def read_health_changed(self, thread):
        self.health_changes = [*getattr(self, "health_changes", []), thread]


def fake_gh(state):
    async def gh(*arguments, payload=None):
        path = arguments[1]
        if path == "repos/acme/app/pulls/7":
            pull = {
                "state": state["state"],
                "merged": state["merged"],
                "head": {"sha": state.get("sha", "abc1234")},
            }
            if state["state"] != "open":
                pull["closed_at"] = pull["merged_at"] = state.get("ended", "2026-01-01T00:00:00Z")
            return json.dumps(pull)
        if path.startswith("repos/acme/app/issues/7/comments"):
            return json.dumps(state["comments"])
        if path.startswith("repos/acme/app/pulls/7/comments"):
            return json.dumps(
                state.get(
                    "line_comments",
                    [
                        {
                            "id": 21,
                            "user": {"login": "carol"},
                            "path": "app.py",
                            "line": 12,
                            "body": "Off by one?",
                            "html_url": "u21",
                        }
                    ],
                )
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
                    {  # What GitHub adds for carol's line comment: no news of its own.
                        "id": 33,
                        "user": {"login": "carol"},
                        "state": "COMMENTED",
                        "body": "",
                        "html_url": "u33",
                        "submitted_at": "2026-11-01T03:00:00Z",
                    },
                ]
            )
        if path.startswith("repos/acme/app/compare/"):
            return json.dumps(state["compare"])
        if path.startswith("repos/acme/app/commits/") and "/check-runs" in path:
            return json.dumps(
                {
                    "check_runs": state.get("runs")
                    or [
                        {"name": "tests", "status": "completed", "conclusion": "failure"},
                        {"name": "lint", "status": "completed", "conclusion": "success"},
                    ]
                }
            )
        if path.startswith("repos/acme/app/commits/") and path.endswith("/status"):
            return json.dumps({"statuses": state.get("statuses", [])})
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
        "github:ci:acme/app#7@abc1234:failed-tests",
    ]
    assert "carol on app.py:12: Off by one?" in text
    assert "carol changes requested: Please add a test" in text
    assert "CI on abc1234: failed: tests" in text
    assert "our own review" not in text

    await source.poll_once()
    assert len(host.deliveries) == 1, "nothing new, nothing delivered"

    # Re-run on the same commit: tests now pass (its latest run counts), a status check fails.
    state["runs"] = [
        {"name": "tests", "status": "completed", "conclusion": "failure", "completed_at": "1"},
        {"name": "tests", "status": "completed", "conclusion": "success", "completed_at": "2"},
        {"name": "lint", "status": "completed", "conclusion": "success", "completed_at": "1"},
    ]
    state["statuses"] = [{"context": "validate", "state": "error"}]
    await source.poll_once()
    assert host.deliveries[-1][1] == ["github:ci:acme/app#7@abc1234:failed-validate"]
    assert "CI on abc1234: failed: validate" in host.deliveries[-1][2]
    state["statuses"] = [{"context": "validate", "state": "pending"}]
    await source.poll_once()
    assert len(host.deliveries) == 2, "nothing while a check is still running"
    state["statuses"] = [{"context": "validate", "state": "success"}]
    await source.poll_once()
    assert "CI on abc1234: all 3 checks passed" in host.deliveries[-1][2]

    state.update(state="closed", merged=True, ended="2026-11-03T00:00:00Z")
    await source.poll_once()
    assert host.deliveries[-1][1] == ["github:state:github:acme/app#7:merged"]
    assert host.subscriptions == [], "a merged PR is no longer followed"
    assert host.unfollowed == [(THREAD, True, True)], "it was merged while followed"


async def test_ci_that_ended_before_a_pr_was_followed_is_not_news(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    state = {
        "state": "open",
        "merged": False,
        "comments": [],
        "runs": [
            {"name": "tests", "status": "completed", "conclusion": "failure",
             "completed_at": "2020-01-01T00:00:00Z"},
        ],
    }  # fmt: skip
    host = Host(store)
    source = GitHubSource(store, host, gh_command=fake_gh(state))
    await source.poll_once()
    assert not any("CI on" in text for _, _, text in host.deliveries), "old CI is where it stands"
    state["sha"] = "def5678"
    state["compare"] = {"status": "ahead", "commits": []}
    state["runs"] = [
        {"name": "tests", "status": "completed", "conclusion": "failure", "completed_at": "2999"}
    ]
    await source.poll_once()
    assert "CI on def5678: failed: tests" in host.deliveries[-1][2], "new CI is news"


async def test_new_commits_on_a_followed_pr_are_news_unless_its_own(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    state = {"state": "open", "merged": False, "comments": []}
    host = Host(store)
    source = GitHubSource(store, host, gh_command=fake_gh(state))

    async def acting_login(repo):
        return "me"  # The account Chief of Staff works as there.

    source.acting_login = acting_login
    await source.poll_once()
    seen = len(host.deliveries)
    state["runs"] = [{"name": "tests", "status": "in_progress"}]

    def commit(login, message):
        return {"author": {"login": login}, "commit": {"message": message, "author": {}}}

    state |= {"sha": "def5678", "compare": {"status": "ahead", "commits": [
        commit("bob", "Handle empty lists\n\nAs asked in review."),
    ]}}  # fmt: skip
    await source.poll_once()
    assert len(host.deliveries) == seen + 1
    assert host.deliveries[-1][1] == ["github:head:acme/app#7@def5678"]
    assert (
        "1 new commit(s), the head is now def5678: bob: Handle empty lists"
        in (host.deliveries[-1][2])
    )
    await source.poll_once()
    assert len(host.deliveries) == seen + 1, "told once"

    state |= {"sha": "0a0b0c0", "compare": {"status": "ahead", "commits": [commit("Me", "Fix")]}}
    await source.poll_once()
    assert len(host.deliveries) == seen + 1, "a push of its own is not news"
    # Bug bash: a worker's own commit with a git email GitHub does not know (no login).
    store.execute(
        """INSERT INTO commands(session_id, call_id, command, kind, exit_code, output, started_at)
        VALUES ('w', 'c', 'git commit -m x', 'commit', 0, ?, 'now')""",
        ("[fix 1a2b3c4] End with a period",),
    )
    anonymous = {
        "sha": "1a2b3c4d5e",
        "author": None,
        "commit": {"message": "End with a period", "author": {"name": "Evan"}},
    }
    state |= {"sha": "1a2b3c4", "compare": {"status": "ahead", "commits": [anonymous]}}
    await source.poll_once()
    assert len(host.deliveries) == seen + 1, "its worker's push is not news to it"
    state |= {"sha": "9f9f9f9", "compare": {"status": "diverged", "commits": []}}
    await source.poll_once()
    assert "force-pushed" in host.deliveries[-1][2]


async def test_a_worker_bringing_its_pr_up_to_date_with_a_moved_base_is_not_news(tmp_path):
    """Bug bash 4 (R3): a worker rebased its PR onto a base someone else had moved and
    force-pushed; its followers were told "The branch was rewritten". The base's commits are not
    the PR's, and the checkout's reflog says the rebased ones were made there, whatever the
    rebase was called ("pull -q --rebase origin main (pick)", "rebase (continue)")."""
    import subprocess

    from chiefofstaff.sources.github import made_locally

    def git(folder, *arguments):
        return subprocess.run(
            ["git", "-C", str(folder), "-c", "user.email=w@w", "-c", "user.name=W",
             "-c", "commit.gpgsign=false", *arguments],
            check=True, capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip

    def add(folder, name):
        (folder / name).write_text(name)
        git(folder, "add", "-A")
        git(folder, "commit", "-qm", f"Add {name}")
        return git(folder, "rev-parse", "HEAD")

    upstream, checkout = tmp_path / "upstream", tmp_path / "checkout"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    start = add(upstream, "base")
    git(tmp_path, "clone", "-q", str(upstream), str(checkout))
    git(checkout, "checkout", "-qb", "feature")
    add(checkout, "one")
    old = add(checkout, "two")
    theirs = add(upstream, "theirs")  # Someone else moves the base.
    git(checkout, "pull", "-q", "--rebase", "origin", "main")
    picked, new = git(checkout, "rev-parse", "HEAD~1"), git(checkout, "rev-parse", "HEAD")
    later = add(upstream, "later")
    git(checkout, "fetch", "-q", "origin")
    git(checkout, "merge", "-q", "--no-edit", "origin/main")
    merged = git(checkout, "rev-parse", "HEAD")

    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    store.create_session("w1", "worker", "Feature", "idle", cwd=str(checkout))

    def commit(sha, login, message):
        return {"sha": sha, "author": {"login": login} if login else None,
                "commit": {"message": message, "author": {"name": login or "W"}}}  # fmt: skip

    compares = {
        f"{old}...{new}": {"status": "diverged", "commits": [commit(theirs, "lead", "Add theirs"),
            commit(picked, "", "Add one"), commit(new, "", "Add two")]},
        f"{new}...{merged}": {"status": "ahead", "commits": [
            commit(later, "lead", "Add later"), commit(merged, "", "Merge")]},
        f"main...{new}": {"total_commits": 2, "commits": [
            commit(picked, "", "Add one"), commit(new, "", "Add two")]},
        f"main...{merged}": {"total_commits": 3, "commits": [
            commit(picked, "", "Add one"), commit(new, "", "Add two"), commit(merged, "", "M")]},
    }  # fmt: skip

    async def gh(*arguments, payload=None):
        return json.dumps(compares[arguments[1].split("/compare/")[1]])

    async def acting_login(repo):
        return "me"

    source = GitHubSource(store, Host(store), gh_command=gh)
    source.acting_login = acting_login
    assert await source.pushed("acme/app", old, new, "main") == "", "its own rebase"
    assert await source.pushed("acme/app", new, merged, "main") == "", "its merge of the base"
    assert "lead: Add later" in await source.pushed("acme/app", new, merged), "no base: theirs"

    # A rebase that only fast-forwarded made nothing there.
    git(checkout, "checkout", "-qb", "follow", start)
    git(checkout, "rebase", "-q", "origin/main")
    assert await made_locally([str(checkout)], [later, theirs]) == set()


async def test_one_broken_pr_does_not_stop_the_others(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    state = {"state": "open", "merged": False, "comments": []}
    host = Host(store)
    host.subscriptions.insert(0, {"thread": "github:acme/gone#1", "since": 1793500000.0})
    gh = fake_gh(state)

    async def with_missing(*arguments, payload=None):
        if "acme/gone" in arguments[1]:
            raise RuntimeError("HTTP 502: Bad Gateway")
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
    assert store.get("ghfail:github:acme/gone#1") == "5", "reads that failed, not polls"


async def test_a_follow_says_what_could_not_be_read_and_recovers_at_once(tmp_path):
    """E11: "Can't read #72" while the PR could be read (its CI checks could not), with no
    reason, and still shown after it could be read again."""
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    host = Host(store)
    gh = fake_gh({"state": "open", "merged": False, "comments": []})
    broken = {"checks": True}

    async def no_checks(*arguments, payload=None):
        if "/check-runs" in arguments[1] and broken["checks"]:
            raise RuntimeError("GitHub request failed: gh: Resource not accessible (HTTP 403)")
        return await gh(*arguments, payload=payload)

    source = GitHubSource(store, host, gh_command=no_checks)
    for _ in range(3):
        await source.poll_once()
    why = json.loads(store.get(f"ghwhy:{THREAD}"))
    assert (why["kind"], why["step"]) == ("access", "CI checks"), why
    assert "Resource not accessible" in why["error"]
    assert host.health_changes == [THREAD], "its card says so once it shows"
    # The PR itself read elsewhere (its card's chip) does not clear what failed on its checks.
    source.read_ok(THREAD, whole=False)
    assert store.get(f"ghfail:{THREAD}") == "3"
    broken["checks"] = False
    await source.poll_once()
    assert store.get(f"ghfail:{THREAD}") == "0" and not store.get(f"ghwhy:{THREAD}")
    assert host.health_changes == [THREAD, THREAD], "and once it can be read again"


async def test_a_followed_pr_that_does_not_exist_is_let_go(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    host = Host(store)
    host.subscriptions = [{"thread": "github:acme/app#42", "since": 1793500000.0}]

    async def gh(*arguments, payload=None):
        raise RuntimeError("gh: Not Found (HTTP 404)")

    source = GitHubSource(store, host, gh_command=gh)
    for _ in range(2):
        await source.poll_once()
    assert host.subscriptions, "a PR not found twice may be a hiccup"
    await source.poll_once()
    assert host.subscriptions == []
    assert host.unfollowed[-1][:2] == ("github:acme/app#42", "missing")


async def test_gh_missing_from_this_computer_lets_no_pr_go(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    host = Host(store)
    host.subscriptions = [{"thread": "github:acme/app#42", "since": 1793500000.0}]

    async def gh(*arguments, payload=None):
        raise RuntimeError(
            "GitHub CLI not found. Install gh and sign in before connecting reviews."
        )

    source = GitHubSource(store, host, gh_command=gh)
    for _ in range(5):
        await source.poll_once()
    assert host.subscriptions, "gh missing here is not a PR that is gone"


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


async def test_a_closed_pr_followed_on_request_brings_its_reopening(tmp_path):
    """Bug bash 3 (H8): asked to follow a PR closed already ("tell me if it is reopened"), the
    worker said it would; the next poll unfollowed it silently and the reopening reached
    nobody. It stays followed for two weeks; then its follower is told it no longer is."""
    import time

    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    store.set(f"ghkeep:{THREAD}", str(time.time()))  # follow_thread, or the Router's `follow`.
    state = {"state": "closed", "merged": False, "comments": [], "line_comments": []}
    state["runs"] = [{"name": "tests", "status": "in_progress"}]
    host = Host(store)
    source = GitHubSource(store, host, gh_command=fake_gh(state))
    await source.poll_once()
    assert host.unfollowed == [] and [s["thread"] for s in host.subscriptions] == [THREAD]
    assert not any("The PR was" in text for _, _, text in host.deliveries), "closed: not news"
    state["state"] = "open"
    await source.poll_once()
    assert host.deliveries[-1][1] == [f"github:state:{THREAD}:reopened:2026-01-01T00:00:00Z"]
    assert "The PR was reopened." in host.deliveries[-1][2]

    state["state"] = "closed"
    host.subscriptions = [{"thread": THREAD, "since": 1793500000.0}]
    store.set(f"ghkeep:{THREAD}", str(time.time() - 15 * 86400))
    await source.poll_once()
    assert host.unfollowed[-1][:2] == (THREAD, "missing") and "14 days" in host.unfollowed[-1][2]


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

    with pytest.raises(RuntimeError, match="404"):  # A missing PR is still missing.
        await source.pull("corp/svc", 404)
    assert store.get("ghaccount:corp") == "evan-corp", "a missing PR does not forget the account"

    calls.clear()
    with pytest.raises(RuntimeError, match="timed out"):  # Not an access problem.
        await source.gh(
            "api", "repos/corp/svc/pulls/9/reviews", "--method=POST", "--input=-", payload={}
        )
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
    # Bug bash 3 (H13): workers opened PRs as the other account, which the line named, though
    # the active one reached the repository (and Chief of Staff acted as it there).
    assert "every repository: as evanl666, gh as it is (no GH_TOKEN)" in text
    assert "only when gh says such a repository is not found" in text.lower()
    corp = worker_context("/w", "/w", [], False, gh_accounts=accounts,
                          gh_acting={"corp": "evan-corp"})  # fmt: skip
    assert "- corp/… repositories: as evan-corp, GH_TOKEN=$(gh auth token --user evan-corp)" in (
        corp
    )
    assert "every other repository: as evanl666" in corp


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


async def test_a_new_review_on_a_busy_pr_is_seen(tmp_path):
    """GitHub lists a PR's reviews oldest first, 100 a page: a new one can be on page two."""
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    host = Host(store)
    state = {"state": "open", "merged": False, "comments": []}
    gh = fake_gh(state)
    old = [
        {"id": n, "user": {"login": "bob"}, "state": "COMMENTED", "body": f"old {n}",
         "html_url": f"u{n}", "submitted_at": "2026-01-01T00:00:00Z"}
        for n in range(100)
    ]  # fmt: skip
    new = [{"id": 500, "user": {"login": "carol"}, "state": "APPROVED", "body": "Ship it",
            "html_url": "u500", "submitted_at": "2026-11-02T00:00:00Z"}]  # fmt: skip

    async def busy(*arguments, payload=None):
        if arguments[1].startswith("repos/acme/app/pulls/7/reviews"):
            # How gh prints every page with --paginate (older versions: one array per page).
            return (
                json.dumps(old) + json.dumps(new) if "--paginate" in arguments else json.dumps(old)
            )
        return await gh(*arguments, payload=payload)

    await GitHubSource(store, host, gh_command=busy).poll_once()
    [(_, keys, text)] = host.deliveries
    assert "github:review:500" in keys and "carol approved: Ship it" in text


async def test_a_failing_review_request_search_does_not_stop_followed_prs(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings(github_enabled=True, github_repos=["acme/app", "other/lib"]))
    state = {
        "state": "open",
        "merged": False,
        "comments": [{"id": 12, "user": {"login": "bob"}, "body": "Nit", "html_url": "u12"}],
    }
    pulls = fake_gh(state)

    async def gh(*arguments, payload=None):
        if arguments[0] == "search":
            if "--repo=other/lib" in arguments:
                raise RuntimeError("HTTP 403: SAML enforcement")
            return "[]"
        return await pulls(*arguments, payload=payload)

    host = Host(store)
    source = GitHubSource(store, host, gh_command=gh)
    await source.poll_once()
    assert [thread for thread, _, _ in host.deliveries] == [THREAD]
    assert source.status["connected"] and "other" in source.status["error"]


async def test_the_owners_own_comments_on_a_followed_pr_are_not_news(tmp_path):
    """Bug bash: what the owner posted by hand on a followed PR came back as news to every
    worker following it."""
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    state = {
        "state": "open",
        "merged": False,
        "comments": [
            {"id": 11, "user": {"login": "Me"}, "body": "I'll fix it", "html_url": "u11"},
            {"id": 12, "user": {"login": "bob"}, "body": "Thanks", "html_url": "u12"},
            # Another account signed in here, not the one used on this repo: someone else's.
            {"id": 13, "user": {"login": "Tester"}, "body": "Leave it?", "html_url": "u13"},
        ],
    }
    gh = fake_gh(state)

    async def signed_in(*arguments, payload=None, **_):
        if arguments[:2] == ("auth", "status"):
            accounts = [
                {"login": "me", "state": "success", "active": True},
                {"login": "tester", "state": "success", "active": False},
            ]
            return json.dumps({"hosts": {"github.com": accounts}})
        return await gh(*arguments, payload=payload)

    host = Host(store)
    await GitHubSource(store, host, gh_command=signed_in).poll_once()
    [(_thread, keys, text)] = host.deliveries
    assert "github:issuecomment:12" in keys and "github:review:32" in keys
    assert "github:issuecomment:13" in keys and "Leave it?" in text
    assert "github:issuecomment:11" not in keys and "github:review:31" not in keys
    assert "I'll fix it" not in text and "our own review" not in text


def review_draft(harness, **target):
    return harness.store.create_draft(
        harness.router_id,
        "github",
        {"repo": "acme/app", "number": 7, "url": "https://github.com/acme/app/pull/7",
         "via": "task", "head_sha": "c0ffee", "verdict": "comment"} | target,
        "Looks fine",
    )  # fmt: skip


def posted(harness):
    return [payload for _arguments, payload in harness.gh_calls if payload]


async def test_a_bare_approval_chosen_in_review_is_posted(harness):
    """Bug bash: a drafted comment switched to Approve, with no summary and no line comments,
    was refused as "Nothing to post"."""
    runtime = await harness.connect()
    harness.gh_replies["api repos/acme/app/pulls/7"] = json.dumps(
        {"state": "open", "head": {"sha": "c0ffee", "ref": "feature"}}
    )
    harness.gh_replies["api repos/acme/app/pulls/7/reviews"] = json.dumps({"id": 9})
    draft = review_draft(harness)
    with pytest.raises(ValueError, match="Nothing to post"):
        await runtime.drafts.resolve(
            draft["id"], DraftDecision(action="send", content={"body": ""})
        )
    approve = DraftDecision(action="send", content={"body": "", "verdict": "approve"})
    assert (await runtime.drafts.resolve(draft["id"], approve))["status"] == "sent"
    [payload] = posted(harness)
    assert payload["event"] == "APPROVE" and payload["body"] == ""


async def test_a_review_is_not_posted_on_the_commit_before_a_push(harness):
    """Bug bash: just after a push, the PR still showed the old head, and the review went on
    the commit before it."""
    runtime = await harness.connect()
    pull = {
        "state": "open",
        "head": {"sha": "c0ffee", "ref": "fix/x", "repo": {"full_name": "bob/app"}},
    }
    harness.gh_replies["api repos/acme/app/pulls/7"] = json.dumps(pull)
    harness.gh_replies["api repos/acme/app/pulls/7/reviews"] = json.dumps({"id": 9})
    harness.gh_replies["api repos/bob/app/git/ref/heads/fix/x"] = json.dumps(
        {"object": {"sha": "beef"}}
    )
    stale = review_draft(harness)
    with pytest.raises(RuntimeError, match="changed after the draft was written"):
        await runtime.drafts.resolve(stale["id"], DraftDecision(action="send"))
    assert harness.store.draft(stale["id"])["status"] == "failed" and posted(harness) == []
    harness.gh_replies["api repos/bob/app/git/ref/heads/fix/x"] = json.dumps(
        {"object": {"sha": "c0ffee"}}
    )
    fresh = review_draft(harness)
    assert (await runtime.drafts.resolve(fresh["id"], DraftDecision(action="send")))[
        "status"
    ] == "sent"
    assert posted(harness)[0]["commit_id"] == "c0ffee"


async def test_a_reply_to_what_a_task_posted_on_a_pr_names_that_task(tmp_path):
    """Bug bash 3 (H2): a reviewer that did not make the PR still hears what answers it: a
    reply to its line comment, or in a conversation it replied in."""
    store = Store(tmp_path / "data")
    store.save_settings(Settings())

    def comment(number, root, body):
        return {"id": number, "in_reply_to_id": root, "user": {"login": "bob"},
                "path": "app.py", "line": 3, "body": body, "html_url": f"u{number}"}  # fmt: skip

    state = {"state": "open", "merged": False, "comments": [], "line_comments": [
        comment(21, None, "Off by one?"), comment(41, 21, "Still off"), comment(52, 50, "Fixed"),
    ]}  # fmt: skip
    gh = fake_gh(state)

    async def replying(*arguments, payload=None):
        if arguments[1].endswith("/comments/21/replies"):
            return json.dumps({"id": 40, "in_reply_to_id": 21, "html_url": "u40"})
        return await gh(*arguments, payload=payload)

    host = Host(store)
    source = GitHubSource(store, host, gh_command=replying)
    target = {"repo": "acme/app", "number": 7, "task_id": "treview"}
    await source.publish_reply(target | {"in_reply_to": 21}, "Not off by one: see line 4.")
    source.posted("github:reviewcomment:50", "Rename this.", target)  # Its review's comment.
    await source.poll_once()
    assert host.replies == {
        "github:reviewcomment:41": ("treview", "bob on app.py:3: Still off (u41)"),
        "github:reviewcomment:52": ("treview", "bob on app.py:3: Fixed (u52)"),
    }


async def test_a_workers_quiet_commit_is_not_news_on_its_pr(tmp_path, git_repo):
    """Bug bash 3 (H3): `git commit -qm` says nothing, inside a command of another kind, and a
    git email GitHub does not know shows no login: a worker's own push came back as news. Its
    checkout's log says it made it; a teammate's commit fetched there is still news."""
    import subprocess

    def git(*arguments, cwd=git_repo):
        return subprocess.run(
            ["git", "-c", "user.email=w@example.com", "-c", "user.name=W", *arguments],
            cwd=cwd, check=True, capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip

    tree = tmp_path / "worker" / "repo"
    git("worktree", "add", "-q", "-b", "chief/fix", str(tree))
    (tree / "a.txt").write_text("a\n")
    git("add", "a.txt", cwd=tree)
    git("commit", "-qm", "Add a", cwd=tree)
    mine = git("rev-parse", "HEAD", cwd=tree)
    other = tmp_path / "teammate"
    git("clone", "-q", str(git_repo), str(other))
    git("commit", "-q", "--allow-empty", "-m", "Theirs", cwd=other)
    theirs = git("rev-parse", "HEAD", cwd=other)
    git("fetch", "-q", str(other), "main:refs/remotes/teammate/main")

    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    store.create_session("w1", "worker", "Fix", "done", cwd=str(tree))
    checkout = {"repo": str(git_repo), "path": str(tree), "branch": "chief/fix"}
    store.execute("UPDATE sessions SET repos=? WHERE id='w1'", (json.dumps([checkout]),))
    state = {"state": "open", "merged": False, "comments": [], "line_comments": []}
    state["runs"] = [{"name": "tests", "status": "in_progress"}]
    host = Host(store)
    source = GitHubSource(store, host, gh_command=fake_gh(state))

    async def acting_login(repo):
        return "me"

    source.acting_login = acting_login
    await source.poll_once()
    seen = len(host.deliveries)

    def anonymous(sha, message):
        return {"sha": sha, "author": None, "commit": {"message": message, "author": {"name": "W"}}}

    state |= {"sha": mine, "compare": {"status": "ahead", "commits": [anonymous(mine, "Add a")]}}
    await source.poll_once()
    assert len(host.deliveries) == seen, "its worker's own commit is not news"
    state |= {
        "sha": theirs,
        "compare": {"status": "ahead", "commits": [anonymous(theirs, "Theirs")]},
    }
    await source.poll_once()
    assert "1 new commit(s)" in host.deliveries[-1][2] and "W: Theirs" in host.deliveries[-1][2]
