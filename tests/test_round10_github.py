"""QA round 10: reading followed PRs (E11, E23, E24): why a read failed, in four kinds; backing
off; trying again now; stopping one follow; what reading a PR kept, gone with its last follower."""

import json

import pytest
from conftest import call_tool, settle
from test_github_source import THREAD, Host, fake_gh

from chiefofstaff.models import Settings
from chiefofstaff.sources.github import (
    MAX_BACKOFF_SECONDS,
    POLL_SECONDS,
    READ_STATE,
    GitHubSource,
    backoff,
    clock,
    failure_kind,
    iso,
    why_words,
)
from chiefofstaff.store import Store

T0 = 1793600000.0
PULL = "api repos/acme/app/pulls/7"


def source_with(store, host, gh):
    source = GitHubSource(store, host, gh_command=gh)
    moment = [T0]
    source.clock = lambda: moment[0]
    return source, moment


def read_state(store, key=THREAD):
    return {prefix: value for prefix in READ_STATE if (value := store.get(f"{prefix}:{key}"))}


def failing(error, also=None):
    """gh failing every read of the PR with `error`; `also`: answers it gives otherwise."""
    calls = []

    async def gh(*arguments, payload=None):
        calls.append(arguments)
        if also and (found := also(arguments)) is not None:
            return found
        raise RuntimeError(f"GitHub request failed: {error}")

    return gh, calls


def reads(calls):
    return sum(1 for arguments in calls if arguments[:2] == ("api", "repos/acme/app/pulls/7"))


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        ("gh: API rate limit exceeded for user ID 1. (HTTP 403)", "rate_limited"),
        ("gh: You have exceeded a secondary rate limit. (HTTP 403)", "rate_limited"),
        ("HTTP 429: Too Many Requests", "rate_limited"),
        ("gh: Resource not accessible by integration (HTTP 403)", "permission"),
        ("gh: Resource protected by organization SAML enforcement. (HTTP 403)", "permission"),
        ("gh: Bad credentials (HTTP 401)", "permission"),
        ("gh: Not Found (HTTP 404)", "not_found"),
        ("GraphQL: Could not resolve to a PullRequest with the number of 9.", "not_found"),
        ("HTTP 502: Bad Gateway", "transient"),
        ("GitHub request timed out.", "transient"),
        ("dial tcp: lookup api.github.com: no such host", "transient"),
        ("GitHub CLI not found. Install gh and sign in before connecting reviews.", "transient"),
        ("something nobody has seen before", "transient"),
    ],
)
def test_a_failed_read_is_told_as_one_of_four_kinds(error, kind):
    assert failure_kind(error) == kind


def test_the_owner_reads_why_and_when_it_is_tried_again():
    later = iso(T0 + 4 * 60)
    assert why_words("transient", "GitHub request timed out.", later, T0) == (
        "GitHub did not answer (timeout); retrying in 4 min"
    )
    assert why_words("transient", "HTTP 502: Bad Gateway", later, T0).startswith(
        "GitHub did not answer (HTTP 502);"
    )
    assert why_words("permission", "gh: Resource not accessible (HTTP 403)", later, T0) == (
        "GitHub says this account cannot read it (permission); retrying in 4 min"
    )
    assert why_words("rate_limited", "API rate limit exceeded", later, T0) == (
        f"GitHub rate limit; retrying at {clock(T0 + 240)}"
    )
    assert why_words("transient", "HTTP 502", iso(T0 - 5), T0).endswith("; retrying now")


def test_backing_off_grows_and_stops_at_an_hour():
    assert [backoff("transient", n, "comments") for n in range(1, 9)] == [
        120, 240, 480, 960, 1920, 3600, 3600, 3600,
    ]  # fmt: skip
    assert [backoff("rate_limited", n, "pull request") for n in range(1, 4)] == [120, 240, 480]
    assert [backoff("permission", n, "pull request") for n in range(1, 6)] == [
        120, 480, 1920, 3600, 3600,
    ]  # fmt: skip
    # Not found is read again at the next poll: three in a row let it go.
    assert {backoff("not_found", n, "pull request") for n in range(1, 4)} == {POLL_SECONDS}
    assert backoff("not_found", 3, "CI checks") == 480, "its CI not found: not the PR"


