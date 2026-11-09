import json
import re
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar, cast, get_origin

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

# owner/repo, also from its github.com address (https://github.com/acme/app, git@github.com:…).
GITHUB_URL = re.compile(
    r"(?:(?:https?://)?(?:www\.)?github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?"
    r"(?:[/#?].*)?",
    re.IGNORECASE,
)


def github_repository(value: object) -> object:
    if not isinstance(value, str):
        return value
    value = value.strip()
    if found := GITHUB_URL.fullmatch(value):
        value = f"{found[1]}/{found[2]}"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError(f"GitHub repository {value}: write it as owner/name, like acme/app.")
    return value


Repository = Annotated[str, BeforeValidator(github_repository)]
Role = Literal["router", "worker", "slack", "watchdog"]
Effort = Literal["none", "low", "medium", "high", "xhigh", "max"]


def inline_refs(schema: dict) -> dict:
    """A JSON schema with its $defs written in place of each $ref."""
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                extra = {key: value for key, value in node.items() if key != "$ref"}
                return resolve({**defs[node["$ref"].rsplit("/", 1)[-1]], **extra})
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


class NestedArguments(BaseModel):
    """Tool arguments with nested objects: the schema models see has no $ref to follow."""

    @classmethod
    def model_json_schema(cls, *args, **kwargs) -> dict:  # pyright: ignore[reportIncompatibleMethodOverride]
        return inline_refs(super().model_json_schema(*args, **kwargs))


ModelT = TypeVar("ModelT", bound=BaseModel)
# A model's tool call that leaked its own markup into a value: `"done</parameter>\n<parameter
# name="summary">Ran …"` (the other parameters inside the first one's string).
LEAKED = re.compile(r'\s*</parameter>\s*<parameter name="(\w+)">\s*')
# The end of the call leaked into the last value: `…</parameter>`, `…</parameter>\n</invoke>`.
LEAK_END = re.compile(r"\s*</parameter>(?:\s*</invoke>)?(?:\s*</function_calls>)?\s*$")


def repair_leaked(
    data: object, fields: set[str], structured: frozenset[str] = frozenset()
) -> object:
    """Tool arguments with leaked parameter markup, split back into their parameters; a list or
    object parameter (`structured`) that came as its JSON text is parsed."""
    if not isinstance(data, dict):
        return data
    fixed = dict(data)
    for key, value in data.items():
        if not isinstance(value, str):
            continue
        if LEAKED.search(value):
            parts = LEAKED.split(value)
            fixed[key] = LEAK_END.sub("", parts[0])
            for name, part in zip(parts[1::2], parts[2::2], strict=True):
                if name in fields and name not in data:
                    fixed[name] = LEAK_END.sub("", part)
        elif LEAK_END.search(value):
            fixed[key] = LEAK_END.sub("", value)
    for name in structured & fixed.keys():
        value = fixed[name]
        if isinstance(value, str) and value.strip()[:1] in {"[", "{"}:
            with suppress(ValueError):
                fixed[name] = json.loads(value)
    return fixed


def structured_fields(model: type[BaseModel]) -> frozenset[str]:
    """Its parameters that are lists or objects."""
    found = set()
    for name, field in model.model_fields.items():
        kind = get_origin(field.annotation) or field.annotation
        if kind in (list, dict, set, tuple) or (
            isinstance(kind, type) and issubclass(kind, BaseModel)
        ):
            found.add(name)
    return frozenset(found)


def leak_proof(model: type[ModelT]) -> type[ModelT]:
    """The tool's arguments model, repairing leaked parameter markup before it validates."""
    fields = set(model.model_fields)
    structured = structured_fields(model)

    class Repaired(model):  # pyright: ignore[reportGeneralTypeIssues]
        @model_validator(mode="before")
        @classmethod
        def repair(cls, data: Any) -> Any:
            return repair_leaked(data, fields, structured)

    Repaired.__name__ = Repaired.__qualname__ = model.__name__
    Repaired.__doc__ = model.__doc__
    return cast("type[ModelT]", Repaired)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def private_folders(home: Path) -> list[Path]:
    """Never writable by workers: SSH keys, and Copilot's settings, sign-ins, session logs and
    plugin code (hooks run from there outside the sandbox). A plugin's own data folder, such as
    a journal in ~/.copilot/<name>, may be opened."""
    copilot = home / ".copilot"
    return [
        home / ".ssh",
        home / "Library" / "Caches" / "copilot",
        *(
            copilot / name
            for name in (
                "session-state",
                "mcp-oauth-config",
                "installed-plugins",
                "servers",
                "ide",
                "logs",
                "pkg",
                "marketplaces",
            )
        ),
        *copilot.glob("*.json"),
        *copilot.glob("*.db*"),
        *copilot.glob("*.lock"),
    ]


