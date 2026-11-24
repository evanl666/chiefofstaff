"""Teams lite's names (teams-v2.md §5.1, §7): a team's goal from the owner's request, each part's
name from its task's title, distinct avatars, and an exchange's title from its lead sentence.
Derived live, without any prompt change: the prototype's derive.js rules, the same cases."""

# ruff: noqa: RUF001  (Chinese full-width punctuation, on purpose: it ends clauses and heads.)

from __future__ import annotations

import re

# Display columns: CJK and full-width characters count 2.
WIDE = re.compile(
    "[\u1100-\u115f\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6]"
)
HEAD_END = re.compile(r"[：:，,。.！!？?;；\n(（]")
BOUNDARY = re.compile(r"[\s:：·\-–—]")
# Where a title goes on after a shared CJK start: at a space or a separator.
GOES_ON = re.compile(r"[\s:：·]")
LEADING = re.compile(
    r"^(?:[\s:：·\-–—,，、]+|的|之间|之|与|和|and\s+|the\s+|of\s+|for\s+)+", re.IGNORECASE
)
PART_HEAD = re.compile(r"[：:·(（]|\s[-–—]\s")
TRAILING = re.compile(r"[\s,;:·，、；：]+$")
# A whole word only ("History" keeps its "Hi").
GREETING = re.compile(
    r"^(?:(?:ok|okay|fyi|note|hi|hello|thanks)(?![A-Za-z])|好的|收到|注意|补充)[\s,，:：!！.。]*",
    re.IGNORECASE,
)
SENTENCE_END = re.compile(r"[。！？]|[.!?](?=\s|\Z)|\n")
GOAL_COLUMNS = 40
PART_COLUMNS = 16
TITLE_COLUMNS = 60


def wide(char: str) -> bool:
    return bool(char) and bool(WIDE.match(char))


def columns(text: str) -> int:
    return sum(2 if wide(char) else 1 for char in text)


def cut(text: str, most: int) -> str:
    """At most `most` columns, cut at a boundary (a space, or between two CJK characters)."""
    text = text.strip()
    if columns(text) <= most:
        return text
    width = at = boundary = 0
    while at < len(text):
        size = 2 if wide(text[at]) else 1
        if width + size > most - 1:
            break
        width += size
        following = text[at + 1] if at + 1 < len(text) else ""
        if following == " " or wide(text[at]) or wide(following):
            boundary = at + 1
        at += 1
    return TRAILING.sub("", text[: boundary or at].strip()) + "\u2026"


def shared_start(titles: list[str]) -> str:
    """What every title starts with, back to a boundary; "" under 4 columns."""
    if len(titles) < 2:
        return ""
    shared = titles[0]
    for title in titles[1:]:
        while not title.startswith(shared):
            shared = shared[:-1]
    chars = list(shared)

    def at_boundary() -> bool:
        last = chars[-1]
        if BOUNDARY.match(last):
            return True
        if not wide(last):
            return False
        index = len(chars)
        following = [title[index] if index < len(title) else "" for title in titles]
        return all(wide(char) or bool(char and GOES_ON.match(char)) for char in following)

    while chars and not at_boundary():
        chars.pop()
    found = "".join(chars)
    return found if columns(found.strip()) >= 4 else ""


def goal_name(request: str, titles: list[str]) -> str:
    """The owner's request, its first clause; without one, the parts' shared title start."""
    head = HEAD_END.split((request or "").strip())[0].strip()
    if columns(head) >= 4:
        return cut(head, GOAL_COLUMNS)
    return cut(shared_start(titles) or (titles[0] if titles else "") or "Team", GOAL_COLUMNS)


def longest_common(one: str, other: str) -> str:
    """The longest piece of `one` that `other` has too, whatever their case ("Sales report"
    of "Export the sales report")."""
    best = ""
    folded, within = one.casefold(), other.casefold()
    if len(folded) != len(one):
        folded, within = one, other  # (A letter whose case changes its length: as written.)
    for start in range(len(one)):
        end = start + len(best) + 1
        while end <= len(one):
            if folded[start:end] not in within:
                break
            best = one[start:end]
            end += 1
    return best.strip()


