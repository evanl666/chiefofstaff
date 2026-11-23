"""A PR in a stack on GitHub, merged from its Ready to merge card (the owner's log, 2026-11-22:
"Request failed: Not merged: This pull request is part of a stack and must be merged using the
asynchronous merge REST API", four times). gh's merge refuses a stacked PR: GitHub's asynchronous
merge merges it, at the head the card showed, followed to its end; the open PRs below it in the
stack merge with it only once the card has said so. GitHub's answers are the ones recorded live
(tests/github_stacks.py)."""

import json
from types import SimpleNamespace

import pytest
from conftest import settle
from github_stacks import CLOSED_BELOW, HEAD_MOVED, MERGED, UUID, StackedGitHub, taken
from test_round5_drafts import merged_pr_task

from chiefofstaff.drafts import MERGING, STILL_MERGING
from chiefofstaff.models import DraftDecision
from chiefofstaff.sources.github import MERGE_FOLLOW, MERGE_WAIT

HEAD = "a" * 40
ALREADY = "A merge request already exists for this pull request."


async def stacked_card(harness, *numbers: int) -> SimpleNamespace:
    """A worker's PR (the last of `numbers`, its stack bottom first) with its Ready to merge card;
    GitHub as recorded, its clock moved by the merge's pauses only."""
    runtime = await harness.connect()
    number = numbers[-1]
    worker, task, _ = await merged_pr_task(harness, runtime, number)
    harness.gh_replies["auth status --json hosts"] = json.dumps(
        {"hosts": {"github.com": [{"login": "me", "active": True, "state": "success"}]}}
    )
    harness.gh_replies["api repos/acme/app"] = json.dumps({"allow_squash_merge": True})
    github = StackedGitHub(runtime.github.raw).stacked(*numbers, heads={number: HEAD})
    github.put = taken(HEAD)
    runtime.github.raw = github
    clock = [1000.0]
    runtime.github.clock = lambda: clock[0]

    async def pause(seconds: float):
        clock[0] += seconds

    runtime.github.pause = pause
    card = harness.store.create_draft(worker, "merge", {
        "label": f"acme/app#{number}", "repo": "acme/app", "number": number, "title": "Part",
        "url": f"https://github.com/acme/app/pull/{number}", "head": HEAD, "task_id": task,
    }, "Part")  # fmt: skip
    return SimpleNamespace(runtime=runtime, github=github, card=card, worker=worker, task=task)


async def merge(harness, card) -> dict:
    return await harness.runtime.drafts.resolve(card["id"], DraftDecision(action="send"))


async def test_a_stacked_pr_merges_through_githubs_asynchronous_merge_at_its_head(harness):
    s = await stacked_card(harness, 11)
    s.github.results = [taken(HEAD), taken(HEAD), MERGED]
    decided = await merge(harness, s.card)
    await settle(s.runtime)
    assert decided["status"] == "sent", decided
    assert decided["outcome"] == "acme/app#11 squashed and merged (GitHub's stack merge)."
    [(path, body)] = s.github.merges()
    assert path == "repos/acme/app/pulls/11/merge-async"
    assert body == {"sha": HEAD, "merge_method": "squash", "merge_action": "direct_merge"}
    told = [m for m in harness.store.messages(s.worker) if "merged" in m["text"]]
    assert told[-1]["text"] == f"You merged it from its card: {decided['outcome']}"
    assert not harness.store.get("mergeasync:acme/app#11"), "followed to its end"


async def test_above_open_prs_the_card_says_they_merge_too_then_merges_them_as_shown(harness):
    s = await stacked_card(harness, 10, 11, 12)
    with pytest.raises(ValueError, match=r"is in a stack above #10 and #11, and GitHub merges"):
        await merge(harness, s.card)
    assert not s.github.merges(), "nothing merged before the owner saw it"
    shown = harness.store.draft(s.card["id"])
    assert shown["status"] == "pending" and "Merge all 3 from here" in shown["outcome"]
    assert [pr["number"] for pr in shown["target"]["stack_below"]] == [10, 11]
    # One below it changed since: said again, still nothing merged.
    s.github.pr(11)["head"] = "b" * 40
    with pytest.raises(ValueError, match="above #10 and #11"):
        await merge(harness, s.card)
    assert not s.github.merges()
    assert harness.store.draft(s.card["id"])["target"]["stack_below"][1]["head"] == "b" * 40
    decided = await merge(harness, s.card)
    assert decided["status"] == "sent"
    assert decided["outcome"] == (
        "acme/app#12 squashed and merged with #10 and #11 below it in its stack "
        "(GitHub's stack merge)."
    )
    assert [pr["state"] for pr in s.github.prs] == ["merged"] * 3


