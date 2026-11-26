// Asks of the owner by subject (ledger.py): one choice asked by several workers (and the
// Router) is one item under Needs you; the others fold into it, as the server counts them
// (digest.needs, Ledger.folded). The richest is shown: a decision card, a question card, a
// question, a note of the Router's, the oldest of a kind.

const CHANNELS = ["decision", "ask_user", "question", "notify"];

// `present`: the keys of the items under Needs you now. `folded`: a key -> the key it folds
// into; `shown`: a shown item's key -> its subject (with the other asks, not its own).
export function folding(subjects, present) {
  const folded = new Map();
  const shown = new Map();
  for (const subject of subjects || []) {
    const order = [...subject.asks].sort((a, b) => CHANNELS.indexOf(a.channel) - CHANNELS.indexOf(b.channel));
    const keys = [];
    const own = new Map();
    for (const ask of order) {
      const key = (ask.keys || []).find((candidate) => present.has(candidate));
      if (!key) continue;
      if (!keys.includes(key)) keys.push(key);
      if (!own.has(key)) own.set(key, ask.id);
    }
    if (!keys.length) continue;
    const mine = own.get(keys[0]);
    // What the others asked (not the Router's note that only says a worker's ask again).
    shown.set(keys[0], { ...subject, others: subject.asks.filter((ask) => ask.id !== mine && !ask.restates && !(ask.keys || []).includes(keys[0])) });
    for (const key of keys.slice(1)) folded.set(key, keys[0]);
  }
  return { folded, shown };
}

// What a shown item's subject line says, and its version (it is drawn again as it changes).
export function subjectVersion(subject) {
  return JSON.stringify([subject.id, subject.asked_by, subject.others.map((ask) => [ask.id, ask.text]), subject.decided?.id || "", (subject.possibly || []).map((other) => [other.id, other.decided || ""])]);
}
