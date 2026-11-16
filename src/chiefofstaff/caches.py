"""Package caches of sandboxed workers: a cache of their own, the owner's beneath it, read-only.

Copilot's sandbox (its dev-tool access) opened the owner's package caches read-write to a
worker's commands in a project of their kind: what a worker wrote there, the owner's builds
would later run unsandboxed. And some tools could not work there at all (corepack's, uv's
Pythons, Maven's wrapper, Gradle's native libraries). So dev-tool access is off for workers
(what they read is named here: tool_paths, registry_config), and every sandboxed worker that
does not read Slack shares a tier of its own, in the system's cache folder, and writes there;
the owner's caches are read-only to it. What the owner already has is read through where a tool
can (Gradle's read-only dependency cache, Maven's tail repository, Go's module proxy, links to
Playwright's browsers), and copied into the tier otherwise: copy-on-write clones (APFS
clonefile, a Linux reflink where the file system has them), in the background, never on a
worker's way. A worker's shell finds the tools pointed there by an init script Copilot sources
before each command (Copilot sessions' shell init scripts), not by its prompt.

A worker that reads Slack gets a tier of its own in its workspace, nothing copied in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .safefs import CannotCloneError, Folder, UnsafePathError, open_folder
from .workspaces import REGISTRY_SECRETS, held_caches, named, too_broad

logger = logging.getLogger(__name__)

SCRIPTS = "worker-shell"  # In its data folder: each worker's init script (read-only to them).
PRIVATE = ".caches"  # In the workspace of a worker that reads Slack.
SEED_EVERY = 600  # Seconds between two background seedings at most.


def default_tier() -> Path:
    """Where workers' caches are: the system's cache folder (~/Library/Caches on macOS,
    $XDG_CACHE_HOME or ~/.cache elsewhere), not the data folder, whose name on macOS has a space
    ("Application Support"): tools split JVM options (MAVEN_OPTS, GRADLE_OPTS) at spaces."""
    home = Path.home()
    base = home / "Library" / "Caches" if system() == "darwin" else xdg_cache(home)
    return base / "chiefofstaff" / "workers"


def system() -> str:
    return "darwin" if sys.platform == "darwin" else "linux"


def env_dir(variable: str, default: Path) -> Path:
    value = os.environ.get(variable, "").strip()
    path = Path(value).expanduser() if value else None
    return path if path and path.is_absolute() else default


def xdg_cache(home: Path) -> Path:
    return env_dir("XDG_CACHE_HOME", home / ".cache")


def xdg_config(home: Path) -> Path:
    return env_dir("XDG_CONFIG_HOME", home / ".config")


def config_value(path: Path, key: str, section: str = "") -> str:
    """A key's value in an ini-like file (.npmrc, pnpm's rc, pip.conf), in `section` when one
    is named; "" when it has none."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
        elif line and line[0] not in "#;" and (not section or current == section):
            name, equals, value = line.partition("=")
            if equals and name.strip() == key:
                return value.strip().strip("\"'")
    return ""


def yaml_value(path: Path, key: str) -> str:
    """A top-level key's value in a YAML file (Yarn's .yarnrc.yml)."""
    with suppress(OSError, UnicodeDecodeError):
        if found := re.search(
            rf"^{re.escape(key)}:[ \t]*(\S.*?)[ \t]*$", path.read_text(), re.MULTILINE
        ):
            return found.group(1).strip("\"'")
    return ""


def toml_value(path: Path, key: str) -> str:
    """A top-level string's value in a TOML file (uv.toml)."""
    import tomllib

    with suppress(OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        value = tomllib.loads(path.read_text()).get(key)
        return value if isinstance(value, str) else ""
    return ""


def xml_value(path: Path, tag: str) -> str:
    """A tag's text in an XML file (Maven's settings.xml), outside comments."""
    with suppress(OSError, UnicodeDecodeError):
        text = re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.DOTALL)
        if found := re.search(rf"<{tag}>\s*([^<]*?)\s*</{tag}>", text):
            return found.group(1)
    return ""


