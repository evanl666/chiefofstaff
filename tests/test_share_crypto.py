"""Share links, format 1 (sharing.py; site/s/sharecrypto.js is the viewer's side): sealed and
opened, the same bytes as WebCrypto makes, and refused whole when anything is changed, cut,
another owner's, too large or a later format. Links parse back to their gist, key and hint."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chiefofstaff import sharing

VECTOR = json.loads((Path(__file__).parent / "share_vector.json").read_text())
KEY = sharing.b64u(bytes(VECTOR["key_bytes"]))
OWNER = VECTOR["owner_id"]
PAYLOAD = VECTOR["payload"]
GIST = "6f1c0d2e9a8b47c3b5e2f0a1d4c7e9b2"


def test_a_card_sealed_opens_again_unchanged():
    blob, key = sharing.seal(PAYLOAD, OWNER)
    assert sharing.open_payload(blob, key, OWNER) == PAYLOAD
    again, other = sharing.seal(PAYLOAD, OWNER)
    assert (again, other) != (blob, key), "a fresh key and nonce for every link"
    assert len(key) == 43 and sharing.unb64u(blob)[0] == sharing.VERSION


def test_the_viewers_webcrypto_and_chief_make_the_same_bytes():
    """The known-answer vector was sealed by WebCrypto (sharecrypto.js sealWith): Python's
    `cryptography` makes the very same blob from the same key and nonce, and opens it."""
    key, nonce = bytes(VECTOR["key_bytes"]), bytes(VECTOR["nonce_bytes"])
    blob, _ = sharing.seal_with(PAYLOAD, OWNER, key, nonce)
    assert blob == VECTOR["blob"]
    assert sharing.open_payload(VECTOR["blob"], KEY, OWNER) == PAYLOAD


def flipped(blob: str, at: int) -> str:
    raw = bytearray(sharing.unb64u(blob))
    raw[at] ^= 1
    return sharing.b64u(bytes(raw))


@pytest.mark.parametrize(
    ("region", "at"),
    [("nonce", 1), ("nonce end", 12), ("ciphertext", 20), ("tag", -1), ("tag start", -16)],
)
def test_any_changed_bit_is_refused_as_damaged(region, at):
    blob = VECTOR["blob"]
    with pytest.raises(sharing.DamagedError):
        sharing.open_payload(flipped(blob, at), KEY, OWNER)


def test_a_wrong_key_another_owner_or_a_cut_blob_is_damaged():
    blob, key = VECTOR["blob"], KEY
    wrong = key[:-2] + ("BA" if key.endswith("AA") else "AA")
    for args in (
        (blob, wrong, OWNER),
        (blob, key, OWNER + 1),  # A copy in another account's gist: not theirs to pass off.
        (blob[: len(blob) // 2], key, OWNER),
        (blob[:30], key, OWNER),
        ("", key, OWNER),
        (blob, key[:-1], OWNER),
    ):
        with pytest.raises(sharing.DamagedError):
            sharing.open_payload(*args)


def test_a_version_byte_below_or_above_ours():
    raw = bytearray(sharing.unb64u(VECTOR["blob"]))
    raw[0] = 2
    with pytest.raises(sharing.NewerError):
        sharing.open_payload(sharing.b64u(bytes(raw)), KEY, OWNER)
    raw[0] = 0
    with pytest.raises(sharing.DamagedError):
        sharing.open_payload(sharing.b64u(bytes(raw)), KEY, OWNER)


@pytest.mark.parametrize("bad", ["abc=", "ab+c", "ab/c", "a", "ab c"])
def test_only_unpadded_base64url(bad):
    with pytest.raises(sharing.DamagedError):
        sharing.unb64u(bad)


def test_a_payload_over_64_kib_is_refused_and_a_card_is_cut_to_fit():
    big = {**PAYLOAD, "card": {**PAYLOAD["card"], "result": "x" * (sharing.MAX_PLAIN + 1)}}
    with pytest.raises(ValueError, match="at most 65536"):
        sharing.seal(big, OWNER)
    # Three bytes a letter: 40,000 characters are 120 KB.
    wide = {**PAYLOAD["card"], "result": "中" * sharing.RESULT_CHARS}
    fitted, cut = sharing.fit(wide, {key: v for key, v in PAYLOAD.items() if key != "card"})
    assert cut and len(sharing.encode(fitted)) <= sharing.MAX_PLAIN
    assert fitted["card"]["result"].endswith(sharing.CUT_NOTE)
    sharing.seal(fitted, OWNER)


def test_not_a_card_is_damaged():
    for payload in ({**PAYLOAD, "v": 2}, {**PAYLOAD, "kind": "chat"}):
        blob, key = sharing.seal(payload, OWNER)
        with pytest.raises(sharing.DamagedError):
            sharing.open_payload(blob, key, OWNER)


def test_a_link_parses_back_to_its_gist_key_and_hint():
    _, key = sharing.seal(PAYLOAD, OWNER)
    link = sharing.make_link(GIST, key, PAYLOAD["expires_at"])
    assert link.startswith("https://evanl666.github.io/chiefofstaff/s/#1.")
    assert len(link) < 140
    parsed = sharing.parse_link(link)
    assert parsed == {
        "gist_id": GIST,
        "key": key,
        "expires_hint": datetime(2026, 11, 29, 22, 5, tzinfo=UTC).isoformat(),
    }
    assert sharing.parse_link(link[link.index("#") :]) == parsed, "the fragment alone"
    assert sharing.parse_link(f"  {link}\n") == parsed
    assert sharing.parse_link(link[:-30]) is None, "cut"
    assert sharing.parse_link("https://evanl666.github.io/chiefofstaff/s/#1.zz.short") is None
    assert sharing.parse_link(f"#2.{GIST}.{key}") == {"newer": True}
    assert sharing.parse_link(f"#0.{GIST}.{key}") is None
    assert sharing.parse_link("") is None


def test_a_link_alone_is_a_share_link_and_nothing_else_is():
    _, key = sharing.seal(PAYLOAD, OWNER)
    link = sharing.make_link(GIST, key, PAYLOAD["expires_at"])
    assert sharing.link_alone(f"  {link} ") == link
    assert sharing.link_alone(f"send this to Maya: {link}") == ""
    assert sharing.link_alone(f"{link} {link}") == ""
    assert sharing.link_alone(link.replace("evanl666.github.io", "example.com")) == ""


def test_a_payload_is_checked_field_by_field_before_anything_is_shown():
    clean = sharing.clean_payload({**PAYLOAD, "extra": "dropped"})
    assert "extra" not in clean and clean["card"]["title"] == PAYLOAD["card"]["title"]
    card = PAYLOAD["card"]
    for bad in (
        {**card, "title": 5},
        {**card, "title": "x" * 301},
        {**card, "status": "failed"},
        {**card, "criteria": [{"text": "x", "state": "pwned"}]},
        {**card, "checks": [{"label": "rm -rf"}]},
        {**card, "prs": [{"label": "a/b#1", "url": "javascript:alert(1)"}]},
        {**card, "prs": [{"label": "a/b#1", "url": "https://evil.example/a/b/pull/1"}]},
        {**card, "changes": {"files": -1}},
        {**card, "changes": {"files": True}},
        {**card, "at": "<b>"},
        {**card, "flags": "one"},
        [],
    ):
        with pytest.raises(sharing.DamagedError):
            sharing.clean_card(bad)
    with pytest.raises(sharing.DamagedError):
        sharing.clean_payload({**PAYLOAD, "expires_at": "soon"})


def test_expiry_is_the_payloads_own_and_fails_closed():
    now = datetime(2026, 11, 25, 1, 56, tzinfo=UTC)
    assert not sharing.expired(PAYLOAD, now)
    assert sharing.expired(PAYLOAD, datetime(2026, 11, 29, 22, 5, tzinfo=UTC)), "at it"
    assert sharing.expired(PAYLOAD, datetime(2026, 11, 29, 22, 5, 1, tzinfo=UTC)), "after it"
    assert sharing.expired({**PAYLOAD, "expires_at": "2026-11-29T22:05:00"}, now), "naive"
    assert sharing.expired({key: v for key, v in PAYLOAD.items() if key != "expires_at"}, now)
    assert sharing.expired({**PAYLOAD, "expires_at": "next week"}, now)
    assert sharing.expired({**PAYLOAD, "expires_at": 1795000000}, now)
    later = (now + timedelta(days=1)).isoformat()
    assert not sharing.expired({**PAYLOAD, "expires_at": later}, now)
