"""Codex's app-server protocol, as `codex app-server generate-json-schema --experimental` writes
it for the Codex on this computer: each message either way on a fake Codex's wire
(tests/fake_codex.py, FAKE_CODEX_WIRE) is checked against it. What Chief of Staff sends must
name only fields Codex knows (a misspelt one is ignored by Codex without a word); what the fake
sends, which Chief of Staff reads, must be what real Codex could send."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from functools import cache
from pathlib import Path

import jsonschema

# Off the protocol on purpose: a test's request Codex never answers, and a request of the fake's
# Chief of Staff does not know (it must answer it with an error, not hang).
OFF_PROTOCOL = {"test/hang", "item/somethingNew"}

# The Codex on this computer, found before any test puts a fake one first on PATH.
CODEX = shutil.which("codex")


def generate(directory: Path, experimental: bool = True) -> Path | None:
    """The schema of the Codex on this computer, written into `directory`; None without one.
    Its CODEX_HOME is a folder of its own (it makes a helper folder there)."""
    codex = CODEX
    if not codex:
        return None
    (directory / "home").mkdir(parents=True, exist_ok=True)
    command = [codex, "app-server", "generate-json-schema", "--out", str(directory)]
    if experimental:
        command.insert(3, "--experimental")
    env = {**os.environ, "CODEX_HOME": str(directory / "home")}
    done = subprocess.run(
        command, capture_output=True, stdin=subprocess.DEVNULL, timeout=60, check=False, env=env
    )
    return directory if done.returncode == 0 else None


def strict(node):
    """Objects with named fields take no others (Codex ignores an unknown one silently). Fields
    shared by the variants of a tagged union sit beside its `oneOf`: each variant takes them."""
    if isinstance(node, dict):
        node = {key: strict(value) for key, value in node.items()}
        variants = node.get("oneOf") or node.get("anyOf")
        if "properties" in node and isinstance(variants, list):
            shared = dict.fromkeys(node["properties"], True)
            for variant in variants:
                if isinstance(variant, dict) and "properties" in variant:
                    variant["properties"] = {**shared, **variant["properties"]}
        elif "properties" in node and "additionalProperties" not in node:
            node["additionalProperties"] = False
        return node
    if isinstance(node, list):
        return [strict(value) for value in node]
    return node


class Protocol:
    """Validators for each method's params, result and notification, from a generated schema."""

    def __init__(self, directory: Path, exact: bool = True):
        self.directory = directory
        self.exact = exact
        self.requests = self.methods("ClientRequest.json")
        self.server_requests = self.methods("ServerRequest.json")
        self.notifications = self.methods("ServerNotification.json")
        self.client_notifications = self.methods("ClientNotification.json")

    def load(self, name: str) -> dict | None:
        for path in (self.directory / name, *(self.directory / v / name for v in ("v2", "v1"))):
            if path.exists():
                schema = json.loads(path.read_text())
                return strict(schema) if self.exact else schema
        return None

    def methods(self, name: str) -> dict[str, tuple[dict, str | None]]:
        """method -> (its message's schema with the file's definitions, its params' name)."""
        whole = self.load(name) or {}
        found = {}
        for entry in whole.get("oneOf") or []:
            method = entry["properties"]["method"]["enum"][0]
            params = (entry["properties"].get("params") or {}).get("$ref", "")
            schema = {**entry, "definitions": whole.get("definitions", {})}
            found[method] = (validator(schema), params.rsplit("/", 1)[-1] or None)
        return found

    @cache  # noqa: B019 (one Protocol per schema, kept for the session)
    def response(self, method: str, params: str | None) -> jsonschema.Draft7Validator | None:
        """The result's schema for `method`, whose params are `params`: XParams -> XResponse,
        or, without params, a/b -> ABResponse."""
        names = [params.removesuffix("Params")] if params and params.endswith("Params") else []
        names.append("".join(part[:1].upper() + part[1:] for part in method.split("/")))
        for name in names:
            if (schema := self.load(name + "Response.json")) is not None:
                return validator(schema)
        return None

    def problems(self, wire: Path) -> list[str]:
        """What on the wire does not match the protocol, one line each."""
        found: list[str] = []
        asked: dict[tuple[str, object], str] = {}  # (who asked, id) -> method
        lines = wire.read_text().splitlines() if wire.exists() else []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # A last line still being written by a fake that was not stopped.
            side, message = entry["from"], entry["message"]
            other = "codex" if side == "client" else "client"
            method = message.get("method")
            if method is not None and "id" in message:
                asked[(side, message["id"])] = method
                known = self.requests if side == "client" else self.server_requests
                found += self.check(side, method, known, message)
            elif method is not None:
                known = self.client_notifications if side == "client" else self.notifications
                found += self.check(side, method, known, message)
            else:
                method = asked.get((other, message.get("id")))
                if method is None or "error" in message:
                    continue
                known = self.server_requests if side == "client" else self.requests
                params = known.get(method, (None, None))[1]
                answer = self.response(method, params)
                if answer is None:
                    found.append(f"{side}: no schema for the answer to {method}")
                    continue
                for error in answer.iter_errors(message.get("result")):
                    found.append(f"{side}: answer to {method}: {describe(error)}")
        return found

    def check(self, side: str, method: str, known: dict, message: dict) -> list[str]:
        if method in OFF_PROTOCOL:
            return []
        if method not in known:
            return [f"{side}: {method} is not in Codex's protocol"]
        checked = known[method][0]
        message = {key: value for key, value in message.items() if key != "jsonrpc"}
        return [f"{side}: {method}: {describe(e)}" for e in checked.iter_errors(message)]