@dataclass(frozen=True)
class Owner:
    """Where the owner's caches are on this machine, as their tools look: the environment, then
    their own configuration in home (never a project's), then the default. Go's as `go env`
    says (go_env, looked up in the background)."""

    home: Path
    go_modcache: str = ""
    go_cache: str = ""

    @property
    def mac(self) -> bool:
        return system() == "darwin"

    def caches(self) -> Path:
        return self.home / "Library" / "Caches" if self.mac else xdg_cache(self.home)

    def configured(self, value: str) -> Path | None:
        """A folder a configuration file names (~, $HOME and ${user.home} its home)."""
        for name in ("${user.home}", "${env.HOME}", "${HOME}", "$HOME"):
            value = value.replace(name, str(self.home))
        if value == "~" or value.startswith("~/"):
            value = str(self.home) + value[1:]
        path = Path(value) if value else None
        return path if path and path.is_absolute() else None

    def npmrc(self) -> Path:
        return env_dir(
            "NPM_CONFIG_USERCONFIG", env_dir("npm_config_userconfig", self.home / ".npmrc")
        )

    def npm(self) -> Path:
        mine = self.configured(config_value(self.npmrc(), "cache")) or self.home / ".npm"
        return env_dir("npm_config_cache", env_dir("NPM_CONFIG_CACHE", mine))

    def pnpm_store(self) -> Path:
        base = self.home / "Library" / "pnpm" if self.mac else self.home / ".local/share/pnpm"
        settings = self.home / "Library" / "Preferences" if self.mac else xdg_config(self.home)
        mine = (
            self.configured(config_value(self.npmrc(), "store-dir"))
            or self.configured(config_value(settings / "pnpm" / "rc", "store-dir"))
            or base / "store"
        )
        return env_dir("npm_config_store_dir", mine)

    def pnpm_store_named(self) -> bool:
        """Whether the owner's environment or configuration names pnpm's store (which then
        wins over workers' PNPM_HOME)."""
        settings = self.home / "Library" / "Preferences" if self.mac else xdg_config(self.home)
        return bool(
            os.environ.get("npm_config_store_dir")  # noqa: SIM112  (npm reads either case)
            or os.environ.get("NPM_CONFIG_STORE_DIR")
            or config_value(self.npmrc(), "store-dir")
            or config_value(settings / "pnpm" / "rc", "store-dir")
        )

    def uv(self) -> Path:
        settings = env_dir("UV_CONFIG_FILE", xdg_config(self.home) / "uv" / "uv.toml")
        mine = self.configured(toml_value(settings, "cache-dir")) or xdg_cache(self.home) / "uv"
        return env_dir("UV_CACHE_DIR", mine)

    def uv_data(self) -> Path:
        return env_dir("XDG_DATA_HOME", self.home / ".local" / "share") / "uv"

    def pip(self) -> Path:
        files = [
            env_dir("PIP_CONFIG_FILE", self.home / ".pip" / "pip.conf"),
            self.home / "Library" / "Application Support" / "pip" / "pip.conf",
            xdg_config(self.home) / "pip" / "pip.conf",
            self.home / ".pip" / "pip.conf",
        ]
        named = (self.configured(config_value(f, "cache-dir", "global")) for f in files)
        return env_dir("PIP_CACHE_DIR", next((p for p in named if p), self.caches() / "pip"))

    def poetry(self) -> Path:
        return env_dir("POETRY_CACHE_DIR", self.caches() / "pypoetry")

    def pre_commit(self) -> Path:
        return env_dir("PRE_COMMIT_HOME", xdg_cache(self.home) / "pre-commit")

    def corepack(self) -> Path:
        return env_dir("COREPACK_HOME", xdg_cache(self.home) / "node" / "corepack")

    def bun(self) -> Path:
        return env_dir("BUN_INSTALL_CACHE_DIR", self.home / ".bun" / "install" / "cache")

    def yarn(self) -> Path:
        mine = self.configured(yaml_value(self.home / ".yarnrc.yml", "globalFolder"))
        return env_dir("YARN_GLOBAL_FOLDER", mine or self.home / ".yarn" / "berry") / "cache"

    def gradle(self) -> Path:
        return env_dir("GRADLE_USER_HOME", self.home / ".gradle")

    def maven(self) -> Path:
        return self.home / ".m2"

    def maven_repository(self) -> Path:
        mine = self.configured(xml_value(self.maven() / "settings.xml", "localRepository"))
        return mine or self.maven() / "repository"

    def cargo(self) -> Path:
        return env_dir("CARGO_HOME", self.home / ".cargo")

    def go_modules(self) -> Path:
        if self.go_modcache and Path(self.go_modcache).is_absolute():
            return Path(self.go_modcache)
        return env_dir("GOMODCACHE", env_dir("GOPATH", self.home / "go") / "pkg" / "mod")

    def go_build(self) -> Path:
        if self.go_cache and Path(self.go_cache).is_absolute():
            return Path(self.go_cache)
        return env_dir("GOCACHE", self.caches() / "go-build")

    def playwright(self) -> Path:
        return env_dir("PLAYWRIGHT_BROWSERS_PATH", self.caches() / "ms-playwright")

    def cache(self, mac: str, linux: str) -> Path:
        """A cache in the system's cache folder: ~/Library/Caches/<mac>, $XDG_CACHE_HOME/<linux>."""
        return self.caches() / mac if self.mac else xdg_cache(self.home) / linux

    def yarn_classic(self) -> Path:
        # Yarn 1 looks in XDG_CACHE_HOME first, on macOS too.
        found = xdg_cache(self.home) / "yarn" if os.environ.get("XDG_CACHE_HOME") else None
        return env_dir("YARN_CACHE_FOLDER", found or self.cache("Yarn", "yarn"))

    def node_gyp(self) -> Path:
        return self.cache("node-gyp", "node-gyp")

    def electron(self) -> Path:
        return env_dir("electron_config_cache", self.cache("electron", "electron"))

    def deno(self) -> Path:
        return env_dir("DENO_DIR", self.cache("deno", "deno"))

    def ccache(self) -> Path:
        return env_dir("CCACHE_DIR", self.cache("ccache", "ccache"))

    def sccache(self) -> Path:
        return env_dir("SCCACHE_DIR", self.cache("Mozilla.sccache", "sccache"))

    def nuget(self) -> Path:
        return env_dir("NUGET_PACKAGES", self.home / ".nuget" / "packages")

    def virtualenv(self) -> Path:
        data = self.home / "Library" / "Application Support" if self.mac else (
            env_dir("XDG_DATA_HOME", self.home / ".local" / "share")
        )  # fmt: skip
        return data / "virtualenv"

    def bundle(self) -> Path:
        return env_dir("BUNDLE_USER_CACHE", self.home / ".bundle" / "cache")

    def toolchains(self) -> list[Path]:
        """Interpreters and tools uv installed (pre-commit, poetry…): read and run, never
        written. Copilot opens neither, so they fail in its sandbox."""
        data = self.uv_data()
        return [
            env_dir("UV_PYTHON_INSTALL_DIR", data / "python"),
            env_dir("UV_TOOL_DIR", data / "tools"),
        ]

    def copilot_tools(self) -> Path:
        """Where the Copilot CLI unpacks itself and its tools (ripgrep, put on its shell's
        PATH)."""
        return self.caches() / "copilot" / "pkg"


