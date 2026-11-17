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
REAL_RUST = caches.rust_project

# Every variable Owner (and the script) reads: a developer's own (GOPATH, PIP_CACHE_DIR…) never
# changes what these tests find. XDG_CONFIG_HOME stays the tests' own home's.
OWNER_VARIABLES = (
    "npm_config_cache", "NPM_CONFIG_CACHE", "npm_config_store_dir", "NPM_CONFIG_STORE_DIR",
    "NPM_CONFIG_USERCONFIG", "npm_config_userconfig", "YARN_GLOBAL_FOLDER", "YARN_CACHE_FOLDER",
    "BUN_INSTALL_CACHE_DIR", "PIP_CACHE_DIR", "PIP_CONFIG_FILE", "UV_CACHE_DIR", "UV_CONFIG_FILE",
    "UV_PYTHON_INSTALL_DIR", "UV_TOOL_DIR", "POETRY_CACHE_DIR", "PRE_COMMIT_HOME", "COREPACK_HOME",
    "XDG_CACHE_HOME", "XDG_DATA_HOME", "GRADLE_USER_HOME", "CARGO_HOME", "GOMODCACHE", "GOCACHE",
    "GOPATH", "PLAYWRIGHT_BROWSERS_PATH", "electron_config_cache", "DENO_DIR", "CCACHE_DIR",
    "SCCACHE_DIR", "NUGET_PACKAGES", "BUNDLE_USER_CACHE", "PNPM_HOME",
)  # fmt: skip


