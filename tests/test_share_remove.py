"""Share links: what is left of a secret after Remove (SECURITY-REVIEW.md, finding 1). Remove
replaces a hit's whole value, so each rule's value must be the whole secret: a private key's
block from BEGIN to its END, an AWS key id with the secret beside it, a link's password whatever
its scheme, a credential line however its name is spelled (DB_PASSWORD=, "password": "…").
Every secret here is made up, and put together at run time (no secret scanner takes this file for
a leak)."""

import base64
import random

import pytest

from chiefofstaff import sharing

DASHES = "-" * 5


def body(seed: int, lines: int = 5) -> list[str]:
    """Base64 lines as a key's body has them (made up)."""
    rng = random.Random(seed)  # noqa: S311  (made up, not a secret)
    raw = bytes(rng.randrange(256) for _ in range(48 * lines))
    text = base64.b64encode(raw).decode()
    return [text[i : i + 64] for i in range(0, len(text), 64)]


def block(kind: str, seed: int, headers: tuple[str, ...] = (), end: bool = True) -> str:
    begin = f"{DASHES}BEGIN {kind}PRIVATE KEY{' BLOCK' if 'PGP' in kind else ''}{DASHES}"
    finish = f"{DASHES}END {kind}PRIVATE KEY{' BLOCK' if 'PGP' in kind else ''}{DASHES}"
    return "\n".join([begin, *headers, *body(seed), *([finish] if end else [])])


KEY_ID = "AKIA" + "IOSFODNN7" + "EXAMPLE"
TEMPORARY_ID = "ASIA" + "IOSFODNN7" + "EXAMPLE"
KEY_SECRET = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"
SESSION = "FwoGZXIvYXdzE" + "BAaDHk0bGVxa2VzdA" + "Nm9yZXhhbXBsZQ"
API_VALUE = "abcd1234" + "efgh"
CLIENT_VALUE = "01234567" + "89abcdef"
HEADER_VALUE = "abcdef01" + "23456789"


def card(result: str) -> dict:
    return {
        "title": "Rotate the deploy credentials",
        "status": "review",
        "status_words": "Ready for review",
        "finished_at": "2026-11-25T01:41:00Z",
        "result": result,
        "verdict": "",
        "criteria": [],
        "checks": [],
        "changes": {"files": 0, "insertions": 0, "deletions": 0, "commits": 0},
        "at": "",
        "prs": [],
        "flags": [],
    }


def after_remove(text: str) -> tuple[str, list[dict]]:
    """The result once every hit is removed, and the hits."""
    shared = card(text)
    hits = sharing.scan(shared)
    return sharing.apply(shared, hits, {hit["id"]: "remove" for hit in hits})["result"], hits


def assert_gone(text: str, secrets: list[str], kept: list[str] = ()):  # type: ignore[assignment]
    left, hits = after_remove(text)
    for secret in secrets:
        assert secret not in left, (secret, left, [hit["value"] for hit in hits])
    for words in kept:
        assert words in left, (words, left)
    assert sharing.REMOVED in left


# A private key: its whole block, BEGIN to its END (or, cut off, its body), never just BEGIN.


@pytest.mark.parametrize(
    ("kind", "headers"),
    [
        ("RSA ", ()),
        ("", ()),
        ("EC ", ()),
        ("OPENSSH ", ()),
        ("ENCRYPTED ", ()),
        ("PGP ", ("Version: GnuPG v2", "")),
        ("RSA ", ("Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-128-CBC,0123456789ABCDEF", "")),
    ],
)
def test_a_private_key_is_removed_whole(kind, headers):
    key = block(kind, 7, headers)
    text = f"Generated a deploy key:\n\n```\n{key}\n```\n\nAdded it to the repository."
    lines = [line for line in key.splitlines()[1:] if line.strip()]
    assert_gone(text, lines, kept=["Generated a deploy key:", "Added it to the repository."])
    [hit] = after_remove(text)[1]
    assert hit["label"] == "a private key" and hit["value"] == key


def test_a_private_key_in_prose_and_in_a_json_string():
    key = block("RSA ", 8)
    assert_gone(f"The key: {key} (rotated)", key.splitlines()[1:], kept=["(rotated)"])
    escaped = key.replace("\n", "\\n")
    service = f'{{"type": "service_account", "private_key": "{escaped}\\n", "client_id": "1"}}'
    assert_gone(service, body(8), kept=['"client_id": "1"'])


def test_a_private_key_cut_off_before_its_end_loses_its_body():
    key = block("RSA ", 9, end=False)
    assert_gone(f"{key}\nThat was all the log showed.", body(9), kept=["That was all"])


def test_two_keys_each_end_where_theirs_does():
    one, two = block("RSA ", 1), block("EC ", 2)
    text = f"{one}\nbetween the two\n{two}"
    left, hits = after_remove(text)
    assert [hit["value"] for hit in hits] == [one, two]
    assert left == f"{sharing.REMOVED}\nbetween the two\n{sharing.REMOVED}"


