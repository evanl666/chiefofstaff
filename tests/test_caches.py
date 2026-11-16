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
    assert f"PNPM_HOME={tier / 'pnpm'}" in found
    assert "npm_config_store_dir" not in found and "npm_config_devdir" not in found, (
        "npm 11.5 and later warn about either at every command"
    )
    stale = caches.script(tier, owner, tmp_path / "ws", tmp_path / "t", True, "", tmp_path / "no")
    status = subprocess.run(
        ["bash", "--norc", "--noprofile", "-c", f". /dev/stdin <<'EOF'\n{stale}EOF\necho $?"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    assert status == "0", "a stale truststore never makes the script fail (Copilot reports it)"
    named = caches.script(tier, owner, tmp_path / "ws", tmp_path / "t", True, "", None, True)
    assert f"npm_config_devdir={tier / 'node-gyp'}" in named, "asked: the owner's lack headers"
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
    yarn = plan.temporary / f".yarn-cache-{os.getuid()}"
    assert yarn.is_symlink() and yarn.readlink() == tier / "yarn-classic", (
        "Yarn 1 finds workers' own cache where it looks when the owner's is not writable"
    )


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
    caches.clear(tier)
    assert not tier.exists() and (victim / "keep").read_text() == "the owner's"
    assert caches.copied_bytes(tier) == 0, "seeding fills it all again"
    assert not [p for p in tier.parent.iterdir() if p.name.startswith(".clearing-")]
    caches.clear(tier)  # Nothing there: nothing to do.
