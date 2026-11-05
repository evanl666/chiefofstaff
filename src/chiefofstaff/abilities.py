"""More abilities for workers than Copilot finds by itself: skills the owner uploads or names by
folder, Copilot plugins to install, and MCP servers (see Settings).

Workers load everything the owner's own Copilot sessions load (plugins, skills, MCP servers),
so a company's or the owner's setup needs nothing here; this adds to it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from .roles import WORKER

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
MAX_SKILL = 100_000
INSTALL_SECONDS = 300


def frontmatter(text: str) -> dict[str, str]:
    """A SKILL.md's name and description (simple `key: value` lines)."""
    found = FRONTMATTER.match(text)
    fields: dict[str, str] = {}
    for line in (found.group(1) if found else "").splitlines():
        key, _, value = line.partition(":")
        if value.strip():
            fields[key.strip()] = value.strip().strip("\"'")
    return fields


class Abilities:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.lock = asyncio.Lock()

    @property
    def skills_dir(self) -> Path:
        return self.store.directory / "skills"

    def skill_directories(self) -> list[str]:
        folders = [str(self.skills_dir)] if self.skills_dir.is_dir() else []
        return folders + self.store.settings().skill_folders

    def skills(self) -> list[dict]:
        found = []
        if self.skills_dir.is_dir():
            for path in sorted(self.skills_dir.glob("*/SKILL.md")):
                fields = frontmatter(path.read_text(errors="replace")[:4000])
                found.append(
                    {
                        "name": path.parent.name,
                        "description": fields.get("description", "")[:300],
                    }
                )
        return found

    async def add_skill(self, content: str) -> dict:
        """A skill the owner uploaded: a SKILL.md with a name and a description."""
        content = content.replace("\r\n", "\n").strip() + "\n"
        if len(content) > MAX_SKILL:
            raise ValueError("A skill file is at most 100 KB.")
        fields = frontmatter(content)
        name = fields.get("name", "").strip().lower().replace(" ", "-")
        if not SKILL_NAME.match(name) or not fields.get("description"):
            raise ValueError(
                "A SKILL.md starts with front matter naming the skill and saying when to use it:\n"
                "---\nname: my-skill\ndescription: What it does and when to use it\n---"
            )
        folder = self.skills_dir / name
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread((folder / "SKILL.md").write_text, content)
        await self.runtime.refresh(WORKER)
        self.runtime.publish_state()
        return {"name": name, "description": fields["description"][:300]}

    async def remove_skill(self, name: str):
        if not SKILL_NAME.match(name) or not (self.skills_dir / name).is_dir():
            raise ValueError("No such skill.")
        await asyncio.to_thread(shutil.rmtree, self.skills_dir / name)
        await self.runtime.refresh(WORKER)
        self.runtime.publish_state()

    def plugin_status(self) -> dict[str, dict]:
        return {
            source: json.loads(self.store.get(f"plugin:{source}") or '{"state": "pending"}')
            for source in self.store.settings().plugins
        }

    async def sync(self):
        """Install the plugins in Settings that are not installed yet (in Copilot itself, so
        the owner's own sessions have them too)."""
        async with self.lock:
            installed = False
            for source in self.store.settings().plugins:
                status = json.loads(self.store.get(f"plugin:{source}") or "{}")
                if status.get("state") == "installed":
                    continue
                self.store.set(f"plugin:{source}", json.dumps({"state": "installing"}))
                self.runtime.publish_state()
                try:
                    output = await self.install(source)
                except Exception as exc:
                    error = str(exc)[-500:]
                    self.store.set(
                        f"plugin:{source}", json.dumps({"state": "failed", "error": error})
                    )
                    logger.info("Could not install the plugin %s: %s", source, error)
                else:
                    self.store.set(
                        f"plugin:{source}",
                        json.dumps({"state": "installed", "note": output[-300:]}),
                    )
                    installed = True
                self.runtime.publish_state()
            if installed:
                await self.runtime.refresh(WORKER)  # Their next turn loads it.

    async def install(self, source: str) -> str:
        command = shlex.split(self.store.settings().copilot_command or "copilot")
        process = await asyncio.create_subprocess_exec(
            *command,
            "plugin",
            "install",
            source,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), INSTALL_SECONDS)
        except TimeoutError:
            process.kill()
            raise RuntimeError(
                "Installing took too long (it may wait for a confirmation)."
            ) from None
        text = output.decode(errors="replace").strip()
        if process.returncode:
            raise RuntimeError(text or f"copilot plugin install exited with {process.returncode}")
        return text

    def view(self) -> dict:
        return {"skills": self.skills(), "plugins": self.plugin_status()}