# AWS: the key id and the secret beside it, removed together.


@pytest.mark.parametrize(
    "text",
    [
        f"[default]\naws_access_key_id = {KEY_ID}\naws_secret_access_key = {KEY_SECRET}\n",
        f"export AWS_ACCESS_KEY_ID={KEY_ID}\nexport AWS_SECRET_ACCESS_KEY={KEY_SECRET}",
        f"Use {KEY_ID} / {KEY_SECRET} for the staging bucket.",
        f'{{"AccessKeyId": "{TEMPORARY_ID}", "SecretAccessKey": "{KEY_SECRET}", '
        f'"SessionToken": "{SESSION}"}}',
        f'{{"SecretAccessKey": "{KEY_SECRET}", "AccessKeyId": "{KEY_ID}"}}',
    ],
)
def test_aws_credentials_lose_the_secret_too(text):
    assert_gone(text, [KEY_ID, TEMPORARY_ID, KEY_SECRET, SESSION])


def test_an_aws_secret_alone_by_its_name():
    assert_gone(f"AWS_SECRET_ACCESS_KEY={KEY_SECRET}", [KEY_SECRET])
    assert_gone(f"aws_secret_access_key: {KEY_SECRET}", [KEY_SECRET])


# A link with a password, whatever its scheme, and whatever its host.


@pytest.mark.parametrize(
    ("link", "password"),
    [
        ("postgres://user:password@db.internal/app", "password"),
        ("postgres://app:S3cretPass@db.example.com:5432/app", "S3cretPass"),
        ("postgresql://app:S3cretPass@db.example.com/app?sslmode=require", "S3cretPass"),
        ("mysql://root:hunter2hunter2@10.0.0.5/shop", "hunter2hunter2"),
        ("redis://:r3disPassw0rd@cache.example.com:6379/0", "r3disPassw0rd"),
        ("mongodb+srv://reader:p4ssw0rd99@cluster0.example.net/db", "p4ssw0rd99"),
        ("amqp://guest:guestPassw0rd@mq.example.com/vhost", "guestPassw0rd"),
        ("https://deploy:s3cretToken@ci.example.com/hook", "s3cretToken"),
        ("ftp://anon:ftpPass123@files.example.org/pub", "ftpPass123"),
    ],
)
def test_a_link_with_a_password_is_removed_whole(link, password):
    text = f"Connect with {link}, then run the migration."
    assert_gone(text, [password, link], kept=["Connect with", "then run the migration."])
    [hit] = after_remove(text)[1]
    assert hit["label"] == "a link with a password" and hit["value"] == link


# Credential lines, however the name is spelled: after _, quoted, in JSON or YAML.


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("DB_PASSWORD=hunter2hunter2", "hunter2hunter2"),
        ("SECRET_KEY=django-insecure-abcdef123456", "django-insecure-abcdef123456"),
        ("JWT_SECRET=supersecretvalue", "supersecretvalue"),
        ("export STRIPE_API_KEY=keyvalue123456", "keyvalue123456"),
        ('password = "correct horse battery"', "correct horse battery"),
        ("password = 'correct horse battery'", "correct horse battery"),
        ('"password": "s3cr3t!pw"', "s3cr3t!pw"),
        (f"{{'api_key': '{API_VALUE}'}}", API_VALUE),
        (f"client_secret: {CLIENT_VALUE}", CLIENT_VALUE),
        ('DATABASE_PASSWORD: "pa ss word"', "pa ss word"),
        (f"x-api-key: {HEADER_VALUE}", HEADER_VALUE),
        ("Set PASSWORD=hunter2hunter2.", "hunter2hunter2"),
    ],
)
def test_a_credential_line_loses_its_value(text, secret):
    assert_gone(f"Configured it: {text}\nDone.", [secret], kept=["Configured it:", "Done."])


@pytest.mark.parametrize(
    "text",
    [
        "The password reset flow works again.",
        "Rotate the secret key monthly.",
        "The token expiry read the real clock.",
        "DB_PASSWORD is set in the vault, not here.",
        "token: none",
        'password: ""',
        "Fixed the api_key validation (see #12).",
    ],
)
def test_words_about_credentials_are_no_hit(text):
    assert sharing.scan(card(text)) == []


# The reviewer's own examples (SECURITY-REVIEW.md), as written there.


def test_the_reviewers_examples():
    key = block("", 11)
    assert_gone(
        f"{key}\n{KEY_ID}\n{KEY_SECRET}\npostgres://user:password@db.internal/app",
        [*key.splitlines()[1:], KEY_ID, KEY_SECRET, "user:password"],
    )
    for name in ("DB_PASSWORD", "AWS_SECRET_ACCESS_KEY", "SECRET_KEY", "JWT_SECRET"):
        assert_gone(f"{name}=value0123456789", ["value0123456789"])
    assert_gone('password = "a quoted secret"', ["a quoted secret"])
    assert_gone('"password": "json-secret-1"', ["json-secret-1"])


