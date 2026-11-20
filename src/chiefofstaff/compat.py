"""Copilot versions, so that none stops Chief of Staff: the protocol the owner's Copilot CLI
speaks (told cheaply, before it starts), the one this SDK speaks and the one it speaks through a
small adapter, the Copilot runtimes already on this machine, and which errors of a newer Copilot
are a version mismatch rather than a fault of the session.

The Copilot CLI updates itself (every two days or so); the SDK speaking a new protocol comes
later. Protocol 4 (Copilot 1.0.113, 2026-11-17) changed only how session.create, session.resume
and session.options.update carry their options (grouped: {"inference": {"reasoningEffort"}}, not
{"reasoningEffort"}); every other method, every event and the sessions kept on disk stayed as
they were. This SDK (protocol 3) speaks it with those options grouped as the SDK of protocol 4
groups them (ADAPTED).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from copilot._sdk_protocol_version import get_sdk_protocol_version

logger = logging.getLogger(__name__)

# The protocol this SDK speaks, and those it speaks through the adapter (only over protocol 3:
# an SDK that speaks 4 itself needs none).
NATIVE = get_sdk_protocol_version()
ADAPTED = frozenset({4}) if NATIVE == 3 else frozenset()
SPOKEN = frozenset({NATIVE}) | ADAPTED

# Each protocol's first Copilot CLI, and the newest one whose protocol is known here: a later
# one's is read from its package (copilot-sdk/sdkProtocolVersion.d.ts), else told at its
# handshake.
PROTOCOL_SINCE = (("1.0.113", 4),)
OLDEST_PROTOCOL = 3
KNOWN_THROUGH = "1.0.115"
PROTOCOL_FILE = Path("copilot-sdk", "sdkProtocolVersion.d.ts")
DECLARED = re.compile(r"SDK_PROTOCOL_VERSION\s*=\s*(\d+)")


def version_parts(version: str) -> tuple[int, ...]:
    """1.0.115-1 -> (1, 0, 115): a prerelease counts as its release."""
    found = re.match(r"(\d+)\.(\d+)\.(\d+)", version or "")
    return tuple(int(part) for part in found.groups()) if found else ()


def protocol_by_version(version: str) -> int | None:
    """The protocol a Copilot CLI of this version speaks, as known here; None beyond that."""
    mine = version_parts(version)
    if not mine or mine > version_parts(KNOWN_THROUGH):
        return None
    spoken = OLDEST_PROTOCOL
    for first, protocol in PROTOCOL_SINCE:
        if mine >= version_parts(first):
            spoken = protocol
    return spoken


def package_roots() -> list[Path]:
    """Where the Copilot CLI keeps the versions it updated itself to (`<root>/<platform>/
    <version>`), in the order the CLI itself looks."""
    home = Path.home()
    cache = os.environ.get("XDG_CACHE_HOME") or str(home / ".cache")
    if sys.platform == "darwin":
        own = home / "Library" / "Caches" / "copilot"
    elif sys.platform == "win32":
        own = Path(os.environ.get("LOCALAPPDATA") or cache) / "copilot"
    else:
        own = Path(cache) / "copilot"
    roots = [os.environ.get("COPILOT_PKG_CACHE_HOME"), os.environ.get("COPILOT_CACHE_HOME"),
             str(own), str(Path(cache) / "copilot"), os.environ.get("COPILOT_HOME"),
             str(home / ".copilot")]  # fmt: skip
    return list(dict.fromkeys(Path(root) / "pkg" for root in roots if root))


def declared_protocol(directory: Path, version: str) -> int | None:
    """The protocol a Copilot CLI package of this version says it speaks (its bundled SDK's)."""
    try:
        package = json.loads((directory / "package.json").read_text())
        if not isinstance(package, dict) or package.get("version") != version:
            return None
        found = DECLARED.search((directory / PROTOCOL_FILE).read_text())
    except (OSError, ValueError):
        return None
    return int(found.group(1)) if found else None


def cli_protocol(command: str, version: str) -> int | None:
    """The protocol the owner's Copilot CLI speaks, without starting it (a few small files: no
    handshake): what its package says (an npm install of that version, or the version it updated
    itself to), else what is known of its version; None when only its handshake can tell.
    Blocking: in a thread."""
    if not version:
        return None
    places = [Path(os.path.realpath(command)).parent]
    for root in package_roots():
        with suppress(OSError):
            places += sorted(root.glob(f"*/{version}"))
    for place in places:
        if (found := declared_protocol(place, version)) is not None:
            return found
    return protocol_by_version(version)


@dataclass(frozen=True)
class Installed:
    """A Copilot runtime already on this machine: its version, what the SDK starts (a program,
    or a script the SDK runs with node) and the protocol it speaks."""

    version: str
    path: str
    protocol: int


def sdk_cache_root() -> Path | None:
    """The SDK's own cache of Copilot runtimes (one folder a version); None without one."""
    try:
        from copilot._cli_download import get_cache_dir
    except ImportError:
        return None
    if os.environ.get("COPILOT_CLI_EXTRACT_DIR"):
        return None  # (One version's folder: the fallback's own.)
    return get_cache_dir(None)


def installed_runtimes(spoken: frozenset[int] = SPOKEN) -> list[Installed]:
    """The Copilot runtimes on this machine this SDK speaks, best first: the SDK's own complete
    bundles (from an earlier download, by this or another SDK), then the versions the owner's
    CLI updated itself to (run with node, as their package's script), each newest first.
    Blocking: in a thread."""
    found: list[Installed] = []
    root = sdk_cache_root()
    try:
        from copilot._cli_version import get_runtime_platform

        platform = get_runtime_platform()
    except (ImportError, RuntimeError):
        platform = ""
    wrapper = "copilot-runtime.exe" if sys.platform == "win32" else "copilot-runtime"
    if root and platform:
        with suppress(OSError):
            for folder in root.iterdir():
                bundle = folder / "prebuilds" / platform
                needed = (bundle / wrapper, bundle / "runtime.node",
                          bundle / ".hostless-runtime-assets-v2")  # fmt: skip
                protocol = protocol_by_version(folder.name)
                if protocol in spoken and all(_filled(path) for path in needed):
                    found.append(Installed(folder.name, str(bundle / wrapper), protocol))
    found.sort(key=lambda each: version_parts(each.version), reverse=True)
    packaged: list[Installed] = []
    if shutil.which("node"):
        for root in package_roots():
            with suppress(OSError):
                for folder in root.glob("*/*"):
                    protocol = declared_protocol(folder, folder.name)
                    if protocol in spoken and _filled(folder / "index.js"):
                        packaged.append(Installed(folder.name, str(folder / "index.js"), protocol))
    packaged.sort(key=lambda each: version_parts(each.version), reverse=True)
    return found + packaged


def _filled(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


# The options session.create and session.resume carry in protocol 4, grouped: each name of
# protocol 3 -> where protocol 4 has it (as the SDK of protocol 4 groups them); a name not
# listed stays where it is (sessionId, model, workingDirectory, systemMessage, ...).
GROUPED = {
    "allowMultipleToolCalls": ("inference", "allowMultipleToolCalls"),
    "allowedModels": ("policy", "allowedModels"),
    "reasoningEffort": ("inference", "reasoningEffort"),
    "reasoningSummary": ("inference", "reasoningSummary"),
    "enableExperimentalMode": ("experiments", "enableExperimentalMode"),
    "contextTier": ("inference", "contextTier"),
    "modelCapabilities": ("inference", "capabilities"),
    "largeOutput": ("tools", "largeOutput"),
    "tools": ("tools", "definitions"),
    "canvases": ("canvases", "definitions"),
    "requestCanvasRenderer": ("canvases", "requestRenderer"),
    "requestExtensions": ("extensions", "enabled"),
    "extensionSdkPath": ("extensions", "sdkPath"),
    "extensionInfo": ("extensions", "identity"),
    "canvasProvider": ("canvases", "provider"),
    "commands": ("interaction", "commands"),
    "toolSearch": ("tools", "search"),
    "imageGeneration": ("tools", "imageGeneration"),
    "availableTools": ("tools", "availableTools"),
    "excludedTools": ("tools", "excludedTools"),
    "excludedBuiltinAgents": ("agents", "excludedBuiltinAgents"),
    "includedBuiltinAgents": ("agents", "includedBuiltinAgents"),
    "includedBuiltinSkills": ("skills", "includedBuiltinSkills"),
    "defaultProvider": ("inference", "defaultProvider"),
    "capi": ("github", "capi"),
    "providers": ("inference", "providers"),
    "models": ("inference", "models"),
    "enableSessionTelemetry": ("github", "telemetryEnabled"),
    "enableCitations": ("inference", "enableCitations"),
    "enableFileChangeTracking": ("filesystem", "trackFileChanges"),
    "sessionLimits": ("execution", "limits"),
    "autopilot": ("execution", "autopilot"),
    "skipCustomInstructions": ("instructions", "skipCustomInstructions"),
    "customAgentsLocalOnly": ("agents", "localDiscoveryOnly"),
    "coauthorEnabled": ("git", "coauthorEnabled"),
    "manageScheduleEnabled": ("tools", "manageScheduleEnabled"),
    "askUserVariant": ("interaction", "askUserVariant"),
    "enableMcpApps": ("mcp", "enableApps"),
    "githubMcpToolConfig": ("github", "mcp"),
    "autoModeRecovery": ("interaction", "autoModeRecovery"),
    "hooks": ("hooks", "hasHandlers"),
    "additionalDirectories": ("filesystem", "additionalDirectories"),
    "streaming": ("events", "streaming"),
    "includeSubAgentStreamingEvents": ("events", "includeSubAgentStreamingEvents"),
    "mcpOAuthTokenStorage": ("mcp", "oauthTokenStorage"),
    "authClientIdMetadataUrl": ("mcp", "authClientIdMetadataUrl"),
    "mcpServers": ("mcp", "servers"),
    "customAgents": ("agents", "definitions"),
    "defaultAgent": ("agents", "default"),
    "agent": ("agents", "selected"),
    "skillDirectories": ("skills", "directories"),
    "agentDirectories": ("agents", "directories"),
    "hookDirectories": ("hooks", "directories"),
    "pluginDirectories": ("plugins", "directories"),
    "instructionDirectories": ("instructions", "directories"),
    "disabledSkills": ("skills", "disabledSkills"),
    "disabledMcpServers": ("mcp", "disabledServers"),
    "sandboxConfig": ("permissions", "sandbox"),
    "infiniteSessions": ("state", "infiniteSessions"),
    "memory": ("state", "memory"),
    "gitHubToken": ("github", "token"),
    "enableManagedSettings": ("github", "fetchManagedSettings"),
    "excludedManagedSettings": ("policy", "excludedManagedSettings"),
    "enforceManagedModelDefaults": ("policy", "enforceManagedModelDefaults"),
    "interactiveUser": ("github", "interactiveUser"),
    "permissionMode": ("permissions", "mode"),
    "managedSettings": ("policy", "managedSettings"),
    "skipEmbeddingRetrieval": ("state", "skipEmbeddingRetrieval"),
    "embeddingCacheStorage": ("state", "embeddingCacheStorage"),
    "organizationCustomInstructions": ("instructions", "organizationText"),
    "enableOnDemandInstructionDiscovery": ("instructions", "onDemandDiscovery"),
    "enableFileHooks": ("hooks", "enableRepositoryHooks"),
    "enableHostGitOperations": ("git", "enableHostGitOperations"),
    "enableSessionStore": ("state", "enableCrossSessionStore"),
    "enableSkills": ("skills", "enabled"),
    "featureFlags": ("experiments", "flags"),
    "expAssignments": ("experiments", "assignments"),
    "trustWorkingDirectory": ("filesystem", "trustWorkingDirectory"),
    "refreshCustomInstructions": ("instructions", "refreshCustomInstructions"),
    "openCanvases": ("canvases", "open"),
    "requestPermission": ("permissions", "request"),
    "requestUserInput": ("interaction", "requestUserInput"),
    "requestShellAuthentication": ("interaction", "requestShellAuthentication"),
    "requestElicitation": ("interaction", "requestElicitation"),
    "requestExitPlanMode": ("interaction", "requestExitPlanMode"),
    "requestAutoModeSwitch": ("interaction", "requestAutoModeSwitch"),
    "requestAutoModeRecovery": ("interaction", "requestAutoModeRecovery"),
    "hasSkillProvider": ("skills", "hasProvider"),
    "hasMemoryStore": ("state", "hasMemoryStore"),
    "gitHubTokenProviderRegistrationId": ("github", "tokenProviderRegistrationId"),
    "configDir": ("configDirectory",),
    "envValueMode": ("mcp", "envValueMode"),
    "requestMcpApps": ("mcp", "enableApps"),
    "isExperimentalMode": ("experiments", "enableExperimentalMode"),
    "enableGitHubTelemetryForwarding": ("github", "telemetryForwarding"),
    "disableResume": ("suppressResumeEvent",),
    "webSearch": ("tools", "webSearch"),
    "shell": ("tools", "shell"),
    "managedMcpServers": ("mcp", "managedServers"),
    "ignoredSkillsLocations": ("skills", "ignoredLocations"),
    "allowAllMcpServerInstructions": ("mcp", "allowAllServerInstructions"),
    "autopilotContinuation": ("execution", "autopilotContinuation"),
}
# Protocol 3 sent these on every open; protocol 4 has no place for them (its default is theirs).
DROPPED = {"toolFilterPrecedence": "excluded"}
# session.options.update's own names (it takes the mutable subset, grouped as above too).
UPDATE_GROUPED = GROUPED | {
    "toolFilterPrecedence": ("tools", "filterPrecedence"),
    "suppressCustomAgentPrompt": ("agents", "suppressCustomAgentPrompt"),
    "enableReasoningSummaries": ("inference", "enableReasoningSummaries"),
    "shellProcessFlags": ("tools", "shellProcessFlags"),
    "logInteractiveShells": ("tools", "logInteractiveShells"),
    "maxInlineBinaryBytes": ("tools", "maxInlineBinaryBytes"),
    "askUserDisabled": ("interaction", "askUserDisabled"),
    "runningInInteractiveMode": ("interaction", "runningInInteractiveMode"),
    "continueOnAutoMode": ("execution", "continueOnAutoMode"),
    "eventsLogDirectory": ("state", "eventsLogDirectory"),
    "eventsLogIncludesSubagents": ("state", "eventsLogIncludesSubagents"),
    "trajectoryFile": ("state", "trajectoryFile"),
    "copilotUrl": ("github", "copilotUrl"),
    "additionalContentExclusionPolicies": ("policy", "additionalContentExclusionPolicies"),
    "verbosity": ("inference", "verbosity"),
}


def grouped(params: dict[str, Any], table: dict[str, tuple[str, ...]] = GROUPED) -> dict:
    """Protocol 3's options of an open (or, with UPDATE_GROUPED, of an update) as protocol 4
    takes them."""
    result: dict[str, Any] = {}
    for name, value in params.items():
        if table is GROUPED and DROPPED.get(name, object()) == value:
            continue
        path = table.get(name, (name,))
        if len(path) == 1:
            result[path[0]] = value
        else:
            result.setdefault(path[0], {})[path[1]] = value
    return result


def adapt(client: Any, protocol: int) -> None:
    """A client of this SDK (connected, its handshake done) speaking `protocol` (ADAPTED) to
    its runtime: the options of what opens and updates a session go as that protocol has them."""
    rpc = client._client
    request = rpc.request

    async def adapted(method: str, params: dict | None = None, *args: Any, **kwargs: Any) -> Any:
        if params and method in ("session.create", "session.resume"):
            params = grouped(params)
        elif params and method == "session.options.update":
            params = grouped(params, UPDATE_GROUPED)
        return await request(method, params, *args, **kwargs)

    rpc.request = adapted
    client._negotiated_protocol_version = protocol


# Errors a newer (or older) Copilot gives a session it opens or resumes for something of the
# SDK's or the session's it does not speak: a version mismatch, not the session's fault, met by
# opening it afresh (Runtime.continue_fresh). Kind -> what Copilot says, each as it said it;
# narrow on purpose: any other error is the session's own and said as it is.
MISMATCHES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("protocol", re.compile(
        r"SDK protocol version mismatch|SDK supports versions \d+-\d+, but server")),
    ("unknown method", re.compile(
        r"\bUnhandled method [\w.$/-]+|\bunknown method: [\w.$/-]+|\bMethod not found\b")),
    ("missing parameter", re.compile(
        r"\bextensionSdkPath is required\b|\bInvalid params: missing field `\w+`")),
    ("extensions", re.compile(
        r"Failed to initialize standalone session extensions?:|No extension launch provider is "
        r"registered for standalone extensions", re.IGNORECASE)),
    ("session format", re.compile(
        r"Legacy session import events are no longer supported|unsupported checkpoint schema "
        r"version")),
)  # fmt: skip


def mismatch(error: BaseException | str) -> str:
    """The kind of version mismatch (MISMATCHES) an error of Copilot's is, "" for none."""
    text = str(error)
    for kind, pattern in MISMATCHES:
        if pattern.search(text):
            return kind
    return ""


# Events a newer Copilot sends that this SDK does not know: a value it has no member for (an
# enum's) is taken as it is, as the SDK's own open enums do; an event it still can't read (a new
# type, a field changed) is taken as one of unknown type, rather than failing (the SDK failed a
# session's whole history for one, and dropped a live one with a traceback). Said once each, at
# debug.
_said: set[str] = set()


def open_enum(kind: type[Enum]) -> None:
    """An SDK enum that takes a value a later Copilot sends as a member of that value (as the
    SDK's own open enums do), rather than failing the event: a session.shutdown's "destroy",
    which Copilot 1.0.112 sends when a session is deleted, failed with a traceback in the log
    and the event was dropped."""
    if "_missing_" in vars(kind):
        return

    def missing(cls: type[Enum], value: object) -> Enum | None:
        if not isinstance(value, str):
            return None
        if value not in _said:
            _said.add(value)
            logger.debug("A Copilot event's %s this SDK doesn't know: %r", cls.__name__, value)
        member = object.__new__(cls)
        member._name_ = "UNKNOWN"
        member._value_ = value
        return cls._value2member_map_.setdefault(value, member)

    kind._missing_ = classmethod(missing)  # type: ignore[method-assign, assignment]


def tolerant(parse: Any) -> Any:
    """The SDK's event parser, taking what it can't read as an event of unknown type."""
    from copilot.generated.session_events import (
        RawSessionEventData,
        SessionEvent,
        SessionEventType,
    )

    def parsing(obj: Any) -> Any:
        try:
            event = parse(obj)
        except Exception as exc:
            fields = obj if isinstance(obj, dict) else {}
            kind = str(fields.get("type") or type(obj).__name__)
            if kind not in _said:
                _said.add(kind)
                logger.debug("A Copilot event this SDK can't read (%s): %s", kind, exc)
            return SessionEvent(
                data=RawSessionEventData(fields.get("data")), id=fields.get("id"),  # type: ignore[arg-type]
                timestamp=fields.get("timestamp"), type=SessionEventType.UNKNOWN,  # type: ignore[arg-type]
                ephemeral=fields.get("ephemeral"), parent_id=fields.get("parentId"),
                raw_type=kind,
            )  # fmt: skip
        if event.type == SessionEventType.UNKNOWN and (kind := str(event.raw_type)) not in _said:
            _said.add(kind)
            logger.debug("A Copilot event of a type this SDK doesn't know: %s", kind)
        return event

    parsing.tolerant = True  # type: ignore[attr-defined]
    return parsing


def tolerate_newer_events() -> None:
    """Every enum of the SDK's session events open, and every place it parses an event taking
    one it can't read. Once (again: nothing more)."""
    import copilot.client
    import copilot.generated.session_events as events
    import copilot.session

    for kind in vars(events).values():
        if isinstance(kind, type) and issubclass(kind, Enum) and kind.__module__ == events.__name__:
            open_enum(kind)
    for module in (copilot.client, copilot.session, events):
        parse = getattr(module, "session_event_from_dict", None)
        if parse is not None and not getattr(parse, "tolerant", False):
            module.session_event_from_dict = tolerant(parse)  # type: ignore[attr-defined]