def owner_folder(raw: str, what: str, example: str = "~/code/app") -> Path:
    """A folder the owner wrote, ~ expanded: a relative one is refused, not taken as inside the
    folder Chief of Staff was started in."""
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError(
            f"{what}: give the folder as an absolute path, e.g. {example} (not {raw.strip()})."
        )
    return path


def repository_around(path: Path) -> Path | None:
    """The git repository a folder is (or is in), up to the home folder (which some keep their
    dotfiles in as a repository)."""
    home = Path.home().resolve()
    for folder in (path, *path.parents):
        if home.is_relative_to(folder):
            return None
        if (folder / ".git").exists():
            return folder
    return None


# Copilot refuses to start a session with a lower credit limit.
MIN_CREDIT_LIMIT = 30

# A Copilot plugin to install: owner/repo, owner/repo:path, name@marketplace or a git URL.
PLUGIN_SOURCE = re.compile(
    r"[\w.-]+/[\w.-]+(:[\w./-]+)?|[\w.-]+@[\w.-]+|https://[\w.-]+/[\w./~%-]+"
)


def plugin_source(value: str) -> str:
    if PLUGIN_SOURCE.fullmatch(value):
        return value
    # Copilot reads x@y as plugin@marketplace: a git URL must be https.
    if ssh := re.fullmatch(r"(?:ssh://)?git@([\w.-]+)[:/]([\w./~%-]+)", value):
        raise ValueError(
            f"Plugin {value}: use its https URL, https://{ssh[1]}/{ssh[2]} (Copilot installs "
            "git URLs over https)."
        )
    raise ValueError(
        f"Plugin {value}: write owner/repo, owner/repo:path, name@marketplace or an https:// "
        "git URL."
    )


def mcp_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value):
        raise ValueError(
            f"MCP server name {value or '(empty)'}: use letters, digits, - and _ (at most 40)."
        )
    return value


PluginSource = Annotated[str, Field(max_length=300), AfterValidator(plugin_source)]
McpName = Annotated[str, AfterValidator(mcp_name)]
HourRange = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")]
# Validating settings as they were saved (by this version or an earlier one): see mcp_off,
# copilot_minimum and validate_paths.
AS_SAVED = {"mcp_saved": True, "saved": True}


def mcp_problem(config: object) -> str:
    """What in an MCP server's configuration Copilot would refuse ("" when nothing): it takes
    the session, then gives up on every message to it, with no error the session sees."""
    if not isinstance(config, dict):
        return 'write an object such as {"command": "…"} or {"url": "…"}.'

    def filled(key: str) -> bool:
        return isinstance(config.get(key), str) and bool(config[key].strip())

    def lacking(key: str) -> str:
        return "empty" if key in config else "missing"

    kinds = ("local", "stdio", "http", "sse")
    kind = config.get("type")
    if "type" in config and kind not in kinds:
        return "type must be local, stdio, http or sse."
    # A local server runs a command; a remote one (http when no type is given) has a url.
    if kind is None and not filled("command") and not filled("url"):
        return "give a command (local) or a url (remote)."
    if kind in ("http", "sse") or (kind is None and not filled("command")):
        if not filled("url"):
            return f'url is {lacking("url")}: type "{kind}" needs one (https://…).'
        if not re.fullmatch(r"https?://[^\s/?#]+\S*", config["url"].strip(), re.IGNORECASE):
            return "url must be an http:// or https:// address."
    elif not filled("command"):
        return f'command is {lacking("command")}: type "{kind}" needs the program to run.'

    def texts(value: object) -> bool:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)

    def table(value: object) -> bool:
        return isinstance(value, dict) and all(isinstance(item, str) for item in value.values())

    checks = {
        "tools": (texts, 'a list of tool names in quotes (["*"] for all)'),
        "args": (texts, "a list of strings"),
        "env": (table, "an object of strings"),
        "headers": (table, "an object of strings"),
        "cwd": (lambda value: isinstance(value, str), "a string"),
        "working_directory": (lambda value: isinstance(value, str), "a string"),
        "timeout": (
            lambda value: isinstance(value, int) and not isinstance(value, bool) and value > 0,
            "a whole number of milliseconds",
        ),
    }
    for key, (fits, what) in checks.items():
        if key in config and not fits(config[key]):
            return f"{key} must be {what}."
    return ""


