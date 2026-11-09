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
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

from .roles import WORKER

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)

SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)
KEY_LINE = re.compile(r"([A-Za-z_][\w-]*)[ \t]*:(.*)")
BLOCK = re.compile(r"([|>])[-+0-9]*[ \t]*(#.*)?")
BOM = "\ufeff"
NO_FRONT_MATTER = (
    "A SKILL.md starts with front matter naming the skill and saying when to use it:\n"
    "---\nname: my-skill\ndescription: What it does and when to use it\n---"
)
MAX_SKILL = 100_000
INSTALL_SECONDS = 300
# git's and Copilot's lines that say what went wrong, among the clone's log.
ERROR_LINE = re.compile(r"(fatal|error|remote)\b\s*:", re.IGNORECASE)
LONG_PATH = re.compile(r"(?<![\w:/.~])~?(?:/[^\s'\"/]+){4,}/?")


def scalar(head: str, rest: list[str]) -> str:
    """A front matter value as YAML reads it: plain or quoted, a folded (>) or literal (|)
    block, or continued on the indented lines after it (`rest`)."""
    head = head.strip()
    if BLOCK.fullmatch(head):
        body = textwrap.dedent("\n".join(rest)).strip()
        if head[0] == "|":
            return body
        return "\n".join(" ".join(part.split()) for part in re.split(r"\n[ \t]*\n", body))
    joined = " ".join(part for part in [head, *(line.strip() for line in rest)] if part)
    if quoted := re.match(r'"((?:[^"\\]|\\.)*)"', joined):
        try:
            return json.loads(f'"{quoted[1]}"')
        except ValueError:
            return quoted[1].replace('\\"', '"')
    if quoted := re.match(r"'((?:[^']|'')*)'", joined):
        return quoted[1].replace("''", "'")
    parts = [re.sub(r"(^|\s)#.*", "", part).strip() for part in [head, *rest]]
    return " ".join(part for part in parts if part)


def front_matter(text: str) -> tuple[tuple[int, int], dict[str, tuple[str, int, int]]] | None:
    """Where a SKILL.md's front matter is, and its top-level entries: key -> (value, its first
    line, the line after its last). None without front matter."""
    found = FRONTMATTER.match(text)
    if not found:
        return None
    lines = found.group(1).split("\n")
    entries: dict[str, tuple[str, int, int]] = {}
    index = 0
    while index < len(lines):
        end = index + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
            end += 1
        stop = end
        while stop > index + 1 and not lines[stop - 1].strip():
            stop -= 1
        if key := KEY_LINE.fullmatch(lines[index]):
            entries.setdefault(key[1], (scalar(key[2], lines[index + 1 : stop]), index, stop))
        index = end
    return found.span(1), entries


def frontmatter(text: str) -> dict[str, str]:
    """A SKILL.md's front matter fields (name, description…)."""
    parsed = front_matter(text.removeprefix(BOM))
    return {key: value for key, (value, _, _) in parsed[1].items()} if parsed else {}


def install_error(text: str) -> str:
    """Why a plugin did not install, in a line or two: the error lines (not git's whole log),
    long paths shortened, cut at a word."""
    text = re.sub(r"\s+(?=(?:fatal|error|remote)\s*:)", "\n", text, flags=re.IGNORECASE)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    errors = [line for line in lines if ERROR_LINE.match(line)]
    picked = [re.sub(r"^remote:\s*", "", line) for line in errors[-2:] or lines[-1:]]
    said = " ".join(dict.fromkeys(picked)).replace(str(Path.home()), "~")
    said = LONG_PATH.sub(lambda found: short_path(found[0]), said)
    if len(said) <= 300:
        return said
    return said[:299].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def short_path(path: str) -> str:
    """~/…/clone-x/repo for a long path."""
    parts = path.rstrip("/").split("/")
    return "/".join([parts[0], "…", *parts[-2:]])


class Abilities:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.lock = asyncio.Lock()
        self.skill_lock = asyncio.Lock()

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
        """A skill the owner uploaded: a SKILL.md with a name and a description. A name written
        otherwise ("My Skill") is saved as listed ("my-skill"), so workers see that name too."""
        content = content.removeprefix(BOM).replace("\r\n", "\n").strip() + "\n"
        if len(content) > MAX_SKILL:
            raise ValueError("A skill file is at most 100 KB.")
        parsed = front_matter(content)
        if not parsed:
            raise ValueError(NO_FRONT_MATTER)
        (start, end), entries = parsed
        given, first, after = entries.get("name", ("", 0, 0))
        name = re.sub(r"\s+", "-", given.strip().lower())
        description = entries.get("description", ("", 0, 0))[0].strip()
        problems = []
        if not given.strip():
            problems.append(
                f"name: {'empty' if 'name' in entries else 'missing'}. Name the skill in "
                "lowercase letters, digits and hyphens, like my-skill."
            )
        elif len(name) > 63:
            problems.append("name: at most 63 characters.")
        elif not SKILL_NAME.match(name):
            problems.append(
                "name: use lowercase letters, digits and hyphens, like my-skill "
                f"(not {given.strip()})."
            )
        if not description:
            problems.append(
                f"description: {'empty' if 'description' in entries else 'missing'}. Say what "
                "the skill does and when to use it."
            )
        if problems:
            raise ValueError("\n".join(problems))
        if given != name:
            lines = content[start:end].split("\n")
            lines[first:after] = [f"name: {name}"]
            content = content[:start] + "\n".join(lines) + content[end:]
        folder = self.skills_dir / name
        async with self.skill_lock:
            if await asyncio.to_thread((folder / "SKILL.md").is_file):
                saved = await asyncio.to_thread((folder / "SKILL.md").read_text, errors="replace")
                if saved == content:
                    raise ValueError(f"You already have this skill: {name}.")
                raise ValueError(f"A skill named {name} exists: remove it first.")
            await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread((folder / "SKILL.md").write_text, content)
        await self.runtime.refresh(WORKER)
        self.runtime.publish_state()
        return {"name": name, "description": description[:300]}

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
                    error = install_error(str(exc))
                    self.store.set(
                        f"plugin:{source}", json.dumps({"state": "failed", "error": error})
                    )
                    logger.info("Could not install the plugin %s: %s", source, str(exc)[-2000:])
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
