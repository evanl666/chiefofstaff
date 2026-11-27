import argparse
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from importlib import metadata
from pathlib import Path

import uvicorn
from platformdirs import user_data_path

from . import claude_setup
from .store import DatabaseProblemError
from .updates import UPDATE_NOTE, Launch, own_args
from .web import create_app

# The running instance's private URL, so starting Chief of Staff again opens it.
LAUNCH_FILE = "launch-url"
# Its private link's token, kept across a restart to update it (restart_into): the page it had
# goes on.
RESTART_TOKEN = "CHIEFOFSTAFF_RESTART_TOKEN"  # noqa: S105  (a variable's name)
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")


def lock_data_file(file):
    if sys.platform == "win32":
        import msvcrt

        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError from exc
    else:
        import fcntl

        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)


def make_private(folder: Path):
    """On Windows, what umask 077 and mode 0700 do elsewhere: only this user (and, as root can
    elsewhere, SYSTEM and Administrators) may read the folder and what is made in it."""
    user = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],  # noqa: S607  (a system command)
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout.strip().rsplit(",", 1)[-1].strip('"')  # fmt: skip
    grants = [f"*{sid}:(OI)(CI)F" for sid in (user, "S-1-5-18", "S-1-5-32-544")]
    subprocess.run(  # noqa: S603  (fixed arguments: this user's id and well-known ids)
        ["icacls", str(folder), "/inheritance:r", "/grant:r", *grants],  # noqa: S607
        capture_output=True, check=True, timeout=30,
    )  # fmt: skip


def open_when_ready(port: int, url: str):
    for _ in range(100):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=0.5
            ) as reply:
                if reply.status == 200:
                    webbrowser.open(url)
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.1)
    logging.getLogger(__name__).warning("Browser not opened: server did not become ready.")


class Server(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, bus, store):
        super().__init__(config)
        self.bus = bus
        self.store = store
        # How to start again once stopped, to update (Runtime.update_when_idle).
        self.launch: Launch | None = None

    def restart(self, launch: Launch):
        """Stop as on Ctrl+C, to start again updated (restart_into); on the server's loop."""
        self.launch = launch
        self.stopping()
        self.should_exit = True

    def handle_exit(self, sig, frame):
        if self.bus.loop and not self.bus.loop.is_closed():
            self.bus.loop.call_soon_threadsafe(self.stopping)
        super().handle_exit(sig, frame)

    def stopping(self):
        """What the stop changes at once, so that it waits for nothing: open browser tabs' event
        streams end (else the server cancels them after a timeout), and what is still written
        until it stops no longer waits for the disk at each write (Store.stopping)."""
        self.bus.close()
        self.store.stopping()


def launch_token() -> tuple[str, bool]:
    """Its private link's token: the one it had before a restart to update it (the page it had
    goes on), else a new one; and whether it is that restart."""
    kept = os.environ.pop(RESTART_TOKEN, "")
    if TOKEN.fullmatch(kept):
        return kept, True
    return secrets.token_urlsafe(32), False


def upgrader(launch: Launch) -> list[str]:
    """What updates Chief of Staff where it was installed (none for uvx: the new version runs in
    its place): `uv tool upgrade`, `pipx upgrade`, or, in its own virtual environment, uv's pip
    or that environment's own."""
    if launch.how == "tool":
        return [shutil.which("uv") or "uv", "tool", "upgrade", "chiefofstaff"]
    if launch.how == "pipx":
        return [shutil.which("pipx") or "pipx", "upgrade", "chiefofstaff"]
    if launch.how == "pip":
        if uv := claude_setup.find_uv():
            return [uv, "pip", "install", "--python", sys.executable, "--upgrade", "chiefofstaff"]
        return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                "--no-input", "--upgrade", "chiefofstaff"]  # fmt: skip
    return []