# U9 (QA 0.1.61): a value cut short, as a text that holds it is cut (a title to its length, a
# command at 200 with …, a result at a share's 40,000), is flagged all the same, in every field
# of the card; and a share's own cuts never cut through one. Nothing of it is left after Remove.

GH = "gh" + "p_" + "Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56Qr78"
LINK = "postgres://app:" + "S3cretPass" + "@db.example.com/app"
CUT_SHORT = [
    GH[:31],
    "github" + "_pat_" + "11ABCDEFG0123",
    KEY_ID[:12],
    "AIza" + "SyA1234567",
    "xo" + "xp-" + "1234567890-ab",
    "sk" + "_live_" + "abcdef",
    "np" + "m_" + "abcdefghij",
    "sk-" + "ant-api03-" + "abcdefgh",
    "sk-" + "proj-" + "abcdefgh",
    "eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJzdW",
    "Bearer " + "abcdefghijkl",
    LINK[:19],
    'password = "correct hor',
    "maya.chen@exam",
]
CUTS = ["", "\u2026", "...", f"\n\n{sharing.CUT_NOTE}"]
PR_URL = "https://github.com/acme/api/pull/7"
FIELDS = {
    "Title": lambda text: {"title": text},
    "Result": lambda text: {"result": text},
    "Done when": lambda text: {"criteria": [{"text": text, "state": "verified", "check": text}]},
    "Checks": lambda text: {"checks": [{"label": "tests", "command": text, "outcome": "passed"}]},
    "Warnings": lambda text: {"flags": [text]},
    "PR": lambda text: {"prs": [{"label": "acme/api#7", "title": text, "url": PR_URL}]},
}


def everything(shared: dict) -> str:
    return "\n".join(text for _, text, _ in sharing.strings(shared))


@pytest.mark.parametrize("piece", CUT_SHORT)
def test_a_value_cut_short_is_flagged_in_every_field(piece):
    for where, put in FIELDS.items():
        for cut in CUTS:
            shared = {**card(""), **put(f"Checked the staging API with {piece}{cut}")}
            hits = sharing.scan(shared)
            assert [hit["where"] for hit in hits] == [[where]], (where, cut, hits)
            left = sharing.apply(shared, hits, {hit["id"]: "remove" for hit in hits})
            assert piece not in everything(left), (where, cut, everything(left))
            assert "Checked the staging API with [removed]" in everything(left), (where, cut)


def test_qas_own_title():
    shared = {**card(""), "title": "Check the staging API: token ghp_Ab12Cd34Ef56Gh78Ij90Kl12Mn3"}
    [hit] = sharing.scan(shared)
    assert hit["label"] == "a GitHub token" and hit["where"] == ["Title"]


@pytest.mark.parametrize(
    "text",
    [
        "Tokens begin with ghp_ and hold 36 characters more.",
        "Set npm_config_registry before npm ci.",
        "It reads sk-learn style names and AKIA prefixes.",
        "Keys start with AKIA\u2026",
        "Use the Bearer scheme\u2026",
        "Cut short as it was written\u2026",
    ],
)
def test_words_about_tokens_are_no_hit_even_at_a_cut(text):
    for put in FIELDS.values():
        assert sharing.scan({**card(""), **put(text)}) == [], text


def view(**fields) -> dict:
    return {"title": "Task", "status": "review", "result": "", "receipts": {}, **fields}


@pytest.mark.parametrize("value", [GH, LINK, "xo" + "xp-" + "1234567890-abcdefghijklmnop"])
def test_a_shares_own_cuts_never_cut_through_a_value(value):
    title = "a " * 145 + value
    pr = {"label": "acme/api#7", "url": "https://github.com/acme/api/pull/7"}
    shared = sharing.snapshot(
        view(
            title=title,
            result="c" * (sharing.RESULT_CHARS - 11) + " " + value + " and on",
            receipts={"prs": [{**pr, "title": "b " * 95 + value}]},
        )
    )
    assert shared["title"] == ("a " * 145).rstrip()
    assert shared["prs"][0]["title"] == ("b " * 95).rstrip()
    assert shared["result"] == "c" * (sharing.RESULT_CHARS - 11) + f"\n\n{sharing.CUT_NOTE}"
    assert value[:8] not in everything(shared)
    # Not through one, it cuts where it did.
    shared = sharing.snapshot(view(title="a " * 200 + value, result="d" * 50_000))
    assert shared["title"] == ("a " * 200)[: sharing.TITLE_CHARS].rstrip()
    assert shared["result"] == "d" * sharing.RESULT_CHARS + f"\n\n{sharing.CUT_NOTE}"


def test_a_shares_cut_at_a_line_never_cuts_through_a_key():
    key = block("RSA ", 4)
    result = "e" * (sharing.RESULT_CHARS - 150) + "\n" + key
    shared = sharing.snapshot(view(result=result))
    assert shared["result"] == "e" * (sharing.RESULT_CHARS - 150) + f"\n\n{sharing.CUT_NOTE}"
    assert not any(line in shared["result"] for line in key.splitlines())
