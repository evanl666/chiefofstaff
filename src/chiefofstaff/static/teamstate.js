// Teams lite (teams-v2.md v3.1 §4): where a team stands, from Home's own board. The server only
// says which tasks are a team's parts; each part is where Home puts its task (board.js
// taskPhase via boardOf) and needs you when Home holds a card or question for it or its
// worker. Pure: no page, so it is tested on its own (tests/test_ui_text.py), and a team and
// its tasks' rows on Home can never disagree.

// A question between parts holds its asker up this long before Home says so in words.
export const BLOCKING_MINUTES = 30;
// A done team leaves Teams this long after the owner has seen it.
export const LEAVES_MINUTES = 10;
// Parts in the order a team's state takes them: the first that applies.
export const STATES = ["needs", "blocked", "working", "watching", "done"];
const OPEN = new Set(["queued", "running", "waiting", "review"]);
const ACCEPTED = new Set(["done", "reviewed", "still-following"]);
// What the owner does for a part that needs them, by Home's marker (`name` its name).
const NEEDS_WORDS = {
  review: (name) => `review ${name}`,
  update: (name) => `news on ${name}`,
  question: (name) => `answer ${name}`,
  waiting: (name) => `answer ${name}`,
  failed: (name) => `${name} failed`,
  stopped: (name) => `${name} stopped`,
  blocked: (name) => `${name} is blocked`,
  stalled: (name) => `${name} made no progress`,
  settings: (name) => `${name}'s settings changed`,
  "check-failed": (name) => `${name} couldn't check back`,
  "runs-unknown": (name) => `${name}'s runs`,
  "pr-changes": (name) => `${name}'s PR`,
  "pr-conflicts": (name) => `${name}'s PR`,
  "pr-failing": (name) => `${name}'s PR`,
  prepared: (name) => `${name} prepared something`,
};
export const needsWords = (marker, name) => (NEEDS_WORDS[marker] || ((who) => `${who} needs you`))(name);

