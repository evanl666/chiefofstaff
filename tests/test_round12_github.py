"""QA round 12, GitHub: nothing hidden in what Chief of Staff posts (E37)."""

import json

from conftest import Harness

from chiefofstaff.models import Settings
from chiefofstaff.sources.github import POSTED_MARK

ACCOUNTS = {"hosts": {"github.com": [{"login": "owner", "state": "success", "active": True}]}}


def review(number: int, review_id: int, body: str, comments: list[tuple[int, str]]) -> dict:
    url = f"https://github.com/acme/app/pull/{number}"
    return {
        "id": f"R{review_id}",
        "url": f"{url}#pullrequestreview-{review_id}",
        "body": body,
        "comments": {
            "nodes": [
                {"id": f"C{cid}", "url": f"{url}#discussion_r{cid}", "body": text, "path": "a.py"}
                for cid, text in comments
            ]
        },
    }


async def test_what_is_posted_carries_nothing_hidden_and_is_still_not_learned_as_yours(tmp_path):
    """E37: every review, line comment and reply ended with <!-- chief-of-staff -->, seen when
    it is edited, quoted or read through the API. Learning how the owner writes still leaves
    out what Chief of Staff posted: by its id, else (the id lost) by its words on that PR."""
    harness = Harness(tmp_path / "data", Settings(github_enabled=True, github_repos=["acme/app"]))
    runtime = await harness.connect()
    github = runtime.github
    harness.gh_replies |= {
        "auth status": json.dumps(ACCOUNTS),
        "auth token": "tok",
        "api repos/acme/app/pulls/7": json.dumps({"state": "open", "head": {"sha": "abc"}}),
        "api repos/acme/app/pulls/7/reviews": json.dumps(
            {"id": 41, "html_url": "https://github.com/acme/app/pull/7#pullrequestreview-41"}
        ),
        # Its line comments could not be listed: their ids are not known.
        "api repos/acme/app/pulls/7/reviews/41/comments": RuntimeError("HTTP 502"),
        "api repos/acme/app/pulls/7/comments/21/replies": json.dumps({"id": 52}),
        "api repos/acme/app/issues/7/comments": json.dumps({"id": 63}),
    }
    target = {"repo": "acme/app", "number": 7, "head_sha": "abc"}
    line = {"path": "a.py", "line": 3, "body": "Why 3 retries?"}
    await github.publish_review(target | {"comments": [line]}, "Two small things.")
    await github.publish_reply(target | {"in_reply_to": 21}, "Fixed in f61047f.")
    await github.publish_conversation_reply(target | {"reply_to_comment": 30}, "@bob Done.")
    payloads = [payload for _, payload in harness.gh_calls if payload]
    bodies = [p["body"] for p in payloads] + [
        c["body"] for p in payloads for c in p.get("comments", [])
    ]
    assert bodies == ["Two small things.", "Fixed in f61047f.", "@bob Done.", "Why 3 retries?"]

    reviews = [
        review(7, 41, "Two small things.", [(77, "Why 3 retries?")]),  # Posted above.
        review(7, 42, "", [(78, "Why  3 retries?\n")]),  # The same words, spaced otherwise.
        # Posted by an older version (on another install): its mark says so.
        review(8, 43, f"Looks good.\n\n{POSTED_MARK}", [(79, f"Rename this.\n\n{POSTED_MARK}")]),
        # The owner's own, on another PR: the same words are theirs there.
        review(8, 44, "Nice. Ship it.", [(80, "Why 3 retries?")]),
    ]
    pull = {"title": "Retry", "repository": {"nameWithOwner": "acme/app"}, "reviews": {}}
    pull["reviews"]["nodes"] = reviews
    harness.gh_replies["api graphql"] = json.dumps({"data": {"search": {"nodes": [pull]}}})
    assert await runtime.habits.collect_reviews() == 2
    learned = harness.store.all("SELECT ref, text FROM observations ORDER BY ref")
    assert [(row["ref"], row["text"]) for row in learned] == [
        ("github:C80", "Why 3 retries?"),
        ("github:R44", "Nice. Ship it."),
    ]
