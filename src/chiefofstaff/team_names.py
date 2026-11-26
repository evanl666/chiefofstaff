"""Teams lite's names (teams-v2.md §5.1, §7; team quality): a team's goal from the owner's
request, each part's name from its task's title, distinct avatars, and an exchange's title from
its lead sentence. Derived live, without a model call or any prompt change.

A name is short, meaningful, distinct in its team and never cut inside a word or a character
(an emoji with its modifiers, a letter with its accents): the owner's own words for the request
(its first clause, without "帮我", "please", "can you", a pasted log or a link), else what its
parts' titles share, else the first part's head; an ellipsis only when nothing shorter fits."""

# ruff: noqa: RUF001  (Chinese full-width punctuation, on purpose: it ends clauses and heads.)

from __future__ import annotations

import re
import unicodedata

# Display columns: CJK and full-width characters count 2.
WIDE = re.compile(
    "[\u1100-\u115f\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6]"
)
# A clause ends at a mark; "." "," ":" only before a space ("3.9", "app.py", "10,000" go on).
HEAD_END = re.compile(r"[：，。！？；\n(（]|[.,:!?;](?=\s|\Z)")
BOUNDARY = re.compile(r"[\s:：·\-–—]")
# Where a title goes on after a shared CJK start: at a space or a separator.
GOES_ON = re.compile(r"[\s:：·]")
LEADING = re.compile(
    r"^(?:[\s:：·\-–—,，、]+|的|之间|之|与|和|(?:and|the|of|for|on|in|to|with|at|by)\s+)+",
    re.IGNORECASE,
)
PART_HEAD = re.compile(r"[：:·(（]|\s[-–—]\s")
TRAILING = re.compile(r"[\s,;:·，、；：]+$")
# A whole word only ("History" keeps its "Hi").
GREETING = re.compile(
    r"^(?:(?:ok|okay|fyi|note|hi|hello|thanks)(?![A-Za-z])|好的|收到|注意|补充)[\s,，:：!！.。]*",
    re.IGNORECASE,
)
SENTENCE_END = re.compile(r"[。！？]|[.!?](?=\s|\Z)|\n")
GOAL_COLUMNS = 48
PART_COLUMNS = 24
# Longer, a Chinese part's name is what follows its last 的 ("远程请求的安全规则": 安全规则).
SHORT_COLUMNS = 12
TITLE_COLUMNS = 60
# What a request starts with that is no part of its name: a greeting, a please, who should do
# it ("你去", "can you"), and in Chinese a softener after the verb ("看一下").
FILLER = re.compile(
    r"^(?:(?:hey|hi|hello|ok|okay|so|also|and|well|please|pls|plz|just|now)(?![\w'])[\s,，!！.]*"
    r"|(?:can|could|would|will) you(?: please)?\s+|i(?:'d| would) like (?:you )?to\s+"
    r"|i (?:want|need)(?: you)? to\s+|we (?:need|want|have) to\s+|we need\s+|let'?s\s+"
    r"|go and\s+|好的|好|嗯|那么|那|然后|另外|还有|对了|顺便|麻烦你?|劳驾|请你?|帮忙|帮我|"
    r"你(?:再|也|先|现在)?(?:去|来|帮我|给我|帮忙)?|给我|我(?:们)?(?:想|要|需要|得)(?:让你|请你|你)?(?:去)?"
    r"|能不能|能否|可不可以|可以|需要|先|再|最后|去|对于|关于|至于)",
    re.IGNORECASE,
)
SOFTENER = re.compile(r"(?<=[\u4e00-\u9fff])一下(?:子)?")
# Said of what is pasted, or of nothing in particular: no name on its own.
VAGUE = re.compile(
    r"^(?:(?:look|looking) (?:into|at) (?:this|that|it)|(?:fix|check|handle|do) (?:this|that|it)"
    r"(?: for me)?|this|that|it|这个|那个|这|那|这个报错|这个问题|看下|看看|整理|处理|弄|搞定|"
    r"debug|help|都做了?|全(?:都|部)?做了?|都要|做吧|开始吧|继续|yes|go(?: ahead)?|both|all of them"
    r"|do (?:it|them|both|all)(?: of them)?)$",
    re.IGNORECASE,
)
DEICTIC = re.compile(r"这个|那个|\b(?:this|that|it)\b", re.IGNORECASE)
PASTED = re.compile(
    r"Traceback|^\s*File \"|^\s*at [\w.$<>]+\(|^\s*[\w.]*(?:Error|Exception)\b|^\s*\$ |^\s*>>>"
    r"|^\s*[{\[<]|^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:|^\s*\[\w+\]",
)
URL = re.compile(r"https?://\S+|www\.\S+")
# Where a clause may be shortened, in the owner's words: at "and", "then", "、", "和"…
SUB_CLAUSE = re.compile(r"\s+(?:and|then|so|but|while|plus)\s+|[、；;]|并且|然后|以及|同时|再|并")
# A word that may not end a name ("the", "of", "for"…).
DANGLING = re.compile(
    r"\s(?:a|an|the|of|for|to|in|on|at|with|and|or|by|from|into|its|their|our|my|your|since|until"
    r"|because|when|as|so|than|about|after|before|via|per|vs|的|和|与|及|在|对|把|给|从)$",
    re.IGNORECASE,
)
FROM = re.compile(r"\s+from\s+(?!.*\sfrom\s)", re.IGNORECASE)
# What a Chinese part's name starts with when it says what is done ("更新过时的文档页面" keeps its
# verb); one that does not is named by what follows its 的 ("远程请求的安全规则": 安全规则).
DOING = re.compile(
    "更新|修复|修|实现|添加|加|补|删除|删|调整|列出|写|做|设计|研究|回复|改|优化|重构|迁移|升级|"
    "部署|检查|整理|排查|调研|分析|导出|导入|发布|上线|接入|支持|清理|统一"
)
# A word in capitals only at its start ("Draft"), which a name after another word writes in
# lower case ("Glossary draft"); an acronym or a name in capitals ("API", "iOS") stays.
SENTENCE = re.compile(r"^[A-Z](?=[a-z]+$)")
# One Latin word ("writer", "user_id"): no part's name once a word before it is gone.
LONE = re.compile(r"^[A-Za-z][\w.\-/#]*$")
# A plain word in lower case, which a name starts in capitals ("iOS", "user_id" as written).
PLAIN = re.compile(r"^[a-z](?=[a-z]*(?:\s|$))")


