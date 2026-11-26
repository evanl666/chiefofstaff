"""The PATH without the owner's own coding agents (`claude`, `codex`), for tests and the UI and
live scripts: nothing there finds (or runs) them unless a stand-in is put on the PATH for it."""

import os
from pathlib import Path

HIDDEN = {"claude", "codex"}


def without_agents(path: str, scratch: Path, hidden: frozenset[str] = frozenset(HIDDEN)) -> str:
    """`path` with each of its folders that has a `claude` or a `codex` (or any of `hidden`:
    `copilot` too, for a computer without Copilot) mirrored without them, in folders made in
    `scratch`."""
    folders = []
    tag = "" if set(hidden) == HIDDEN else "-" + "-".join(sorted(hidden))
    for index, folder in enumerate(path.split(os.pathsep)):
        if folder and any((Path(folder) / name).exists() for name in hidden):
            mirror = scratch / f"path-without-agents{tag}-{index}"
            mirror.mkdir(parents=True, exist_ok=True)
            for entry in Path(folder).iterdir():
                if entry.name not in hidden and not (mirror / entry.name).exists():
                    (mirror / entry.name).symlink_to(entry)
            folder = str(mirror)
        folders.append(folder)
    return os.pathsep.join(folders)
