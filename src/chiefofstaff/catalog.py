"""Finding the worker that owns a task: keyword search over each worker's record."""

import math
import re
from collections import Counter

LATIN = re.compile(r"[a-z0-9]+")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
STOP = frozenset(
    "a an and are as at be by can could do does for from has have how i in is it its me my of "
    "on or our please should that the their them then there this to was we what when where "
    "which who why will with would you your".split()
    # Words every request to a worker uses.
    + "also done just let ok owner please report router session sessions task tasks tell "
    "thanks worker workers".split()
)
# A term found in a more telling field counts more.
WEIGHTS = {"title": 4, "task": 3, "paths": 3, "reports": 2, "messages": 1}


def terms(text: str) -> set[str]:
    """Lowercase words, and character pairs for Chinese and Japanese text."""
    text = text.lower()
    found = {word for word in LATIN.findall(text) if word not in STOP and len(word) > 1}
    found.update(word for word in LATIN.findall(text) if word.isdigit())
    for run in CJK.findall(text):
        found.update([run] if len(run) == 1 else (run[i : i + 2] for i in range(len(run) - 1)))
    return found


def rank(query: str, docs: dict[str, dict[str, str]]) -> list[tuple[str, list[str]]]:
    """Matching doc ids with the terms they matched, best first.

    Rare terms, terms in telling fields, and covering more of the query count more."""
    wanted = terms(query)
    if not wanted or not docs:
        return []
    indexed = {
        doc_id: {field: terms(text) & wanted for field, text in fields.items()}
        for doc_id, fields in docs.items()
    }
    frequency = Counter(
        term for fields in indexed.values() for term in set().union(*fields.values())
    )
    if len(docs) >= 5:
        # Words most workers share cannot tell tasks apart (unless the query has nothing else),
        # except a subject word that names some of them in their titles.
        titled = {term for fields in indexed.values() for term in fields.get("title", ())}
        common = {
            term
            for term, count in frequency.items()
            if count > len(docs) / 2 and term not in titled
        }
        wanted = wanted - common or wanted
    scores = []
    for doc_id, fields in indexed.items():
        score, matched = 0.0, []
        for term in sorted(wanted):
            weight = max(
                (WEIGHTS[field] for field, found in fields.items() if term in found), default=0
            )
            if weight:
                matched.append(term)
                score += weight * math.log(1 + len(docs) / frequency[term])
        if matched:
            scores.append((score * len(matched) / len(wanted), doc_id, matched))
    return [(doc_id, matched) for _, doc_id, matched in sorted(scores, key=lambda s: -s[0])]