def wide(char: str) -> bool:
    return bool(char) and bool(WIDE.match(char))


def columns(text: str) -> int:
    return sum(2 if wide(char) else 1 for char in text)


def clusters(text: str) -> list[str]:
    """The text's user-perceived characters: a letter with its accents, an emoji with its
    modifiers and joined emoji, a flag's two letters, kept together."""
    found: list[str] = []
    joined = False
    for char in text:
        code = ord(char)
        extends = (
            unicodedata.combining(char)
            or 0xFE00 <= code <= 0xFE0F
            or 0x1F3FB <= code <= 0x1F3FF
            or 0xE0020 <= code <= 0xE007F
            or char == "\u200d"
        )
        flag = 0x1F1E6 <= code <= 0x1F1FF
        if found and (
            extends
            or joined
            or (flag and len(found[-1]) == 1 and 0x1F1E6 <= ord(found[-1]) <= 0x1F1FF)
        ):
            found[-1] += char
        else:
            found.append(char)
        joined = char == "\u200d"
    return found


def fit(text: str, most: int) -> str:
    """At most `most` columns without an ellipsis when it can be: whole words dropped from the
    end, never leaving one that may not end it ("for", "the"); else ""."""
    text = " ".join(text.split())
    if columns(text) <= most:
        return text
    bare = re.sub(r"(?<=\S) (?:the|a|an) (?=\S)", " ", text, flags=re.IGNORECASE)
    if columns(bare) <= most:
        return bare
    words = text.split(" ")
    while len(words) > 1:
        dropped = words.pop()
        shorter = TRAILING.sub("", " ".join(words))
        while DANGLING.search(" " + shorter) and " " in shorter:
            dropped = shorter.rsplit(" ", 1)[1]
            shorter = TRAILING.sub("", shorter.rsplit(" ", 1)[0])
        if dropped.casefold() in ("to", "into") and (start := FROM.search(shorter)):
            shorter = shorter[: start.start()]  # "from Python 3.9" without "to 3.12" misleads.
        if columns(shorter) <= most and len(shorter.split()) >= 2:
            return shorter
    return ""


def cut(text: str, most: int) -> str:
    """At most `most` columns, cut at a boundary (a space, or between two CJK characters),
    never inside a character (an emoji, an accented letter); an ellipsis says so."""
    text = text.strip()
    if columns(text) <= most:
        return text
    pieces = clusters(text)
    width = at = boundary = 0
    while at < len(pieces):
        size = columns(pieces[at][0]) or 1
        if width + size > most - 1:
            break
        width += size
        following = pieces[at + 1][0] if at + 1 < len(pieces) else ""
        if following == " " or wide(pieces[at][0]) or wide(following):
            boundary = at + 1
        at += 1
    kept = TRAILING.sub("", "".join(pieces[: boundary or at]).strip())
    while len(kept) > 1 and kept[-1] in "的和与及":
        kept = TRAILING.sub("", kept[:-1])  # "…的…": not on a word that leads to the next.
    return kept + "\u2026"


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
        index = len(chars)
        following = [title[index] if index < len(title) else "" for title in titles]
        if not wide(last):
            return all(not char or bool(GOES_ON.match(char)) for char in following)
        return all(wide(char) or bool(char and GOES_ON.match(char)) for char in following)

    while chars and not at_boundary():
        chars.pop()
    found = "".join(chars)
    return found if columns(found.strip()) >= 4 else ""


