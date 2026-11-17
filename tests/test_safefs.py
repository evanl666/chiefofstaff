"""Chief of Staff writes where sandboxed workers write too (their caches, their temporary and own
folders) without ever following a link one of them made (safefs.py), and nothing writes those
folders but through it."""

import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from chiefofstaff import caches, safefs, workspaces

ROOT = Path(__file__).resolve().parents[1] / "src" / "chiefofstaff"


@pytest.fixture
def victim(tmp_path):
    """One of the owner's files, which no link may lead Chief of Staff to write."""
    home = tmp_path / "home"
    home.mkdir()
    file = home / ".zshenv"
    file.write_text("export PATH=/usr/bin:/bin\n")
    file.chmod(0o644)
    return file


def untouched(victim: Path) -> bool:
    return victim.read_text() == "export PATH=/usr/bin:/bin\n" and (
        stat.S_IMODE(victim.stat().st_mode) == 0o644
    )


def test_a_folder_is_opened_without_following_any_link_on_the_way(tmp_path, victim):
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "a" / "b").symlink_to(victim.parent)
    with pytest.raises(safefs.UnsafePathError):
        safefs.open_folder(root / "a" / "b", root)
    with pytest.raises(safefs.UnsafePathError):
        safefs.open_folder(root / "a" / "b" / "c", root, create=True)
    assert not (victim.parent / "c").exists(), "nothing made through it"
    with safefs.open_folder(root / "a" / "b", root, create=True, relink=True) as folder:
        folder.write("f", "mine")
    assert (root / "a" / "b").is_dir() and not (root / "a" / "b").is_symlink()
    assert untouched(victim) and sorted(p.name for p in victim.parent.iterdir()) == [".zshenv"]


def test_writes_renames_links_and_deletions_replace_a_link_never_follow_it(tmp_path, victim):
    with safefs.open_folder(tmp_path / "tier", tmp_path, create=True) as folder:
        for name in ("file", "link", "gone", "moved", "mode"):
            (folder.path / name).symlink_to(victim)
        folder.write("file", "launcher", 0o755)
        written = folder.path / "file"
        assert written.read_text() == "launcher" and not written.is_symlink()
        assert stat.S_IMODE((folder.path / "file").stat().st_mode) == 0o755
        folder.symlink("link", tmp_path, replace=True)
        assert (folder.path / "link").readlink() == tmp_path
        folder.delete("gone")
        folder.move("file", "moved")
        folder.add_mode("mode", 0o111)
        (folder.path / "dir").mkdir()
        (folder.path / "dir" / "inside").symlink_to(victim.parent)
        folder.remove("dir")
        assert folder.read("mode") is None, "a link is not read through"
        assert not [n for n in folder.names() if n.startswith(".cos-")], "no leftover"
    assert untouched(victim) and victim.parent.is_dir()


def test_a_clone_lands_only_in_the_folder_opened(tmp_path, victim):
    source = tmp_path / "owner-cache"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "mod.py").write_text("owner's")
    with safefs.open_folder(tmp_path / "tier", tmp_path, create=True) as folder:
        try:
            assert folder.clone(source, "copy")
        except safefs.CannotCloneError:
            pytest.skip("this file system cannot clone")
        assert (folder.path / "copy" / "pkg" / "mod.py").read_text() == "owner's"
        (folder.path / "taken").symlink_to(victim)
        assert not folder.clone(source / "pkg" / "mod.py", "taken"), "never over what is there"
    assert untouched(victim)


# Chief of Staff's code that writes where workers write: through safefs only. A raw write in
# one of these is a link a worker can redirect (the security review's finding:
# launch_downloaded wrote the launcher by path).
PATH_WRITES = {
    "write_text", "write_bytes", "symlink_to", "hardlink_to", "touch", "chmod", "lchmod",
    "mkdir", "rmdir", "unlink", "rename",
}  # fmt: skip
OS_WRITES = {
    "rename", "replace", "remove", "unlink", "rmdir", "mkdir", "makedirs", "symlink", "link",
    "chmod", "lchmod", "chown", "truncate", "removedirs",
}  # fmt: skip
SHUTIL_WRITES = {"rmtree", "copy", "copy2", "copyfile", "copytree", "move", "copymode"}
GUARDED = {
    "caches.py": None,  # All of it: the tier, workers' temporary folders, the truststore.
    "workspaces.py": [
        "copy_files", "place_tree", "repoint", "move_ignored", "to_trash", "trash_in", "untrash",
        "writable", "rmtree_writable", "delete_moved",
    ],
}  # fmt: skip


def raw_writes(text: str) -> list[str]:
    import ast

    found = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name) and function.id == "open":
            mode = node.args[1] if len(node.args) > 1 else None
            if isinstance(mode, ast.Constant) and set(str(mode.value)) & set("wax+"):
                found.append(f"{node.lineno}: open(…, {mode.value!r})")
        if not isinstance(function, ast.Attribute):
            continue
        owner = function.value.id if isinstance(function.value, ast.Name) else ""
        name = function.attr
        if (
            (owner == "os" and name in OS_WRITES)
            or (owner == "shutil" and name in SHUTIL_WRITES)
            or (owner not in {"os", "shutil"} and name in PATH_WRITES)
            or (name == "replace" and len(node.args) == 1 and not node.keywords)  # Path's
        ):
            found.append(f"{node.lineno}: {ast.unparse(node)[:90]}")
    return found


