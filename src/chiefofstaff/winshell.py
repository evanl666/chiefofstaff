r"""What a Windows shell's command line (PowerShell's, cmd's) writes in a checkout of the owner's
a worker was given only to read: policy.checkout_write's reading on Windows, where Copilot's
shell is PowerShell and Codex wraps each command in PowerShell.

Reads go through: Get-Content, dir, Select-String, findstr, git status and the like, run there or
naming files there. A command that writes there is CHECKOUT_WRITES: a redirection (`>`, `>>`,
`2>`…), Set-Content, Add-Content, Out-File, New-Item, Remove-Item, Move-Item, Rename-Item,
Copy-Item's destination, del, copy, xcopy, robocopy…, git that changes files, an install, a
build or a test run there (as on macOS and Linux: CheckoutWrites.command). Anything else run
there, or in a line naming one, is CHECKOUT_ASKS: a program not known, a method call
(`$f.Delete()`, `[IO.File]::WriteAllText(…)`), a variable for a path, Invoke-Expression, a
script. Either way the worker is sent to a copy of its own (Runtime.own_copy).

Paths as Windows takes them: a drive letter, `\` or `/`, in any case, with a `\\?\` prefix or
not, `.` and `..`, and the trailing dots and spaces Windows drops from a name.
"""

from __future__ import annotations

import base64
import binascii
import itertools
import ntpath
import os
import re
from contextlib import suppress
from pathlib import Path
from typing import NamedTuple

from .policy import (
    CHECKOUT_ASKS,
    CHECKOUT_WRITES,
    GLOB,
    MADE_UP,
    MOST_LINE,
    MOST_NESTED,
    CheckoutWrites,
    TooComplexError,
)

# Shells whose lines are read as on macOS and Linux (Git Bash, WSL): on Windows, not read (a
# line run in a read-only checkout, or naming one, asks).
POSIX_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
# What a program's name may end with on Windows (`git.exe`): the program is the name before it.
PROGRAM_ENDINGS = (".exe", ".com", ".cmd", ".bat")
# Scripts: a program of their own (not known here).
SCRIPT_ENDINGS = (".ps1", ".psm1", ".cmd", ".bat", ".vbs", ".js", ".wsf", ".py")
# Where a command writes to no file: PowerShell's $null, Windows' devices.
NO_FILE = frozenset({"$null", "nul", "nul:", "con", "con:"})
# A program asked only for its version or its help writes nothing where it runs.
ONLY_ASKED = frozenset(
    {"--version", "-version", "-v", "-V", "--help", "-help", "-h", "-?", "/?", "/h", "help",
     "version"}
)  # fmt: skip
# PowerShell's provider paths for files (`FileSystem::C:\x`, its module's name first or not): the
# path after it.
FILE_PROVIDER = re.compile(r"^(?:[\w.]+\\)?filesystem::", re.IGNORECASE)
# A drive of a provider that holds no files (HKLM:, Env:, Variable:, Cert:, Function:…).
OTHER_DRIVE = re.compile(r"^[A-Za-z][A-Za-z0-9_]+:")
# A command that changes where the commands after it run.
PS_LOCATION = frozenset(
    {"cd", "chdir", "sl", "set-location", "pushd", "push-location", "popd", "pop-location"}
)
LOCATION_WORD = re.compile(
    r"(?i)(?:^|[\s;|&({])(?:cd|chdir|sl|set-location|pushd|push-location|popd|pop-location)"
    r"(?:$|[\s;|&)}])"
)
# PowerShell's words that start a statement of their own (what they enclose is read: groups).
PS_KEYWORDS = frozenset(
    {"if", "elseif", "else", "switch", "foreach", "for", "while", "do", "until", "try",
     "catch", "finally", "trap", "function", "filter", "param", "begin", "process", "end",
     "return", "exit", "break", "continue", "throw", "data", "dynamicparam"}
)  # fmt: skip
ASSIGNMENTS = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "??="})
# PowerShell's commands (and their aliases) that write no file.
PS_READERS = frozenset(
    {"get-content", "gc", "cat", "type", "get-childitem", "gci", "dir", "ls", "get-item", "gi",
     "get-itemproperty", "gp", "get-itempropertyvalue", "gpv", "test-path", "resolve-path",
     "rvpa", "convert-path", "cvpa", "join-path", "split-path", "get-location", "gl", "pwd",
     "select-string", "sls", "select-object", "select", "where-object", "where", "?",
     "foreach-object", "foreach", "%", "sort-object", "sort", "group-object", "group",
     "measure-object", "measure", "compare-object", "compare", "diff", "format-table", "ft",
     "format-list", "fl", "format-wide", "fw", "format-custom", "fc", "format-hex", "fhx",
     "out-string", "out-host", "oh", "out-null", "out-default", "write-output", "echo",
     "write", "write-host", "write-verbose", "write-warning", "write-debug",
     "write-information", "write-error", "write-progress", "get-filehash", "get-date", "date",
     "get-command", "gcm", "get-help", "help", "man", "get-process", "gps", "ps",
     "get-service", "gsv", "get-variable", "gv", "get-member", "gm", "get-alias", "gal",
     "get-psdrive", "gdr", "get-module", "gmo", "get-host", "get-random", "get-unique", "gu",
     "get-acl", "get-culture", "get-uptime", "get-computerinfo", "get-history", "h",
     "history", "ghy", "convertfrom-json", "convertto-json", "convertfrom-csv",
     "convertto-csv", "import-csv", "ipcsv", "convertfrom-stringdata", "import-clixml",
     "select-xml", "convertto-html", "convertfrom-markdown", "start-sleep", "sleep",
     "clear-host", "cls", "clear", "set-variable", "sv", "new-variable", "nv",
     "remove-variable", "rv", "clear-variable", "clv", "get-ciminstance", "gcim",
     "get-wmiobject", "gwmi", "test-connection", "resolve-dnsname", "get-netipaddress",
     "get-executionpolicy", "get-psreadlineoption", "get-timezone", "get-winevent",
     "get-eventlog", "test-json", "get-error", "measure-command", "trace-command",
     "wait-process", "get-job", "receive-job", "wait-job", "get-psprovider", "get-pssession",
     "more", "tree", "hostname", "whoami", "findstr", "find", "where.exe", "timeout",
     "ipconfig", "systeminfo", "tasklist", "ver", "chcp", "ping", "nslookup", "netstat",
     "set"}
)  # fmt: skip
# PowerShell's commands that write the files their path parameters (and first positional
# arguments: PS_FIRST, or all: PS_ALL) name.
PS_FIRST = frozenset(
    {"set-content", "sc", "add-content", "ac", "clear-content", "clc", "out-file",
     "tee-object", "tee", "export-csv", "epcsv", "export-clixml", "set-item", "si",
     "set-itemproperty", "sp", "new-itemproperty", "remove-itemproperty", "rp", "clear-item",
     "cli", "clear-itemproperty", "clp", "rename-item", "rni", "ren", "new-item", "ni",
     "set-acl", "start-transcript", "export-formatdata", "export-modulemember",
     "update-help", "save-help", "export-alias", "epal", "export-console", "save-module",
     "install-module", "export-pssession", "epsn", "set-authenticodesignature",
     "unblock-file", "new-modulemanifest", "update-modulemanifest", "new-temporaryfile"}
)  # fmt: skip
PS_ALL = frozenset(
    {"remove-item", "ri", "rm", "rmdir", "rd", "del", "erase", "move-item", "mi", "mv", "move",
     "mkdir", "md"}
)  # fmt: skip
# Copy-Item and the like: their destination (or where they run).
PS_COPIERS = frozenset({"copy-item", "cpi", "cp", "copy", "expand-archive", "compress-archive"})
# Downloads: their -OutFile.
PS_DOWNLOADS = frozenset(
    {"invoke-webrequest", "iwr", "curl", "wget", "invoke-restmethod", "irm",
     "start-bitstransfer"}
)  # fmt: skip
# Their parameters that name what they write (any prefix of one names it, as PowerShell takes
# one), and those whose value is something else.
PS_PATHS = ("path", "literalpath", "lp", "pspath", "filepath", "destination", "destinationpath",
            "outputpath", "outfile", "target", "name")  # fmt: skip