def restart_into(launch: Launch, token: str, directory: Path):
    """Chief of Staff, stopped to update, updated (upgrader) and started again in this
    process's place (its terminal, its arguments, its private link); when that fails, this
    version again, and its start says why (Runtime.after_update)."""
    print("Updating Chief of Staff and starting it again…", flush=True)
    environment = {**os.environ, RESTART_TOKEN: token}
    note = directory / UPDATE_NOTE
    note.unlink(missing_ok=True)
    if upgrade := upgrader(launch):
        # Claude's Agent SDK, which `uv tool upgrade` removes (its tool's record does not name
        # it: Chief of Staff installed it), put back at once; else the new version's start sets
        # it up (claude_setup.py). (pipx and pip leave it.)
        keep = claude_setup.kept_through_upgrade() if launch.how == "tool" else ""
        try:
            done = subprocess.run(  # noqa: S603  (its installer, and its package)
                upgrade, capture_output=True, text=True, timeout=900, check=False,
                stdin=subprocess.DEVNULL,
            )  # fmt: skip
            if done.returncode:
                said = (done.stderr or done.stdout).strip()[-500:]
                note.write_text(said or f"{' '.join(upgrade[:3])} failed")
            elif keep:
                subprocess.run(  # noqa: S603  (uv, and the Claude extra's own requirement)
                    [upgrade[0], "pip", "install", "--python", sys.executable, keep],
                    capture_output=True, text=True, timeout=claude_setup.SETUP_SECONDS,
                    check=False, stdin=subprocess.DEVNULL,
                )  # fmt: skip
        except (OSError, subprocess.SubprocessError) as exc:
            note.write_text(str(exc))
    sys.stderr.flush()
    try:
        os.execve(launch.restart[0], list(launch.restart), environment)  # noqa: S606
    except OSError as exc:
        note.write_text(f"{launch.restart[0]} could not be started ({exc.strerror or exc})")
    os.execve(sys.executable, [sys.executable, "-m", "chiefofstaff", *own_args()], environment)  # noqa: S606


def running_instance(directory: Path) -> str:
    """The private URL of the Chief of Staff already serving this data directory, if it answers."""
    try:
        url = (directory / LAUNCH_FILE).read_text().strip()
        port = int(url.split(":")[2].split("/")[0])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as reply:
            return url if reply.status == 200 else ""
    except (OSError, ValueError, IndexError):
        return ""


def version() -> str:
    try:
        return metadata.version("chiefofstaff")
    except metadata.PackageNotFoundError:
        return "dev"


def default_data_dir() -> Path:
    """The data directory; one from before the rename stays in use (worktree paths are absolute)."""
    current, legacy = user_data_path("chiefofstaff"), user_data_path("work-buddy")
    return legacy if legacy.is_dir() and not current.exists() else current


