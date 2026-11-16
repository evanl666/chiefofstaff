"""Package caches of sandboxed workers: a cache of their own, the owner's beneath it, read-only.

Copilot's sandbox (its dev-tool access) opens the owner's package caches read-write to a
worker's commands in a project of their kind: what a worker wrote there, the owner's builds
would later run unsandboxed. And some tools cannot work there at all (corepack's, uv's Pythons,
Maven's wrapper, Gradle's native libraries). So every sandboxed worker that does not read Slack
shares a tier of its own, in Chief of Staff's data folder, and writes there; the owner's caches
are read-only to it. What the owner already has is read through where a tool can (Gradle's
read-only dependency cache, Maven's tail repository, Go's module proxy, links to Playwright's
browsers), and copied into the tier otherwise: copy-on-write clones (APFS clonefile, a Linux
reflink where the file system has them), in the background, never on a worker's way. A
worker's shell finds the tools pointed there by an init script Copilot sources before each
command (Copilot sessions' shell init scripts), not by its prompt.

A worker that reads Slack gets a tier of its own in its workspace, nothing copied in.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .workspaces import held_caches, too_broad

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


def ours(path: Path, root: Path) -> bool:
    """Whether `path` is `root` or under it with no link on the way (nothing a worker made can
    send Chief of Staff, which runs unsandboxed, to write elsewhere). A part that does not
    exist yet is fine: it is made as a folder."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    here = root
    for part in ("", *relative.parts):
        here = here / part if part else here
        try:
            if here.is_symlink():
                return False
        except OSError:
            return False
    return True


def system() -> str:
    return "darwin" if sys.platform == "darwin" else "linux"


def env_dir(variable: str, default: Path) -> Path:
    value = os.environ.get(variable, "").strip()
    path = Path(value).expanduser() if value else None
    return path if path and path.is_absolute() else default


def xdg_cache(home: Path) -> Path:
    return env_dir("XDG_CACHE_HOME", home / ".cache")


@dataclass(frozen=True)
class Owner:
    """Where the owner's caches are on this machine (defaults, or where the environment moves
    them, as the tools themselves look)."""

    home: Path

    @property
    def mac(self) -> bool:
        return system() == "darwin"

    def caches(self) -> Path:
        return self.home / "Library" / "Caches" if self.mac else xdg_cache(self.home)

    def npm(self) -> Path:
        return env_dir("npm_config_cache", env_dir("NPM_CONFIG_CACHE", self.home / ".npm"))

    def pnpm_store(self) -> Path:
        base = self.home / "Library" / "pnpm" if self.mac else self.home / ".local/share/pnpm"
        return env_dir("npm_config_store_dir", base / "store")

    def uv(self) -> Path:
        return env_dir("UV_CACHE_DIR", xdg_cache(self.home) / "uv")

    def uv_data(self) -> Path:
        return env_dir("XDG_DATA_HOME", self.home / ".local" / "share") / "uv"

    def pip(self) -> Path:
        return env_dir("PIP_CACHE_DIR", self.caches() / "pip")

    def poetry(self) -> Path:
        return env_dir("POETRY_CACHE_DIR", self.caches() / "pypoetry")

    def pre_commit(self) -> Path:
        return env_dir("PRE_COMMIT_HOME", xdg_cache(self.home) / "pre-commit")

    def corepack(self) -> Path:
        return env_dir("COREPACK_HOME", xdg_cache(self.home) / "node" / "corepack")

    def bun(self) -> Path:
        return env_dir("BUN_INSTALL_CACHE_DIR", self.home / ".bun" / "install" / "cache")

    def yarn(self) -> Path:
        return self.home / ".yarn" / "berry" / "cache"

    def gradle(self) -> Path:
        return env_dir("GRADLE_USER_HOME", self.home / ".gradle")

    def maven(self) -> Path:
        return self.home / ".m2"

    def cargo(self) -> Path:
        return env_dir("CARGO_HOME", self.home / ".cargo")

    def go_modules(self) -> Path:
        return env_dir("GOMODCACHE", env_dir("GOPATH", self.home / "go") / "pkg" / "mod")

    def go_build(self) -> Path:
        return env_dir("GOCACHE", self.caches() / "go-build")

    def playwright(self) -> Path:
        return env_dir("PLAYWRIGHT_BROWSERS_PATH", self.caches() / "ms-playwright")

    def toolchains(self) -> list[Path]:
        """Interpreters and tools uv installed (pre-commit, poetry…): read and run, never
        written. Copilot opens neither, so they fail in its sandbox."""
        data = self.uv_data()
        return [
            env_dir("UV_PYTHON_INSTALL_DIR", data / "python"),
            env_dir("UV_TOOL_DIR", data / "tools"),
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
        ("pnpm", owner.pnpm_store(), "pnpm-store", 0),
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
    ]


