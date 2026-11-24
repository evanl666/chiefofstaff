"""A sandbox policy's filesystem rules, enforced by the operating system as Copilot's sandbox
enforces them: writes only under its read-write paths, never under its read-only ones (they
win), its denied paths neither read nor written. macOS's sandbox-exec, or bubblewrap on Linux
where it runs (as Copilot's sandbox there; not in most containers or CI runners). For the tests
that run a real command against a step's actual policy (shepherd.step_policy) on a real
repository."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path


def quoted(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def spelled(path: str) -> list[str]:
    """A path as the sandbox may see it: as given and resolved (/var is /private/var)."""
    return list(dict.fromkeys([path, os.path.realpath(path)]))


def profile(policy) -> str:
    def subpaths(paths) -> str:
        return " ".join(f"(subpath {quoted(p)})" for path in paths for p in spelled(path))

    lines = ["(version 1)", "(allow default)", "(deny file-write*)"]
    if policy.readwrite:
        lines.append(f"(allow file-write* {subpaths(policy.readwrite)})")
    lines.append(
        '(allow file-write* (literal "/dev/null") (literal "/dev/tty") (regex #"^/dev/fd/"))'
    )
    if policy.readonly:
        lines.append(f"(deny file-write* {subpaths(policy.readonly)})")
    if policy.denied:
        lines.append(f"(deny file-read* file-write* {subpaths(policy.denied)})")
    return "\n".join(lines) + "\n"


def bwrap_arguments(policy) -> list[str]:
    found = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for path in policy.readwrite:
        if os.path.exists(path):  # noqa: PTH110
            found += ["--bind", path, path]
    for path in policy.readonly:
        if os.path.exists(path):  # noqa: PTH110
            found += ["--ro-bind", path, path]
    for path in policy.denied:
        if os.path.isdir(path):  # noqa: PTH112
            found += ["--tmpfs", path]
        elif os.path.exists(path):  # noqa: PTH110
            found += ["--ro-bind", "/dev/null", path]
    return found


@functools.cache
def kind() -> str:
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "macos"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        with_root = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "true"]
        try:
            works = subprocess.run(with_root, capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            works = False
        return "bwrap" if works else ""
    return ""


SANDBOX = bool(kind())


def run(policy, line: str, cwd: str | Path) -> subprocess.CompletedProcess:
    """A shell line in the policy's sandbox, as a step's command runs."""
    assert SANDBOX, "no sandbox here"
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    wrapper = (
        ["sandbox-exec", "-p", profile(policy)] if kind() == "macos" else bwrap_arguments(policy)
    )
    return subprocess.run(
        [*wrapper, "/bin/sh", "-c", line],
        cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
