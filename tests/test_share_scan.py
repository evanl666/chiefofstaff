"""Share links: what a card shares (an allow-list of its view's fields), paths on this computer
shortened, and what looks private found, one hit per value, each removed everywhere it shows."""

import random
import re
import string

import pytest

from chiefofstaff import sharing

ROOT = "/Users/jordan/.chief/w-3/acme-api"


def card(**fields) -> dict:
    base = {
        "title": "Fix the flaky login test",
        "status": "review",
        "status_words": "Ready for review",
        "finished_at": "2026-11-25T01:41:00Z",
        "result": "",
        "verdict": "",
        "criteria": [],
        "checks": [],
        "changes": {"files": 0, "insertions": 0, "deletions": 0, "commits": 0},
        "at": "",
        "prs": [],
        "flags": [],
    }
    return {**base, **fields}


def found(text: str) -> list[tuple[str, str]]:
    return [(hit["label"], hit["value"]) for hit in sharing.hits_in(text)]


# Made-up secrets, put together here so that no secret scanner takes this file for a leak.
GITHUB_TOKEN = "gh" + "p_" + "EXAMPLEexampleEXAMPLEexample01234567"
FOUND = [
    ("token {} here", "a GitHub token", GITHUB_TOKEN),
    ("{}", "a GitHub token", "github" + "_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz"),
    ("key {}.", "an AWS access key", "AKIA" + "IOSFODNN7EXAMPLE"),
    ("{}", "a Google API key", "AIza" + "SyA1234567890abcdefghijklmnopqrstuv"),
    ("{}", "a private key", "-----BEGIN RSA " + "PRIVATE KEY-----"),
    ("{}", "a Slack token", "xo" + "xb-1234567890-abcdefghij"),
    ("{}", "a Stripe key", "sk" + "_live_" + "abcdefghijklmnop1234"),
    ("{}", "an npm token", "np" + "m_" + "abcdefghijklmnopqrstuvwxyz0123456789"),
    ("{}", "an Anthropic key", "sk-" + "ant-api03-" + "abcdefghijklmnopqrstuvwxyz012345"),
    ("{}", "an OpenAI key", "sk-" + "proj-" + "abcdefghijklmnopqrstuvwxyz0123456789"),
    (
        "jwt {}",
        "a JSON web token",
        "eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJ" + "zdWIiOiIxMjM0In0.abcdefghijklmnop",
    ),
    ("Authorization: {}", "a bearer token", "Bearer " + "abcdefghijklmnopqrstuvwx"),
    ("{}", "a password or key", "password = " + "hunter2hunter2"),
    ("{}", "a link with a password", "https://deploy:" + "s3cret@ci.example.com/hook"),
    ("{}", "a signed link", "https://bucket.s3.amazonaws.com/a.zip?X-Amz-Signature=abc123"),
    ("see {}.", "a Slack link", "https://acme.slack.com/archives/C01/p123"),
    ("staging, {}, with", "an internal address", "http://10.20.4.17:8080/login"),
    ("{}", "an internal address", "http://jenkins/job/api/12"),
    ("{}", "an internal address", "http://localhost:3000/"),
    ("{}", "an internal address", "https://grafana.corp/d/abc"),
    ("ssh {}", "an internal address", "192.168.1.20"),
    ("on {}.", "an internal address", "172.16.0.4:5432"),
    ("{}", "an internal address", "fd12:3456:789a::1"),
    ("{} is down", "an internal host name", "db.prod.internal"),
    ("{}", "an internal host name", "printer.home.arpa"),
    ("Maya ({}) said", "an email address", "maya.chen@example.com"),
]


@pytest.mark.parametrize(
    ("text", "label", "value"),
    [(around.format(value), label, value) for around, label, value in FOUND],
)
def test_each_rule_finds_what_it_names(text, label, value):
    assert found(text) == [(label, value)]