# Copy-on-write clones. macOS: clonefile(2), which clones a folder whole. Linux: the FICLONE
# ioctl, file by file, where the file system has reflinks (Btrfs, XFS); elsewhere nothing is
# copied (no full copies: the tier fills from the network once, for every worker).
CLONE_NOFOLLOW = 0x0001
FICLONE = 0x40049409
_libc = None


def clone(source: Path, target: Path) -> bool:
    """A copy-on-write copy of a file or folder at `target` (which must not exist). False when
    this file system cannot clone (nothing is left at `target` then)."""
    global _libc
    if sys.platform == "darwin":
        if _libc is None:
            _libc = ctypes.CDLL("libc.dylib", use_errno=True)
        if _libc.clonefile(os.fsencode(source), os.fsencode(target), CLONE_NOFOLLOW) == 0:
            return True
        code = ctypes.get_errno()
        if code in (errno.EXDEV, errno.ENOTSUP):
            raise CannotCloneError(os.strerror(code))
        return False
    return reflink_tree(source, target)


class CannotCloneError(OSError):
    """The tier's file system cannot clone the owner's files (another volume, no reflinks)."""


def reflink_tree(source: Path, target: Path) -> bool:
    import fcntl  # Not on Windows (where nothing is sandboxed).

    if source.is_symlink():
        target.symlink_to(source.readlink())
        return True
    if source.is_dir():
        target.mkdir()
        for child in source.iterdir():
            reflink_tree(child, target / child.name)
        return True
    with source.open("rb") as src, target.open("xb") as dst:
        try:
            fcntl.ioctl(dst.fileno(), FICLONE, src.fileno())
        except OSError as exc:
            target.unlink(missing_ok=True)
            if exc.errno in (errno.EXDEV, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOTTY):
                raise CannotCloneError(os.strerror(exc.errno)) from None
            return False
    shutil.copystat(source, target, follow_symlinks=False)
    return True


def place(source: Path, target: Path) -> bool:
    """Clone `source` to `target` whole: under another name first, then renamed, so that no
    tool ever finds half of it. Nothing is overwritten."""
    if os.path.lexists(target):
        return False
    temporary = target.with_name(f".cos-seed-{uuid4().hex[:8]}-{target.name}")
    try:
        if not clone(source, temporary):
            return False
        if os.path.lexists(target):
            return False  # A worker made it meanwhile: its own stays.
        temporary.rename(target)
        return True
    except CannotCloneError:
        raise
    except OSError:
        return False
    finally:
        if temporary.is_dir() and not temporary.is_symlink():
            shutil.rmtree(temporary, ignore_errors=True)
        elif os.path.lexists(temporary):
            temporary.unlink()


def fill(source: Path, target: Path, depth: int) -> int:
    """Clone into `target` each unit of `source` it lacks (see seeds); how many."""
    if not source.is_dir() or source.is_symlink():
        return 0
    copied = 0
    for root, folders, files in os.walk(source):
        here = Path(root)
        level = len(here.relative_to(source).parts)
        into = target / here.relative_to(source)
        if not ours(into, target):
            folders[:] = []
            continue
        into.mkdir(parents=True, exist_ok=True)
        names = files if not depth or level < depth - 1 else [*folders, *files]
        if depth and level == depth - 1:
            folders[:] = []
        folders[:] = [name for name in folders if not name.startswith(".cos-seed-")]
        for name in names:
            if name.startswith((".cos-seed-", ".tmp", "tmp")) or name.endswith(".lock"):
                continue
            copied += place(here / name, into / name)
    return copied


