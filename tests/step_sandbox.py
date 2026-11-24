"""A sandbox policy's filesystem rules, enforced by the operating system as Copilot's sandbox
enforces them (macOS's sandbox-exec): writes only under its read-write paths, never under its
read-only ones (they win), its denied paths neither read nor written. For the tests that run a
real command against a step's actual policy (shepherd.step_policy) on a real repository."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SANDBOX_EXEC = shutil.which("sandbox-exec") if sys.platform == "darwin" else None


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


def run(policy, line: str, cwd: str | Path) -> subprocess.CompletedProcess:
    """A shell line in the policy's sandbox, as a step's command runs."""
    assert SANDBOX_EXEC, "no sandbox-exec here"
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(  # noqa: S603
        [SANDBOX_EXEC, "-p", profile(policy), "/bin/sh", "-c", line],
        cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