@pytest.mark.parametrize(
    "text",
    [
        "Opened https://github.com/acme/api/pull/418",
        "feedback: looks good",
        "at 75dc3be, 8 passed in 0.29s",
        "the token expiry read the real clock",
        "version 10.2.3 of the SDK",
        "https://docs.python.org/3/library/re.html",
        "f(a:b) and fe:ed",
        "uv run pytest -q",
        "password reset flow",
    ],
)
def test_ordinary_words_are_no_hit(text):
    assert found(text) == []


def test_overlaps_go_to_the_more_specific_rule():
    # A token in a URL with a password: the token is the secret named.
    assert found(f"API_TOKEN={GITHUB_TOKEN}") == [("a GitHub token", GITHUB_TOKEN)]
    assert found("https://u:pass@10.0.0.1/x") == [
        ("a link with a password", "https://u:pass@10.0.0.1/x")
    ]


def test_a_value_from_the_environment_is_a_hit(monkeypatch):
    value = "abcDEF123456" + "ghiJKL789xyz"
    monkeypatch.setenv("ACME_DEPLOY_TOKEN", value)
    from chiefofstaff.shares import environment

    hits = sharing.scan(card(result=f"Deployed with {value}"), environment())
    assert [(hit["label"], hit["value"]) for hit in hits] == [
        ("a value from your environment", value)
    ]


def test_one_hit_per_value_with_where_and_how_often():
    shared = card(
        title="Mail maya.chen@example.com",
        result="Asked maya.chen@example.com twice: maya.chen@example.com. Also 10.0.0.9.",
        flags=["Says tests pass, but 10.0.0.9 refused"],
        criteria=[{"text": "ok", "state": "verified", "check": "curl http://10.0.0.9/health"}],
    )
    hits = sharing.scan(shared)
    assert [(h["id"], h["value"], h["where"], h["count"]) for h in hits] == [
        ("h1", "maya.chen@example.com", ["Title", "Result"], 3),
        ("h2", "10.0.0.9", ["Result", "Warnings"], 2),
        ("h3", "http://10.0.0.9/health", ["Done when"], 1),
    ]
    decided = sharing.apply(shared, hits, {"h1": "remove", "h2": "keep", "h3": "remove"})
    assert decided["title"] == "Mail [removed]"
    assert decided["result"] == "Asked [removed] twice: [removed]. Also 10.0.0.9."
    assert decided["criteria"][0]["check"] == "curl [removed]"
    assert decided["flags"] == ["Says tests pass, but 10.0.0.9 refused"]
    assert shared["title"] == "Mail maya.chen@example.com", "the card itself is left as it was"
    assert sharing.undecided(hits, {"h1": "remove", "h2": "keep"}) == [hits[2]]
    assert sharing.undecided(hits, {"h1": "remove", "h2": "keep", "h3": "maybe"}) == [hits[2]]


def test_paths_are_shortened_in_every_field():
    shared, shortened = sharing.shorten(
        card(
            title="Tidy ~/notes/plan.md",
            result=(
                f"Fixed {ROOT}/app/login.py and {ROOT}/tests/test_login.py.\n"
                "Logs in /Users/jordan/Library/Logs/acme.log, also /home/maya/src/x.py, "
                r"C:\Users\jordan\proj\main.go and ~/.config/acme.toml."
            ),
            checks=[
                {
                    "label": "tests",
                    "command": f"uv run pytest {ROOT}/tests",
                    "outcome": "passed",
                    "failed_before": False,
                    "runs": 1,
                }
            ],
            criteria=[{"text": "ok", "state": "verified", "check": f"cd {ROOT} && make"}],
            flags=["Not reading /Users/jordan/.chief/w-3/other: gone."],
            prs=[
                {
                    "label": "acme/api#1",
                    "url": "https://github.com/acme/api/pull/1",
                    "title": "Edit /home/maya/a.txt",
                    "state": "OPEN",
                    "checks": "",
                    "review": "",
                }
            ],
        ),
        [ROOT],
    )
    assert shared["title"] == "Tidy …/plan.md"
    assert shared["result"] == (
        "Fixed app/login.py and tests/test_login.py.\n"
        "Logs in …/acme.log, also …/x.py, …/main.go and …/acme.toml."
    )
    assert shared["checks"][0]["command"] == "uv run pytest tests"
    assert shared["criteria"][0]["check"] == "cd acme-api && make"
    assert shared["flags"] == ["Not reading …/other: gone."]
    assert shared["prs"][0]["title"] == "Edit …/a.txt"
    assert {item["to"] for item in shortened} >= {"app/login.py", "tests/test_login.py", "…/x.py"}