def prepare_downgrade(parser: argparse.ArgumentParser, data_dir: Path) -> int:
    """--prepare-downgrade (more-agents.md 8), with Chief of Staff not running on this data
    directory: 0 when no process of another agent is left, else 1, saying which."""
    from . import agents, shepherd
    from .store import Store

    directory = data_dir.expanduser().resolve()
    if not (directory / "chiefofstaff.db").exists():
        print(f"Nothing to prepare: no Chief of Staff data in {directory}.", flush=True)
        return 0
    data_lock = (directory / "service.lock").open("a")
    try:
        lock_data_file(data_lock)
    except BlockingIOError:
        data_lock.close()
        parser.error(
            "Chief of Staff is running on this data directory: stop it (Ctrl+C in its "
            "terminal), then run this again."
        )
    try:
        store = Store(directory)
        # Prepared for you: a worker left preparing a fix is let go of first.
        preparing = shepherd.prepare_downgrade(store)
        if preparing:
            named = ", ".join(f"“{title}”" for title in preparing)
            print(f"Stopped preparing fixes in {len(preparing)} worker(s): {named}.", flush=True)
        if not agents.ever_used(store):
            # Bug E80: no "close the workers on other agents" when there never were any.
            if not preparing:
                print("Nothing to prepare: no workers ran on another agent.", flush=True)
            return 0
        stopped, left = agents.prepare_downgrade(store)
        copilot = agents.copilot_on(store)
    finally:
        data_lock.close()
    if stopped:
        named = ", ".join(f"“{title}”" for title in stopped)
        print(f"Stopped {len(stopped)} worker(s) on other coding agents: {named}.", flush=True)
    if left:
        print(
            "These processes of other coding agents are still running; end them before you "
            "downgrade:\n" + "\n".join(f"  {pid} {command}" for pid, command in left.items()),
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(
        "No process of another coding agent is running. An earlier Chief of Staff runs every "
        "worker on Copilot: close the workers on other agents you no longer need, then install "
        "it."
        + ("" if copilot else " Copilot is turned off here: an earlier one needs it, signed in."),
        flush=True,
    )
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Run Chief of Staff: a Router that runs Copilot, Codex and Claude Code as one team."
        )
    )
    parser.add_argument("--version", action="version", version=f"chiefofstaff {version()}")
    parser.add_argument("--port", type=int, default=None, help="default 8765 (8799 for --demo)")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="try it without an agent or Slack: scripted sessions in a temporary folder",
    )
    # Before installing an earlier version: the workers on other coding agents stop and none of
    # their processes may run (more-agents.md 8). Said where it applies (the guide, the Coding
    # agents group), not in --help, which stays as it was without agents.
    parser.add_argument("--prepare-downgrade", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.prepare_downgrade:
        sys.exit(prepare_downgrade(parser, args.data_dir or default_data_dir()))
    args.port = args.port or (8799 if args.demo else 8765)
    extra = {}
    if args.demo:
        from . import demo

        args.data_dir = args.data_dir or Path(tempfile.mkdtemp(prefix="chiefofstaff-demo-"))
        project = demo.make_project(args.data_dir.expanduser().resolve() / "projects")
        extra["runtime_factory"] = demo.factory(project)
    args.data_dir = args.data_dir or default_data_dir()
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port between 1024 and 65535.")
    os.umask(0o077)
    directory = args.data_dir.expanduser().resolve()
    created = not directory.exists()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created and sys.platform == "win32":
        # Only when made: icacls goes through everything already in a folder (seconds for a
        # data directory with worktrees).
        try:
            make_private(directory)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Could not make {directory} private: {exc}", file=sys.stderr, flush=True)
    data_lock = (directory / "service.lock").open("a")
    try:
        lock_data_file(data_lock)
    except BlockingIOError:
        data_lock.close()
        if url := running_instance(directory):
            print(f"Chief of Staff is already running: {url}", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            return
        parser.error(
            "Another Chief of Staff process is using this data directory and is not answering. "
            "Stop it (Ctrl+C in its terminal), or choose another --data-dir."
        )
    token, restarted = launch_token()
    url = f"http://127.0.0.1:{args.port}/#token={token}"
    # Bind before opening the browser so an unrelated listener cannot receive the launch token.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        # There SO_REUSEADDR would let another user bind the same port and take the page (and
        # its token); a port in TIME_WAIT can be bound again without it.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        # Allows an immediate restart (port in TIME_WAIT); a live listener still blocks the bind.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
    except OSError:
        listener.close()
        parser.error(f"Port {args.port} is unavailable. Choose another --port.")
    try:
        app = create_app(directory, token, args.port, **extra)
    except DatabaseProblemError as exc:
        # Said as it is, what to do with it, and no traceback.
        listener.close()
        data_lock.close()
        parser.exit(1, f"{exc}\n")
    launch = directory / LAUNCH_FILE
    launch.write_text(url)  # Readable only by the owner (umask 077, like the data directory).
    print(
        f"Chief of Staff\nPrivate launch URL (do not share): {url}\nData: {directory}", flush=True
    )
    print(
        "Closing the browser does not stop your sessions. Ctrl+C stops Chief of Staff.", flush=True
    )
    if args.demo:
        print("Demo mode: scripted sessions, no account needed. The scenario plays by itself.")
    if not args.no_browser and not restarted:
        threading.Thread(target=open_when_ready, args=(args.port, url), daemon=True).start()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=args.port, access_log=False, timeout_graceful_shutdown=3
    )
    server = Server(config, app.state.bus, app.state.store)
    app.state.runtime.restarter = server.restart
    try:
        server.run(sockets=[listener])
    except KeyboardInterrupt:
        pass
    finally:
        launch.unlink(missing_ok=True)
        listener.close()
        data_lock.close()
    if server.launch:
        restart_into(server.launch, token, directory)


if __name__ == "__main__":
    # `python -m chiefofstaff` as the `chiefofstaff` command: `send …` sends (cli.py).
    from .cli import main as command

    sys.exit(command())