# Copilot's dev-tool access, off for workers (backend.CopilotBackend.sandbox): it opened the
# owner's caches read-write, a folder deeper than one Chief of Staff held read-only included
# (its rules apply shallow to deep), and none of it can be narrowed. What it granted to read
# is granted here by name instead (its own list, CLI 1.0.111): toolchains in their usual places
# in home, the toolchain homes the environment names, every folder on PATH; its read-write
# caches are workers' own (script). Never a folder holding credentials (~/.cargo).
TOOLCHAINS = (
    ".cargo/bin", "go/bin", ".nvm/versions",
    ".nvm/alias", ".deno/bin", ".volta/bin", ".volta/tools", ".asdf/bin", ".asdf/libexec",
    ".asdf/installs", ".asdf/shims", ".local/share/mise/installs", ".local/share/mise/shims",
    "Library/Application Support/fnm", ".local/share/fnm", ".local/share/pipx", ".local/pipx",
    ".pyenv/versions", ".pyenv/shims", ".pyenv/libexec", ".pyenv/version", "Library/Python",
    ".sdkman/candidates", ".dotnet/sdk", ".dotnet/shared", ".dotnet/host", ".dotnet/packs",
    ".dotnet/templates", ".dotnet/sdk-manifests", ".dotnet/store", ".dotnet/tools", ".gem/ruby",
    ".rbenv/versions", ".rbenv/shims", ".rbenv/libexec", ".rbenv/version", ".rvm/rubies",
    ".bun/bin", "Library/Java/JavaVirtualMachines", ".jdks", ".tool-versions",
    ".config/mise/config.toml",
)  # fmt: skip
# The variable, and the folders under it (none: the whole folder it names).
TOOL_HOMES = {
    "JAVA_HOME": (), "GOROOT": (), "GOPATH": (), "GOBIN": (), "CARGO_HOME": ("bin",),
    "NVM_DIR": (), "NVM_SYMLINK": (), "NODE_PATH": (),
    "VOLTA_HOME": (), "FNM_DIR": (), "PNPM_HOME": (), "BUN_INSTALL": ("bin",), "DENO_INSTALL": (),
    "PYENV_ROOT": (), "PIPX_HOME": (), "CONDA_PREFIX": (), "VIRTUAL_ENV": (), "PYTHONPATH": (),
    "PYTHONHOME": (), "SDKMAN_DIR": ("candidates",), "DOTNET_ROOT": (), "RBENV_ROOT": (),
    "ASDF_DATA_DIR": (), "MISE_DATA_DIR": (), "MAVEN_HOME": (), "M2_HOME": (), "GRADLE_HOME": (),
    "ANDROID_HOME": (), "ANDROID_SDK_ROOT": (), "FLUTTER_ROOT": (), "VCPKG_ROOT": (),
    "GIT_EXEC_PATH": (), "LD_LIBRARY_PATH": (),
}  # fmt: skip
# rustup's toolchains, as Copilot granted them: to a Rust project's commands only. Elsewhere,
# rustup's `rustc` fails at once, as it did: pip runs `rustc --version` for its user agent at
# every install, and waits up to 0.5 s for it (a first rustc in a sandbox takes about that).
RUST_TOOLCHAINS = (".rustup/toolchains", ".rustup/settings.toml")
RUST_HOMES = {"RUSTUP_HOME": ("toolchains", "settings.toml")}
RUST_MARKERS = ("Cargo.toml", "rust-toolchain", "rust-toolchain.toml")
# The owner's registry and proxy settings in home it granted to read too: for workers on the
# owner's own tasks (a private registry), never for one that reads Slack. Gradle's and Cargo's
# are linked into the tier (owner_settings).
REGISTRY_CONFIG = (
    ".npmrc", ".yarnrc", ".yarnrc.yml", ".bunfig.toml", ".m2/settings.xml", ".config/go/env",
    "Library/Application Support/go/env", ".config/pip/pip.conf", ".pip/pip.conf",
    "Library/Application Support/pip/pip.conf", ".config/uv/uv.toml", ".bundle/config", ".gemrc",
    ".nuget/NuGet/NuGet.Config", ".config/NuGet/NuGet.Config", ".conan2/profiles",
    # And the credentials a private registry needs, which it let them read as well: the
    # owner's own tasks build as in their terminal (never for one that reads Slack).
    ".netrc", ".cargo/credentials", ".cargo/credentials.toml", ".m2/settings-security.xml",
    "Library/Application Support/pypoetry/auth.toml",
    "Library/Application Support/pypoetry/config.toml", ".config/pypoetry/auth.toml",
    ".config/pypoetry/config.toml", ".gradle/init.gradle", ".gradle/init.gradle.kts",
)  # fmt: skip
MOVED_CONFIG = {
    "NPM_CONFIG_USERCONFIG": (), "npm_config_userconfig": (), "NPM_CONFIG_GLOBALCONFIG": (),
    "npm_config_globalconfig": (), "PIP_CONFIG_FILE": (), "UV_CONFIG_FILE": (), "GOENV": (),
    "NETRC": (), "CARGO_HOME": ("credentials", "credentials.toml"),
    "POETRY_CONFIG_DIR": ("auth.toml", "config.toml"),
    "GRADLE_USER_HOME": ("init.gradle", "init.gradle.kts"),
}  # fmt: skip


def tool_paths(owner: Owner, rust: bool = False) -> list[str]:
    """What workers' commands run, read-only: the toolchains above (rustup's for a Rust
    project), uv's Pythons and tools, the Copilot CLI's own tools, the folders on PATH
    (path_folders)."""
    found = [*named(owner.home, TOOLCHAINS, TOOL_HOMES), *owner.toolchains()]
    if rust:
        found += named(owner.home, RUST_TOOLCHAINS, RUST_HOMES)
    found.append(owner.copilot_tools())
    found += path_folders(owner.home)
    return unsecret(found, owner.home)


def unsecret(paths: list[Path], home: Path) -> list[str]:
    """Those of these paths that exist, but none that is home or above it, nor one holding the
    owner's credentials."""
    secrets = [home / name for name in REGISTRY_SECRETS]
    return [
        str(path)
        for path in dict.fromkeys(paths)
        if path.exists()
        and not too_broad(str(path))
        and not any(secret.is_relative_to(path) for secret in secrets)
    ]


def rust_project(folders: list[Path], depth: int = 4) -> bool:
    """Whether one of these folders is a Rust project, or has one up to `depth` folders down
    (a maturin package deep in a monorepo), never looking into node_modules, a hidden folder or
    through a link. Looked at once as a worker opens, in a thread."""
    pending = [(folder, 0) for folder in folders]
    while pending:
        here, level = pending.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            continue
        for entry in entries:
            if entry.name in RUST_MARKERS and not entry.is_dir(follow_symlinks=False):
                return True
            if (
                level < depth
                and entry.is_dir(follow_symlinks=False)
                and not entry.name.startswith(".")
                and entry.name != "node_modules"
            ):
                pending.append((Path(entry.path), level + 1))
    return False


def path_folders(home: Path) -> list[Path]:
    """Each folder on PATH and, for a bin folder deep enough not to be one of home's own
    (~/.cargo), the tool's folder above it (its lib, its share)."""
    found = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        folder = Path(entry)
        if folder.is_absolute():
            found.append(folder)
            above = folder.parent
            if folder.name in {"bin", "shims"} and home not in {above, above.parent}:
                found.append(above)
    return found


def global_npmrc() -> list[Path]:
    """npm's global configuration, beside the node on PATH."""
    if not (node := shutil.which("node")):
        return []
    return [
        Path(binary).parent.parent / "etc" / "npmrc" for binary in (node, os.path.realpath(node))
    ]


def registry_config(owner: Owner) -> list[str]:
    """The owner's registry settings that exist, read-only for workers on the owner's own
    tasks."""
    found = [*named(owner.home, REGISTRY_CONFIG, MOVED_CONFIG), *global_npmrc()]
    return [
        str(path) for path in dict.fromkeys(found) if path.exists() and not too_broad(str(path))
    ]