PS_VALUES = ("value", "encoding", "itemtype", "type", "filter", "include", "exclude",
             "newname", "credential", "stream", "delimiter", "width", "inputobject",
             "compressionlevel", "erroraction", "ea", "warningaction", "wa", "errorvariable",
             "ev", "outvariable", "ov", "outbuffer", "ob", "pipelinevariable", "pv",
             "informationaction", "infa", "informationvariable", "iv", "aclobject", "uri",
             "method", "body", "headers", "contenttype", "useragent", "timeoutsec",
             "maximumredirection", "certificate", "scope", "repository", "requiredversion",
             "minimumversion", "maximumversion", "module", "uiculture", "culture")  # fmt: skip
# cmd's commands that write no file (and Windows' own programs that write none).
CMD_READERS = frozenset(
    {"dir", "type", "echo", "echo.", "more", "findstr", "find", "where", "tree", "set", "ver",
     "vol", "cls", "title", "rem", "help", "path", "prompt", "color", "chcp", "exit",
     "setlocal", "endlocal", "pause", "timeout", "date", "time", "ping", "hostname", "whoami",
     "ipconfig", "systeminfo", "tasklist", "fc", "comp", "nslookup", "netstat", "pushd",
     "popd", "cd", "chdir", "verify"}
)  # fmt: skip
# Their commands that write the files they name (all of them, or the first).
CMD_ALL = frozenset({"del", "erase", "rd", "rmdir", "md", "mkdir", "move", "attrib", "compact",
                     "cipher"})  # fmt: skip