def whole_words(text: str, piece: str) -> bool:
    """Whether a piece of a text starts and ends at a word's edge (a CJK character is one):
    "port" of "Import" is no piece to drop."""
    at = text.find(piece)
    before = text[at - 1] if at > 0 else ""
    after = text[at + len(piece)] if at + len(piece) < len(text) else ""

    def edge(outside: str, inside: str) -> bool:
        return not outside.isalnum() or wide(outside) or wide(inside)

    return edge(before, piece[0]) and edge(after, piece[-1])


def part_names(titles: list[str], goal: str) -> list[str]:
    """Each part's name from its task's title: without what all of them start with, without
    words of the goal, its head, and in Chinese what follows the last 的 when still long; a
    clash gets a number."""
    shared = shared_start(titles)
    names = []
    for title in titles:
        rest = title[len(shared) :] if shared else title
        common = longest_common(rest, goal or "")
        if columns(common) >= 4 and whole_words(rest, common):
            rest = rest.replace(common, " ", 1)
        rest = LEADING.sub("", rest).strip()
        rest = PART_HEAD.split(rest)[0].strip() or rest
        if columns(rest) > PART_COLUMNS and re.search("的|之", rest):
            after = re.split("的|之", rest)[-1].strip()
            if columns(after) >= 4:
                rest = after
        rest = re.sub("^[a-z]", lambda found: found.group(0).upper(), rest)
        names.append(cut(rest or title, PART_COLUMNS))
    seen: dict[str, int] = {}
    numbered = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        numbered.append(f"{name} {seen[name]}" if seen[name] > 1 else name)
    return numbered


def avatars(names: list[str]) -> list[str]:
    """A part's avatar: its name's first character (a letter in capitals), else the next one
    not taken in the team, else its number."""
    used: set[str] = set()
    found = []
    for index, name in enumerate(names):
        mark = next((ch.upper() for ch in re.sub(r"\s+", "", name) if ch.upper() not in used), "")
        mark = mark or str(index + 1)
        used.add(mark)
        found.append(mark)
    return found


def message_title(text: str, question: bool = False) -> tuple[str, str]:
    """An exchange's title (its lead sentence; a question's, the first that asks) and the rest
    of its text, Markdown and a leading greeting dropped."""
    plain = (text or "").replace("`", "")
    plain = re.sub(r"\*\*|__", "", plain)
    plain = re.sub(r"^\s*(?:#+|>)\s*", "", plain, flags=re.MULTILINE)
    plain = GREETING.sub("", plain).strip()
    sentences = []
    start = 0
    for found in SENTENCE_END.finditer(plain):
        sentences.append(plain[start : found.start() + 1])
        start = found.start() + 1
    if start < len(plain):
        sentences.append(plain[start:])
    real = [item for item in sentences if item.strip()] or [plain]
    lead = real[0]
    if question:
        lead = next((item for item in real if re.search("[?？]$", item.strip())), real[0])
    lead = lead.strip()
    rest = plain[plain.find(lead) + len(lead) :].strip()
    title = re.sub("[。.！!]$", "", lead)

    def after(piece: str) -> str:
        return re.sub(r"^[；;，,\s]+", "", plain[plain.find(piece) + len(piece) :])

    clause = re.split("[；;]", title)[0]
    if not question and clause != title and columns(clause) >= 12:
        title = clause
    if columns(title) <= TITLE_COLUMNS and title != re.sub("[。.！!]$", "", lead):
        return title, capital(after(title))
    if columns(title) > TITLE_COLUMNS:
        short = re.split("[；;，,]", title)[0]
        if 12 <= columns(short) <= TITLE_COLUMNS:
            return short, capital(after(short))
        return cut(title, TITLE_COLUMNS), capital(plain)
    return title, capital(rest)


def capital(text: str) -> str:
    return re.sub("^[a-z]", lambda found: found.group(0).upper(), text)