# What is copied into the tier, per tool: (name, the owner's folder, the tier's folder, the
# depth of its units). Each unit is cloned whole, under another name and then renamed, so that
# a tool never finds half of one: 0 for a cache whose units are files, n for one whose units
# are the folders n levels down (uv's unpacked wheels, Bun's packages, Gradle's distributions).
def seeds(owner: Owner) -> list[tuple[str, Path, str, int]]:
    uv = owner.uv()
    gradle, maven, cargo = owner.gradle(), owner.maven(), owner.cargo()
    return [
        ("npm", owner.npm() / "_cacache", "npm/_cacache", 0),
        ("npm", owner.npm() / "_npx", "npm/_npx", 1),
        ("pnpm", owner.pnpm_store(), "pnpm/store", 0),
        ("yarn", owner.yarn(), "yarn-berry/cache", 0),
        ("bun", owner.bun(), "bun", 1),
        ("pip", owner.pip(), "pip", 0),
        *[("uv", folder, f"uv/{folder.name}", 1) for folder in sorted(uv.glob("archive-v*"))],
        *[
            ("uv", folder, f"uv/{folder.name}", 0)
            for pattern in ("wheels-v*", "simple-v*")
            for folder in sorted(uv.glob(pattern))
        ],
        ("poetry", owner.poetry() / "cache", "poetry/cache", 0),
        ("poetry", owner.poetry() / "artifacts", "poetry/artifacts", 0),
        ("pre-commit", owner.pre_commit(), "pre-commit", 1),
        ("corepack", owner.corepack(), "corepack", 3),
        ("cargo", cargo / "registry" / "index", "cargo/registry/index", 0),
        ("cargo", cargo / "registry" / "cache", "cargo/registry/cache", 0),
        ("gradle", gradle / "wrapper" / "dists", "gradle/wrapper/dists", 2),
        ("maven", maven / "wrapper" / "dists", "maven/wrapper/dists", 2),
        ("go", owner.go_build(), "go/build", 0),
        # Those the init script moves for tools Copilot's dev-tool access opened (Yarn 1's
        # under XDG_CACHE_HOME, which it looks in first, on macOS too).
        ("yarn1", owner.yarn_classic(), "xdg/yarn", 2),
        ("cargo", cargo / "git" / "db", "cargo/git/db", 1),
        ("cargo", cargo / "git" / "checkouts", "cargo/git/checkouts", 2),
        ("nuget", owner.nuget(), "nuget/packages", 2),
        ("electron", owner.electron(), "electron", 0),
        ("deno", owner.deno(), "deno", 0),
        ("ccache", owner.ccache(), "ccache", 0),
        ("sccache", owner.sccache(), "sccache", 0),
        ("virtualenv", owner.virtualenv(), "virtualenv", 0),
        ("bundle", owner.bundle(), "bundle", 0),
        *([] if owner.mac else [("node-gyp", owner.node_gyp(), "xdg/node-gyp", 1)]),
    ]


# Copy-on-write clones (safefs.Folder.clone): APFS clonefile on macOS, a Linux reflink where the
# file system has them; elsewhere nothing is copied (no full copies: the tier fills from the
# network once, for every worker). Everything Chief of Staff writes in the tier, which every
# sandboxed worker writes too, goes through safefs: never through a link a worker made.


class Budget:
    """What seeding may still copy (not clone) where the file system cannot clone (ext4, another
    volume): a whole copy of the owner's caches takes their space again."""

    def __init__(self, left: int):
        self.left = left
        self.copied = 0

    def take(self, size: int) -> bool:
        if size > self.left:
            return False
        self.left -= size
        self.copied += size
        return True


def place(source: Path, into: Folder, name: str, budget: Budget | None = None) -> bool:
    """Clone `source` to `name` in `into` whole (copied within `budget` where this file system
    cannot clone): under another name first, then renamed, so that no tool ever finds half of
    it. Nothing is overwritten."""
    if into.lstat(name) is not None:
        return False
    temporary = into.temporary_name(name).replace(".cos-", ".cos-seed-", 1)
    try:
        try:
            made = into.clone(source, temporary)
        except CannotCloneError:
            if budget is None or not budget.take(size_of(source)):
                raise
            made = into.copy_tree(source, temporary)
        if not made:
            return False
        if into.lstat(name) is not None:
            return False  # A worker made it meanwhile: its own stays.
        into.move(temporary, name)
        return True
    except CannotCloneError:
        raise
    except OSError:
        return False
    finally:
        with suppress(OSError):
            into.remove(temporary)


def size_of(path: Path) -> int:
    """The bytes of a file, or of all the files of a folder (not through links)."""
    found = path.lstat()
    if not stat.S_ISDIR(found.st_mode):
        return found.st_size
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            with suppress(OSError):
                total += (Path(root) / name).lstat().st_size
    return total


def fill(
    source: Path, target: Folder, depth: int, budget: Budget | None = None,
    seen: dict[str, int] | None = None,
) -> int:  # fmt: skip
    """Clone into the `target` folder each unit of `source` it lacks (see seeds); how many.
    `seen`: each folder's modification time when it was last filled from, updated: one that has
    not changed since is not looked at again (a million-file store, every pass)."""
    if not source.is_dir() or source.is_symlink():
        return 0
    copied = 0
    for root, folders, files in os.walk(source):
        here = Path(root)
        relative = here.relative_to(source).parts
        level = len(relative)
        if depth and level == depth - 1:
            names, folders[:] = [*folders, *files], []
        else:
            names = files if not depth or level < depth - 1 else [*folders, *files]
        folders[:] = [name for name in folders if not name.startswith(".cos-")]
        key = "/".join(relative)
        try:
            changed = here.stat().st_mtime_ns
        except OSError:
            continue
        if seen is not None and seen.get(key) == changed:
            continue  # Nothing added or removed here since: its files are in the tier.
        try:
            into = target.descend(relative, create=True)
        except (UnsafePathError, NotADirectoryError):
            folders[:] = []  # A worker's link or file there: not filled through.
            continue
        with into:
            done = True
            for name in names:
                if name.startswith((".cos-", ".tmp", "tmp")) or name.endswith(".lock"):
                    continue
                if into.lstat(name) is not None:
                    continue
                if place(here / name, into, name, budget):
                    copied += 1
                else:
                    done = False  # Tried again next pass.
        if seen is not None and done:
            seen[key] = changed
    return copied


def clear(tier: Path) -> None:
    """Workers' caches deleted (made again as the next worker starts, filled again by the next
    seeding): moved aside in one rename first, out of every worker's reach, then deleted by
    folder descriptors, never following a link, made writable where a tool left them read-only
    (Go's modules). Also what an earlier clear left, and what seeding knew it had filled."""
    from .workspaces import rmtree_writable

    parent = tier.parent
    with open_folder(parent, parent, create=True) as folder:
        if folder.lstat(tier.name) is not None:
            folder.move(tier.name, f".clearing-{uuid4().hex[:8]}")
        with suppress(OSError):
            folder.remove(SEEDED)
        aside = [name for name in folder.names() if name.startswith(".clearing-")]
    for name in aside:
        rmtree_writable(parent / name, parent)