async def test_once_the_prs_below_merged_it_merges_alone(harness):
    s = await stacked_card(harness, 10, 11)
    s.github.pr(10)["state"] = "merged"
    decided = await merge(harness, s.card)
    assert decided["outcome"] == "acme/app#11 squashed and merged (GitHub's stack merge)."


@pytest.mark.parametrize(
    ("answer", "words", "status"),
    [
        (CLOSED_BELOW, "Not merged: Its stack on GitHub can't merge as it is (PR #10 is closed. "
         "Closed PRs are not allowed in a stack.): fix it there, then merge.", "pending"),
        (HEAD_MOVED, "Not merged: New commits were pushed since: check them, then merge.",
         "rejected"),
    ],
)  # fmt: skip
async def test_a_stack_merge_github_refuses_says_why_in_words(harness, answer, words, status):
    s = await stacked_card(harness, 11)
    s.github.results = [answer]
    with pytest.raises(RuntimeError) as raised:
        await merge(harness, s.card)
    assert str(raised.value) == words
    assert harness.store.draft(s.card["id"])["status"] == status


async def test_a_head_moved_before_it_was_taken_is_refused_at_once(harness):
    s = await stacked_card(harness, 11)
    s.github.put = (HEAD_MOVED, "HTTP 400")
    with pytest.raises(RuntimeError, match="New commits were pushed since"):
        await merge(harness, s.card)
    assert harness.store.draft(s.card["id"])["status"] == "rejected"


async def test_one_github_is_at_already_is_followed_never_asked_twice(harness):
    s = await stacked_card(harness, 11)
    s.github.put = (taken(HEAD, ALREADY), "HTTP 409")
    decided = await merge(harness, s.card)
    assert decided["status"] == "sent" and len(s.github.merges()) == 1


async def test_one_github_is_at_already_for_another_commit_is_not_followed(harness):
    s = await stacked_card(harness, 11)
    s.github.put = (taken("c" * 40, ALREADY), "HTTP 409")
    with pytest.raises(RuntimeError, match="Another merge of it, of another commit, is under way"):
        await merge(harness, s.card)
    assert harness.store.draft(s.card["id"])["status"] == "pending"


async def test_where_github_has_no_stack_merge_it_says_what_to_do(harness):
    s = await stacked_card(harness, 11)
    s.github.put = ({"message": "Not Found", "status": "404"}, "Not Found (HTTP 404)")
    with pytest.raises(RuntimeError) as raised:
        await merge(harness, s.card)
    assert str(raised.value) == (
        "Not merged: It is in a stack on GitHub, and GitHub's merge for stacks couldn't be used "
        "from here (GitHub here has no asynchronous merge): merge it on GitHub, from the PR's "
        "page."
    )


async def test_a_merge_past_the_wait_goes_on_in_the_background_to_its_end(harness):
    s = await stacked_card(harness, 11)
    polls = int(MERGE_WAIT) + 2  # Still at it once the click's wait is over.
    s.github.results = [*[taken(HEAD)] * polls, MERGED]
    decided = await merge(harness, s.card)
    assert decided["status"] == "pending" and decided["outcome"] == MERGING
    again = await merge(harness, s.card)
    assert again["outcome"] == MERGING and len(s.github.merges()) == 1, "not asked again"
    await settle(s.runtime)
    done = harness.store.draft(s.card["id"])
    assert done["status"] == "sent", done
    assert done["outcome"] == "acme/app#11 squashed and merged (GitHub's stack merge)."
    assert s.runtime.tasks.get(s.task)["status"] == "review", "merged is not accepted"


async def test_one_github_is_still_at_after_a_while_is_left_to_its_pr(harness):
    s = await stacked_card(harness, 11)
    s.github.results = [taken(HEAD)] * int(MERGE_FOLLOW)
    decided = await merge(harness, s.card)
    assert decided["outcome"] == MERGING
    await settle(s.runtime)
    still = harness.store.draft(s.card["id"])
    assert still["status"] == "pending" and still["outcome"] == STILL_MERGING
    assert json.loads(harness.store.get("mergeasync:acme/app#11"))["uuid"] == UUID
