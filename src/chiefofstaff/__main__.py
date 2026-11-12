import argparse
import logging
import os
import secrets
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

from .web import create_app

# The running instance's private URL, so starting Chief of Staff again opens it.
LAUNCH_FILE = "launch-url"


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
    def __init__(self, config: uvicorn.Config, bus):
        super().__init__(config)
        self.bus = bus

    def handle_exit(self, sig, frame):
        # Open browser tabs keep an event stream open; ending them now lets the server stop at
        # once instead of cancelling them after a timeout.
        if self.bus.loop and not self.bus.loop.is_closed():
            self.bus.loop.call_soon_threadsafe(self.bus.close)
        super().handle_exit(sig, frame)


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


def main():
    parser = argparse.ArgumentParser(
        description="Run Chief of Staff: a Router that coordinates your Copilot sessions."
    )
    parser.add_argument("--version", action="version", version=f"chiefofstaff {version()}")
    parser.add_argument("--port", type=int, default=None, help="default 8765 (8799 for --demo)")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="try it without Copilot or Slack: scripted sessions in a temporary folder",
    )
    args = parser.parse_args()
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
    token = secrets.token_urlsafe(32)
    url = f"http://127.0.0.1:{args.port}/#token={token}"
    # Bind before opening the browser so an unrelated listener cannot receive the launch token.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Allows an immediate restart (port in TIME_WAIT); a live listener still blocks the bind.
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
    except OSError:
        listener.close()
        parser.error(f"Port {args.port} is unavailable. Choose another --port.")
    app = create_app(directory, token, args.port, **extra)
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
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(args.port, url), daemon=True).start()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=args.port, access_log=False, timeout_graceful_shutdown=3
    )
    try:
        Server(config, app.state.bus).run(sockets=[listener])
    except KeyboardInterrupt:
        pass
    finally:
        launch.unlink(missing_ok=True)
        listener.close()
        data_lock.close()


if __name__ == "__main__":
    # `python -m chiefofstaff` as the `chiefofstaff` command: `send …` sends (cli.py).
    from .cli import main as command

    sys.exit(command())
