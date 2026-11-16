"""Sandboxed workers' caches (caches.py): what fills them, what their shell is given."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from chiefofstaff import caches

# The real functions (tests' autouse fixture stubs them for every other test).
SEED = caches.seed


def clones(tmp_path: Path) -> bool:
    """Whether this file system clones (APFS; Btrfs or XFS with reflinks)."""
    source, target = tmp_path / "probe", tmp_path / "probe-clone"
    source.write_text("x")
    try:
        return caches.clone(source, target)
    except caches.CannotCloneError:
        return False


def test_the_tier_gets_what_it_lacks_whole_never_over_what_it_has(tmp_path):
    if not clones(tmp_path):
        pytest.skip("this file system cannot clone")
    owner = tmp_path / "owner"
    (owner / "archive-v0" / "abc" / "pkg").mkdir(parents=True)
    (owner / "archive-v0" / "abc" / "pkg" / "mod.py").write_text("owner's")
    (owner / "archive-v0" / "def").mkdir()
    (owner / "archive-v0" / "def" / "x").write_text("x")
    (owner / "archive-v0" / ".lock").write_text("")
    tier = tmp_path / "tier"
    (tier / "def").mkdir(parents=True)
    (tier / "def" / "mine").write_text("a worker's")

    assert caches.fill(owner / "archive-v0", tier, 1) == 1, "abc whole; def is the tier's own"
    assert (tier / "abc" / "pkg" / "mod.py").read_text() == "owner's"
    assert not (tier / "def" / "x").exists() and (tier / "def" / "mine").exists()
    assert not (tier / ".lock").exists()
    assert not [p for p in tier.iterdir() if p.name.startswith(".cos-seed-")]
    (tier / "abc" / "pkg" / "mod.py").write_text("changed in the tier")
    assert (owner / "archive-v0" / "abc" / "pkg" / "mod.py").read_text() == "owner's", (
        "a copy: what a worker writes never reaches the owner's"
    )
    assert caches.fill(owner / "archive-v0", tier, 1) == 0, "nothing again"

    files = tmp_path / "npm"
    (files / "index-v5" / "aa").mkdir(parents=True)
    (files / "index-v5" / "aa" / "bucket").write_text("index")
    assert caches.fill(files, tmp_path / "tier-npm", 0) == 1
    assert (tmp_path / "tier-npm" / "index-v5" / "aa" / "bucket").read_text() == "index"


def test_seeding_fills_each_tool_from_where_the_owner_keeps_it(tmp_path, monkeypatch):
    if not clones(tmp_path):
        pytest.skip("this file system cannot clone")
    for name in ("npm_config_cache", "UV_CACHE_DIR", "XDG_CACHE_HOME", "GRADLE_USER_HOME"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    (home / ".npm" / "_cacache" / "content-v2").mkdir(parents=True)
    (home / ".npm" / "_cacache" / "content-v2" / "blob").write_text("tarball")
    dist = home / ".gradle" / "wrapper" / "dists" / "gradle-9-bin" / "hash"
    dist.mkdir(parents=True)
    (dist / "gradle-9.ok").write_text("")
    timings = SEED(tmp_path / "tier", caches.Owner(home))
    assert (tmp_path / "tier" / "npm" / "_cacache" / "content-v2" / "blob").exists()
    assert (tmp_path / "tier" / "gradle" / "wrapper" / "dists" / "gradle-9-bin" / "hash").is_dir()
    assert timings["npm_copied"] == 1 and "total" in timings


def test_a_workers_init_script_starts_no_process_and_points_java_at_the_proxy(tmp_path):
    owner = caches.Owner(tmp_path / "home")
    text = caches.script(
        tmp_path / "tier", owner, tmp_path / "ws", tmp_path / "tmp", True, "", tmp_path / "ts.p12"
    )
    for line in text.splitlines():
        assert line.startswith(("#", "export ", "_cos_p=", "if ", "  ", "fi", "unset ", "[ -s ")), (
            f"only assignments and tests: {line}"
        )
    found = subprocess.run(
        ["bash", "--norc", "--noprofile", "-c", f". /dev/stdin <<'EOF'\n{text}EOF\nenv"],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"], "HTTPS_PROXY": "http://127.0.0.1:54313"},
    ).stdout
    assert f"npm_config_cache={tmp_path / 'tier' / 'npm'}" in found
    assert "-Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=54313" in found
    assert "javax.net.ssl.trustStore" not in found, "no truststore made: none named"
    assert f"GOPROXY=file://{tmp_path / 'home' / 'go' / 'pkg' / 'mod' / 'cache' / 'download'}," in (
        found
    )
    assert f"TMPDIR={tmp_path / 'tmp'}/" in found
    plain = caches.script(
        tmp_path / "tier", owner, tmp_path / "ws", tmp_path / "t", False, "", None
    )
    assert "GRADLE_RO_DEP_CACHE" not in plain and "GOPROXY" not in plain, "nothing read through"


def test_the_owners_browsers_and_settings_are_linked_not_copied(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    for name in ("PLAYWRIGHT_BROWSERS_PATH", "GRADLE_USER_HOME", "CARGO_HOME"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    browser = home / "Library" / "Caches" / "ms-playwright" / "chromium-1255"
    browser.mkdir(parents=True)
    (home / ".gradle").mkdir()
    (home / ".gradle" / "gradle.properties").write_text("systemProp.https.proxyHost=corp")
    owner, tier = caches.Owner(home), tmp_path / "tier"
    caches.link_browsers(tier, owner)
    caches.link_settings(tier, owner)
    assert (tier / "ms-playwright" / "chromium-1255").resolve() == browser.resolve()
    assert (tier / "gradle" / "gradle.properties").read_text().endswith("corp")
    (tier / "gradle" / "gradle.properties").unlink()
    (tier / "gradle" / "gradle.properties").symlink_to(tmp_path)
    caches.link_settings(tier, owner)
    assert (tier / "gradle" / "gradle.properties").resolve() == (
        home / ".gradle" / "gradle.properties"
    ).resolve(), "pointed elsewhere by a worker: put back"
    assert [str(home / ".gradle" / "gradle.properties")] == [
        str(source) for source, _ in caches.owner_settings(tier, owner)
    ]


def test_a_workers_temporary_folder_is_made_only_as_its_own(tmp_path):
    folder = tmp_path / "cos-12345678"
    assert caches.make_temp(folder) and caches.make_temp(folder), "made, then found as made"
    assert folder.stat().st_mode & 0o777 == 0o700
    planted = tmp_path / "cos-planted"
    planted.symlink_to(tmp_path)
    assert not caches.make_temp(planted), "a link someone put there is not taken"
    assert len(str(caches.worker_temp("0123456789abcdef"))) <= 80, "room for a socket's name"
