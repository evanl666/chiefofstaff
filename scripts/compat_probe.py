"""Whether this Chief of Staff runs on the Copilot CLI and SDK installed here, without a model
call: their versions and protocols, the Copilot it chooses (the CLI itself, spoken natively or
through the adapter, or Copilot's own fallback runtime), its handshake, and, signed in, a session
created and resumed on it (never sent a message). Copilot keeps its state in a folder of the
probe's own, never ~/.copilot, and is signed in only by a token in the environment
(COPILOT_GITHUB_TOKEN), never the system keychain.

    python scripts/compat_probe.py [--cli copilot] [--report probe.md]

Prints what it found (Markdown, also to --report) and exits 1 when users of this release would
not run on this CLI as it is: a protocol the SDK speaks neither itself nor through the adapter
(they get the fallback), Copilot not starting, or a mismatch opening a session. Run by the daily
Copilot compatibility workflow (.github/workflows/compat.yml) on the newest CLI.
"""

import argparse
import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from chiefofstaff import backend, compat


async def probe(cli: str) -> tuple[list[str], list[str]]:
    """(facts, problems)."""
    facts: list[str] = []
    problems: list[str] = []
    path = shutil.which(cli)
    if not path:
        return facts, [f"No Copilot CLI at `{cli}`."]
    theirs = await backend.cli_version(path)
    declared = await asyncio.to_thread(compat.cli_protocol, path, theirs)
    facts += [
        f"Copilot CLI: {theirs or 'unknown'} ({path})",
        f"Its protocol, before it starts: {declared if declared is not None else 'not known'}",
        f"Copilot SDK: {backend.sdk_version()} (protocol {compat.NATIVE}; spoken: "
        f"{', '.join(map(str, sorted(compat.SPOKEN)))})",
    ]
    backend.PREFETCH = False
    copilot = backend.copilot_backend(path)
    try:
        await copilot.start()
    except Exception as exc:
        return facts, [*problems, f"Copilot did not start: {exc}"]
    try:
        status = await copilot.client.get_status()
        protocol = getattr(copilot.client, "_negotiated_protocol_version", None) or getattr(
            status, "protocolVersion", None
        )
        if copilot.fallback:
            facts.append(f"Runs: Copilot {copilot.fallback[1]}, the fallback (protocol {protocol})")
            problems.append(
                f"Copilot CLI {theirs} speaks a protocol this SDK speaks neither itself nor "
                f"through the adapter: users run Copilot {backend.FALLBACK_CLI} instead."
            )
        else:
            how = "natively" if protocol == compat.NATIVE else "through the adapter"
            facts.append(f"Runs: the CLI itself, protocol {protocol}, {how}")
        auth = await copilot.get_auth_status()
        facts.append(f"Signed in: {'yes' if auth.isAuthenticated else 'no'}")
        session_id = str(uuid4())
        try:
            options = {
                "on_permission_request": lambda *_: None,
                "working_directory": str(Path.cwd()),
                "enable_config_discovery": True,
            }
            session = await copilot.create_session(session_id=session_id, name="probe", **options)
            await session.disconnect()
            resumed = await copilot.resume_session(session_id, **options)
            await resumed.get_events()
            await resumed.disconnect()
            facts.append("Sessions: created and resumed")
        except Exception as exc:
            if kind := compat.mismatch(exc):
                problems.append(f"A session could not be opened ({kind}): {exc}")
            elif not auth.isAuthenticated:
                facts.append(f"Sessions: not checked (not signed in: {exc})")
            else:
                problems.append(f"A session could not be opened: {exc}")
        finally:
            with contextlib.suppress(Exception):
                await copilot.client.delete_session(session_id)
    finally:
        await copilot.stop()
    return facts, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cli", default="copilot", help="the Copilot CLI to probe")
    parser.add_argument("--report", type=Path, help="also write the report (Markdown) here")
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="copilot-probe-") as home:
        os.environ["COPILOT_HOME"] = home
        os.environ["COPILOT_DISABLE_KEYTAR"] = "1"
        facts, problems = asyncio.run(probe(arguments.cli))
    lines = [f"- {fact}" for fact in facts]
    lines += [f"- **Problem:** {problem}" for problem in problems] or ["- No problem found."]
    report = "\n".join(lines) + "\n"
    sys.stdout.write(report)
    if arguments.report:
        arguments.report.write_text(report)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
