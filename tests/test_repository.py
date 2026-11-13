"""The repository holds what Chief of Staff ships and its tests and docs: nothing a run left."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# What is at its top. Something new there is added here on purpose, never by a stray `git add`.
TOP = {
    ".github",
    ".gitignore",
    ".gitleaksignore",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "README.zh-CN.md",
    "SECURITY.md",
    "docs",
    "eslint.config.mjs",
    "pyproject.toml",
    "scripts",
    "slack-manifest.json",
    "src",
    "tests",
    "uv.lock",
}


def test_only_what_it_ships_is_tracked_at_the_top_of_the_repository():
    """Review: a test's output (out.json) was committed at the top of the repository."""
    done = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=False
    )
    if done.returncode or not done.stdout:
        pytest.skip("Not a git checkout.")
    top = {line.split("/", 1)[0] for line in done.stdout.splitlines()}
    assert top <= TOP, f"not expected at the top: {sorted(top - TOP)}"