def copied_bytes(tier: Path) -> int:
    """What seeding copied (not cloned) into the tier, where the file system cannot clone."""
    with suppress(OSError, ValueError), open_folder(tier.parent / SEEDED, tier.parent) as notes:
        return int(json.loads(notes.read("state.json") or b"{}").get("copied", 0))
    return 0


def tier_folder(tier: Path, trusted: Path | None = None) -> Folder:
    """The tier, opened from the folder above it, which no worker can change (a link a worker
    put in its place is removed: never followed)."""
    return open_folder(tier, trusted or tier.parent, create=True, relink=True)


SEEDED = "seeded"  # Beside the tier: what each pass found (fill's `seen`), and what it copied.
COPY_BUDGET = 8 * 1024**3  # At most this much copied, not cloned (and a quarter of free space).


def seed(tier: Path, owner: Owner) -> dict:
    """Fill the tier from the owner's caches: what it lacks of them, cloned (copied where this
    file system cannot clone, within COPY_BUDGET), each tool on its own: one that fails or
    cannot be cloned never stops the others. Each tool's time, in seconds, and what it took."""
    timings: dict = {}
    started = time.monotonic()
    with (
        open_folder(tier.parent / SEEDED, tier.parent, create=True, mode=0o700) as notes,
        tier_folder(tier) as root,
    ):
        state = json.loads(notes.read("state.json") or b"{}")
        free = shutil.disk_usage(tier).free
        budget = Budget(max(0, min(COPY_BUDGET, free // 4) - state.get("copied", 0)))
        for name, source, into, depth in seeds(owner):
            began = time.monotonic()
            key = hashlib.sha256(f"{source}:{into}".encode()).hexdigest()[:16]
            seen = state.setdefault("seen", {}).setdefault(key, {})
            try:
                with root.descend(Path(into).parts, create=True) as folder:
                    count = fill(source, folder, depth, budget, seen)
            except UnsafePathError:
                logger.warning("Workers' %s cache is a link: not filled", name)
                continue
            except CannotCloneError as exc:
                timings[f"{name}_not_copied"] = str(exc)
                continue
            except OSError:
                logger.warning("Copying your %s cache for workers stopped", name, exc_info=True)
                continue
            timings[name] = round(timings.get(name, 0) + time.monotonic() - began, 2)
            timings[f"{name}_copied"] = timings.get(f"{name}_copied", 0) + count
        state["copied"] = state.get("copied", 0) + budget.copied
        notes.write("state.json", json.dumps(state))
    timings["copied_bytes"] = state["copied"]
    timings["total"] = round(time.monotonic() - started, 2)
    return timings


def link_browsers(tier: Folder, owner: Owner) -> None:
    """Each browser Playwright installed for the owner, as a link in the tier (the owner's own
    folder, read-only to workers): found at once, never downloaded again; a version the owner
    lacks downloads into the tier. Cheap: done as each worker starts. On macOS, Chromium's
    headless shell gets a launcher instead (single_process_shell)."""
    source = owner.playwright()
    try:
        target = tier.child("ms-playwright", create=True)
    except UnsafePathError:
        return
    with target:
        if system() == "darwin":
            for name in target.names():
                if name.startswith("chromium_headless_shell-"):
                    try:
                        with target.child(name) as shell:
                            for kind in shell.names():
                                if shell.is_folder(kind):
                                    with shell.child(kind) as folder:
                                        launch_downloaded(shell, folder, "chrome-headless-shell")
                    except OSError as exc:
                        logger.info("Workers' %s: no launcher (%s)", name, exc)
        with suppress(OSError):
            if not source.is_dir() or os.path.samestat(source.stat(), os.fstat(target.fd or 0)):
                return
        for entry in sorted(source.iterdir()) if source.is_dir() else []:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            try:
                if system() == "darwin" and entry.name.startswith("chromium_headless_shell-"):
                    single_process_shell(entry, target)
                elif target.lstat(entry.name) is None:
                    target.symlink(entry.name, entry)
            except OSError as exc:  # Another worker starting made it meanwhile, or the like.
                logger.info("Your browser %s for workers: %s", entry.name, exc)


# Chromium checks in a Mach service when it starts its other processes, which Copilot's macOS
# sandbox refuses ("bootstrap_check_in … Permission denied"): every launch fails. In a single
# process it starts none, and runs there. Playwright's headless tests use the headless shell.
SHELL_LAUNCHER = (
    "#!/bin/sh\n"
    "# Chief of Staff: Chromium in one process (Copilot's sandbox refuses more).\n"
    'exec {binary} --single-process "$@"\n'
)


def launch_downloaded(shell: Folder, folder: Folder, name: str) -> None:
    """A headless shell a worker downloaded into the tier: its program moved aside, with the
    launcher in its place (from the next worker's start). By name in the folders opened: what
    a worker puts there meanwhile is replaced, never written through."""
    found = folder.lstat(name)
    if not found or not stat.S_ISREG(found.st_mode) or found.st_size < 4096:
        return  # A launcher already, a link, or the owner's program linked.
    if shell.lstat("INSTALLATION_COMPLETE") is None:
        return  # Still being installed.
    real = f"{name}.real"
    if folder.lstat(real) is None:
        folder.move(name, real)
        folder.write(
            name, SHELL_LAUNCHER.format(binary=shlex.quote(str(folder.path / real))), 0o755
        )


def single_process_shell(source: Path, target: Folder) -> None:
    """The owner's headless shell, linked file by file, with a launcher in place of its program
    that runs it in a single process."""
    name = source.name
    found = target.lstat(name)
    if found and stat.S_ISLNK(found.st_mode):
        target.delete(name)  # A link to the owner's folder: never written through.
    try:
        mirrored = target.child(name, create=True)
    except UnsafePathError:
        return
    with mirrored:
        for folder in sorted(path for path in source.iterdir() if path.is_dir()):
            program = folder / "chrome-headless-shell"
            try:
                mirror = mirrored.child(folder.name, create=True)
            except UnsafePathError:
                continue
            with mirror:
                for item in sorted(folder.iterdir()):
                    if mirror.lstat(item.name) is not None:
                        continue
                    if item == program:
                        launcher = SHELL_LAUNCHER.format(binary=shlex.quote(str(item)))
                        mirror.write(item.name, launcher, 0o755)
                    else:
                        with suppress(FileExistsError):
                            mirror.symlink(item.name, item)
        for item in sorted(source.iterdir()):
            if not item.is_dir() and mirrored.lstat(item.name) is None:
                with suppress(FileExistsError):
                    mirrored.symlink(item.name, item)


def owner_settings(tier: Path, owner: Owner) -> list[tuple[Path, Path]]:
    """The owner's settings tools read from the folder the tier replaces (Gradle's
    gradle.properties and init scripts, Cargo's config), with where each is linked in the tier:
    a proxy or a mirror they name applies to workers too. Not credentials."""
    pairs = [
        (owner.gradle() / "gradle.properties", tier / "gradle" / "gradle.properties"),
        (owner.gradle() / "init.d", tier / "gradle" / "init.d"),
        (owner.cargo() / "config.toml", tier / "cargo" / "config.toml"),
        (owner.cargo() / "config", tier / "cargo" / "config"),
        # A private registry's tokens, which the owner's builds use: read-only to workers on
        # the owner's own tasks (registry_config), never linked for one that reads Slack.
        (owner.cargo() / "credentials.toml", tier / "cargo" / "credentials.toml"),
        (owner.cargo() / "credentials", tier / "cargo" / "credentials"),
        (owner.gradle() / "init.gradle", tier / "gradle" / "init.gradle"),
        (owner.gradle() / "init.gradle.kts", tier / "gradle" / "init.gradle.kts"),
    ]
    return [(source, link) for source, link in pairs if source.exists()]


def link_settings(tier: Folder, owner: Owner) -> None:
    for source, link in owner_settings(tier.path, owner):
        try:
            parent = tier.descend(link.parent.relative_to(tier.path).parts, create=True)
        except (UnsafePathError, NotADirectoryError):
            continue
        with parent, suppress(OSError):  # Each on its own: one that fails stops no other.
            found = parent.lstat(link.name)
            if found and stat.S_ISLNK(found.st_mode) and parent.readlink(link.name) != str(source):
                parent.delete(link.name)  # A worker pointed it elsewhere: put back.
                found = None
            if found is None:
                parent.symlink(link.name, source)


# uv before 0.9.29 panics in Copilot's macOS sandbox: its HTTP client reads the system's proxy
# settings from a service the sandbox closes ("Attempted to create a NULL object"), and no
# setting skips that (system-configuration 0.7, in uv 0.9.29, handles it).
UV_FIXED = (0, 9, 29)


def uv_stamp() -> tuple[str, int]:
    """Which uv the owner's shell finds, and when it last changed (an upgrade): uv is looked at
    again when this does."""
    path = shutil.which("uv") or ""
    with suppress(OSError):
        return path, Path(path).stat().st_mtime_ns if path else 0
    return path, 0


def node_headers() -> bool | None:
    """Whether node-gyp's headers for the node on PATH are already in the owner's cache (read
    from there, they need no folder of workers' own); None without node."""
    if not (node := shutil.which("node")):
        return None
    try:
        found = subprocess.run(  # noqa: S603  (the owner's node, as their shell finds it)
            [node, "-p", "process.versions.node"], capture_output=True, text=True, timeout=20,
            check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    version = found.stdout.strip()
    return bool(version) and (Owner(Path.home()).node_gyp() / version / "installVersion").is_file()


def authority_stamp(copilot_home: Path) -> int:
    """When Copilot's lasting certificate authority was made (0: there is none)."""
    with suppress(OSError):
        return (copilot_home / "credential-proxy-ca" / "ca.pem").stat().st_mtime_ns
    return 0


def old_uv() -> str:
    """The owner's uv version when it is one that crashes in the sandbox (macOS), else ""."""
    if system() != "darwin" or not (uv := shutil.which("uv")):
        return ""
    try:
        found = subprocess.run(  # noqa: S603  (the owner's uv, as their shell finds it)
            [uv, "--version"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    words = found.stdout.split()
    if len(words) < 2:
        return ""
    try:
        version = tuple(int(part) for part in words[1].split("+")[0].split(".")[:3])
    except ValueError:
        return ""
    return words[1] if version < UV_FIXED else ""


def go_env() -> dict[str, str]:
    """The owner's GOPROXY, GOMODCACHE and GOCACHE, as `go env` has them (their environment
    and Go's own configuration file); empty when Go is not installed."""
    if not shutil.which("go"):
        return {}
    try:
        found = subprocess.run(
            ["go", "env", "-json", "GOPROXY", "GOMODCACHE", "GOCACHE"],  # noqa: S607  (the owner's go)
            capture_output=True, text=True, timeout=20, check=False,
        )  # fmt: skip
        values = json.loads(found.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    return {key: value for key, value in values.items() if isinstance(value, str)}


# Java reads neither HTTPS_PROXY nor SSL_CERT_FILE: the JVM (Gradle and its wrapper, Maven) is
# pointed at the sandbox's proxy, by parameter expansion only (no process started per command).
JAVA_PROXY = (
    "_cos_p=${HTTPS_PROXY:-$https_proxy}\n"
    'if [ -n "$_cos_p" ]; then\n'
    "  _cos_p=${_cos_p#*://}; _cos_p=${_cos_p%%/*}\n"
    '  export JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:+$JAVA_TOOL_OPTIONS }'
    "-Dhttp.proxyHost=${_cos_p%:*} -Dhttp.proxyPort=${_cos_p##*:} "
    "-Dhttps.proxyHost=${_cos_p%:*} -Dhttps.proxyPort=${_cos_p##*:} "
    '-Dhttp.nonProxyHosts=localhost|127.*|[::1]"\n'
    "fi\n"
    "unset _cos_p\n"
)


def script(
    tier: Path,
    owner: Owner,
    workspace: Path,
    temporary: Path,
    read_through: bool,
    goproxy: str,
    truststore: Path | None,
    devdir: bool = False,
) -> str:
    """A worker's shell init script: every tool pointed at the tier, read through to the
    owner's caches where the tool can (`read_through`), a temporary folder of its own, the JVM
    at the sandbox's proxy. Only assignments: sourcing it starts no process."""
    q = shlex.quote
    t = tier
    lines = [
        "# Chief of Staff: where this worker's tools keep their caches (rewritten at each start).",
        f"export npm_config_cache={q(str(t / 'npm'))}",
        # pnpm's store, in its home (not npm_config_store_dir, which npm 11.5 and later warns
        # about at every command), unless the owner's own configuration names one.
        f"export PNPM_HOME={q(str(t / 'pnpm'))}",
        # npm's and pnpm's checks for a newer version of themselves: pnpm can't record one in
        # the sandbox, so it fetched its own package list (0.7 MB) at every install.
        "export npm_config_update_notifier=false",
        f"export YARN_GLOBAL_FOLDER={q(str(t / 'yarn-berry'))}",
        f"export BUN_INSTALL_CACHE_DIR={q(str(t / 'bun'))}",
        f"export PIP_CACHE_DIR={q(str(t / 'pip'))} UV_CACHE_DIR={q(str(t / 'uv'))}",
        # uvx and `uv tool` work in uv's tool folder (the owner's: read-only, its tools run).
        f"export UV_TOOL_DIR={q(str(t / 'uv-tools'))}"
        f" UV_TOOL_BIN_DIR={q(str(t / 'uv-tools' / 'bin'))}",
        f"export POETRY_CACHE_DIR={q(str(t / 'poetry'))} POETRY_VIRTUALENVS_IN_PROJECT=true",
        f"export PRE_COMMIT_HOME={q(str(t / 'pre-commit'))} COREPACK_HOME={q(str(t / 'corepack'))}",
        f"export CARGO_HOME={q(str(t / 'cargo'))}",
        # GOPATH too: Go writes its checksum database's state there (pkg/sumdb) at `go get`.
        f"export GOPATH={q(str(t / 'go'))} GOMODCACHE={q(str(t / 'go' / 'mod'))}"
        f" GOCACHE={q(str(t / 'go' / 'build'))}",
        f"export GRADLE_USER_HOME={q(str(t / 'gradle'))}",
        # Gradle's native libraries would load from that writable folder, which the sandbox
        # refuses; its daemons are this worker's own (a daemon keeps the sandbox it started in).
        'export GRADLE_OPTS="-Dorg.gradle.native=false '
        f"-Dorg.gradle.daemon.registry.base={t / 'gradle-daemons' / temporary.name} "
        '-Dorg.gradle.daemon.idletimeout=900000${GRADLE_OPTS:+ $GRADLE_OPTS}"',
        f"export MAVEN_USER_HOME={q(str(t / 'maven'))}",
        f"export PLAYWRIGHT_BROWSERS_PATH={q(str(t / 'ms-playwright'))}",
        # The other caches Copilot's dev-tool access opened in the owner's home, and any tool
        # that keeps its cache under XDG_CACHE_HOME (gh's): workers' own too.
        f"export XDG_CACHE_HOME={q(str(t / 'xdg'))} DENO_DIR={q(str(t / 'deno'))}",
        f"export electron_config_cache={q(str(t / 'electron'))}",
        f"export VIRTUALENV_OVERRIDE_APP_DATA={q(str(t / 'virtualenv'))}"
        f" BUNDLE_USER_CACHE={q(str(t / 'bundle'))}",
        f"export NUGET_PACKAGES={q(str(t / 'nuget' / 'packages'))}"
        f" NUGET_HTTP_CACHE_PATH={q(str(t / 'nuget' / 'http'))}",
        f"export CCACHE_DIR={q(str(t / 'ccache'))} SCCACHE_DIR={q(str(t / 'sccache'))}"
        f" HUGO_CACHEDIR={q(str(t / 'hugo'))} BAZELISK_HOME={q(str(t / 'bazelisk'))}",
        f"export TMPDIR={q(str(temporary) + '/')}",
    ]
    if owner.pnpm_store_named():
        lines.append(f"export npm_config_store_dir={q(str(t / 'pnpm' / 'store'))}")
    if devdir:
        # node-gyp's headers: on macOS only this moves them (on Linux XDG_CACHE_HOME does),
        # wanted only when the owner's own lack the node on PATH's (npm 11.5 warns about it).
        lines.append(f"export npm_config_devdir={q(str(t / 'node-gyp'))}")
    maven = f"-Dmaven.repo.local={t / 'maven' / 'repository'}"
    if read_through:
        lines.append(f"export GRADLE_RO_DEP_CACHE={q(str(owner.gradle() / 'caches'))}")
        if owner.maven_repository().is_dir():
            maven += f" -Dmaven.repo.local.tail={owner.maven_repository()}"
        downloads = owner.go_modules() / "cache" / "download"
        upstream = goproxy or "https://proxy.golang.org,direct"
        lines.append(f"export GOPROXY={q(f'file://{downloads},{upstream}')}")
    lines.append(f'export MAVEN_OPTS="{maven}${{MAVEN_OPTS:+ $MAVEN_OPTS}}"')
    lines.append(JAVA_PROXY.rstrip())
    if truststore:
        store = (
            f"-Djavax.net.ssl.trustStore={truststore} -Djavax.net.ssl.trustStorePassword=changeit"
        )
        lines.append(
            f'[ -s {q(str(truststore))} ] && export JAVA_TOOL_OPTIONS="$JAVA_TOOL_OPTIONS {store}"'
        )
    # Copilot reports an init script that ends with an error before every command.
    lines.append("true")
    return "\n".join(lines) + "\n"


JAVA = "java"  # Beside the tier, read-only to workers: their Java truststores.


def truststore_file(tier: Path, stamp: int) -> Path:
    """Where the truststore for the authority made at `stamp` (authority_stamp) is (once made:
    workers' scripts use it when it is there)."""
    return tier.parent / JAVA / f"truststore-{stamp}.p12"


def java_folder(tier: Path) -> Path:
    """The truststores' folder, made now: a worker's sandbox reads it from its start, before
    its truststore is made in it."""
    with open_folder(tier.parent / JAVA, tier.parent, create=True, mode=0o755) as folder:
        return folder.path


def truststore(tier: Path, copilot_home: Path) -> Path | None:
    """A Java truststore for workers: the JDK's own roots and the certificate authority Copilot
    keeps (`copilot sandbox ca create`), whose proxy answers for github.com in the sandbox
    (where Gradle's distributions download from). Made once per authority (about a second,
    in the background); None without a lasting one (Copilot then makes one for each command,
    which no truststore can follow)."""
    authority = copilot_home / "credential-proxy-ca" / "ca.pem"
    stamp = authority_stamp(copilot_home)
    if not stamp:
        return None
    java_home = os.environ.get("JAVA_HOME") or ""
    if not java_home and Path("/usr/libexec/java_home").exists():
        found = subprocess.run(
            ["/usr/libexec/java_home"], capture_output=True, text=True, check=False, timeout=20
        )
        java_home = found.stdout.strip() if found.returncode == 0 else ""
    roots = Path(java_home) / "lib" / "security" / "cacerts"
    keytool = Path(java_home) / "bin" / "keytool"
    if not roots.is_file() or not keytool.is_file():
        return None
    # Beside the tier, where no worker writes (read-only to them), not in it: keytool writes by
    # path, which must lead nowhere a worker chose.
    name = truststore_file(tier, stamp).name
    with open_folder(tier.parent / JAVA, tier.parent, create=True, mode=0o755) as folder:
        if folder.lstat(name) is not None:
            return folder.path / name
        partial = folder.temporary_name(name)
        quiet = {"capture_output": True, "check": True, "timeout": 120, "env": java_env()}
        try:
            copy = ["-importkeystore", "-noprompt", "-srckeystore", str(roots), "-srcstorepass",
                    "changeit", "-destkeystore", str(folder.path / partial), "-deststoretype",
                    "PKCS12", "-deststorepass", "changeit"]  # fmt: skip
            add = ["-importcert", "-noprompt", "-alias", "copilot-sandbox", "-file",
                   str(authority), "-keystore", str(folder.path / partial), "-storepass",
                   "changeit"]  # fmt: skip
            for arguments in (copy, add):
                subprocess.run([str(keytool), *arguments], **quiet)  # noqa: S603  (the JDK's keytool)
            folder.move(partial, name)
        except (OSError, subprocess.SubprocessError):
            return None
        finally:
            with suppress(OSError):
                folder.remove(partial)
        return folder.path / name


def java_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "JAVA_TOOL_OPTIONS"}


TEMP = "temp"  # Beside the tier: each worker's temporary folder.


def worker_temp(session_id: str, tier: Path | None = None) -> Path:
    """A worker's own temporary folder: short (Unix sockets made there must fit 104 bytes), in
    a folder beside the tier that no worker can write (not the system's temporary folder, which
    Copilot's sandbox lets every worker write), so that only Chief of Staff makes it."""
    return (tier or default_tier()).parent / TEMP / session_id[:8]


def make_temp(path: Path) -> bool:
    """Make a worker's temporary folder, the owner's alone (0700); False when it cannot be.
    One there already is that worker's own from before (no other can make it there): kept as
    it is if it is a folder, made again if it is a link or a file."""
    try:
        with (
            open_folder(path.parent, path.parent.parent, create=True, mode=0o700) as parent,
            parent.child(path.name, create=True, mode=0o700, relink=True) as own,
        ):
            if own.fd is not None and own.fd >= 0:
                if os.fstat(own.fd).st_uid != os.getuid():
                    return False
                os.fchmod(own.fd, 0o700)
        return True
    except OSError:
        return False


def remove_temp(session_id: str, tier: Path | None = None) -> None:
    """A closed worker's temporary folder goes, never through a link."""
    path = worker_temp(session_id, tier)
    with suppress(OSError), open_folder(path.parent, path.parent.parent) as parent:
        parent.remove(path.name)


@dataclass
class Plan:
    """What a sandboxed worker's caches add to its sandbox, and its init script's text."""

    tier: Path  # Its tools' caches: the shared tier, or its own when it reads Slack.
    trusted: Path  # The folder above the tier, which no worker can write.
    temporary: Path
    readwrite: list[str]
    readonly: list[str]
    text: str
    slack: bool


def plan(
    tier: Path,
    workspace: Path,
    session_id: str,
    scripts: Path,
    *,
    slack: bool,
    read_through: bool,
    go: dict[str, str] | None = None,
    truststore_path: Path | None = None,
    home: Path | None = None,
    rust: bool = False,
    devdir: bool = False,
) -> Plan:
    """A worker's caches: the shared tier (a tier of its own in its workspace when it reads
    Slack, nothing of the owner's copied there), its toolchains read-only, the owner's caches
    and registry settings read-only (read through) unless it reads Slack, and its init script.
    Nothing is written here (see prepare)."""
    go = go or {}
    owner = Owner(home or Path.home(), go.get("GOMODCACHE", ""), go.get("GOCACHE", ""))
    own = workspace / PRIVATE if slack else tier
    temporary = worker_temp(session_id, tier)
    readonly = tool_paths(owner, rust)
    readonly.append(str(scripts))
    if not slack:
        # The owner's caches, read through (Gradle's, Maven's, Go's, Playwright's) or not; one
        # that does not exist yet stays closed (Copilot grants nothing of its own here). Of
        # those a configuration file moves (the owner's own, in home), only the two read
        # through: Maven's repository and Go's modules.
        readonly += held_caches(owner.home)[0]
        readonly += unsecret([owner.maven_repository(), owner.go_modules()], owner.home)
        readonly += registry_config(owner)
        readonly += [str(source) for source, _ in owner_settings(own, owner)]
        readonly += [str(truststore_path.parent)] if truststore_path else []
    readwrite = [str(own), str(temporary)]
    # Nothing of the owner's is read through for a worker that reads Slack: it can't read it.
    text = script(
        own, owner, workspace, temporary, read_through and not slack, go.get("GOPROXY", ""),
        None if slack else truststore_path, devdir and not slack,
    )  # fmt: skip
    readonly = [path for path in dict.fromkeys(readonly) if not too_broad(path)]
    trusted = workspace.parent if slack else tier.parent
    return Plan(own, trusted, temporary, readwrite, readonly, text, slack)


PREPARING = threading.Lock()  # One worker readied at a time: two would race for the same links.


def prepare(found: Plan, session_id: str, scripts: Path, home: Path | None = None) -> Path | None:
    """Before a worker's session opens: its temporary folder and its init script (in `scripts`,
    read-only to workers) first, which its tools need; then the links into the owner's
    browsers and settings, each on its own (one that fails is logged and stops nothing). The
    script's path; None when its temporary folder could not be made its own."""
    owner = Owner(home or Path.home())
    with PREPARING:
        if not make_temp(found.temporary):
            return None
        path = write_script(scripts, session_id, found.text)
        try:
            with tier_folder(found.tier, found.trusted) as tier:
                link_browsers(tier, owner)
                if not found.slack:
                    link_settings(tier, owner)
        except OSError as exc:
            logger.warning("Worker %s's caches folder: %s", session_id, exc)
    return path


def write_script(folder: Path, session_id: str, text: str) -> Path:
    """A worker's init script, in Chief of Staff's data folder, where workers can read it and
    never write it."""
    name = f"{session_id}.sh"
    with open_folder(folder, folder.parent, create=True, mode=0o700) as scripts:
        if scripts.read(name) != text.encode():
            scripts.write(name, text, 0o644)
    return folder / name