CMD_FIRST = frozenset({"ren", "rename", "mklink", "icacls", "cacls", "takeown"})
# Their copiers: their last argument (or where they run) is where they write.
CMD_COPIERS = frozenset({"copy", "xcopy", "robocopy", "replace", "expand", "extrac32"})
# What starts a statement cmd runs in its own way (a block, a loop, another script).
CMD_COMPLEX = frozenset({"if", "for", "call", "start", "goto"})


def plain_path(path: str) -> str:
    r"""A Windows path as the system takes it: `/` as `\`, no `\\?\` (or `\\.\`) prefix, `.` and
    `..` resolved, the trailing dots and spaces of each name dropped (`app.` is `app`)."""
    text = path.replace("/", "\\")
    if text[:8].upper() == "\\\\?\\UNC\\":
        text = "\\" + text[7:]
    elif text[:4] in {"\\\\?\\", "\\\\.\\"}:
        text = text[4:]
    text = ntpath.normpath(text)
    drive, rest = ntpath.splitdrive(text)
    names = [name.rstrip(". ") if name.strip(".") else name for name in rest.split("\\")]
    return drive + "\\".join(names)


def path_key(path: str) -> str:
    """A path as Windows compares it: plain_path, in any case."""
    return plain_path(path).casefold()


def program_of(name: str) -> str:
    """A program's name as it is run (`C:\\Git\\cmd\\git.exe` is `git`), in lower case."""
    base = name.replace("/", "\\").rsplit("\\", 1)[-1].casefold()
    for ending in PROGRAM_ENDINGS:
        if base.endswith(ending):
            return base[: -len(ending)]
    return base


class Word(NamedTuple):
    """A word of a Windows shell's line: its text (MADE_UP in it where a variable or a
    command's output stands), whether it was all quoted, and the groups (`(…)`, `{…}`, `$(…)`)
    in it, by their place in Line.groups."""

    text: str
    quoted: bool = False
    groups: tuple[int, ...] = ()

    @property
    def made_up(self) -> bool:
        return MADE_UP in self.text


class Token(NamedTuple):
    """A word, an operator (";", "\\n", "|", "&&", "||", "&", ","), a redirection (">", ">>",
    "2>"…; "merge" for `2>&1`: no file), or a group (its place in Line.groups)."""

    kind: str  # "word", "op", "redirect", "group"
    word: Word = Word("")
    value: str = ""


SINGLE_QUOTES = "'\u2018\u2019\u201a\u201b"
# The dashes PowerShell takes as `-` (an en dash before a parameter's name).
DASHES = str.maketrans(dict.fromkeys("\u2013\u2014\u2015", "-"))
DOUBLE_QUOTES = '"\u201c\u201d\u201e'
CLOSERS = {"(": ")", "{": "}"}


def group_end(text: str, start: int) -> int:
    """Where the group opened at `start` (`(` or `{`) ends: the index past its closer. Quotes
    and nested groups in it are skipped; TooComplexError when it doesn't end."""
    expected = [CLOSERS[text[start]]]
    at = start + 1
    while at < len(text):
        char = text[at]
        if char == "`":
            at += 2
            continue
        if char in SINGLE_QUOTES or char in DOUBLE_QUOTES:
            at = quote_end(text, at)
            continue
        if char in CLOSERS:
            expected.append(CLOSERS[char])
        elif char in ")}":
            if char != expected.pop():
                raise TooComplexError
            if not expected:
                return at + 1
        at += 1
    raise TooComplexError


def quote_end(text: str, start: int) -> int:
    """The index past a quoted string that starts at `start` ('…' or "…")."""
    quotes = SINGLE_QUOTES if text[start] in SINGLE_QUOTES else DOUBLE_QUOTES
    at = start + 1
    while at < len(text):
        char = text[at]
        if char == "`" and quotes == DOUBLE_QUOTES:
            at += 2
            continue
        if char in quotes:
            if at + 1 < len(text) and text[at + 1] in quotes:
                at += 2  # (A quote doubled: one quote.)
                continue
            return at + 1
        at += 1
    raise TooComplexError


