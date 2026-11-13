"""The permission rule for a worker's shell commands as Chief of Staff v0.1.42 shipped it, frozen
here (from src/chiefofstaff/policy.py at tag v0.1.42): what it denied, the rule of today must
deny too (tests/test_policy.py)."""

import re

# Commands a worker may not run on its own; it must ask the owner through the Router.
DANGEROUS = [
    (re.compile(r"(^|[\s;&|(])sudo\s"), "sudo is not allowed"),
    (
        re.compile(
            r"\brm\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(/|~|\$HOME)/?(\s|$)"
        ),
        "recursive delete of home or root",
    ),
    (re.compile(r"\bgit\s+push\b[^;&|\n]*(\s--force(-with-lease)?\b|\s-f\b|\s\+\S)"), "force push"),
    (re.compile(r"\bgit\s+push\b[^;&|\n]*[\s:](main|master)(\s|$)"), "push to the default branch"),
    (
        re.compile(r"\bgh\s+pr\s+(merge|close|review|comment)\b"),
        "merging, closing or commenting on PRs as the owner",
    ),
    (
        re.compile(r"\bgh\s+issue\s+(close|comment|delete|edit)\b"),
        "changing issues as the owner",
    ),
    (
        re.compile(r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(sh|bash|zsh)\b"),
        "piping a download into a shell",
    ),
    (
        re.compile(r"\b(mkfs|diskutil\s+erase|shutdown|reboot)\b"),
        "system-level destructive command",
    ),
]


GH_API = re.compile(r"\bgh\s+api\b([^;&|\n]*)")


GH_METHOD = re.compile(r"(?:^|\s)(?:-X|--method)[\s=]*([A-Za-z]+)")


GH_FIELDS = re.compile(r"(?:^|\s)(?:-f|-F|--field|--raw-field|--input)(?:[\s=]|$)")


def gh_api_writes(command: str) -> bool:
    """gh api defaults to POST as soon as fields or an input body are given."""
    for match in GH_API.finditer(command):
        arguments = match.group(1)
        if method := GH_METHOD.search(arguments):
            if method.group(1).upper() != "GET":
                return True
        elif re.search(r"\bgraphql\b", arguments):
            if "mutation" in arguments.lower():
                return True
        elif GH_FIELDS.search(arguments):
            return True
    return False


def old_denial(command: str) -> list[str]:
    """Why v0.1.42 refused this command for a worker without yolo (empty: allowed)."""
    reasons = [reason for pattern, reason in DANGEROUS if pattern.search(command)]
    if gh_api_writes(command):
        reasons.append("writing to GitHub through gh api")
    return reasons