def validator(schema: dict) -> jsonschema.Draft7Validator:
    return jsonschema.Draft7Validator(schema)


def describe(error: jsonschema.ValidationError) -> str:
    """Where and what; in a union, what kept the closest variant from matching."""
    while error.context:
        error = variant_error(error)
        if not error.context:
            break
    where = "/".join(str(part) for part in error.absolute_path) or "(whole)"
    return f"{where}: {error.message[:300]}"


def variant_error(error: jsonschema.ValidationError) -> jsonschema.ValidationError:
    """In a tagged union, the error of the variant whose `type` the message has."""
    branches: dict[object, list] = {}
    for found in error.context or []:
        branches.setdefault(found.relative_schema_path[0], []).append(found)
    tagged = [
        errors
        for errors in branches.values()
        if not any(list(e.relative_path) == ["type"] and e.validator == "enum" for e in errors)
    ]
    if len(tagged) == 1:
        return tagged[0][0]
    return jsonschema.exceptions.best_match(error.context or []) or error


def unlisted(wire: Path) -> list[str]:
    """What Chief of Staff sent that backend_codex.CODEX_SENDS (or CODEX_NOTIFIES) does not
    list, so a version's check would not look for it."""
    from chiefofstaff.backend_codex import CODEX_NOTIFIES, CODEX_SENDS

    found = []
    for line in wire.read_text().splitlines() if wire.exists() else []:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry["message"]
        method = message.get("method")
        if entry["from"] != "client" or method is None or method in OFF_PROTOCOL:
            continue
        if "id" not in message:
            if method not in CODEX_NOTIFIES:
                found.append(f"{method} is not in CODEX_NOTIFIES")
            continue
        listed = CODEX_SENDS.get(method)
        if listed is None:
            found.append(f"{method} is not in CODEX_SENDS")
            continue
        found += [
            f"{method} {field} is not in CODEX_SENDS"
            for field in message.get("params") or {}
            if field not in listed
        ]
    return list(dict.fromkeys(found))


_protocols: dict[tuple[Path, bool], Protocol | None] = {}


def protocol(folder: Path, experimental: bool = True) -> Protocol | None:
    """The protocol of the Codex on this computer (generated once into `folder`), or None."""
    key = (folder, experimental)
    if key not in _protocols:
        directory = generate(folder / ("experimental" if experimental else "stable"), experimental)
        _protocols[key] = Protocol(directory) if directory else None
    return _protocols[key]