class Line:
    """A PowerShell line as tokens: its words, operators and redirections; the text of each group
    in it (read on its own: Line.groups); whether it calls a method (`.Delete()`, `::`)."""

    def __init__(self, text: str):
        if len(text) > MOST_LINE:
            raise TooComplexError
        self.text = text.translate(DASHES)
        self.tokens: list[Token] = []
        self.groups: list[str] = []
        self.calls = False
        self.parts: list[str] = []
        self.quoted = True
        self.started = False
        self.word_groups: list[int] = []
        self.read()

    def flush(self):
        if self.started:
            word = Word("".join(self.parts), self.quoted, tuple(self.word_groups))
            self.tokens.append(Token("word", word))
        self.parts, self.quoted, self.started, self.word_groups = [], True, False, []

    def add(self, text: str, quoted: bool = False):
        self.parts.append(text)
        self.started = True
        self.quoted = self.quoted and quoted

    def group(self, at: int, opener: str) -> int:
        """A group starting at `at` (its opener `opener`, as `$(`): its text kept to read on its
        own; in a word, a value made up there. The index past it."""
        start = at + len(opener) - 1
        end = group_end(self.text, start)
        self.groups.append(self.text[start + 1 : end - 1])
        if opener in {"(", "{"} and not self.started:
            self.tokens.append(Token("group", value=str(len(self.groups) - 1)))
        else:
            self.word_groups.append(len(self.groups) - 1)
            self.add(MADE_UP)
        return end

    def read(self):
        text, at = self.text, 0
        while at < len(text):
            char = text[at]
            following = text[at + 1] if at + 1 < len(text) else ""
            if char in " \t\r\f\v":
                self.flush()
                at += 1
            elif char == "\n":
                self.flush()
                self.tokens.append(Token("op", value="\n"))
                at += 1
            elif char == "`":
                if following in "\r\n":  # A line continued.
                    at += 3 if text[at + 1 : at + 3] == "\r\n" else 2
                    continue
                self.add(following)
                at += 2
            elif char == "#" and not self.started:
                end = text.find("\n", at)
                at = len(text) if end < 0 else end
            elif char == "<" and following == "#" and not self.started:
                end = text.find("#>", at + 2)
                if end < 0:
                    raise TooComplexError
                at = end + 2
            elif (
                char == "@"
                and following in SINGLE_QUOTES + DOUBLE_QUOTES
                and (text[at + 2 : at + 4].strip(" \t") in {"", "\r"} or text[at + 2] in "\r\n")
            ):
                at = self.here_string(at)
            elif char in SINGLE_QUOTES:
                end = quote_end(text, at)
                body = text[at + 1 : end - 1]
                for quote in SINGLE_QUOTES:
                    body = body.replace(quote * 2, quote)
                self.add(body, quoted=True)
                at = end
            elif char in DOUBLE_QUOTES:
                at = self.double_quoted(at)
            elif char == "$" and following == "(":
                at = self.group(at, "$(")
            elif char == "@" and following in "({":
                at = self.group(at, "@" + following)
            elif char == "@" and re.match(r"[A-Za-z_]", following) and not self.started:
                found = re.match(r"@\w+", text[at:])
                self.add(MADE_UP)  # (`@params`: a variable's values, splatted.)
                at += len(found.group(0)) if found else 1
            elif char == "$":
                found = re.match(r"\$(\{[^}]*\}|[\w:?^$]+)", text[at:])
                name = found.group(0) if found else "$"
                if name.lower() == "$null" and not self.started:
                    self.add("$null")
                else:
                    self.add(MADE_UP)
                at += len(name)
            elif char in "({":
                if char == "(" and self.started and re.search(r"\.\w+$", "".join(self.parts)):
                    self.calls = True  # (`$f.Delete(`: a method it calls.)
                at = self.group(at, char)
            elif char in ")}":
                raise TooComplexError
            elif char == ":" and following == ":":
                self.calls = True  # ([IO.File]::WriteAllText: a method of .NET's.)
                self.add("::")
                at += 2
            elif char == "&":
                self.flush()
                self.tokens.append(Token("op", value="&&" if following == "&" else "&"))
                at += 2 if following == "&" else 1
            elif char == "|":
                self.flush()
                self.tokens.append(Token("op", value="||" if following == "|" else "|"))
                at += 2 if following == "|" else 1
            elif char in ";,":
                self.flush()
                self.tokens.append(Token("op", value=char))
                at += 1
            elif char == ">" or (char in "123456*" and following == ">" and not self.started):
                at = self.redirection(at)
            elif char == "<":
                raise TooComplexError  # (Reserved in PowerShell: no input redirection.)
            else:
                self.add(char)
                at += 1
        self.flush()

    def here_string(self, at: int) -> int:
        """`@'…'@`, `@"…"@`: a value (made up when it may hold a command's output)."""
        quote = "'" if self.text[at + 1] in SINGLE_QUOTES else '"'
        end = self.text.find("\n" + quote + "@", at + 2)
        if end < 0:
            raise TooComplexError
        body = self.text[at + 2 : end]
        if quote == '"' and ("$(" in body or "$" in body):
            if "$(" in body:
                raise TooComplexError
            self.add(MADE_UP)
        else:
            self.add(body, quoted=True)
        return end + 3

    def double_quoted(self, at: int) -> int:
        """A "…" string: its text, but a variable or `$(…)` in it made up (that one read)."""
        text, at = self.text, at + 1
        self.started = True
        while at < len(text):
            char = text[at]
            if char == "`":
                self.add(text[at + 1 : at + 2], quoted=True)
                at += 2
            elif char in DOUBLE_QUOTES:
                if at + 1 < len(text) and text[at + 1] in DOUBLE_QUOTES:
                    self.add('"', quoted=True)
                    at += 2
                    continue
                return at + 1
            elif char == "$" and text[at + 1 : at + 2] == "(":
                at = self.group(at, "$(")
            elif char == "$" and re.match(r"\$[\w{:]", text[at:]):
                found = re.match(r"\$(\{[^}]*\}|[\w:]+)", text[at:])
                self.add(MADE_UP)
                at += len(found.group(0)) if found else 1
            else:
                self.add(char, quoted=True)
                at += 1
        raise TooComplexError

    def redirection(self, at: int) -> int:
        """`>`, `>>`, `2>`, `*>`…: the file the next word names; `2>&1`: none."""
        text = self.text
        stream = ""
        if text[at] != ">":
            stream, at = text[at], at + 1
        else:
            self.flush()
        operator, at = ">", at + 1
        if text[at : at + 1] == ">":
            operator, at = ">>", at + 1
        if text[at : at + 1] == "&":
            at += 1
            while at < len(text) and text[at].isdigit():
                at += 1
            self.tokens.append(Token("redirect", value="merge"))
            return at
        self.tokens.append(Token("redirect", value=stream + operator))
        return at