async def test_a_pr_github_does_not_answer_for_is_read_less_and_less_often(tmp_path):
    """E23: it was read every poll five times, then every tenth poll for ever."""
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    gh, calls = failing("HTTP 502: Bad Gateway")
    source, moment = source_with(store, Host(store), gh)
    read_at = []
    for poll in range(100):
        before = reads(calls)
        await source.poll_once()
        if reads(calls) > before:
            read_at.append(poll)
            assert store.get(f"ghnext:{THREAD}") == iso(
                moment[0] + min(MAX_BACKOFF_SECONDS, POLL_SECONDS * 2 ** (len(read_at) - 1))
            )
        moment[0] += POLL_SECONDS
    # 1, 2, 4, 8, 16 polls apart, then every 30 (an hour).
    assert read_at == [0, 1, 3, 7, 15, 31, 61, 91]
    assert store.get(f"ghfail:{THREAD}") == "8"


async def test_a_rate_limit_waits_for_when_github_lifts_it(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    reset = T0 + 1500
    core = {"limit": 5000, "remaining": 0, "reset": reset}

    def limits(arguments):
        return json.dumps({"resources": {"core": core}}) if arguments[1] == "rate_limit" else None

    gh, calls = failing("gh: API rate limit exceeded for user ID 1. (HTTP 403)", limits)
    source, moment = source_with(store, Host(store), gh)
    await source.poll_once()
    why = source.why(THREAD)
    assert (why["kind"], why["retry_at"]) == ("rate_limited", iso(reset))
    assert why["error"] == f"GitHub rate limit; retrying at {clock(reset)}"
    assert store.get(f"ghnext:{THREAD}") == iso(reset)
    moment[0] = reset - 60
    await source.poll_once()
    assert reads(calls) == 1, "not before GitHub lifts it"
    moment[0] = reset
    await source.poll_once()
    assert reads(calls) == 2
    # A secondary limit says no time: it backs off as usual (the third failure in a row).
    core["remaining"] = 10
    moment[0] += MAX_BACKOFF_SECONDS
    await source.poll_once()
    assert store.get(f"ghnext:{THREAD}") == iso(moment[0] + 4 * POLL_SECONDS)


async def test_a_retry_after_github_gives_is_kept(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    gh, _ = failing("HTTP 429: Too Many Requests; Retry-After: 900")
    source, _ = source_with(store, Host(store), gh)
    await source.poll_once()
    assert store.get(f"ghnext:{THREAD}") == iso(T0 + 900)


async def test_a_pr_that_could_not_be_read_is_never_taken_for_one_that_is_gone(tmp_path):
    """Only GitHub saying three times in a row that the PR does not exist lets it go: a timeout
    or a rate limit between says nothing of it; no access says it is there."""
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    host = Host(store)
    answers = iter(
        [
            "gh: Not Found (HTTP 404)",
            "GitHub request timed out.",
            "gh: Not Found (HTTP 404)",
            "gh: API rate limit exceeded (HTTP 403)",
            "gh: Resource not accessible by integration (HTTP 403)",
            "gh: Not Found (HTTP 404)",
            "gh: Not Found (HTTP 404)",
            "HTTP 502: Bad Gateway",
        ]
    )
    kinds = []

    async def gh(*arguments, payload=None):
        if arguments[:2] != ("api", "repos/acme/app/pulls/7"):
            raise RuntimeError("unknown")  # Its rate limit, the accounts signed in.
        raise RuntimeError(f"GitHub request failed: {next(answers)}")

    source, moment = source_with(store, host, gh)
    for _ in range(8):
        await source.poll_once()
        kinds.append(source.why(THREAD)["kind"])
        moment[0] += MAX_BACKOFF_SECONDS
    assert kinds == [
        "not_found", "transient", "not_found", "rate_limited", "permission", "not_found",
        "not_found", "transient",
    ]  # fmt: skip
    assert host.subscriptions, "never three not-found reads in a row"
    assert store.get(f"gh404:{THREAD}") == "2"
    # Its CI checks not found is not the PR gone.
    state = {"state": "open", "merged": False, "comments": [], "line_comments": []}
    plain = fake_gh(state)

    async def no_checks(*arguments, payload=None):
        if "/check-runs" in arguments[1]:
            raise RuntimeError("GitHub request failed: gh: Not Found (HTTP 404)")
        return await plain(*arguments, payload=payload)

    source.raw = no_checks
    for _ in range(4):
        await source.poll_once()
        moment[0] += MAX_BACKOFF_SECONDS
    assert host.subscriptions and source.why(THREAD)["step"] == "CI checks"
    assert not store.get(f"gh404:{THREAD}")


async def test_a_read_clears_what_failed_at_once_and_its_card_says_so(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    host = Host(store)
    state = {"state": "open", "merged": False, "comments": []}
    broken = {"now": True}
    plain = fake_gh(state)

    async def gh(*arguments, payload=None):
        if broken["now"] and arguments[1] == "repos/acme/app/pulls/7":
            raise RuntimeError("GitHub request failed: GitHub request timed out.")
        return await plain(*arguments, payload=payload)

    source, moment = source_with(store, host, gh)
    await source.poll_once()
    assert set(read_state(store)) == {"ghfail", "ghwhy", "ghnext"}
    assert source.why(THREAD)["retry_at"] == iso(T0 + POLL_SECONDS)
    broken["now"] = False
    moment[0] += POLL_SECONDS
    await source.poll_once()
    left = read_state(store)
    assert not {"ghfail", "gh404", "ghskip", "ghwhy"} & set(left)
    assert left["ghread"] == iso(moment[0])
    assert left["ghnext"] == iso(moment[0] + POLL_SECONDS)
    assert source.why(THREAD) == {}
    assert host.health_changes == [THREAD], "its card says so at once, failed once or more"


async def follower(runtime, harness, title):
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title=title, instructions=title,
        why_new="test", follow=["github:acme/app#7"],
    )  # fmt: skip
    return json.loads(result.text_result_for_llm)["session_id"]


def answering(harness, comments=()):
    harness.gh_replies.update(
        {
            PULL: json.dumps(
                {"state": "open", "merged": False, "title": "Fix", "head": {"sha": "abc"}}
            ),
            "api repos/acme/app/issues/7/comments": json.dumps(list(comments)),
            "api repos/acme/app/pulls/7/comments": "[]",
            "api repos/acme/app/pulls/7/reviews": "[]",
            "api repos/acme/app/commits/abc/check-runs": json.dumps({"check_runs": []}),
            "api repos/acme/app/commits/abc/status": json.dumps({"statuses": []}),
        }
    )


def harness_reads(harness):
    return sum(1 for arguments, _ in harness.gh_calls if " ".join(arguments) == PULL)


async def test_try_again_reads_a_pr_now_whatever_its_backoff(harness):
    runtime = await harness.connect()
    worker = await follower(runtime, harness, "Fix #7")
    moment = [T0]
    runtime.github.clock = lambda: moment[0]
    harness.gh_replies[PULL] = RuntimeError("GitHub request failed: HTTP 502: Bad Gateway")
    await runtime.github.poll_once()
    assert runtime.follow_health(worker) == {THREAD: 1}
    published = []
    runtime.read_health_changed = published.append
    before = harness_reads(harness)
    moment[0] += 30
    await runtime.github.poll_once()
    assert harness_reads(harness) == before, "backing off"
    tried = await runtime.retry_follow(worker, "github:Acme/App#7")
    assert harness_reads(harness) == before + 1, "read at once"
    assert (tried["failures"], tried["why"]["kind"]) == (2, "transient")
    assert tried["why"]["retry_at"] == tried["next_at"] == iso(moment[0] + 2 * POLL_SECONDS)
    assert tried["why"]["error"] == "GitHub did not answer (HTTP 502); retrying in 4 min"
    assert published == [THREAD], "its card says when it is tried next"
    answering(harness)
    tried = await runtime.retry_follow(worker, THREAD)
    assert tried == {
        "key": THREAD,
        "failures": 0,
        "why": {},
        "read_at": iso(moment[0]),
        "next_at": iso(moment[0] + POLL_SECONDS),
    }
    assert runtime.follow_health(worker) == {} and published == [THREAD, THREAD]
    with pytest.raises(ValueError, match="does not follow"):
        await runtime.retry_follow(worker, "github:acme/app#8")


async def test_stopping_one_follow_leaves_the_other_followers_as_they_were(harness):
    runtime = await harness.connect()
    first = await follower(runtime, harness, "Fix #7")
    second = await follower(runtime, harness, "Review #7")
    moment = [T0]
    runtime.github.clock = lambda: moment[0]
    harness.gh_replies[PULL] = RuntimeError(
        "GitHub request failed: gh: Resource not accessible by integration (HTTP 403)"
    )
    for _ in range(3):
        await runtime.github.poll_once()
        moment[0] += MAX_BACKOFF_SECONDS
    why = runtime.follow_why(THREAD)
    assert set(why) == {"kind", "error", "step", "at", "retry_at", "detail"}
    assert why["kind"] == "permission" and "(permission)" in why["error"]
    kept = read_state(harness.store)
    assert runtime.unfollow_for(first, THREAD) is True
    assert harness.store.follows(first) == [] and harness.store.follows(second) == [THREAD]
    assert runtime.follow_health(second) == {THREAD: 3}
    assert read_state(harness.store) == kept and runtime.follow_why(THREAD) == why
    # Following it again does not wipe what is known of it: the other still sees it unreadable.
    runtime.follow(first, [THREAD])
    assert runtime.follow_health(second) == {THREAD: 3} and read_state(harness.store) == kept
    runtime.unfollow_for(first, THREAD)
    assert read_state(harness.store) == kept
    runtime.unfollow_for(second, THREAD)
    assert read_state(harness.store) == {}, "nobody follows it: nothing of reading it is kept"


async def test_what_reading_a_pr_kept_goes_with_its_last_follower(harness):
    runtime = await harness.connect()
    worker = await follower(runtime, harness, "Fix #7")
    other = await follower(runtime, harness, "Review #7")
    runtime.github.clock = lambda: T0
    comment = {"id": 11, "user": {"login": "bob"}, "body": "LGTM", "html_url": "u11"}
    answering(harness, [comment])
    await runtime.github.poll_once()
    await settle(runtime)
    assert {"ghsince", "ghhead", "ghread", "ghnext"} <= set(read_state(harness.store))
    await runtime.workers.close(worker)
    await runtime.github.poll_once()
    assert read_state(harness.store), "the other still follows it"
    await runtime.workers.close(other)
    calls = harness_reads(harness)
    harness.store.set("ghfail:slack:D0123ABC", "0")  # What following a Slack DM wrote before.
    await runtime.github.poll_once()
    assert read_state(harness.store) == {}
    assert not harness.store.get("ghfail:slack:D0123ABC")
    assert harness_reads(harness) == calls, "nor is it read"
    # Its news stays.
    assert harness.store.one("SELECT id FROM triggers WHERE key='github:issuecomment:11'")
    assert any("LGTM" in m["text"] for m in harness.store.messages(worker))


async def test_a_pr_that_ended_leaves_nothing_of_reading_it(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings(Settings())
    host = Host(store)
    state = {"state": "closed", "merged": True, "ended": "2026-11-02T00:00:00Z", "comments": []}
    source, _ = source_with(store, host, fake_gh(state))
    await source.poll_once()
    assert host.unfollowed == [(THREAD, True, True)]
    assert read_state(store) == {}, "not even when it was last read"


async def test_a_pr_merged_from_its_card_leaves_nothing_of_reading_it(harness):
    runtime = await harness.connect()
    await follower(runtime, harness, "Fix #7")
    runtime.github.clock = lambda: T0
    answering(harness)
    await runtime.github.poll_once()
    assert read_state(harness.store)
    runtime.unfollow(THREAD, finished=True, merged=True)
    assert read_state(harness.store) == {}


async def test_a_pr_not_found_is_let_go_and_never_read_again(harness):
    runtime = await harness.connect()
    worker = await follower(runtime, harness, "Fix #7")
    moment = [T0]
    runtime.github.clock = lambda: moment[0]
    harness.gh_replies[PULL] = RuntimeError("GitHub request failed: gh: Not Found (HTTP 404)")
    for _ in range(3):
        await runtime.github.poll_once()
        moment[0] += POLL_SECONDS
    assert harness.store.follows(worker) == []
    assert read_state(harness.store) == {}, "at once"
    notes = [text for kind, text in harness.kinds(worker) if kind == "notice"]
    assert any(f"Stopped following {THREAD}" in note for note in notes)
    calls = harness_reads(harness)
    await runtime.github.poll_once()
    assert harness_reads(harness) == calls


async def test_a_pr_a_report_only_names_is_not_followed_nor_read(harness):
    runtime = await harness.connect()
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Fix", instructions="fix"
    )
    worker = json.loads(result.text_result_for_llm)["session_id"]
    harness.gh_replies["api repos/acme/app/pulls/9"] = RuntimeError(
        "GitHub request failed: gh: Not Found (HTTP 404)"
    )
    summary = "Fixed it, as https://github.com/acme/app/pull/9 (an example) did."
    await call_tool(runtime, "report", worker, status="done", summary=summary)
    await settle(runtime)
    assert harness.store.follows(worker) == []
    calls = len(harness.gh_calls)
    await runtime.github.poll_once()
    assert len(harness.gh_calls) == calls
    assert read_state(harness.store, "github:acme/app#9") == {}


def test_why_from_before_is_told_in_the_four_kinds(harness):
    harness.store.set(f"ghwhy:{THREAD}", json.dumps(
        {"kind": "access", "step": "CI checks", "error": "HTTP 403", "at": ""}))  # fmt: skip
    why = harness.runtime.follow_why(THREAD)
    assert (why["kind"], why["detail"], why["retry_at"]) == ("permission", "HTTP 403", None)
    assert why["error"] == "GitHub says this account cannot read it (permission)"