def test_nothing_writes_where_workers_write_but_through_safefs():
    import inspect
    import textwrap

    found = []
    for name, functions in GUARDED.items():
        sources = (
            [(ROOT / name).read_text()]
            if functions is None
            else [inspect.getsource(getattr(workspaces, function)) for function in functions]
        )
        for text in sources:
            found += [f"{name}:{line}" for line in raw_writes(textwrap.dedent(text))]
    assert not found, "\n".join(found)
    assert raw_writes("p.write_text('x')\nos.rename(a, b)\nPath(x).replace(y)") == [
        "1: p.write_text('x')", "2: os.rename(a, b)", "3: Path(x).replace(y)",
    ], "the check itself"  # fmt: skip


ATTACKER = r"""
import os, sys, time
tier, victim, count, seconds, swap = sys.argv[1:6]
count, seconds, swap = int(count), float(seconds), swap == "swap"
shells = [f"{tier}/ms-playwright/chromium_headless_shell-{9000 + i}" for i in range(count)]
def arm():
    for shell in shells:
        folder = f"{shell}/chrome-mac"
        for path in (folder + ".x", folder):
            if os.path.islink(path):
                os.unlink(path)
        os.makedirs(folder, exist_ok=True)
        open(f"{shell}/INSTALLATION_COMPLETE", "w").close()
        for path in (f"{folder}/chrome-headless-shell", f"{folder}/chrome-headless-shell.real"):
            if os.path.lexists(path):
                os.unlink(path)
        with open(f"{folder}/chrome-headless-shell", "wb") as file:
            file.write(b"\x7fELF" + b"\0" * 5000)
arm()
open(f"{tier}/armed", "w").close()
end = time.time() + seconds
while time.time() < end:
    if os.path.exists(f"{tier}/rearm"):
        os.unlink(f"{tier}/rearm"); arm(); open(f"{tier}/armed", "w").close()
    for shell in shells:
        folder = f"{shell}/chrome-mac"
        try:
            if swap:  # The program's folder, swapped for a link to the owner's.
                os.rename(folder, folder + ".x"); os.symlink(victim, folder)
                os.unlink(folder); os.rename(folder + ".x", folder)
            else:  # A link where the program was renamed from.
                os.symlink(victim, f"{folder}/chrome-headless-shell")
        except OSError:
            pass
"""


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("sandbox-exec"), reason="macOS's sandbox"
)
@pytest.mark.parametrize("swap", ["link", "swap"])
def test_a_sandboxed_worker_racing_chief_of_staffs_writes_in_the_tier_never_wins(
    tmp_path, victim, swap
):
    """The security review's race, both ways (a link where launch_downloaded renamed the program
    from; the program's folder swapped for a link): a worker in a sandbox that may write only
    the tier, against Chief of Staff readying the tier as each worker starts, 200 times."""
    tier = (tmp_path / "chief" / "workers").resolve()
    tier.mkdir(parents=True)
    owner_dir = victim.parent
    if swap == "swap":
        big = owner_dir / "chrome-headless-shell"
        big.write_bytes(b"\0" * 8192)
    target = owner_dir if swap == "swap" else victim
    profile = tmp_path / "worker.sb"
    profile.write_text(
        "(version 1)\n(deny default)\n"
        "(allow process-fork) (allow process-exec) (allow sysctl-read) (allow mach-lookup)\n"
        '(allow file-read*)\n(allow file-write* (literal "/dev/null"))\n'
        f'(allow file-read* file-write* (subpath "{tier}"))\n'
    )
    script = tmp_path / "attacker.py"
    script.write_text(ATTACKER)
    attacker = subprocess.Popen(
        ["sandbox-exec", "-f", str(profile), sys.executable, str(script), str(tier),
         str(target), "4", "120", swap],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )  # fmt: skip
    owner = caches.Owner(tmp_path / "no-browsers")
    before = sorted(p.name for p in owner_dir.iterdir())
    try:
        for _ in range(200):
            deadline = time.monotonic() + 20
            while not (tier / "armed").exists():
                assert attacker.poll() is None, attacker.stderr.read().decode()
                assert time.monotonic() < deadline, "the attacker stopped arming"
                time.sleep(0.002)
            (tier / "armed").unlink()
            with caches.tier_folder(tier) as folder:
                caches.link_browsers(folder, owner)  # As each worker starts (prepare).
            assert untouched(victim), "the owner's file written"
            assert sorted(p.name for p in owner_dir.iterdir()) == before, "the owner's folder"
            (tier / "rearm").touch()
    finally:
        attacker.kill()
        attacker.wait()
    if swap == "swap":
        assert big.stat().st_size == 8192