def seed(tier: Path, owner: Owner) -> dict:
    """Fill the tier from the owner's caches: what it lacks of them, cloned. Each tool's time,
    in seconds, and whether this file system can clone at all."""
    timings: dict = {}
    started = time.monotonic()
    for name, source, into, depth in seeds(owner):
        began = time.monotonic()
        if not ours(tier / into, tier):
            logger.warning("Workers' %s cache is a link: not filled", name)
            continue
        try:
            count = fill(source, tier / into, depth)
        except CannotCloneError as exc:
            logger.info("Workers' caches are not copied from yours here: %s", exc)
            timings["cannot_clone"] = str(exc)
            break
        except OSError:
            logger.warning("Copying your %s cache for workers stopped", name, exc_info=True)
            continue
        timings[name] = round(timings.get(name, 0) + time.monotonic() - began, 2)
        timings[f"{name}_copied"] = timings.get(f"{name}_copied", 0) + count
    timings["total"] = round(time.monotonic() - started, 2)
    return timings


def link_browsers(tier: Path, owner: Owner) -> None:
    """Each browser Playwright installed for the owner, as a link in the tier (the owner's own
    folder, read-only to workers): found at once, never downloaded again; a version the owner
    lacks downloads into the tier. Cheap: done as each worker starts. On macOS, Chromium's
    headless shell gets a launcher instead (single_process_shell)."""
    source, target = owner.playwright(), tier / "ms-playwright"
    if not ours(target, tier):
        return
    target.mkdir(parents=True, exist_ok=True)
    if system() == "darwin":
        for own in target.glob("chromium_headless_shell-*/*/chrome-headless-shell"):
            if ours(own, target):
                launch_downloaded(own)
    if not source.is_dir() or source.resolve() == target.resolve():
        return
    for entry in source.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if system() == "darwin" and entry.name.startswith("chromium_headless_shell-"):
            single_process_shell(entry, target / entry.name)
        else:
            with suppress(FileExistsError):
                (target / entry.name).symlink_to(entry)


# Chromium checks in a Mach service when it starts its other processes, which Copilot's macOS
# sandbox refuses ("bootstrap_check_in … Permission denied"): every launch fails. In a single
# process it starts none, and runs there. Playwright's headless tests use the headless shell.
SHELL_LAUNCHER = (
    "#!/bin/sh\n"
    "# Chief of Staff: Chromium in one process (Copilot's sandbox refuses more).\n"
    'exec {binary} --single-process "$@"\n'
)


def launch_downloaded(program: Path) -> None:
    """A headless shell a worker downloaded into the tier: its program moved aside, with the
    launcher in its place (from the next worker's start)."""
    if program.is_symlink() or not program.is_file() or program.stat().st_size < 4096:
        return  # A launcher already, or the owner's program linked.
    if not (program.parent.parent / "INSTALLATION_COMPLETE").exists():
        return  # Still being installed.
    real = program.with_name(program.name + ".real")
    if not real.exists():
        program.rename(real)
        program.write_text(SHELL_LAUNCHER.format(binary=shlex.quote(str(real))))
        program.chmod(0o755)


def single_process_shell(source: Path, target: Path) -> None:
    """The owner's headless shell, linked file by file, with a launcher in place of its program
    that runs it in a single process."""
    if target.is_symlink():
        target.unlink()  # A link to the owner's folder: never written through.
    for folder in [path for path in source.iterdir() if path.is_dir()]:
        program = folder / "chrome-headless-shell"
        mirror = target / folder.name
        if not ours(mirror, target):
            continue
        mirror.mkdir(parents=True, exist_ok=True)
        for item in folder.iterdir():
            link = mirror / item.name
            if os.path.lexists(link):
                continue
            if item == program:
                link.write_text(SHELL_LAUNCHER.format(binary=shlex.quote(str(item))))
                link.chmod(0o755)
            else:
                link.symlink_to(item)
    for item in source.iterdir():
        if not item.is_dir() and not os.path.lexists(target / item.name):
            (target / item.name).symlink_to(item)


def owner_settings(tier: Path, owner: Owner) -> list[tuple[Path, Path]]:
    """The owner's settings tools read from the folder the tier replaces (Gradle's
    gradle.properties and init scripts, Cargo's config), with where each is linked in the tier:
    a proxy or a mirror they name applies to workers too. Not credentials."""
    pairs = [
        (owner.gradle() / "gradle.properties", tier / "gradle" / "gradle.properties"),
        (owner.gradle() / "init.d", tier / "gradle" / "init.d"),
        (owner.cargo() / "config.toml", tier / "cargo" / "config.toml"),
        (owner.cargo() / "config", tier / "cargo" / "config"),
    ]
    return [(source, link) for source, link in pairs if source.exists()]