def own_words(request: str) -> str:
    """The owner's own words of a request: without what they pasted (a log, a stack trace, code,
    a link), the first lines that are theirs."""
    text = re.sub(r"```.*?(```|\Z)", " ", request or "", flags=re.DOTALL)
    lines = []
    for line in text.splitlines():
        if PASTED.search(line):
            if lines:
                break
            continue
        lines.append(line)
    return URL.sub(" ", "\n".join(lines)).strip()


def unfilled(clause: str) -> str:
    """A clause without what is no part of a name: greetings, please, who should do it."""
    clause = SOFTENER.sub("", clause.strip())
    while True:
        shorter = FILLER.sub("", clause, count=1).strip(" ,，:：;；.。!！-—–")
        if shorter == clause:
            break
        clause = shorter
    clause = re.sub(r"^\d+[).、]\s*", "", clause)
    return PLAIN.sub(lambda found: found.group(0).upper(), clause)


def clauses(text: str) -> list[str]:
    """A request's clauses, in order: at punctuation, a line's end, a list's number, and in
    Chinese at a space between two characters (the owner's comma when they type fast)."""
    marked = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "\n", text)
    marked = re.sub(r"\s\d+[).、](?!\d)\s*", "\n", marked)
    return [piece.strip() for piece in HEAD_END.split(marked) if piece.strip()]


def goal_name(request: str, titles: list[str]) -> str:
    """A team's name: the owner's own name for the request (its first clause that says what,
    without fillers, pastes and links), or the words of it all its parts' titles share; when
    that is too long, what the titles share, its first part (to "and", "then"…), the first
    part's head, whole words of it; cut only when nothing fits."""
    first = ""
    for clause in clauses(own_words(request)):
        clause = unfilled(clause)
        lead = unfilled(SUB_CLAUSE.split(clause)[0])
        if columns(clause) < 4 or VAGUE.match(clause) or VAGUE.match(lead):
            continue
        if DEICTIC.search(lead) and columns(lead) <= 16 and titles:
            continue  # "这个报错", "fix it": its parts say what.
        first = clause
        break
    shared = TRAILING.sub("", shared_start(titles)).strip(" :：·-–—")
    named = columns(shared) >= 6 and (" " in shared or wide(shared[0]))
    if first and columns(first) <= GOAL_COLUMNS:
        # What the owner's clause is about, when every part has it: the name ("The onboarding
        # emails are a mess": Onboarding emails); a clause that ends on it says more.
        if named and re.sub(
            r"^(?:the|a|an|our|my)\s+", "", first, flags=re.IGNORECASE
        ).casefold().startswith(shared.casefold()):
            return PLAIN.sub(lambda found: found.group(0).upper(), shared)
        return first
    if named:
        return cut(PLAIN.sub(lambda found: found.group(0).upper(), shared), GOAL_COLUMNS)
    if first:
        piece = unfilled(SUB_CLAUSE.split(first)[0])
        if 8 <= columns(piece) <= GOAL_COLUMNS:
            return piece
    if titles:
        head = PART_HEAD.split(titles[0])[0].strip()
        if 4 <= columns(head) <= GOAL_COLUMNS:
            return head
    if shorter := fit(first or (titles[0] if titles else ""), GOAL_COLUMNS):
        return shorter
    return cut(first or (titles[0] if titles else "") or "Team", GOAL_COLUMNS)


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


def at_edge(text: str, piece: str) -> bool:
    """Whether a piece of the goal starts or ends a part's title as a word of its own: at its
    start followed by a space or a mark ("退款 API"), at its end after one; never inside a word
    ("导入后发": not "后发"), nor from the middle (the rest would not read)."""
    if text.startswith(piece):
        after = text[len(piece) : len(piece) + 1]
        return not after or not (after.isalnum() or wide(after))
    if text.endswith(piece):
        before = text[-len(piece) - 1 : -len(piece)]
        return not before or not (before.isalnum() or wide(before))
    return False