class Watch(StrictModel):
    """Something to look out for on the owner's behalf, with whatever tools work on this
    machine (Slack, Glean, a company search, mail, docs…)."""

    what: str = Field(min_length=3, max_length=500)
    # Which tool or source to use, when the owner knows ("Glean search", "the Slack MCP").
    how: str = Field(default="", max_length=300)
    enabled: bool = True


class Settings(StrictModel):
    copilot_command: str = "copilot"
    model: str = Field(default="", max_length=100)
    role_models: dict[Role, Annotated[str, Field(max_length=100)]] = Field(default_factory=dict)
    # Applied when the session's model supports that effort; otherwise the model's default.
    role_efforts: dict[Role, Effort] = Field(default_factory=dict)
    worker_credit_limit: float = Field(default=0, ge=0, le=100000)
    # Learn how the owner writes and works from their Slack messages, PR reviews, commits and
    # Copilot sessions (see habits.py), and turn workers' fixed mistakes into checks (checks.py).
    learn_from_work: bool = True
    learn_checks: bool = True
    worker_idle_hours: int = Field(default=24, ge=1, le=720)
    # A running task with no activity, nothing it waits on and no check-back for this long is
    # stalled: its worker is asked once where it stands, and the owner sees it under Stalled.
    stall_hours: int = Field(default=4, ge=1, le=168)
    # Workers act without permission checks, as the owner's own Copilot sessions do; they still
    # follow their instructions. Workers that read Slack keep the checks unless set per worker.
    worker_yolo: bool = True
    # Workers' shell commands run in Chief of Staff's OS sandbox: they write only to their own
    # folders. Off: workers run like the owner's own Copilot sessions (with the owner's own
    # Copilot sandbox settings, if any). Workers that read Slack are always sandboxed.
    worker_sandbox: bool = False
    # Folders sandboxed workers may also write to (a Copilot plugin's own folder, say).
    sandbox_writable: list[str] = Field(default_factory=list, max_length=10)
    # Sandboxed workers use the owner's dependency and build caches (Gradle, Maven, npm…), as
    # sessions in a terminal do, instead of downloading and building everything again.
    shared_caches: bool = True
    # Copied task reports end with a short "Orchestrated with Chief of Staff" line.
    report_footer: bool = True
    digest_time: str = Field(default="09:00", pattern=r"^(|([01]\d|2[0-3]):[0-5]\d)$")
    # Where the owner's repositories live (e.g. ~/workspace). Each git repository in it is a
    # project; one a task needs that is not on this machine is cloned into it; each worker works
    # in its own folder in .chief/ inside it.
    code_folder: str = Field(default="", max_length=500)
    # Folders outside the code folder that workers may also work in.
    projects: list[str] = Field(default_factory=list, max_length=30)
    # Tasks without a project get a folder here; empty means inside the data directory.
    scratch_directory: str = Field(default="", max_length=500)
    max_workers: int = Field(default=4, ge=1, le=1000)
    paused: bool = False
    mac_notifications: bool = True
    watchdog_enabled: bool = True
    slack_enabled: bool = False
    slack_poll_seconds: int = Field(default=60, ge=20, le=900)
    # Reacting with this emoji to any Slack message hands it to Chief of Staff; empty turns it off.
    slack_delegate_emoji: str = Field(default="robot_face", max_length=60)
    github_enabled: bool = False
    github_repos: list[Repository] = Field(default_factory=list, max_length=10)
    # More for workers than Copilot finds by itself (the owner's plugins, skills and MCP
    # servers are loaded anyway): folders of skills (each skill a folder with a SKILL.md),
    # Copilot plugins to install, and MCP servers as in Copilot's mcp-config.json.
    skill_folders: list[str] = Field(default_factory=list, max_length=20)
    plugins: list[PluginSource] = Field(default_factory=list, max_length=20)
    mcp_servers: dict[McpName, dict[str, Any]] = Field(default_factory=dict, max_length=10)
    # Saved MCP servers Copilot would refuse (an earlier version's looser checks took them): kept
    # as written, for the owner to fix in Settings; workers do not get them (see mcp_off).
    mcp_servers_off: dict[McpName, dict[str, Any]] = Field(default_factory=dict, max_length=10)
    # Watches run in work hours every watch_minutes, otherwise every watch_after_hours_minutes
    # (0: not after hours), in the machine's time zone.
    watches: list[Watch] = Field(default_factory=list, max_length=10)
    watch_hours: HourRange = "09:00-18:00"
    watch_minutes: int = Field(default=5, ge=1, le=240)
    watch_after_hours_minutes: int = Field(default=30, ge=0, le=1440)
    # Away from this computer (no keyboard or mouse for away_minutes): what needs the owner
    # also reaches them as away_how says (a Slack DM from a bot, say).
    away_notify: bool = False
    away_minutes: int = Field(default=10, ge=1, le=240)
    away_how: str = Field(default="", max_length=500)

    @model_validator(mode="before")
    @classmethod
    def mcp_off(cls, data: Any, info: ValidationInfo) -> Any:
        """Settings as saved (context `mcp_saved`: True, or the servers saved now by name): a
        saved MCP server Copilot would refuse is kept off, not refused, so loading or saving
        another setting never loses it (nor the others); one the owner adds or changes is
        checked (mcp_config). A server in mcp_servers replaces its copy that was off."""
        saved = (info.context or {}).get("mcp_saved")
        if saved is None or not isinstance(data, dict):
            return data
        servers, off = data.get("mcp_servers") or {}, data.get("mcp_servers_off") or {}
        if not isinstance(servers, dict) or not isinstance(off, dict):
            return data
        servers, off = dict(servers), dict(off)
        for name, config in list(servers.items()):
            if mcp_problem(config) and (saved is True or saved.get(name) == config):
                off[name] = servers.pop(name)
        for name, config in list(off.items()):
            if name in servers:
                del off[name]
            elif not mcp_problem(config):
                servers[name] = off.pop(name)
        return {**data, "mcp_servers": servers, "mcp_servers_off": off}

    @field_validator("mcp_servers")
    @classmethod
    def mcp_config(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        for name, config in value.items():
            if problem := mcp_problem(config):
                raise ValueError(f"MCP server {name}: {problem}")
            config.setdefault("tools", ["*"])
        return value

    @field_validator("skill_folders")
    @classmethod
    def skill_paths(cls, value: list[str]) -> list[str]:
        folders = []
        for raw in value:
            path = owner_folder(raw, "Skill folders", "~/skills")
            if not path.is_dir():
                raise ValueError(f"Not an existing folder: {raw}")
            folders.append(str(path.resolve()))
        return list(dict.fromkeys(folders))

    @field_validator("worker_credit_limit")
    @classmethod
    def copilot_minimum(cls, value: float, info: ValidationInfo) -> float:
        if 0 < value < MIN_CREDIT_LIMIT:
            if (info.context or {}).get("saved"):
                return MIN_CREDIT_LIMIT  # Saved before this was checked: Copilot's minimum.
            raise ValueError(
                f"Credit limit per worker: at least {MIN_CREDIT_LIMIT} (Copilot starts no "
                "session with less), or 0 for no limit."
            )
        return value

    @field_validator("slack_delegate_emoji")
    @classmethod
    def emoji_code(cls, value: str) -> str:
        value = value.strip(":").strip().lower()
        if not re.fullmatch(r"[a-z0-9_+'-]*", value):
            raise ValueError(
                "Delegate emoji: the emoji's name as Slack writes it, like eyes or "
                "white_check_mark."
            )
        return value

    @field_validator("model", "role_models")
    @classmethod
    def auto_model(cls, value: str | dict[Role, str]) -> str | dict[Role, str]:
        """Auto, AUTO…: Copilot's choice, which it knows only as `auto`."""
        if isinstance(value, dict):
            return {
                role: "auto" if model.lower() == "auto" else model for role, model in value.items()
            }
        return "auto" if value.lower() == "auto" else value

    @field_validator("plugins")
    @classmethod
    def unique_plugins(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_paths(self, info: ValidationInfo):
        # A saved project whose folder is gone (moved, deleted, a drive not mounted) stays, shown
        # as not found, until the owner removes it; a new one must exist.
        context = info.context or {}
        saved = set(context.get("projects_saved") or ())
        projects = []
        for raw in self.projects:
            path = owner_folder(raw, "Other project folders").resolve()
            if not path.is_dir() and not (context.get("saved") or {raw, str(path)} & saved):
                raise ValueError(f"Project is not an existing directory: {raw}")
            if path == Path.home() or path == Path(path.anchor):
                raise ValueError("Choose project directories, not your home or filesystem root.")
            if str(path) not in projects:
                projects.append(str(path))
        self.projects = projects
        writable = []
        writable_saved = set(context.get("writable_saved") or ())
        for raw in self.sandbox_writable:
            if not raw.strip():
                continue
            path = owner_folder(raw, "Also writable in the sandbox", "~/.copilot/journal")
            path = path.resolve()
            home = Path.home().resolve()
            if path == Path(path.anchor) or home.is_relative_to(path):
                raise ValueError("The sandbox cannot open your whole home or a folder above it.")
            if path == home / ".copilot" or any(
                private.is_relative_to(path) or path.is_relative_to(private)
                for private in private_folders(home)
            ):
                raise ValueError(f"The sandbox cannot open {path}: it holds keys or settings.")
            # Saved from Settings (writable_saved: what was saved before), as with projects: a
            # saved one whose folder is gone stays; a new one must exist.
            if (
                "writable_saved" in context
                and not path.is_dir()
                and not {raw.strip(), str(path)} & writable_saved
            ):
                raise ValueError(f"Folder not found: {raw.strip()}")
            if str(path) not in writable:
                writable.append(str(path))
        self.sandbox_writable = writable
        if self.code_folder:
            path = owner_folder(self.code_folder, "Code folder", "~/workspace").resolve()
            if not path.is_dir():
                raise ValueError(f"The code folder does not exist: {self.code_folder}")
            if path == Path.home() or path == Path(path.anchor):
                raise ValueError("Choose the folder your repositories are in, like ~/workspace.")
            if (path / ".git").exists():
                # Its task folders would land inside the repository as untracked files.
                raise ValueError(
                    f"{path} is a repository. The code folder is the folder your repositories "
                    "are in, like its parent; add a single repository under Other project folders."
                )
            self.code_folder = str(path)
        if self.scratch_directory:
            scratch = owner_folder(self.scratch_directory, "Task folders", "~/ChiefOfStaff")
            scratch = scratch.resolve()
            if scratch == Path.home() or scratch == Path(scratch.anchor):
                raise ValueError("Task folders: choose a folder, not your home or filesystem root.")
            # Checked when chosen, as for the code folder: a worker's folder would land in the
            # repository as untracked files, and git there would work on that repository.
            if (
                not context.get("saved")
                and str(scratch) != context.get("scratch_saved")
                and (repository := repository_around(scratch))
            ):
                raise ValueError(
                    f"Task folders: {scratch} is in the repository {repository}. Choose a folder "
                    "outside your repositories, like ~/ChiefOfStaff."
                )
            self.scratch_directory = str(scratch)
        self.github_repos = list(dict.fromkeys(repo.lower() for repo in self.github_repos))
        if self.github_enabled and not self.github_repos:
            raise ValueError("Choose repositories before enabling review-request monitoring.")
        return self


class SkillUpload(StrictModel):
    content: str = Field(min_length=10, max_length=100_000)


class Attachment(StrictModel):
    """An image the owner attached to a message (pasted or dropped into the composer)."""

    name: str = Field(default="image", max_length=200)
    type: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
    # Base64, without a data: prefix; at most 5 MB decoded.
    data: str = Field(min_length=4, max_length=7_000_000)


class OwnerMessage(StrictModel):
    text: str = Field(default="", max_length=20000)
    # From the Router chat, addressed to one worker: it goes to that worker directly.
    to: str = Field(default="", max_length=64)
    attachments: list[Attachment] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def something(self) -> "OwnerMessage":
        if not self.text.strip() and not self.attachments:
            raise ValueError("Write a message or attach an image.")
        return self


class NewLesson(StrictModel):
    text: str = Field(min_length=1, max_length=600)
    # "", "slack", or a project folder.
    scope: str = Field(default="", max_length=500)


class FocusRequest(StrictModel):
    # 0 ends focus now.
    minutes: int = Field(ge=0, le=600)


class ProjectPath(StrictModel):
    path: str = Field(min_length=1, max_length=1000)


class TaskNote(StrictModel):
    text: str = Field(default="", max_length=20000)


class Unfollow(StrictModel):
    text: str = Field(max_length=500)  # The thread: slack:… or github:owner/repo#n.
    task_id: str = Field(default="", max_length=20)  # For this task only (else for all).


class FollowRetry(StrictModel):
    key: str = Field(min_length=1, max_length=500)  # The PR it follows: github:owner/repo#n.


class ArtifactHide(StrictModel):
    key: str = Field(max_length=2100)
    hidden: bool = True
    tone: str = Field(default="", max_length=20)
    # The same one under the key an earlier version gave it (its address): replaced.
    also: list[Annotated[str, Field(max_length=2100)]] = Field(default_factory=list, max_length=20)


# Under GitHub's 65536 characters for a review and Slack's 40000 for a message.
MAX_DRAFT = 39000


class DraftDecision(StrictModel):
    # mark_sent: a draft whose sending was interrupted turned out to be delivered.
    # revise: the drafting session drafts it again, as `reason` asks.
    action: Literal["send", "reject", "choose", "answer", "mark_sent", "revise"]
    # For a form a session showed the owner: field name -> value.
    content: dict[str, str | int | float | bool | list[str]] = Field(default_factory=dict)
    # For a workspace question: the chosen folder, or "" for no project.
    choice: str = Field(default="", max_length=1000)
    text: str = Field(default="", max_length=MAX_DRAFT)
    reason: str = Field(default="", max_length=2000)


class WithdrawDraft(BaseModel):
    draft_id: str = Field(description="Your draft that waits for the owner (d…)")
    reason: str = Field(
        description="Why, in one line addressed to the owner (you), in their language, e.g. "
        "'Alice no longer needs your approval'"
    )


class SlackToken(StrictModel):
    token: str = Field(max_length=500)

    @field_validator("token")
    @classmethod
    def user_token(cls, value: str) -> str:
        if re.search(r"\s", value):
            raise ValueError("A Slack token has no spaces: paste it again.")
        if not re.fullmatch(r"xoxp-[A-Za-z0-9-]+", value):
            raise ValueError(
                "That is not a Slack user token (xoxp-…). Bot tokens (xoxb-) do not work "
                "with Slack's MCP server."
            )
        return value


class SessionModel(StrictModel):
    model: str = Field(default="", max_length=100)
    effort: Literal["", "none", "low", "medium", "high", "xhigh", "max"] = ""
    context: Literal["", "long_context"] = ""


class SetupNotes(StrictModel):
    text: str = Field(default="", max_length=30000)
    # What the owner saw when they started editing; their changes apply against it.
    initial: str = Field(default="", max_length=30000)


class SessionYolo(StrictModel):
    mode: Literal["", "on", "off"] = ""  # "" follows Settings.


class OpenFolder(StrictModel):
    app: Literal["finder", "vscode"]


class ProfileText(StrictModel):
    text: str = Field(default="", max_length=20000)
