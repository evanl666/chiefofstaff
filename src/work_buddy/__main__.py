import argparse
import fcntl
import logging
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn
from platformdirs import user_data_path

from .web import create_app


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


def main():
    parser = argparse.ArgumentParser(description="Run your local Work Buddy workbench.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=user_data_path("work-buddy"))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if sys.platform == "win32":
        parser.error("This prototype currently supports macOS and Linux process management.")
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port between 1024 and 65535.")
    os.umask(0o077)
    directory = args.data_dir.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_lock = (directory / "service.lock").open("a")
    try:
        fcntl.flock(data_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        data_lock.close()
        parser.error("Another Work Buddy process is already using this data directory.")
    token = secrets.token_urlsafe(32)
    url = f"http://127.0.0.1:{args.port}/#token={token}"
    # Bind before opening the browser so an unrelated listener cannot receive the launch token.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
    except OSError:
        listener.close()
        parser.error(f"Port {args.port} is unavailable. Choose another --port.")
    app = create_app(directory, token, args.port)
    print(f"Work Buddy\nPrivate launch URL (do not share): {url}\nData: {directory}", flush=True)
    print("Closing the browser does not stop work. Ctrl+C stops this local service.", flush=True)
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(args.port, url), daemon=True).start()
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False)
    try:
        uvicorn.Server(config).run(sockets=[listener])
    finally:
        listener.close()
        data_lock.close()


if __name__ == "__main__":
    main()