def part_names(titles: list[str], goal: str) -> list[str]:
    """Each part's name from its task's title: without what all of them start with, without
    the goal's words at its start or end (or, when still long, whole words from its middle),
    its head, and in Chinese what follows the last 的 when still long; shortened at whole
    words, an ellipsis only when nothing shorter fits; a clash gets a number. Never one word
    left of a two-word name ("Token writer", "Login page"), nor an identifier for a head that
    is the goal itself ("修复 app.py 的 KeyError", not "user_id")."""
    shared = shared_start(titles)
    word = shared.strip(" :：·-–—")
    if shared and " " not in word and not wide(shared[0]):
        heads = [PART_HEAD.split(title[len(shared) :])[0].strip() for title in titles]
        if all(LONE.match(head) for head in heads):
            # "Glossary draft", "Token writer", "Checkout API": the word they share is half
            # of each name, and one alone ("Draft") says little in a list.
            kept = [
                f"{word} {SENTENCE.sub(lambda found: found.group(0).lower(), head)}"
                for head in heads
            ]
            return numbered([fit(name, PART_COLUMNS) or cut(name, PART_COLUMNS) for name in kept])
    names = []
    for title in titles:
        rest = title[len(shared) :] if shared else title
        head = PART_HEAD.split(rest)[0].strip() or rest.strip()
        common = longest_common(rest, goal or "")
        stripped = rest.strip()
        if (
            columns(common) >= 4
            and columns(stripped) - columns(common) >= 2
            and at_edge(stripped, common)
        ):
            without = DANGLING.sub("", " " + rest.replace(common, " ", 1).strip()).strip()
            without = LEADING.sub("", without).strip()
            left = PART_HEAD.split(without)[0].strip()
            if (
                not left
                or (" " not in common and re.fullmatch(r"[a-z][a-z\-]*", left))
                or (LONE.match(left) and head.casefold() == common.casefold())
            ):
                rest = (
                    head  # "Login page", not "Page" ("API" is one); the goal's head, not "user_id".
                )
            else:
                rest = without
        rest = LEADING.sub("", rest).strip()
        rest = PART_HEAD.split(rest)[0].strip() or rest
        rest = " ".join(rest.split())
        if columns(rest) > PART_COLUMNS:
            first = re.split(r"\s+(?:on|for|in|with|from|to|at)\s+", rest, maxsplit=1)[0]
            if 4 <= columns(first) <= PART_COLUMNS:
                rest = first
        if columns(rest) > PART_COLUMNS and goal and not wide(rest[0]):
            # Still long: the goal's words from its middle, as whole words ("the missing days").
            for word in sorted(set(goal.casefold().split()), key=len, reverse=True):
                if len(word) >= 4 and columns(rest) > PART_COLUMNS:
                    shorter = re.sub(
                        rf"(?<!\S){re.escape(word)}(?!\S)", "", rest, flags=re.IGNORECASE
                    )
                    if len(shorter.split()) >= 2:
                        rest = " ".join(shorter.split())
        if (
            columns(rest) > PART_COLUMNS
            and rest[0] in "为给对"
            and columns(rest) - 2 <= PART_COLUMNS
        ):
            rest = rest[1:].strip()  # "为缺少 user_id 的请求加测试": who it is for, as it was said.
        elif (
            columns(rest) > SHORT_COLUMNS
            and re.search("的|之", rest)
            and (columns(rest) > PART_COLUMNS or not DOING.match(rest))
        ):
            after = re.split("的|之", rest)[-1].strip()
            if columns(after) >= 6 and not LONE.match(after):
                rest = after
        # A word in capitals as it is written ("iOS", "eBay"); a plain one starts in capitals.
        rest = PLAIN.sub(lambda found: found.group(0).upper(), rest)
        names.append(fit(rest, PART_COLUMNS) or cut(rest or title, PART_COLUMNS))
    return numbered(names)


def numbered(names: list[str]) -> list[str]:
    """Names alike get a number ("Fix login 2")."""
    seen: dict[str, int] = {}
    found = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        found.append(f"{name} {seen[name]}" if seen[name] > 1 else name)
    return found


def avatars(names: list[str]) -> list[str]:
    """A part's avatar: the first character of its own words (after a first word every name
    shares: "Glossary draft" is D), a letter in capitals; else the next one not taken in the
    team, else its number."""
    firsts = {name.split(" ", 1)[0] for name in names}
    common = len(names) > 1 and len(firsts) == 1 and all(" " in name for name in names)
    used: set[str] = set()
    found = []
    for index, name in enumerate(names):
        own = name.split(" ", 1)[1] if common else name
        mark = next((ch.upper() for ch in re.sub(r"\s+", "", own) if ch.upper() not in used), "")
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
