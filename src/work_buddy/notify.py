import asyncio
import logging
import shutil
import sys

logger = logging.getLogger(__name__)


def applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def mac_notification(title: str, text: str):
    """Best-effort macOS banner; failures never affect the work that triggered it."""
    executable = shutil.which("osascript")
    if sys.platform != "darwin" or not executable:
        return
    body = " ".join(text.split())[:240]
    script = (
        f"display notification {applescript_string(body)} with title {applescript_string(title)}"
    )
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-e",
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), timeout=5)
    except (OSError, TimeoutError):
        logger.warning("macOS notification failed", exc_info=True)