def test_no_local_path_is_left_in_any_field():
    """Generated paths (home folders, ~, Windows, checkouts elsewhere), in every field."""
    rng = random.Random(7)  # noqa: S311  (made-up paths, not secrets)

    def word() -> str:
        return "".join(rng.choice(string.ascii_lowercase + "-_.") for _ in range(rng.randint(1, 9)))

    roots = ["/srv/work/acme-api", "/Users/jordan/.chief/w-1/web"]
    for _ in range(200):
        tail = "/".join(word() for _ in range(rng.randint(0, 4)))
        path = rng.choice(
            [
                f"/Users/{word()}/{tail}",
                f"/home/{word()}/{tail}",
                f"~/{tail}",
                "C:\\Users\\" + "\\".join([word(), *tail.split("/")]),
                f"{rng.choice(roots)}/{tail}",
                rng.choice(roots),
            ]
        )
        around = rng.choice(["{}", "see {} now", "`{}`", "({})", "{}.", '"{}"'])
        text = around.format(path)
        shared, _ = sharing.shorten(
            card(
                title=text,
                result=text,
                flags=[text],
                criteria=[{"text": text, "state": "verified", "check": text}],
            ),
            roots,
        )
        for _, value, _ in sharing.strings(shared):
            assert not re.search(r"/Users/|/home/|~/|[A-Za-z]:\\Users\\|/srv/work", value), (
                text,
                value,
            )


VIEW = {
    "id": "t1",
    "title": "Fix the flaky login test",
    "status": "review",
    "goal": "The owner's own request, word for word",
    "acceptance": "secret acceptance text",
    "result": "Fixed it.",
    "progress": "earlier progress",
    "credits": 4.2,
    "runs": 3,
    "cwd": "/Users/jordan/.chief/w-3",
    "branch": "chief/fix-login",
    "worker": "Fix login worker",
    "watching": ["github:acme/api#418"],
    "earlier_results": [{"text": "an earlier result"}],
    "links": ["https://acme.slack.com/archives/C1/p1"],
    "given_links": ["https://docs.example.com"],
    "progress_updates": [{"text": "half way"}],
    "decisions": [{"question": "x"}],
    "files": ["/Users/jordan/.chief/w-3/notes.md"],
    "artifacts": [{"name": "run"}],
    "receipts": {
        "verdict": "verified",
        "criteria": [
            {
                "criterion": "tests pass",
                "state": "verified",
                "command": "pytest -q",
                "evidence": "8 passed",
                "exit_code": 0,
            }
        ],
        "tests": {
            "command": "pytest -q",
            "passed": True,
            "exit_code": 0,
            "runs": 2,
            "failed_before": True,
            "own": True,
            "cwd": "/Users/jordan/x",
        },
        "checks": {
            "command": "ruff check",
            "passed": False,
            "exit_code": 1,
            "runs": 1,
            "failed_before": False,
        },
        "build": None,
        "changed": {"files": 1, "insertions": 3, "deletions": 1, "commits": 1, "uncommitted": 2},
        "checkouts": [{"path": "/Users/jordan/.chief/w-3/acme-api", "branch": "chief/fix"}],
        "version": [{"branch": "chief/fix", "commit": "75dc3be1a2b3", "edited": False}],
        "prs": [
            {
                "label": "acme/api#418",
                "url": "https://github.com/acme/api/pull/418",
                "title": "Fix login",
                "state": "OPEN",
                "checks": "passing",
                "review": "",
                "head": "abc",
                "branch": "chief/fix",
                "author": "jordan",
                "failing": [],
            },
            {"label": "acme/api#9", "url": "https://evil.example/acme/api/pull/9"},
        ],
        "commands": [{"command": "cat ~/.ssh/id_rsa", "kind": "", "exit_code": 0}],
        "flags": ["Commits generated files: dist/app.js."],
    },
}


