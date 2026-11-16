"""Sandboxed workers' caches (caches.py): what fills them, what their shell is given."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from chiefofstaff import caches, safefs

# The real functions (tests' autouse fixture stubs them for every other test).
SEED = caches.seed
REAL_OLD_UV = caches.old_uv


def clones(tmp_path: Path) -> bool:
    """Whether this file system clones (APFS; Btrfs or XFS with reflinks)."""
    source = tmp_path / "probe"
    source.write_text("x")
    try:
        with safefs.open_folder(tmp_path, tmp_path) as folder:
            return folder.clone(source, "probe-clone")
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

    def fill(source, target, depth):
        with safefs.open_folder(target, tmp_path, create=True) as folder:
            return caches.fill(source, folder, depth)

    assert fill(owner / "archive-v0", tier, 1) == 1, "abc whole; def is the tier's own"
    assert (tier / "abc" / "pkg" / "mod.py").read_text() == "owner's"
    assert not (tier / "def" / "x").exists() and (tier / "def" / "mine").exists()
    assert not (tier / ".lock").exists()
    assert not [p for p in tier.iterdir() if p.name.startswith(".cos-seed-")]
    (tier / "abc" / "pkg" / "mod.py").write_text("changed in the tier")
    assert (owner / "archive-v0" / "abc" / "pkg" / "mod.py").read_text() == "owner's", (
        "a copy: what a worker writes never reaches the owner's"
    )
    assert fill(owner / "archive-v0", tier, 1) == 0, "nothing again"

    files = tmp_path / "npm"
    (files / "index-v5" / "aa").mkdir(parents=True)
    (files / "index-v5" / "aa" / "bucket").write_text("index")
    assert fill(files, tmp_path / "tier-npm", 0) == 1
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
    assert "npm_config_update_notifier=false" in found, "no version checks at every install"
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


def test_the_owners_own_configuration_says_where_their_caches_are(tmp_path, monkeypatch):
    """Copied from where the owner's tools keep their caches, and Maven's and Go's read through
    there, as their configuration in home says (npm's cache and pnpm's store-dir in ~/.npmrc,
    Yarn's globalFolder, pip's and uv's cache-dir, Maven's localRepository) and `go env` does."""
    monkeypatch.setattr(sys, "platform", "darwin")
    for name in (
        "npm_config_cache", "NPM_CONFIG_CACHE", "npm_config_store_dir", "NPM_CONFIG_USERCONFIG",
        "npm_config_userconfig", "YARN_GLOBAL_FOLDER", "PIP_CACHE_DIR", "PIP_CONFIG_FILE",
        "UV_CACHE_DIR", "UV_CONFIG_FILE", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "GOMODCACHE",
        "GOCACHE", "GOPATH",
    ):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    home, moved = tmp_path / "home", tmp_path / "moved"
    files = {
        ".npmrc": f"; mine\ncache={moved}/npm\nstore-dir = ~/stores/pnpm\n",
        ".yarnrc.yml": f"globalFolder: '{moved}/yarn'\nnpmScopes:\n  cacheFolder: /nope\n",
        "Library/Application Support/pip/pip.conf": (
            f"[install]\ncache-dir=/no\n[global]\ncache-dir = {moved}/pip\n"
        ),
        ".config/uv/uv.toml": f'cache-dir = "{moved}/uv"\n',
        ".m2/settings.xml": "<settings><!-- <localRepository>/nope</localRepository> -->"
        "<localRepository>${user.home}/repo</localRepository></settings>",
    }  # fmt: skip
    for name, text in files.items():
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(text)
    (home / "repo").mkdir()
    owner = caches.Owner(home, go_modcache=str(moved / "gomod"), go_cache=str(moved / "gobuild"))
    assert owner.npm() == moved / "npm"
    assert owner.pnpm_store() == home / "stores" / "pnpm"
    assert owner.yarn() == moved / "yarn" / "cache"
    assert owner.pip() == moved / "pip"
    assert owner.uv() == moved / "uv"
    assert owner.maven_repository() == home / "repo"
    assert (owner.go_modules(), owner.go_build()) == (moved / "gomod", moved / "gobuild")
    sources = {source for _, source, _, _ in caches.seeds(owner)}
    for source in (moved / "npm" / "_cacache", home / "stores" / "pnpm", moved / "pip"):
        assert source in sources, source
    text = caches.script(tmp_path / "tier", owner, tmp_path / "ws", tmp_path / "t", True, "", None)
    assert f"-Dmaven.repo.local.tail={home / 'repo'}" in text
    assert f"GOPROXY=file://{moved / 'gomod' / 'cache' / 'download'}," in text
    assert caches.Owner(tmp_path / "other").npm() == tmp_path / "other" / ".npm", "or where it is"


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
    with caches.tier_folder(tier) as folder:
        caches.link_browsers(folder, owner)
        caches.link_settings(folder, owner)
    assert (tier / "ms-playwright" / "chromium-1255").resolve() == browser.resolve()
    assert (tier / "gradle" / "gradle.properties").read_text().endswith("corp")
    (tier / "gradle" / "gradle.properties").unlink()
    (tier / "gradle" / "gradle.properties").symlink_to(tmp_path)
    with caches.tier_folder(tier) as folder:
        caches.link_settings(folder, owner)
    assert (tier / "gradle" / "gradle.properties").resolve() == (
        home / ".gradle" / "gradle.properties"
    ).resolve(), "pointed elsewhere by a worker: put back"
    assert [str(home / ".gradle" / "gradle.properties")] == [
        str(source) for source, _ in caches.owner_settings(tier, owner)
    ]


def test_a_workers_temporary_folder_is_made_only_as_its_own(tmp_path, monkeypatch):
    tier = tmp_path / "chief" / "workers"
    folder = caches.worker_temp("12345678-abcd", tier)
    assert folder.parent.parent == tier.parent, "beside the tier, where no worker writes"
    assert caches.make_temp(folder) and caches.make_temp(folder), "made, then found as made"
    assert folder.stat().st_mode & 0o777 == 0o700
    (folder / "jest-cache").mkdir()
    assert caches.make_temp(folder) and (folder / "jest-cache").is_dir(), "its own, kept"
    folder.chmod(0o755)
    assert caches.make_temp(folder) and folder.stat().st_mode & 0o777 == 0o700
    # The worker (none other can write there) puts a link in its place: never followed.
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    (folder / "jest-cache").rmdir()
    folder.rmdir()
    folder.symlink_to(victim)
    assert caches.make_temp(folder) and folder.is_dir() and not folder.is_symlink()
    assert victim.stat().st_mode & 0o777 == 0o755 and (victim / "keep").exists()
    folder.rmdir()
    folder.symlink_to(victim)
    caches.remove_temp("12345678-abcd", tier)
    assert not os.path.lexists(folder) and (victim / "keep").exists(), "the link alone goes"
    monkeypatch.undo()
    real = caches.worker_temp("0123456789abcdef", caches.default_tier())
    inside = real.relative_to(Path.home())
    assert len(f"/Users/someone/{inside}") <= 80, "room for a socket's name"
    assert inside.parts[-2:] == ("temp", "01234567") and "chiefofstaff" in inside.parts, (
        "not the system's temporary folder, where every worker writes"
    )


def test_chief_of_staff_never_writes_where_a_link_a_worker_made_leads(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    for name in ("PLAYWRIGHT_BROWSERS_PATH", "GRADLE_USER_HOME", "CARGO_HOME"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    shell = home / "Library" / "Caches" / "ms-playwright" / "chromium_headless_shell-1"
    (shell / "chrome-headless-shell-mac-arm64").mkdir(parents=True)
    program = shell / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell"
    program.write_bytes(b"\0" * 8192)
    (shell / "INSTALLATION_COMPLETE").write_text("")
    (home / ".gradle").mkdir()
    (home / ".gradle" / "gradle.properties").write_text("x=1")
    owner, tier = caches.Owner(home), tmp_path / "tier"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # A worker links folders of the tier to the owner's browsers and to another folder.
    (tier / "ms-playwright").mkdir(parents=True)
    (tier / "ms-playwright" / "chromium_headless_shell-2").symlink_to(shell)
    (tier / "gradle").symlink_to(elsewhere)
    with caches.tier_folder(tier) as folder:
        caches.link_browsers(folder, owner)
        caches.link_settings(folder, owner)
    assert program.stat().st_size == 8192 and not program.with_name("x.real").exists()
    assert not list(shell.rglob("*.real")), "the owner's program is never moved"
    assert not list(elsewhere.iterdir()), "nothing written where a link leads"
    (tier / "npm").symlink_to(elsewhere)
    (home / ".npm" / "_cacache").mkdir(parents=True)
    (home / ".npm" / "_cacache" / "blob").write_text("x")
    SEED(tier, owner)
    assert not list(elsewhere.iterdir()), "nor filled"
    # The tier itself, replaced by a link to one of the owner's folders: the link goes.
    tier.rename(tmp_path / "old-tier")
    tier.symlink_to(elsewhere)
    with caches.tier_folder(tier) as folder:
        caches.link_settings(folder, owner)
    assert tier.is_dir() and not tier.is_symlink() and not list(elsewhere.iterdir())


def test_an_old_uv_is_named_for_workers(tmp_path, monkeypatch):
    from chiefofstaff import prompts

    monkeypatch.setattr(sys, "platform", "darwin")
    for version, old in (("0.9.21", "0.9.21"), ("0.9.29", ""), ("0.13.8", ""), ("bad", "")):
        uv = tmp_path / version / "uv"
        uv.parent.mkdir()
        uv.write_text(f"#!/bin/sh\necho 'uv {version} (Homebrew 2025-12-30)'\n")
        uv.chmod(0o755)
        monkeypatch.setenv("PATH", str(uv.parent))
        assert REAL_OLD_UV() == old, version
    text = prompts.worker_context("/w", "/w", [], True, "on", caches=True, old_uv="0.9.21")
    assert "This machine's uv (0.9.21) crashes in this sandbox" in text
    assert "crashes in this sandbox" not in prompts.worker_context("/w", "/w", [], True, "on")