def link_settings(tier: Path, owner: Owner) -> None:
    for source, link in owner_settings(tier, owner):
        if not ours(link.parent, tier):
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() and link.readlink() != source:
            link.unlink()  # A worker pointed it elsewhere: put back.
        if not os.path.lexists(link):
            link.symlink_to(source)


# uv before 0.9.29 panics in Copilot's macOS sandbox: its HTTP client reads the system's proxy
# settings from a service the sandbox closes ("Attempted to create a NULL object"), and no
# setting skips that (system-configuration 0.7, in uv 0.9.29, handles it).
UV_FIXED = (0, 9, 29)


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


def go_proxy() -> str:
    """The owner's GOPROXY (go env), "" when Go is not installed."""
    if not shutil.which("go"):
        return ""
    try:
        found = subprocess.run(
            ["go", "env", "GOPROXY"],  # noqa: S607  (the owner's go, as their shell finds it)
            capture_output=True, text=True, timeout=20, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return ""
    return found.stdout.strip()


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
) -> str:
    """A worker's shell init script: every tool pointed at the tier, read through to the
    owner's caches where the tool can (`read_through`), a temporary folder of its own, the JVM
    at the sandbox's proxy. Only assignments: sourcing it starts no process."""
    q = shlex.quote
    t = tier
    lines = [
        "# Chief of Staff: where this worker's tools keep their caches (rewritten at each start).",
        f"export npm_config_cache={q(str(t / 'npm'))}",
        f"export npm_config_store_dir={q(str(t / 'pnpm-store'))}",
        # npm's and pnpm's checks for a newer version of themselves: pnpm can't record one in
        # the sandbox, so it fetched its own package list (0.7 MB) at every install.
        "export npm_config_update_notifier=false",
        f"export YARN_GLOBAL_FOLDER={q(str(t / 'yarn-berry'))}",
        f"export BUN_INSTALL_CACHE_DIR={q(str(t / 'bun'))}",
        f"export PIP_CACHE_DIR={q(str(t / 'pip'))} UV_CACHE_DIR={q(str(t / 'uv'))}",
        f"export POETRY_CACHE_DIR={q(str(t / 'poetry'))} POETRY_VIRTUALENVS_IN_PROJECT=true",
        f"export PRE_COMMIT_HOME={q(str(t / 'pre-commit'))} COREPACK_HOME={q(str(t / 'corepack'))}",
        f"export CARGO_HOME={q(str(t / 'cargo'))}",
        f"export GOMODCACHE={q(str(t / 'go' / 'mod'))} GOCACHE={q(str(t / 'go' / 'build'))}",
        f"export GRADLE_USER_HOME={q(str(t / 'gradle'))}",
        # Gradle's native libraries would load from that writable folder, which the sandbox
        # refuses; its daemons are this worker's own (a daemon keeps the sandbox it started in).
        'export GRADLE_OPTS="-Dorg.gradle.native=false '
        f"-Dorg.gradle.daemon.registry.base={t / 'gradle-daemons' / temporary.name} "
        '-Dorg.gradle.daemon.idletimeout=900000${GRADLE_OPTS:+ $GRADLE_OPTS}"',
        f"export MAVEN_USER_HOME={q(str(t / 'maven'))}",
        f"export PLAYWRIGHT_BROWSERS_PATH={q(str(t / 'ms-playwright'))}",
        f"export TMPDIR={q(str(temporary) + '/')}",
    ]
    maven = f"-Dmaven.repo.local={t / 'maven' / 'repository'}"
    if read_through:
        lines.append(f"export GRADLE_RO_DEP_CACHE={q(str(owner.gradle() / 'caches'))}")
        if (owner.maven() / "repository").is_dir():
            maven += f" -Dmaven.repo.local.tail={owner.maven() / 'repository'}"
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
    return "\n".join(lines) + "\n"