def test_the_snapshot_is_its_allow_list_and_nothing_else():
    shared = sharing.snapshot(VIEW, "2026-11-25T01:41:00Z")
    assert shared == {
        "title": "Fix the flaky login test",
        "status": "review",
        "status_words": "Ready for review",
        "finished_at": "2026-11-25T01:41:00Z",
        "result": "Fixed it.",
        "verdict": "verified",
        "criteria": [{"text": "tests pass", "state": "verified", "check": "pytest -q"}],
        "checks": [
            {
                "label": "tests",
                "command": "pytest -q",
                "outcome": "passed",
                "failed_before": True,
                "runs": 2,
            },
            {
                "label": "checks",
                "command": "ruff check",
                "outcome": "failed",
                "failed_before": False,
                "runs": 1,
            },
        ],
        "changes": {"files": 1, "insertions": 3, "deletions": 1, "commits": 1},
        "at": "75dc3be",
        "prs": [
            {
                "label": "acme/api#418",
                "url": "https://github.com/acme/api/pull/418",
                "title": "Fix login",
                "state": "OPEN",
                "checks": "passing",
                "review": "",
            }
        ],
        "flags": ["Commits generated files: dist/app.js."],
    }
    flat = repr(shared)
    for never in (
        "owner's own request",
        "acceptance",
        "4.2",
        "chief/fix",
        "w-3",
        "earlier",
        "slack.com",
        "docs.example",
        "half way",
        "id_rsa",
        "evil.example",
        "worker",
    ):
        assert never not in flat, never
    assert sharing.clean_card(shared) == shared


def test_its_end_is_a_time_every_browser_reads():
    assert sharing.snapshot(VIEW, "2026-11-25T01:41:00.341675+00:00")["finished_at"] == (
        "2026-11-25T01:41:00Z"
    )
    assert sharing.snapshot(VIEW, "2026-11-24T17:41:00-08:00")["finished_at"] == (
        "2026-11-25T01:41:00Z"
    )
    assert sharing.snapshot(VIEW, "soon")["finished_at"] == ""


def test_tests_run_outside_its_checkout_are_not_its_evidence():
    view = {
        **VIEW,
        "receipts": {**VIEW["receipts"], "tests": {**VIEW["receipts"]["tests"], "own": False}},
    }
    assert [item["label"] for item in sharing.snapshot(view)["checks"]] == ["checks"]


def test_a_long_result_is_cut_at_a_paragraph_and_says_so():
    para = "word " * 1000
    text = "\n\n".join([para.strip()] * 12)  # 60,000 characters.
    cut, was = sharing.cut_result(text)
    assert was and len(cut) <= sharing.RESULT_CHARS + len(sharing.CUT_NOTE) + 2
    assert cut.endswith(f"word\n\n{sharing.CUT_NOTE}")
    assert sharing.cut_result("short") == ("short", False)
    shared = sharing.snapshot({**VIEW, "result": text})
    assert shared["result"].endswith(sharing.CUT_NOTE)


def test_the_digest_is_of_what_the_preview_shows():
    one = card(result="a@example.com")
    hits = sharing.scan(one)
    assert sharing.digest(one, hits) == sharing.digest(card(result="a@example.com"), hits)
    assert sharing.digest(one, hits) != sharing.digest(card(result="b@example.com"), hits)


def test_its_text_for_a_router():
    shared = sharing.snapshot(VIEW)
    text = sharing.card_text(shared)
    assert text.startswith("## Fix the flaky login test\n\nReady for review\n\nFixed it.")
    assert (
        "- ✓ tests pass" in text and "- acme/api#418: https://github.com/acme/api/pull/418" in text
    )
