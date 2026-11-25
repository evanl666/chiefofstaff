"""Builds the share links viewer for GitHub Pages: site/ as it is, with Home's own files the
viewer draws with copied into s/ (the card's renderer sharecard.js, style.css, mascot.css and
the favicon), so the viewer's card is Home's card, and the captain inlined into s/index.html (no
script writes markup there: Trusted Types allow none).

    python scripts/build_site.py [OUT]     (OUT defaults to _site)

.github/workflows/pages.yml runs it, then scripts/viewer_check.py on what it built.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
STATIC = ROOT / "src" / "chiefofstaff" / "static"
FROM_HOME = ("sharecard.js", "style.css", "mascot.css", "favicon.svg")
CAPTAIN = "<!--captain-->"


def captain() -> str:
    """The captain's SVG, as Home's mascot.js draws it."""
    module = json.dumps((STATIC / "mascot.js").as_uri())
    done = subprocess.run(
        ["node", "--input-type=module", "-e",
         f"const m = await import({module}); process.stdout.write(m.DUCK_SVG);"],
        capture_output=True, text=True, check=True, timeout=60,
    )  # fmt: skip
    svg = done.stdout.strip()
    if not svg.startswith("<svg") or "<script" in svg:
        raise SystemExit("mascot.js gave no captain")
    return svg


def build(out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SITE, out)
    for name in FROM_HOME:
        shutil.copy(STATIC / name, out / "s" / name)
    page = out / "s" / "index.html"
    html = page.read_text()
    if html.count(CAPTAIN) != 2:
        raise SystemExit("s/index.html: expected two places for the captain")
    page.write_text(html.replace(CAPTAIN, captain()))
    return out


if __name__ == "__main__":
    built = build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "_site")
    print(f"built {built}")