class CmdLine(Line):
    """A cmd line as tokens (Line's, with cmd's rules: `^` escapes, "…" quotes, `%VAR%` and
    `!VAR!` made up, `(` and `)` group commands, `<` reads a file)."""

    def read(self):
        text, at = self.text, 0
        while at < len(text):
            char = text[at]
            following = text[at + 1] if at + 1 < len(text) else ""
            if char in " \t\r\f\v":
                self.flush()
                at += 1
            elif char == "\n":
                self.flush()
                self.tokens.append(Token("op", value="\n"))
                at += 1
            elif char == "^":
                self.add(following)
                at += 2
            elif char == '"':
                end = text.find('"', at + 1)
                end = len(text) if end < 0 else end
                self.add(self.variables(text[at + 1 : end]), quoted=True)
                at = end + 1
            elif char in "%!":
                found = re.match(r"%~[a-zA-Z$:]*\d|%\d|%[^%\s]+%|![^!\s]+!", text[at:])
                self.add(MADE_UP if found else char)
                at += len(found.group(0)) if found else 1
            elif char == "@" and not self.started and not self.tokens_in_statement():
                at += 1  # (`@echo off`: the command itself.)
            elif char == "&":
                self.flush()
                self.tokens.append(Token("op", value="&&" if following == "&" else ";"))
                at += 2 if following == "&" else 1
            elif char == "|":
                self.flush()
                self.tokens.append(Token("op", value="||" if following == "|" else "|"))
                at += 2 if following == "|" else 1
            elif char in "()":
                self.flush()
                self.tokens.append(Token("op", value=";"))
                at += 1
            elif char == ">" or (char.isdigit() and following == ">" and not self.started):
                at = self.redirection(at)
            elif char == "<":
                self.flush()
                self.tokens.append(Token("redirect", value="<"))
                at += 1
            else:
                self.add(char)
                at += 1
        self.flush()

    def tokens_in_statement(self) -> bool:
        return bool(self.tokens) and self.tokens[-1].kind != "op"

    @staticmethod
    def variables(text: str) -> str:
        return re.sub(r"%[^%\s]+%|![^!\s]+!", MADE_UP, text)


