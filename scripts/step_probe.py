"""Run Prepared for you's step check (step_probe.py) against the real Copilot CLI on this
computer, once, and print what it found. No model is called (each action runs through the
session's own tool pipeline) and nothing is written to GitHub (dry-run pushes, a markdown render
that needs no account, all expected to be blocked).

Copilot runs with a COPILOT_HOME of its own (made in the scratch folder, its config.json copied
from yours, read only); Chief of Staff's data is in the scratch folder too.

    uv run python scripts/step_probe.py SCRATCH_FOLDER
"""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path


def scratch_home(root: Path) -> Path:
    home = root / "copilot-home"
    home.mkdir(parents=True, exist_ok=True)
    text = (Path.home() / ".copilot" / "config.json").read_text()
    kept = [line for line in text.splitlines() if not line.lstrip().startswith("//")]
    (home / "config.json").write_text(json.dumps(json.loads("\n".join(kept))))
    # A configured MCP server: the step session must not have its tools.
    marker = {"type": "local", "command": sys.executable, "args": ["-c", "pass"], "tools": ["*"]}
    (home / "mcp-config.json").write_text(json.dumps({"mcpServers": {"probe-marker": marker}}))
    return home


async def main(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    os.environ["COPILOT_HOME"] = str(scratch_home(root))
    from chiefofstaff import step_probe
    from chiefofstaff.bus import Bus
    from chiefofstaff.runtime import Runtime, default_backend
    from chiefofstaff.store import Store

    data = root / "data"
    if data.exists():
        shutil.rmtree(data)
    store = Store(data)
    runtime = Runtime(store, Bus())
    backend = default_backend(store.settings())
    await backend.start()
    runtime.backend = backend
    try:
        result = await step_probe.probe(runtime)
    finally:
        await backend.stop()
        store.close()
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    sys.exit(asyncio.run(main(Path(sys.argv[1]).resolve())))
