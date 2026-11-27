"""The repository holds what Chief of Staff ships and its tests and docs: nothing a run left."""

import re
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
    "SECURITY-REVIEW.md",
    "SECURITY.md",
    "docs",
    "eslint.config.mjs",
    "pyproject.toml",
    "scripts",
    # The share links viewer, published on GitHub Pages (.github/workflows/pages.yml).
    "site",
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


def anchors(path: Path) -> set[str]:
    """The places a link can land on in a Markdown file, as GitHub names them: its headings
    (outside code blocks; a repeated one numbered) and its `<a id>`s."""
    found: set[str] = set()
    seen: dict[str, int] = {}
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and (heading := re.match(r"#{1,6} (.+)", line)):
            words = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading.group(1))
            words = re.sub(r"[^\w\- ]", "", words.replace("`", "").strip().lower())
            name = words.replace(" ", "-")
            found.add(f"{name}-{seen[name]}" if seen.get(name) else name)
            seen[name] = seen.get(name, 0) + 1
        found |= set(re.findall(r'<a (?:id|name)="([^"]+)"', line))
    return found


def test_the_docs_links_land_where_they_point():
    """The guide was cut down and its detail moved to docs/reference.md (0.1.61): every link
    between the docs, and every link of the app's into them (released versions' too, which
    point at main), still lands on a file and a section that are there."""
    broken = []
    for doc in [*ROOT.glob("*.md"), *ROOT.glob("docs/*.md")]:
        text = re.sub(r"```.*?```", "", doc.read_text(encoding="utf-8"), flags=re.DOTALL)
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if re.match(r"https?:|mailto:", target):
                continue
            name, _, anchor = target.partition("#")
            path = (doc.parent / name).resolve() if name else doc
            if not path.exists() or (anchor and anchor not in anchors(path)):
                broken.append(f"{doc.name}: {target}")
    app = [path for path in (ROOT / "src").rglob("*") if path.suffix in {".py", ".js", ".html"}]
    for source in app:
        for doc, anchor in re.findall(r"docs/(guide|reference)\.md#([\w-]+)", source.read_text()):
            if anchor not in anchors(ROOT / "docs" / f"{doc}.md"):
                broken.append(f"{source.name}: docs/{doc}.md#{anchor}")
    # Released versions link these (session.js, agentspage.js, index.html, the README).
    for anchor in ("workers-on-other-coding-agents", "send-to-chief", "slack", "github-reviews"):
        if anchor not in anchors(ROOT / "docs" / "guide.md"):
            broken.append(f"docs/guide.md#{anchor}")
    assert not broken, broken