class WindowsCheckout(CheckoutWrites):
    """CheckoutWrites for a Windows shell's line: paths as Windows takes them (plain_path), in
    any case; a program not known asks when it runs in, or the line names, a read-only
    checkout (on macOS and Linux, only one run there on files there)."""

    def __init__(self, text: str, kept: tuple[str, ...]):
        places = set()
        for folder in kept:
            forms = [folder]
            if os.name == "nt":
                with suppress(OSError, ValueError):
                    forms.append(os.path.realpath(folder))
            places.update(path_key(form).rstrip("\\") for form in forms)
        self.places = tuple(sorted(places))
        self.home = str(Path("~").expanduser())
        named = text.replace("/", "\\").casefold()
        home = path_key(self.home)
        self.mentioned = any(place in named for place in self.places) or any(
            place.startswith(home + "\\") and "~" + place[len(home) :] in named
            for place in self.places
        )
        self.found = ""
        self.links: dict[str, str] = {}

    def inside(self, path: str, real: bool = False) -> bool:
        key = path_key(path)
        if any(key == place or key.startswith(place + "\\") for place in self.places):
            return True
        if not real or os.name != "nt":
            return False
        try:
            key = path_key(os.path.realpath(path))
        except (OSError, ValueError):
            return False
        return any(key == place or key.startswith(place + "\\") for place in self.places)

    def place(self, word: str, folder: str | None) -> str | None:
        if not word or MADE_UP in word or "`" in word:
            return None
        word = FILE_PROVIDER.sub("", word)
        if word[:4].replace("/", "\\") in {"\\\\?\\", "\\\\.\\"}:
            word = plain_path(word)  # (`\\?\C:\x`: C:\x; its `?` is no pattern.)
        if OTHER_DRIVE.match(word) and not re.match(r"^[A-Za-z]:", word):
            return ""  # (A registry key, a variable: no file.)
        if word.casefold() in NO_FILE:
            return ""
        if word == "~" or word.startswith(("~\\", "~/")):
            word = self.home + word[1:]
        elif word.startswith("~"):
            return None  # (Another account's home.)
        if match := GLOB.search(word):  # (What it names is in the folder before the pattern.)
            names = re.split(r"[\\/]", word[: match.start()])
            word = "\\".join(names[:-1]) or ("\\" if match.start() == 0 else ".")
        word = word.replace("/", "\\")
        drive, rest = ntpath.splitdrive(word)
        if drive and rest.startswith("\\"):
            return plain_path(word)  # C:\x, \\server\share\x
        if word.startswith("\\\\"):
            return plain_path(word)
        if folder is None:
            return None
        here, _ = ntpath.splitdrive(folder)
        if drive:  # C:x: from that drive's own folder.
            if drive.casefold() != here.casefold():
                return None
            word = rest
        if word.startswith("\\"):
            return plain_path(here + word)
        return plain_path(ntpath.join(folder, word))

    def unknown(self, name: str, arguments: list[str], folder: str | None):
        """A program not known: asked about when it is in a read-only checkout, or runs in one,
        or the line names one; not when all it is asked is its version or its help."""
        program = self.place(name, folder) if re.search(r"[\\/]", name) else None
        asked_only = bool(arguments) and set(arguments) <= ONLY_ASKED
        if (program and self.inside(program, real=True)) or (not asked_only and self.near(folder)):
            self.ask()
            return
        for word in [] if asked_only else arguments:  # (Naming one by another path: `..\app`.)
            value = (word.partition("=")[2] or word.partition(":")[2]) if word[:1] in "-/" else word
            if (where := self.place(value, folder)) and self.inside(where):
                self.ask()
                return

    def read(
        self, text: str, folder: str | None, dialect: str
    ) -> list[tuple[str, str | None, str]]:
        """A line's statements, each where it runs: what each writes. The lines its commands
        run (a group's, `powershell -Command`'s, `cmd /c`'s), where and in which shell."""
        if dialect == "posix":
            if self.near(folder):
                self.ask()
            return []
        line = CmdLine(text) if dialect == "cmd" else Line(text)
        if line.calls and self.near(folder):
            self.ask()
        moves = any(LOCATION_WORD.search(group) for group in line.groups)
        runs: list[tuple[str, str | None, str]] = []
        words: list[Word] = []
        targets: list[Word] = []
        pushed: list[str | None] = []
        redirecting = ""  # ">": the next word is a file written; "<": one read.
        calling = False
        joiner = ""
        for token in [*line.tokens, Token("op", value=";")]:
            if token.kind == "redirect":
                if token.value != "merge":
                    redirecting = "<" if token.value == "<" else ">"
                continue
            if token.kind == "word":
                if redirecting == ">":
                    targets.append(token.word)
                elif not redirecting:
                    words.append(token.word)
                redirecting = ""
                for index in token.word.groups:
                    runs.append((line.groups[index], folder, "ps"))
                continue
            if token.kind == "group":
                runs.append((line.groups[int(token.value)], folder, "ps"))
                words.append(Word(MADE_UP, groups=(int(token.value),)))
                continue
            if token.value == "&" and not words and dialect != "cmd":
                calling = True  # (`& 'C:\tool.exe' args`: the call operator.)
                continue
            if token.value == ",":
                continue  # (An array's items: words of the same command.)
            before = folder
            grouped = any(word.groups for word in words)
            folder, more = self.statement(words, targets, folder, dialect, calling, pushed)
            runs += more
            if joiner == "||" and folder != before:
                folder = self.either(before, folder)  # (It may not have run.)
            if moves and grouped:
                folder = None  # (A group moved somewhere: where, not known.)
            words, targets, calling, redirecting = [], [], False, ""
            joiner = token.value
        return runs

    def statement(
        self,
        words: list[Word],
        targets: list[Word],
        folder: str | None,
        dialect: str,
        calling: bool,
        pushed: list[str | None],
    ) -> tuple[str | None, list[tuple[str, str | None, str]]]:
        """One command (or expression) and where it leaves the shell: what it writes, and the
        lines it runs."""
        for target in targets:
            self.writes(target.text, folder)
        if not words:
            return folder, []
        cmd = dialect == "cmd"
        first = words[0]
        if not cmd and not calling and first.text == "." and len(words) > 1:
            self.unknown(words[1].text, [w.text for w in words[2:]], folder)  # Dot-sourced.
            return None, []
        if not cmd and not calling and first.text.casefold() in PS_KEYWORDS:
            return folder, []  # (What it encloses: its groups, read on their own.)
        if not cmd and (after := assigned(words)) is not None:
            words = after  # `$x = Get-Content f`: the command after it.
            if not words:
                return folder, []
            first = words[0]
        if re.fullmatch(r"[A-Za-z]:", first.text):
            return None, []  # (A drive's own folder: not known.)
        expression = (
            not cmd
            and not calling
            and (
                first.made_up
                or first.text[:1] in "$[0123456789-+(@"
                or (
                    first.quoted
                    and not re.search(r"\.(exe|com|cmd|bat|ps1)$", first.text, re.IGNORECASE)
                )
            )
        )
        if expression or (calling and first.text == MADE_UP and first.groups):
            return folder, []  # (A value, or a block called: its groups are read.)
        if first.made_up:
            if self.near(folder):
                self.ask()  # (`& $tool`: a program not known.)
            return folder, []
        name = first.text
        rest = [word.text for word in words[1:]]
        program = program_of(name)
        if program in PS_LOCATION or (cmd and program in {"cd", "chdir", "pushd", "popd"}):
            return self.located(program, words[1:], folder, pushed, cmd), []
        return folder, self.command_of(name, program, rest, folder, cmd)

    def located(
        self, program: str, words: list[Word], folder: str | None, pushed, cmd: bool
    ) -> str | None:
        """Where `cd`, Set-Location, Push-Location, Pop-Location leave the shell."""
        if program in {"popd", "pop-location"}:
            return pushed.pop() if pushed else None
        target: Word | None = None
        for index, word in enumerate(words):
            name = word.text.lstrip("-").casefold()
            if not cmd and word.text.startswith("-") and not word.quoted:
                if any(path.startswith(name) for path in ("path", "literalpath", "lp")):
                    target = words[index + 1] if index + 1 < len(words) else None
                    break
                continue
            if cmd and word.text.casefold() == "/d":
                continue
            target = word
            break
        if program in {"pushd", "push-location"}:
            pushed.append(folder)
        if target is None:
            return folder if cmd else self.moved(folder, "~")
        if target.made_up or target.text in {"-", "+"}:
            return None
        return self.moved(folder, target.text) or None

    def command_of(
        self, name: str, program: str, rest: list[str], folder: str | None, cmd: bool
    ) -> list[tuple[str, str | None, str]]:
        """A command by its program (PowerShell's, cmd's, or one of the system's): what it
        writes; the lines it runs (a shell's, Invoke-Expression's), and in which shell."""
        script = name.casefold().endswith(SCRIPT_ENDINGS)
        if re.search(r"[\\/]", name) or script:
            where = self.place(name, folder)
            if (where and self.inside(where, real=True)) or script:
                self.unknown(name, rest, folder)  # A script, or a program of the checkout's.
                return []
        exe = name.casefold().endswith(PROGRAM_ENDINGS)
        if program in {"powershell", "pwsh"}:
            return self.shell_of(rest, folder, "ps")
        if program == "cmd":
            return self.shell_of(rest, folder, "cmd")
        if not exe and not cmd:  # (PowerShell's own names first: `rm`, `cp`, `ls` are its.)
            if program in {"invoke-expression", "iex"}:
                return [(" ".join(rest), folder, "ps")] if rest else []
            if program in PS_READERS:
                return []
            if program in PS_FIRST | PS_ALL | PS_COPIERS | PS_DOWNLOADS:
                self.cmdlet(program, rest, folder)
                if program in {"curl", "wget"}:  # (curl.exe in PowerShell 7: its -o, -O.)
                    self.command([program, *rest], [], folder)
                return []
        if cmd and program in CMD_COMPLEX:
            if self.near(folder):
                self.ask()
            return []
        if program in CMD_READERS:
            return []
        if program in CMD_ALL | CMD_FIRST | CMD_COPIERS | {"sort"}:
            self.cmd_writes(program, rest, folder)
            return []
        # One of the system's programs: as on macOS and Linux (git, npm, pytest, python…); a
        # POSIX shell's line it runs (`bash -c`) is not read here.
        runs = self.command([program, *rest], [], folder)
        return [(line, where, "posix") for line, where in runs]

    def shell_of(self, rest: list[str], folder: str | None, dialect: str):
        """`powershell -Command …`, `pwsh -c …`, `-EncodedCommand`, `cmd /c …`: the line it runs,
        where (`-WorkingDirectory`); `-File` (a script): a program not known."""
        words = list(rest)
        if dialect == "cmd":
            while (
                words and words[0].startswith("/") and words[0].casefold() not in {"/c", "/k", "/r"}
            ):
                words.pop(0)
            if not words:
                return []
            line = " ".join(words[1:] if words[0].casefold() in {"/c", "/k", "/r"} else words)
            return [(line, folder, "cmd")] if line.strip() else []
        valued = ("executionpolicy", "ep", "ex", "inputformat", "outputformat", "windowstyle",
                  "version", "configurationname", "custompipename", "settingsfile",
                  "workingdirectory", "wd", "of", "if", "w")  # fmt: skip
        while words and words[0][:1] in {"-", "/"}:
            option = words.pop(0)[1:].casefold()
            if option == "c" or ("command".startswith(option) and len(option) > 1):
                break
            if option in {"encodedcommand", "ec", "e", "en", "enc"}:
                try:
                    encoded = words[0] if words else ""
                    script = base64.b64decode(encoded, validate=True).decode("utf-16-le")
                except (binascii.Error, UnicodeDecodeError, ValueError):
                    raise TooComplexError from None
                return [(script, folder, "ps")]
            if option in {"file", "f"} and words:
                self.unknown(words[0], words[1:], folder)
                return []
            if option in valued and words:
                value = words.pop(0)
                if option in {"workingdirectory", "wd"}:
                    folder = self.moved(folder, value)
        if not words:
            return []
        if words[0] == "-":
            raise TooComplexError  # (Its input: not read here.)
        if words[0].casefold().endswith(".ps1"):
            self.unknown(words[0], words[1:], folder)
            return []
        return [(" ".join(words), folder, "ps")]

    def cmdlet(self, program: str, rest: list[str], folder: str | None):
        """A PowerShell command that writes files: those its path parameters and positional
        arguments name (Copy-Item and the like: their destination; a download: its -OutFile)."""
        named: list[tuple[str, str]] = []
        positional: list[str] = []
        at = 0
        while at < len(rest):
            word = rest[at]
            at += 1
            if not word.startswith("-") or len(word) < 2 or word[1:2].isdigit():
                positional.append(word)
                continue
            option, colon, attached = word[1:].partition(":")
            option = option.casefold()
            if option == "whatif":
                return  # (It says what it would do, and does nothing.)
            path = next((name for name in PS_PATHS if name.startswith(option)), "")
            if path and option not in {"n", "na"}:
                named.append((path, attached if colon else (rest[at] if at < len(rest) else "")))
                at += 0 if colon else 1
            elif any(name.startswith(option) for name in PS_VALUES):
                at += 0 if colon else 1
        if program in PS_DOWNLOADS:
            names = {"outfile", "outputpath", "destination"}
            written = [value for name, value in named if name in names]
        elif program in PS_COPIERS:
            destination = [v for name, v in named if name in {"destination", "destinationpath"}]
            written = destination or positional[1:2] or ["."]
        elif program in PS_ALL:
            written = [value for _, value in named] + positional
            written = written or (["."] if program in {"mkdir", "md"} else [MADE_UP])
        else:
            written = [value for _, value in named] + positional[:1]
            if not written:  # (Its paths from the pipeline: not known here.)
                written = ["."] if program in {"new-item", "ni"} else [MADE_UP]
        for value in written:
            self.writes(value, folder)

    def cmd_writes(self, program: str, rest: list[str], folder: str | None):
        """A Windows program (or cmd's command) that writes the files it names: all, the first,
        or (a copier) its last, or where it runs."""
        plain = [w for w in rest if not re.fullmatch(r"/[A-Za-z?]+(:.*)?|/\d+", w)]
        if program == "sort":
            written = [b for a, b in itertools.pairwise(rest) if a.casefold() in {"/o", "/output"}]
        elif program == "robocopy":
            moves = any(w.casefold() in {"/mov", "/move"} for w in rest)  # (Its source too.)
            written = plain[:2] if moves else plain[1:2]
        elif program == "takeown":
            written = [b for a, b in itertools.pairwise(rest) if a.casefold() == "/f"]
        elif program in CMD_COPIERS:
            written = plain[-1:] if len(plain) > 1 else ["."]
        elif program in CMD_FIRST:
            written = plain[:1]
        elif program == "attrib":
            written = [w for w in plain if w[:1] not in "+-"]
        else:
            written = plain
        for value in written:
            self.writes(value, folder)