def truststore(tier: Path, copilot_home: Path) -> Path | None:
    """A Java truststore for workers: the JDK's own roots and the certificate authority Copilot
    keeps (`copilot sandbox ca create`), whose proxy answers for github.com in the sandbox
    (where Gradle's distributions download from). Made once per authority; None without a
    lasting one (Copilot then makes one for each command, which no truststore can follow)."""
    authority = copilot_home / "credential-proxy-ca" / "ca.pem"
    if not authority.is_file():
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
    stamp = int(authority.stat().st_mtime)
    store = tier / "java" / f"truststore-{stamp}.p12"
    if store.is_file():
        return store
    store.parent.mkdir(parents=True, exist_ok=True)
    partial = store.with_suffix(".tmp")
    partial.unlink(missing_ok=True)
    quiet = {"capture_output": True, "check": True, "timeout": 120, "env": java_env()}
    try:
        copy = ["-importkeystore", "-noprompt", "-srckeystore", str(roots), "-srcstorepass",
                "changeit", "-destkeystore", str(partial), "-deststoretype", "PKCS12",
                "-deststorepass", "changeit"]  # fmt: skip
        add = ["-importcert", "-noprompt", "-alias", "copilot-sandbox", "-file", str(authority),
               "-keystore", str(partial), "-storepass", "changeit"]  # fmt: skip
        for arguments in (copy, add):
            subprocess.run([str(keytool), *arguments], **quiet)  # noqa: S603  (the JDK's keytool)
    except (OSError, subprocess.SubprocessError):
        partial.unlink(missing_ok=True)
        return None
    partial.rename(store)
    return store


def java_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "JAVA_TOOL_OPTIONS"}


def worker_temp(session_id: str) -> Path:
    """A worker's own temporary folder: short (Unix sockets made there must fit 104 bytes), in
    the owner's own temporary folder (which only they can enter), made only by Chief of Staff."""
    import tempfile

    base = Path(tempfile.gettempdir())
    return base / f"cos-{session_id[:8]}"


def make_temp(path: Path) -> bool:
    """Make a worker's temporary folder, the owner's alone; False if something else is there."""
    try:
        path.mkdir(mode=0o700)
        return True
    except FileExistsError:
        info = path.lstat()
        return path.is_dir() and not path.is_symlink() and info.st_uid == os.getuid()
    except OSError:
        return False


def remove_temp(session_id: str) -> None:
    """A closed worker's temporary folder goes (only one Chief of Staff made: the owner's)."""
    path = worker_temp(session_id)
    with suppress(OSError):
        if path.is_dir() and not path.is_symlink() and path.lstat().st_uid == os.getuid():
            shutil.rmtree(path, ignore_errors=True)


@dataclass
class Plan:
    """What a sandboxed worker's caches add to its sandbox, and its init script's text."""

    tier: Path  # Its tools' caches: the shared tier, or its own when it reads Slack.
    temporary: Path
    readwrite: list[str]
    readonly: list[str]
    denied: list[str]
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
    goproxy: str = "",
    truststore_path: Path | None = None,
    home: Path | None = None,
) -> Plan:
    """A worker's caches: the shared tier (a tier of its own in its workspace when it reads
    Slack, nothing of the owner's copied there), the owner's caches read-only (absent ones
    closed) and read through, uv's interpreters and tools readable, and its init script.
    Nothing is written here (see prepare)."""
    owner = Owner(home or Path.home())
    own = workspace / PRIVATE if slack else tier
    temporary = worker_temp(session_id)
    readonly, denied = held_caches(owner.home)
    readonly += [str(path) for path in owner.toolchains() if path.is_dir()]
    readonly.append(str(scripts))
    if not slack:
        readonly += [str(source) for source, _ in owner_settings(own, owner)]
    readwrite = [str(own), str(temporary)]
    text = script(
        own, owner, workspace, temporary, read_through, goproxy,
        None if slack else truststore_path,
    )  # fmt: skip
    readonly = [path for path in dict.fromkeys(readonly) if not too_broad(path)]
    return Plan(own, temporary, readwrite, readonly, list(dict.fromkeys(denied)), text, slack)


def prepare(found: Plan, session_id: str, scripts: Path, home: Path | None = None) -> Path | None:
    """Before a worker's session opens: its folders, the links into the owner's caches, and its
    init script (in `scripts`, read-only to workers). The script's path; None when its
    temporary folder could not be made its own."""
    owner = Owner(home or Path.home())
    if found.tier.is_symlink():
        found.tier.unlink()  # Its own folder only, never where a link leads.
    found.tier.mkdir(parents=True, exist_ok=True)
    link_browsers(found.tier, owner)
    if not found.slack:
        link_settings(found.tier, owner)
    if not make_temp(found.temporary):
        return None
    return write_script(scripts, session_id, found.text)


def write_script(folder: Path, session_id: str, text: str) -> Path:
    """A worker's init script, where workers can read it and never write it."""
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / f"{session_id}.sh"
    if not path.is_file() or path.read_text() != text:
        partial = path.with_suffix(".tmp")
        partial.write_text(text)
        partial.replace(path)
    return path