// A short, stable key of a text (FNV-1a), in base 36.
export function hashKey(text) {
  let hash = 0x811c9dc5;
  for (const char of text) {
    hash ^= char.codePointAt(0);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(36);
}
// The latest moment Home sees on a task: its own change, its news, what it watches.
export function latestOf(row) {
  if (!row) return "";
  const watched = Array.isArray(row.monitoring) ? row.monitoring.map((item) => item.changed_at || item.last_check || "") : [];
  return [row.updated_at, row.ended_at, row.news?.at, ...watched].filter(Boolean).sort().pop() || "";
}
export function minutesSince(at, now) {
  return at ? Math.max(0, Math.round((now - Date.parse(at)) / 60000)) : 0;
}
// "12 min", "1 h 40 min", "3 h", "2 d".
export function duration(minutes) {
  if (minutes < 60) return `${minutes} min`;
  if (minutes < 1440) return minutes % 60 && minutes < 600 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${Math.floor(minutes / 60)} h`;
  return `${Math.floor(minutes / 1440)} d`;
}

// One part's place: { state, accepted, finished, next, phase, row }. `ctx`: Home's board
// phases and task rows by id, and what Home's pending cards and questions are for
// (`needs.tasks`, task ids; `needs.sessions`, workers whose item names no task).
export function partOf(member, ctx) {
  const row = ctx.tasks.get(member.task) || null;
  const phase = ctx.phases.get(member.task) || null;
  const base = { ...member, row, phase, accepted: false, finished: false, next: false, held: null };
  if (member.moved_out) return { ...base, state: "done", finished: true, moved: true };
  const status = row?.status || member.status || "";
  const asked = member.tasks.some((task) => ctx.needs.tasks.has(task)) || (ctx.needs.sessions.has(member.session) && OPEN.has(status));
  if (asked) return { ...base, state: "needs", marker: phase?.marker || "question" };
  if (!phase) {
    // Not on Home's board: ended long ago with nothing pending (or not listed yet).
    if (status === "review" || status === "waiting") return { ...base, state: "needs", marker: status === "review" ? "review" : "question" };
    if (status === "running") return { ...base, state: "working" };
    if (status === "queued") return { ...base, state: "watching", next: true };
    return { ...base, state: "done", finished: true, accepted: status === "done" && !row?.set_aside && !member.set_aside };
  }
  if (phase.section === "needs" || phase.section === "") return { ...base, state: "needs", marker: phase.marker };
  if (phase.section === "running") return { ...base, state: "working" };
  if (phase.section === "finished") return { ...base, state: "done", finished: true, accepted: ACCEPTED.has(phase.marker) };
  return { ...base, state: "watching", next: ["queued", "turn"].includes(phase.marker) };
}

// Where a team stands (§4.2): its parts, its state (needs > blocked > working > watching >
// done), its words, its completion key and whether the owner's look at it still applies.
export function teamState(team, ctx) {
  const now = ctx.now ?? Date.now();
  const parts = team.members.map((member) => partOf(member, ctx));
  const byId = new Map(parts.map((part) => [part.id, part]));
  for (const question of team.waiting || []) {
    const part = byId.get(question.from);
    if (!part || !question.blocks || part.state === "needs" || part.finished) continue;
    if (!part.held || question.minutes > part.held.minutes) part.held = question;
    part.state = "blocked";
  }
  const state = STATES.find((one) => parts.some((part) => part.state === one)) || "done";
  const done = state === "done";
  const finished = parts.filter((part) => part.finished).length;
  const accepted = parts.some((part) => part.accepted);
  const count = (test) => parts.filter(test).length;
  // Completion key: each part's place on Home and the latest moment Home sees on it.
  const key = hashKey(parts.map((part) => [part.id, part.task, part.moved ? "out" : "", part.phase?.section ?? "-", part.phase?.marker ?? "", part.row?.status || part.status || "", latestOf(part.row) || part.ended_at || ""].join(":")).join("|"));
  const seen = team.seen && team.seen.key === key ? team.seen : null;
  const leavesAt = seen && !seen.dismissed ? Date.parse(seen.at) + LEAVES_MINUTES * 60000 : seen ? 0 : null;
  const left = done && seen !== null && (seen.dismissed || now >= leavesAt);
  const endedAt = parts.map((part) => part.row?.ended_at || part.ended_at || "").filter(Boolean).sort().pop() || "";
  const name = (id) => byId.get(id)?.name || "";
  let words;
  let tone = "";
  const needing = parts.find((part) => part.state === "needs");
  const held = parts.filter((part) => part.held && part.held.minutes >= BLOCKING_MINUTES).sort((a, b) => b.held.minutes - a.held.minutes)[0];
  if (needing) {
    words = `Waiting on you · ${needsWords(needing.marker, needing.name)}`;
    tone = "amber";
  } else if (held) {
    words = `${held.name} waits on ${name(held.held.to)}'s answer · ${duration(held.held.minutes)}`;
    tone = "amber";
  } else if (done) {
    const took = endedAt ? duration(minutesSince(team.request.at, Date.parse(endedAt))) : "";
    words = accepted ? ["Done", `${parts.length} parts`, took].filter(Boolean).join(" · ") : "Done · set aside";
    tone = accepted ? "green" : "";
  } else {
    const pieces = [
      [count((part) => part.state === "working"), "working"],
      [count((part) => part.state === "blocked"), "waiting on a teammate"],
      [count((part) => part.state === "watching" && part.next), "up next"],
      [count((part) => part.state === "watching" && !part.next), "watching"],
    ];
    words = pieces.filter(([n]) => n).map(([n, what]) => `${n} ${what}`).join(" · ");
  }
  const segment = (part) => (part.finished ? (part.accepted ? "done" : "ended") : part.state === "needs" || part.state === "blocked" ? "waiting" : part.state === "watching" ? (part.next ? "next" : "watching") : part.state);
  return {
    state,
    done,
    accepted,
    parts,
    finished,
    total: parts.length,
    key,
    seen,
    leavesAt,
    left,
    endedAt,
    words,
    tone,
    segments: parts.map(segment),
    needing,
    held: held || null,
  };
}

// What Home's pending cards and questions are for (inbox.js needsYou): task ids, and workers
// whose item names no task (an ask_owner card while its turn runs, say).
export function pendingFor(items) {
  const tasks = new Set();
  const sessions = new Set();
  const add = (taskId, sessionId) => (taskId ? tasks.add(taskId) : sessionId && sessions.add(sessionId));
  for (const draft of [...(items.drafts || []), ...(items.ok || []), ...(items.uncertain || [])]) {
    const askers = draft.kind === "decision" ? draft.target?.askers || [] : [];
    if (askers.length) for (const asker of askers) add(asker.task_id, asker.session_id);
    else add(draft.target?.task_id || draft.task_id, draft.session_id);
  }
  for (const question of items.questions || []) add(question.task_id, question.session_id);
  for (const task of items.tasks || []) tasks.add(task.id);
  return { tasks, sessions };
}