ASSIGNED = re.compile(r"[+\-*/%]?(?:\?\?)?=")


def assigned(words: list[Word]) -> list[Word] | None:
    """What a statement assigning a variable runs (`$x = cmd`, `$x=cmd`, `$x =cmd`); None for
    a statement that assigns none."""
    first, second = words[0].text, words[1].text if len(words) > 1 else ""
    if second in ASSIGNMENTS:
        return words[2:]
    if not first.startswith(MADE_UP):
        return None
    if found := ASSIGNED.match(first.lstrip(MADE_UP)):
        rest = first.lstrip(MADE_UP)[found.end() :]
        return [Word(rest), *words[1:]] if rest else words[1:]
    if found := ASSIGNED.match(second):
        return [Word(second[found.end() :]), *words[2:]]
    return None


def windows_checkout_write(command: str, cwd: str, kept: tuple[str, ...], shell: str) -> str:
    """policy.checkout_write on Windows: the line read as PowerShell reads it (cmd's, for a line
    Codex runs in cmd); one a POSIX shell runs (Git Bash) is not read: run in a read-only
    checkout, or naming one, it asks."""
    guard = WindowsCheckout(command, kept)
    if shell in POSIX_SHELLS:
        return CHECKOUT_ASKS if guard.near(cwd or None) else ""
    pending = [(command, cwd or None, 0, "cmd" if shell == "cmd" else "ps")]
    try:
        while pending and guard.found != CHECKOUT_WRITES:
            text, folder, depth, dialect = pending.pop()
            if depth > MOST_NESTED:
                raise TooComplexError
            pending += [(line, where, depth + 1, how)
                        for line, where, how in guard.read(text, folder, dialect)]  # fmt: skip
    except (ValueError, TooComplexError, IndexError):
        if guard.near(cwd or None):
            return guard.found or CHECKOUT_ASKS
    return guard.found