@pytest.fixture(autouse=True)
def owners_variables_cleared(monkeypatch):
    for name in OWNER_VARIABLES:
        monkeypatch.delenv(name, raising=False)


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
        assert line.startswith(
            ("#", "export ", "_cos_p=", "if ", "  ", "fi", "unset ", "[ -s ", "true")
        ), f"only assignments and tests: {line}"
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
    tier = tmp_path / "tier"
    assert f"GOPATH={tier / 'go'}" in found, "Go's checksum database state, written at go get"
    assert f"GOMODCACHE={tier / 'go' / 'mod'}" in found
    assert "GOBIN" not in found, "go install's default, GOPATH's bin (a GOBIN set fails cross)"
    owned = caches.script(tier, owner, tmp_path / "ws", tmp_path / "t", True, "", None,
                          gobin=True)  # fmt: skip
    assert f"GOBIN={tier / 'go' / 'bin'}" in owned, "the owner's GOBIN (go env -w) is read-only"
    assert f"PNPM_HOME={tier / 'pnpm'}" in found
    assert "npm_config_store_dir" not in found, "npm 11.5 and later warn about it at every command"
    stale = caches.script(tier, owner, tmp_path / "ws", tmp_path / "t", True, "", tmp_path / "no")
    status = subprocess.run(
        ["bash", "--norc", "--noprofile", "-c", f". /dev/stdin <<'EOF'\n{stale}EOF\necho $?"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    assert status == "0", "a stale truststore never makes the script fail (Copilot reports it)"
    (tmp_path / "home" / ".npmrc").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / ".npmrc").write_text("store-dir=/elsewhere/pnpm-store\n")
    named = caches.script(tier, owner, tmp_path / "ws", tmp_path / "t", True, "", None)
    assert f"npm_config_store_dir={tier / 'pnpm' / 'store'}" in named, (
        "the owner's ~/.npmrc names a store, which would win over PNPM_HOME"
    )
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
    (home / ".gradle" / "init.gradle").write_text("allprojects {}")
    (home / ".cargo").mkdir()
    (home / ".cargo" / "credentials.toml").write_text("[registries.corp]\ntoken = 'x'")
    owner, tier = caches.Owner(home), tmp_path / "tier"
    with caches.tier_folder(tier) as folder:
        caches.link_browsers(folder, owner)
        caches.link_settings(folder, owner)
    assert (tier / "ms-playwright" / "chromium-1255").resolve() == browser.resolve()
    assert (tier / "gradle" / "gradle.properties").read_text().endswith("corp")
    assert (tier / "gradle" / "init.gradle").read_text() == "allprojects {}"
    assert (tier / "cargo" / "credentials.toml").resolve() == (
        home / ".cargo" / "credentials.toml"
    ).resolve(), "a private registry's token, as the owner's builds have it"
    (tier / "gradle" / "gradle.properties").unlink()
    (tier / "gradle" / "gradle.properties").symlink_to(tmp_path)
    with caches.tier_folder(tier) as folder:
        caches.link_settings(folder, owner)
    assert (tier / "gradle" / "gradle.properties").resolve() == (
        home / ".gradle" / "gradle.properties"
    ).resolve(), "pointed elsewhere by a worker: put back"
    assert [str(source) for source, _ in caches.owner_settings(tier, owner)] == [
        str(home / ".gradle" / "gradle.properties"),
        str(home / ".cargo" / "credentials.toml"),
        str(home / ".gradle" / "init.gradle"),
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


def test_two_workers_readied_together_both_get_their_caches(tmp_path, monkeypatch):
    """Two workers opening at once raced for the same links, and the loser's init script was
    never written (its installs then failed against the owner's read-only caches)."""
    import threading

    monkeypatch.setattr(sys, "platform", "darwin")
    home = tmp_path / "home"
    browsers = home / "Library" / "Caches" / "ms-playwright"
    for name in ("chromium-1", "firefox-2", "webkit-3"):
        (browsers / name).mkdir(parents=True)
    tier = tmp_path / "chief" / "workers"
    shell = tier / "ms-playwright" / "chromium_headless_shell-9" / "chrome-mac"
    shell.mkdir(parents=True)
    (shell / "chrome-headless-shell").write_bytes(b"\0" * 8192)
    (shell.parent / "INSTALLATION_COMPLETE").write_text("")
    scripts = tmp_path / "data" / "worker-shell"
    found, failures = {}, []

    def ready(session: str):
        try:
            plan = caches.plan(
                tier, tmp_path / "ws" / session, session, scripts, slack=False,
                read_through=True, home=home,
            )  # fmt: skip
            found[session] = (caches.prepare(plan, session, scripts, home), plan)
        except Exception as exc:
            failures.append(exc)

    for _ in range(10):
        found.clear()
        threads = [threading.Thread(target=ready, args=(f"{n:08d}-x",)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not failures, failures
        for script, plan in found.values():
            assert script and script.is_file() and plan.temporary.is_dir()

    def broken(*args):
        raise OSError("a link it could not make")

    monkeypatch.setattr(caches, "link_browsers", broken)
    plan = caches.plan(tier, tmp_path / "ws" / "w", "99999999-x", scripts, slack=False,
                       read_through=True, home=home)  # fmt: skip
    script = caches.prepare(plan, "99999999-x", scripts, home)
    assert script and script.is_file() and plan.temporary.is_dir(), "its tools' caches all the same"


def test_yarn_classic_is_seeded_where_it_looks_under_the_workers_cache_home(tmp_path, monkeypatch):
    """Yarn 1 looks in XDG_CACHE_HOME first, on macOS too (its own ~/Library/Caches/Yarn only
    without it): workers' is in the tier, so the owner's Yarn 1 cache is seeded there."""
    monkeypatch.setattr(caches, "system", lambda: "darwin")
    home = tmp_path / "home"
    (home / "Library" / "Caches" / "Yarn" / "v6" / "npm-left-pad-1.3.0").mkdir(parents=True)
    tier = tmp_path / "caches" / "workers"
    plan = caches.plan(tier, tmp_path / "ws" / "w", "12345678-x", tmp_path / "scripts",
                       slack=False, read_through=True, home=home)  # fmt: skip
    assert f"XDG_CACHE_HOME={tier / 'xdg'}" in plan.text
    SEED(tier, caches.Owner(home))
    assert (tier / "xdg" / "yarn" / "v6" / "npm-left-pad-1.3.0").is_dir()


def test_seeding_goes_on_past_a_cache_it_cannot_clone_and_copies_within_a_budget(
    tmp_path, monkeypatch
):
    """On a file system without clones (ext4) or another volume, seeding stopped at the first
    tool: every cache was cold. Each tool now goes on its own, copied (not cloned) within a
    budget, and the caches the init script moves (NuGet's, Electron's, deno's…) are filled too."""
    home = tmp_path / "home"
    for path in (".npm/_cacache/index-v5/aa/bucket", ".nuget/packages/newtonsoft/13.0.1/x.nupkg"):
        (home / path).parent.mkdir(parents=True, exist_ok=True)
        (home / path).write_text("x" * 100)
    (home / "Library" / "Caches" / "electron").mkdir(parents=True)
    (home / "Library" / "Caches" / "electron" / "electron-v30.zip").write_text("zip")
    monkeypatch.setattr(sys, "platform", "darwin")

    def cannot(self, source, name):
        raise safefs.CannotCloneError(18, "Cross-device link")

    monkeypatch.setattr(safefs.Folder, "clone", cannot)
    tier = tmp_path / "chief" / "workers"
    timings = SEED(tier, caches.Owner(home))
    assert (tier / "npm" / "_cacache" / "index-v5" / "aa" / "bucket").read_text() == "x" * 100
    assert (tier / "nuget" / "packages" / "newtonsoft" / "13.0.1" / "x.nupkg").exists()
    assert (tier / "electron" / "electron-v30.zip").read_text() == "zip"
    assert timings["copied_bytes"] == 203
    monkeypatch.setattr(caches, "COPY_BUDGET", 203)
    (home / ".npm" / "_cacache" / "index-v5" / "bb").mkdir()
    (home / ".npm" / "_cacache" / "index-v5" / "bb" / "more").write_text("y")
    timings = SEED(tier, caches.Owner(home))
    assert not (tier / "npm" / "_cacache" / "index-v5" / "bb" / "more").exists(), "over budget"
    assert "npm_not_copied" in timings


def test_seeding_again_skips_folders_that_did_not_change(tmp_path, monkeypatch):
    if not clones(tmp_path):
        pytest.skip("this file system cannot clone")
    home = tmp_path / "home"
    for name in ("aa", "bb", "cc"):
        (home / ".npm" / "_cacache" / "index-v5" / name).mkdir(parents=True)
        (home / ".npm" / "_cacache" / "index-v5" / name / "bucket").write_text(name)
    tier = tmp_path / "chief" / "workers"
    assert SEED(tier, caches.Owner(home))["npm_copied"] == 3
    looked = []
    real = caches.place

    def counted(source, into, name, budget=None):
        looked.append(name)
        return real(source, into, name, budget)

    monkeypatch.setattr(caches, "place", counted)
    (tier / "npm" / "_cacache" / "index-v5" / "aa" / "bucket").unlink()  # A worker's doing.
    SEED(tier, caches.Owner(home))
    assert looked == [], "nothing changed in the owner's: not looked at again"
    (home / ".npm" / "_cacache" / "index-v5" / "bb" / "new").write_text("new")
    SEED(tier, caches.Owner(home))
    assert looked == ["new"], "only what changed"


def test_rust_projects_are_found_deep_but_never_in_node_modules_or_hidden_folders(tmp_path):
    root = tmp_path / "repo"
    (root / "node_modules" / "native" / "Cargo.toml").parent.mkdir(parents=True)
    (root / "node_modules" / "native" / "Cargo.toml").write_text("")
    (root / ".cache" / "Cargo.toml").parent.mkdir(parents=True)
    (root / ".cache" / "Cargo.toml").write_text("")
    assert not REAL_RUST([root])
    deep = root / "python" / "packages" / "fast" / "rust"
    deep.mkdir(parents=True)
    (deep / "Cargo.toml").write_text("")
    assert REAL_RUST([root]), "a maturin package four folders down"


def test_workers_caches_can_be_cleared_even_go_s_read_only_modules(tmp_path):
    """Go leaves its module cache read-only (a plain rm fails); a link a worker put there leads
    nowhere it is followed. Made again as the next worker starts."""
    tier = tmp_path / "chief" / "workers"
    module = tier / "go" / "mod" / "example.com" / "x@v1.0.0"
    module.mkdir(parents=True)
    (module / "x.go").write_text("package x")
    (module / "x.go").chmod(0o444)
    module.chmod(0o555)
    victim = tmp_path / "home"
    victim.mkdir()
    (victim / "keep").write_text("the owner's")
    (tier / "npm").symlink_to(victim)
    (tier.parent / caches.SEEDED).mkdir()
    (tier.parent / caches.SEEDED / "state.json").write_text('{"copied": 5}')
    caches.clear(tier, caches.Owner(tmp_path / "nobody"))
    assert list(tier.iterdir()) == [tier / "ms-playwright"] and (victim / "keep").exists()
    assert caches.copied_bytes(tier) == 0, "seeding fills it all again"
    assert not [p for p in tier.parent.iterdir() if p.name.startswith(".clearing-")]
    caches.clear(tier, caches.Owner(tmp_path / "nobody"))  # Nothing in it: nothing to do.


def test_clearing_makes_the_links_running_workers_need_again_at_once(tmp_path, monkeypatch):
    """Clear moved the whole tier aside and nothing made its links again until another worker
    started: one running lost the owner's browsers and its Gradle and Cargo settings. Made
    again at once, after any seeding pass and any worker being readied."""
    import threading

    monkeypatch.setattr(caches, "system", lambda: "linux")
    home = tmp_path / "home"
    (home / ".cache" / "ms-playwright" / "chromium-1200").mkdir(parents=True)
    (home / ".gradle").mkdir()
    (home / ".gradle" / "gradle.properties").write_text("systemProp.https.proxyHost=corp\n")
    owner = caches.Owner(home)
    tier = tmp_path / "chief" / "workers"
    (tier / "npm").mkdir(parents=True)
    (tier / "npm" / "blob").write_text("x")
    done = threading.Event()

    def clear():
        caches.clear(tier, owner)
        done.set()

    for lock in (caches.SEEDING, caches.PREPARING):
        with lock:
            thread = threading.Thread(target=clear)
            thread.start()
            assert not done.wait(0.3), "waits for it"
        thread.join(10)
        assert done.is_set()
        done.clear()
    assert not (tier / "npm").exists()
    assert (tier / "ms-playwright" / "chromium-1200").resolve() == (
        home / ".cache" / "ms-playwright" / "chromium-1200"
    )
    settings = home / ".gradle" / "gradle.properties"
    assert (tier / "gradle" / "gradle.properties").resolve() == settings


def test_read_only_paths_another_one_holds_are_left_out(tmp_path, monkeypatch):
    """Every read-only path is a rule Copilot writes and the sandbox reads at each command: one
    inside another read-only folder adds nothing (a worker's commands took ~8 ms longer each),
    unless it is in a writable folder (it carves that out) or leads elsewhere (a link)."""
    monkeypatch.setattr(caches, "temp_roots", lambda: [str(tmp_path / "temp" / "T")])
    tools, work, elsewhere = tmp_path / "tools", tmp_path / "work", tmp_path / "elsewhere"
    for folder in (tools / "go" / "bin", work / ".git" / "hooks", elsewhere):
        folder.mkdir(parents=True)
    (tools / "linked").symlink_to(elsewhere)
    readonly = [str(tools), str(tools / "go" / "bin"), str(tools / "go"), str(tools / "linked"),
                str(work / ".git" / "hooks"), str(tmp_path / "missing" / "x"), str(tools / "gone"),
                str(tmp_path / "temp" / "T" / "x")]  # fmt: skip
    kept = caches.fewest([*readonly, str(tmp_path)], [str(work)])
    assert kept == [str(tools / "linked"), str(work / ".git" / "hooks"),
                    str(tmp_path / "temp" / "T" / "x"), str(tmp_path)], (
        "all held by tmp_path, but in what is writable, and a link's own rule"
    )  # fmt: skip
    # A link in a writable folder keeps its own rule (it holds the link itself in place), even
    # where what it leads to is held.
    (work / "env").symlink_to(elsewhere)
    kept = caches.fewest([str(tmp_path / "elsewhere"), str(work / "env")], [str(work)])
    assert kept == [str(tmp_path / "elsewhere"), str(work / "env")]
    kept = caches.fewest(readonly, [str(work)])
    assert kept == [str(tools), str(tools / "linked"), str(work / ".git" / "hooks"),
                    str(tmp_path / "missing" / "x"),
                    str(tmp_path / "temp" / "T" / "x")]  # fmt: skip


def test_the_owners_go_tools_are_found_where_a_makefile_looks(tmp_path):
    """Workers' GOPATH is in the tier: a Makefile's `$(go env GOPATH)/bin/golangci-lint` found
    an empty folder. The owner's Go tools are linked there (read-only to workers on the owner's
    tasks; a tool a worker installed stays); never for a worker that reads Slack."""
    home = tmp_path / "home"
    gobin = tmp_path / "gopath" / "bin"
    gobin.mkdir(parents=True)
    (gobin / "golangci-lint").write_text("#!/bin/sh\n")
    (gobin / "stringer").write_text("#!/bin/sh\n")
    tier, scripts = tmp_path / "chief" / "workers", tmp_path / "data" / "worker-shell"
    (tier / "go" / "bin").mkdir(parents=True)
    (tier / "go" / "bin" / "stringer").write_text("a worker's own")
    go = {"GOPATH": str(tmp_path / "gopath")}
    found = caches.plan(tier, tmp_path / "ws" / "w", "11111111-a", scripts, slack=False,
                        read_through=True, go=go, home=home)  # fmt: skip
    assert str(gobin) in found.readonly
    assert caches.prepare(found, "11111111-a", scripts)
    assert (tier / "go" / "bin" / "golangci-lint").resolve() == gobin / "golangci-lint"
    assert (tier / "go" / "bin" / "stringer").read_text() == "a worker's own"
    reader = caches.plan(tier, tmp_path / "ws" / "r", "22222222-b", scripts, slack=True,
                         read_through=False, go=go, home=home)  # fmt: skip
    assert str(gobin) not in reader.readonly
    assert caches.prepare(reader, "22222222-b", scripts)
    assert not (tmp_path / "ws" / "r" / caches.PRIVATE / "go" / "bin").exists()


def test_each_workers_init_script_is_its_own_and_goes_when_it_closes(tmp_path):
    """Every worker could read every worker's init script, and none was ever deleted: an
    owner-task worker's names the owner's GOPROXY, which may hold a token, and workers that
    read Slack read it. Each is in a folder of its worker's own, the only one it reads."""
    home = tmp_path / "home"
    tier, scripts = tmp_path / "chief" / "workers", tmp_path / "data" / "worker-shell"
    go = {"GOPROXY": "https://user:KEY@corp.example/go"}
    own = caches.plan(tier, tmp_path / "ws" / "w", "11111111-a", scripts, slack=False,
                      read_through=True, go=go, home=home)  # fmt: skip
    path = caches.prepare(own, "11111111-a", scripts)
    assert path == scripts / "11111111-a" / "init.sh" and "user:KEY@" in path.read_text()
    reader = caches.plan(tier, tmp_path / "ws" / "r", "22222222-b", scripts, slack=True,
                         read_through=False, go=go, home=home)  # fmt: skip
    theirs = caches.prepare(reader, "22222222-b", scripts)
    assert theirs and "KEY" not in theirs.read_text()
    assert str(scripts / "22222222-b") in reader.readonly
    assert not [p for p in reader.readonly if p == str(scripts) or p == str(path.parent)]
    caches.remove_script(scripts, "11111111-a")
    assert not path.parent.exists() and theirs.exists()


@pytest.mark.parametrize(
    ("platform", "tmpdir", "roots"),
    [
        ("linux", None, ["/tmp", "/var/tmp", "/dev/shm"]),
        ("linux", "/srv/scratch/t", ["/srv/scratch/t", "/tmp", "/var/tmp", "/dev/shm"]),
        ("linux", "/", ["/tmp", "/var/tmp", "/dev/shm"]),
        ("darwin", "/private/var/folders/ab/cd/T", ["/private/var/folders/ab/cd"]),
        ("darwin", "/tmp", ["/private/tmp"]),
    ],
)
def test_every_temporary_folder_copilot_lets_commands_write_holds_its_carve_outs(
    platform, tmpdir, roots, monkeypatch
):
    """Copilot's sandbox lets commands write the temporary folder its own environment names
    (TMPDIR: the whole per-user folder on macOS), and on Linux /tmp as well where bubblewrap
    mounts it: Chief of Staff's code in any of them keeps its read-only rules. Never / (it
    would hold every path, and nothing would be left out)."""
    monkeypatch.setattr(caches, "system", lambda: platform)
    monkeypatch.setattr(caches.tempfile, "gettempdir", lambda: tmpdir or "/tmp")
    if tmpdir:
        monkeypatch.setenv("TMPDIR", tmpdir)
    else:
        monkeypatch.delenv("TMPDIR", raising=False)
    found = caches.temp_roots()
    assert all(os.path.realpath(root) in found for root in roots), found
    assert "/" not in found
    pinned = ["/tmp/chief/site-packages/chiefofstaff", "/nonexistent-chief-root/lib"]
    in_temp = platform == "linux" or tmpdir == "/tmp"
    assert caches.carve_outs(pinned, []) == (pinned[:1] if in_temp else [])
    kept = caches.fewest(["/nonexistent-chief-root", "/nonexistent-chief-root/go/bin"], [])
    assert kept == ["/nonexistent-chief-root"]


def test_workers_read_in_place_only_the_owners_caches_they_read_through(tmp_path, monkeypatch):
    """Each read-only path is a rule at every command, and the tier has its own copies of most
    of the owner's caches: a worker on the owner's tasks reads in place only Gradle's dependency
    cache, Maven's repository and Go's module downloads (read through), Playwright's browsers
    (linked), pre-commit's hook environments (the tier's copies run them by their paths) and
    node-gyp's headers."""
    monkeypatch.setattr(caches, "system", lambda: "darwin")
    home = tmp_path / "home"
    # Yarn 4's global cache and pnpm's store: a checkout's .pnp.cjs, or node_modules in
    # pnpm's global virtual store, loads packages from them where they are.
    read = (".gradle/caches", ".m2/repository", "go/pkg/mod", "Library/Caches/ms-playwright",
            ".cache/pre-commit", "Library/Caches/node-gyp", ".electron-gyp", ".yarn/berry/cache",
            "Library/pnpm/store")  # fmt: skip
    copied = (".npm", ".cache/uv", "Library/Caches/pip", ".gradle/wrapper", ".m2/wrapper",
              ".cargo/registry", "Library/Caches/go-build")  # fmt: skip
    for name in (*read, *copied):
        (home / name).mkdir(parents=True)
    found = caches.plan(tmp_path / "tier", tmp_path / "ws" / "w", "11111111-a", tmp_path / "s",
                        slack=False, read_through=True, home=home)  # fmt: skip
    for name in read:
        assert str(home / name) in found.readonly, name
    for name in copied:
        assert str(home / name) not in found.readonly, name
    reader = caches.plan(tmp_path / "tier", tmp_path / "ws" / "r", "22222222-b", tmp_path / "s",
                         slack=True, read_through=False, home=home)  # fmt: skip
    assert not [path for path in reader.readonly if path.startswith(str(home))], "none for Slack"


def test_what_copilots_base_profile_reads_already_takes_no_rule(tmp_path, monkeypatch):
    """Copilot's macOS profile lets every command read /usr's folders, /bin, /sbin, Homebrew's,
    /Library and /System: a rule for one of them, or a folder in them, adds nothing."""
    base = tmp_path / "opt" / "homebrew"
    monkeypatch.setattr(caches, "BASE_READABLE", (str(base),))
    monkeypatch.setattr(caches, "temp_roots", list)
    kept = caches.fewest([str(base), str(base / "bin"), str(base / "opt" / "node")], [])
    assert kept == []
    monkeypatch.setenv("PATH", f"{base / 'bin'}{os.pathsep}{tmp_path / 'tools' / 'bin'}")
    folders = caches.path_folders(tmp_path / "home")
    assert base not in folders and tmp_path / "tools" in folders


def test_node_gyp_builds_in_workers_own_folder_with_the_owners_headers(tmp_path, monkeypatch):
    """On macOS node-gyp keeps headers in ~/Library/Caches/node-gyp (env-paths, not
    XDG_CACHE_HOME), read-only to workers: a project on another Node version made node-gyp
    mkdir there, EPERM (it falls back on EACCES only), and native modules failed. Workers'
    own folder always, the owner's header versions linked into it (no download)."""
    monkeypatch.setattr(caches, "system", lambda: "darwin")
    home = tmp_path / "home"
    headers = home / "Library" / "Caches" / "node-gyp" / "22.11.0"
    (headers / "include" / "node").mkdir(parents=True)
    (headers / "installVersion").write_text("11\n")
    tier, scripts = tmp_path / "chief" / "workers", tmp_path / "data" / "worker-shell"
    found = caches.plan(tier, tmp_path / "ws" / "w", "11111111-a", scripts, slack=False,
                        read_through=True, home=home)  # fmt: skip
    assert f"export npm_config_devdir={tier / 'node-gyp'}" in found.text
    assert caches.prepare(found, "11111111-a", scripts)
    assert (tier / "node-gyp" / "22.11.0").resolve() == headers
    monkeypatch.setattr(caches, "system", lambda: "linux")
    linux = caches.plan(tier, tmp_path / "ws" / "w", "11111111-a", scripts, slack=False,
                        read_through=True, home=home)  # fmt: skip
    assert "npm_config_devdir" not in linux.text, "XDG_CACHE_HOME moves it there"


@pytest.mark.parametrize(
    ("pnpm_home", "xdg_data"),
    [("Library/Application Support/pnpm", ""), ("", "Library/Application Support/data")],
)
def test_pnpms_store_is_found_as_pnpm_finds_it_and_never_read_by_slack(
    pnpm_home, xdg_data, tmp_path, monkeypatch
):
    """pnpm looks for its store in its configured store-dir, then $PNPM_HOME/store, then
    $XDG_DATA_HOME/pnpm/store, then the platform's place; only the last was looked at, so with
    a PNPM_HOME of the owner's own (on PATH) the store beside pnpm was granted to a Slack
    reader. A child named store of a pnpm home is never granted to one."""
    monkeypatch.setattr(caches, "system", lambda: "darwin")
    for name in ("PNPM_HOME", "XDG_DATA_HOME", "npm_config_store_dir", "NPM_CONFIG_STORE_DIR"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    folder = home / pnpm_home if pnpm_home else home / xdg_data / "pnpm"
    for name in ("pnpm", "store/v10/files/00/private.js"):
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text("x")
    if pnpm_home:
        monkeypatch.setenv("PNPM_HOME", str(folder))
    if xdg_data:
        monkeypatch.setenv("XDG_DATA_HOME", str(home / xdg_data))
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}/usr/bin")
    owner = caches.Owner(home)
    assert owner.pnpm_store() == folder / "store"
    found = caches.plan(tmp_path / "tier", tmp_path / "ws" / "r", "22222222-b", tmp_path / "s",
                        slack=True, read_through=False, home=home)  # fmt: skip
    assert str(folder / "pnpm") in found.readonly
    assert not any(Path(path).is_relative_to(folder / "store") or path == str(folder)
                   for path in found.readonly), found.readonly  # fmt: skip
    # Whatever the owner's settings say of the store, never a child named store.
    monkeypatch.setenv("npm_config_store_dir", str(tmp_path / "elsewhere"))
    found = caches.plan(tmp_path / "tier", tmp_path / "ws" / "r", "22222222-b", tmp_path / "s",
                        slack=True, read_through=False, home=home)  # fmt: skip
    assert not any(Path(path).is_relative_to(folder / "store") for path in found.readonly)


def test_a_slack_reader_runs_pnpms_global_tools_and_reads_no_other_package(tmp_path, monkeypatch):
    """pnpm 11 keeps a global tool's shim in its home's bin, its install group in global/v11,
    and its packages in the store's links: a Slack reader got the shim, not what it runs. It
    reads each install group's node_modules and exactly the links entries they use (resolved
    as it opens), never the rest of the store; past a limit (each is a rule at every
    command), none, and its prompt says so."""
    monkeypatch.setattr(caches, "system", lambda: "darwin")
    for name in ("PNPM_HOME", "XDG_DATA_HOME", "npm_config_store_dir", "NPM_CONFIG_STORE_DIR"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    pnpm = home / "Library" / "Application Support" / "pnpm"
    links = pnpm / "store" / "v11" / "links"

    def package(scope: str, name: str, version: str) -> Path:
        root = links / scope / name.split("/")[-1] / version / f"h-{name.replace('/', '-')}"
        (root / "node_modules" / name).mkdir(parents=True)
        (root / "node_modules" / name / "index.js").write_text("module.exports = 1\n")
        return root

    typescript, dep = package("@", "typescript", "7.0.2"), package("@", "dep", "1.0.0")
    scoped = package("@types", "@types/node", "22.0.0")
    private = package("@acme", "@acme/private", "1.0.0")
    (typescript / "node_modules" / "dep").symlink_to(dep / "node_modules" / "dep")
    (typescript / "node_modules" / "@types").mkdir()
    types = typescript / "node_modules" / "@types" / "node"
    types.symlink_to(scoped / "node_modules" / "@types" / "node")
    group = pnpm / "global" / "v11" / "4686-1a1e8584b5f-f7af69f5c046337f"
    (group / "node_modules").mkdir(parents=True)
    (group / "node_modules" / "typescript").symlink_to(typescript / "node_modules" / "typescript")
    (pnpm / "global" / "v11" / "802682db").symlink_to(group)
    (pnpm / "bin").mkdir()
    (pnpm / "bin" / "tsc").write_text("#!/bin/sh\n")
    monkeypatch.setenv("PNPM_HOME", str(pnpm))
    monkeypatch.setenv("PATH", f"{pnpm / 'bin'}{os.pathsep}/usr/bin")

    def plan():
        return caches.plan(tmp_path / "tier", tmp_path / "ws" / "r", "22222222-b",
                           tmp_path / "s", slack=True, read_through=False, home=home)  # fmt: skip

    # Neither a link whose real path names links/node_modules (nothing there) nor one to an
    # owner folder that looks like links/…/node_modules (outside the store) is followed.
    (group / "node_modules" / "odd").symlink_to(links / "node_modules" / "x")
    outside = home / "work" / "links" / "proj" / "node_modules" / "thing"
    outside.mkdir(parents=True)
    (outside.parent.parent / "secret.txt").write_text("x")
    (group / "node_modules" / "thing").symlink_to(outside)
    found = plan()
    assert not any(
        Path(path).is_relative_to(home / "work")
        or path in {str(links), str(links / "node_modules")}
        for path in map(os.path.realpath, found.readonly)
    ), found.readonly
    assert str(pnpm / "bin") in found.readonly
    assert os.path.realpath(group / "node_modules") in found.readonly
    for root in (typescript, dep, scoped):
        assert os.path.realpath(root) in found.readonly, root
    assert not any(Path(path).is_relative_to(private) for path in found.readonly), "not used"
    assert not [path for path in found.readonly if path in {str(links), str(pnpm / "store")}]
    assert not found.notes
    monkeypatch.setattr(caches, "LINKS_LIMIT", 2)
    found = plan()
    assert not any(Path(path).is_relative_to(links) for path in found.readonly)
    assert found.notes == [caches.PNPM_GLOBALS_CLOSED]
    from chiefofstaff import prompts

    said = prompts.worker_context("/w", "/w", [], False, "on", slack=True, notes=found.notes)
    assert caches.PNPM_GLOBALS_CLOSED in said
