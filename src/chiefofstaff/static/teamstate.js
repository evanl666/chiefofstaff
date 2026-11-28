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
  // Up next, but only on work a part went on with outside the team: it says so.
  for (const part of parts) {
    if (part.state !== "watching" || !part.next) continue;
    const before = (team.edges || []).filter((edge) => edge.kind === "after" && edge.to === part.id && edge.state !== "met").map((edge) => byId.get(edge.from));
    if (before.length && before.every((other) => other?.moved)) part.outside = [...new Set(before.map((other) => other.moved_out?.title || other.name))];
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
  let headline;
  let tone = "";
  const needing = parts.find((part) => part.state === "needs");
  const held = parts.filter((part) => part.held && part.held.minutes >= BLOCKING_MINUTES).sort((a, b) => b.held.minutes - a.held.minutes)[0];
  // One headline, the same on Home's row, the sidebar and the team's page: its row's words
  // say it first.
  if (needing) {
    headline = "Waiting on you";
    words = `${headline} · ${needsWords(needing.marker, needing.name)}`;
    tone = "amber";
  } else if (held) {
    headline = `${held.name} waits on ${name(held.held.to)}'s answer`;
    words = `${headline} · ${duration(held.held.minutes)}`;
    tone = "amber";
  } else if (done) {
    const took = endedAt ? duration(minutesSince(team.request.at, Date.parse(endedAt))) : "";
    headline = "Done";
    words = accepted ? ["Done", `${parts.length} parts`, took].filter(Boolean).join(" · ") : "Done · set aside";
    tone = accepted ? "green" : "";
  } else {
    const away = [...new Set(parts.flatMap((part) => part.outside || []))];
    const awayWords = away.length === 1 ? `waiting on “${away[0]}” (outside this team)` : "waiting on work outside this team";
    const pieces = [
      [count((part) => part.state === "working"), "working"],
      [count((part) => part.state === "blocked"), "waiting on a teammate"],
      [count((part) => Boolean(part.outside)), awayWords],
      [count((part) => part.state === "watching" && part.next && !part.outside), "up next"],
      [count((part) => part.state === "watching" && !part.next), "watching"],
    ].filter(([n]) => n);
    const [first = [0, "watching"]] = pieces;
    headline = capitalized(first[1]);
    // Waiting on work outside alone: the plain words, no count.
    words = pieces.length === 1 && first[1] === awayWords ? headline : pieces.map(([n, what]) => `${n} ${what}`).join(" · ");
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
    headline,
    tone,
    segments: parts.map(segment),
    needing,
    held: held || null,
  };
}

const capitalized = (text) => text.charAt(0).toUpperCase() + text.slice(1);

// The team page's header: its headline (as on Home's row and the sidebar), how many parts are
// done, how long since the request (it ticks). `clock` formats a moment ("9:12").
export function headerWords(team, st, clock, now = Date.now()) {
  if (st.done) return st.endedAt ? `Done at ${clock(st.endedAt)} · from your request at ${clock(team.request.at)}` : `Done · from your request at ${clock(team.request.at)}`;
  return `${st.headline} · ${st.finished} of ${st.total} done · ${duration(minutesSince(team.request.at, now))}`;
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

// ---------------------------------------------------------------------------------------------
// The team page (team-page-v4.md): what only a view of every part at once can say, from what
// Chief of Staff keeps (no model call). Pure, as above: tested on its own.

const firstLineOf = (text = "") => String(text || "").split("\n").map((line) => line.trim()).find(Boolean) || "";
export const listed = (names) => (names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names[0] || "");
// "just now", "12 min ago", "3 h ago", "2 d ago".
export function ago(at, now = Date.now()) {
  const minutes = minutesSince(at, now);
  return minutes < 1 ? "just now" : `${duration(minutes)} ago`;
}
// What a need asks of the owner: an answer (its part stopped on it), a part stuck (failed,
// stopped), a result to review, or something to look at (news, its PR, its runs).
const ASKS = new Set(["", "question", "waiting", "settings"]);
const STUCK = new Set(["failed", "stopped", "blocked", "stalled", "check-failed", "kept-paused", "kept-stopped"]);
export function needKind(marker = "") {
  if (marker === "review") return "review";
  if (STUCK.has(marker)) return "stuck";
  return ASKS.has(marker || "") ? "ask" : "look";
}
const NEED_RANK = { ask: 0, stuck: 1, review: 2, look: 3 };
// Since when a part needs the owner (its task's last change: when it asked, or reported).
export const needSince = (part) => part.row?.updated_at || part.ended_at || "";
const nameIn = (st) => (id) => st.parts.find((part) => part.id === id)?.name || "";
const orderIn = (st) => (id) => st.parts.findIndex((part) => part.id === id);

// The parts that can't go on until this one does: those that wait on it (an `after` not met
// yet), and those that wait on them, in the plan's order; never a finished one.
export function waitingOn(team, id, st) {
  const finished = new Set(st.parts.filter((part) => part.finished).map((part) => part.id));
  const found = [];
  let next = [id];
  while (next.length) {
    const more = [];
    for (const from of next) {
      for (const edge of team.edges || []) {
        if (edge.kind !== "after" || edge.from !== from || edge.state === "met" || edge.to === id || finished.has(edge.to) || found.includes(edge.to)) continue;
        found.push(edge.to);
        more.push(edge.to);
      }
    }
    next = more;
  }
  const order = orderIn(st);
  return found.sort((a, b) => order(a) - order(b));
}

// The one need the brief puts first (team-page-v4 §2.1): of the parts that need the owner, the
// one that holds most of the request up (itself, when stopped on it, and every part that waits
// on it), then an answer before a part stuck before a result to review before the rest, then
// the one waiting longest, then the plan's order. { part, kind, holds, waited }, or null.
export function chooseNeed(team, st, now = Date.now()) {
  const order = orderIn(st);
  const scored = st.parts
    .filter((part) => part.state === "needs")
    .map((part) => {
      const kind = needKind(part.marker);
      const holds = waitingOn(team, part.id, st).length + (kind === "ask" || kind === "stuck" ? 1 : 0);
      return { part, kind, holds, waited: minutesSince(needSince(part), now) };
    });
  scored.sort((a, b) => b.holds - a.holds || NEED_RANK[a.kind] - NEED_RANK[b.kind] || b.waited - a.waited || order(a.part.id) - order(b.part.id));
  return scored[0] || null;
}

// When a part not started starts, in words: after the owner's OK on a part, after parts still
// at it (saying which results it has already), on work outside the team, or up next.
export function startsWords(team, st, part) {
  if (part.outside) return `Waiting on “${part.outside.join("” and “")}” (outside this team)`;
  const name = nameIn(st);
  const order = orderIn(st);
  const into = (team.edges || []).filter((edge) => edge.kind === "after" && edge.to === part.id).sort((a, b) => order(a.from) - order(b.from));
  const owner = into.filter((edge) => edge.state === "owner").map((edge) => name(edge.from));
  const pending = into.filter((edge) => !["met", "owner"].includes(edge.state)).map((edge) => name(edge.from));
  const met = into.filter((edge) => edge.state === "met").map((edge) => name(edge.from));
  const has = met.length && (owner.length || pending.length) ? `Has ${listed(met)}'s ${met.length > 1 ? "results" : "result"} ✓ · s` : "S";
  if (owner.length) return `${has}tarts after your OK on ${listed(owner)}`;
  if (pending.length) return `${has}tarts after ${listed(pending)}`;
  return "Up next";
}

// The brief (team-page-v4 §2.1): where the request stands and the one thing that moves it.
// "need": a part needs the owner (`unblocks`: what waits on it; `also`: other needs); "held": a
// question between parts has held one up half an hour; "calm": nothing needs them (who is at
// work, what starts next); "done".
export function briefOf(team, st, now = Date.now()) {
  if (st.done) return { kind: "done" };
  const name = nameIn(st);
  const need = chooseNeed(team, st, now);
  if (need) {
    const { part, kind } = need;
    const after = waitingOn(team, part.id, st).map(name);
    const gated = (team.edges || []).some((edge) => edge.from === part.id && edge.state === "owner");
    let unblocks = "";
    if (after.length && kind === "review" && gated) unblocks = `${listed(after)} ${after.length > 1 ? "start" : "starts"} once you accept it.`;
    else if (after.length) unblocks = `${listed(after)} can't start until ${part.name} is done.`;
    // Parts that ask the owner the same card (one choice, joined): one need, asked by them all
    // (QA 0.1.61 Q10: "needs you too" for the card the brief shows).
    const same = (other) => Boolean(part.askKey) && other.askKey === part.askKey;
    const along = st.parts.filter((other) => other !== part && other.state === "needs" && same(other)).map((other) => other.name);
    const others = st.parts.filter((other) => other.state === "needs" && other !== part && !same(other));
    let also = "";
    if (others.length === 1) also = needKind(others[0].marker) === "review" ? `${others[0].name}'s result is ready too (below).` : `${others[0].name} needs you too (below).`;
    else if (others.length) also = `${others.length} more parts need you (below).`;
    return { kind: "need", need: kind, part, marker: part.marker || "", waited: need.waited, unblocks, also, along };
  }
  if (st.held) {
    const after = waitingOn(team, st.held.id, st).map(name);
    return { kind: "held", part: st.held, question: st.held.held, to: name(st.held.held.to), waited: st.held.held.minutes, unblocks: after.length ? `${listed(after)} can't start until ${st.held.name} is done.` : "" };
  }
  const working = st.parts.filter((part) => part.state === "working");
  const waiting = st.parts.filter((part) => part.state === "blocked").map((part) => `${part.name} waits on ${name(part.held.to)}'s answer`);
  const next = st.parts.find((part) => part.state === "watching" && part.next);
  const words = next ? startsWords(team, st, next) : "";
  return { kind: "calm", working: working.map((part) => part.name), waiting, next: next || null, nextWords: words === "Up next" ? "" : words.charAt(0).toLowerCase() + words.slice(1) };
}

// A part's one status line on the plan, in plain words (never an internal word). `ask`: the
// question it asks the owner, when known; `clock` formats a moment.
export function partWords(team, st, part, { ask = "", clock = (at) => at } = {}) {
  const row = part.row;
  const name = nameIn(st);
  if (part.moved) return `Continued in “${part.moved_out.title}” outside this team`;
  if (part.state === "blocked") return `Waits on ${name(part.held.to)}'s answer · ${duration(part.held.minutes)}`;
  if (part.state === "needs") {
    const kind = needKind(part.marker);
    if (kind === "review") return "Result ready for you";
    if (part.marker === "update") return "News on its result for you";
    if (kind === "ask") return ask ? `Asks you: ${ask}` : "Asks you something";
    if (part.marker === "failed") return part.phase?.why ? `Failed: ${part.phase.why}` : "Failed";
    return part.phase?.why || "Needs you";
  }
  if (part.state === "working") return firstLineOf(row?.progress_updates?.[0]?.text || row?.progress) || "Working";
  if (part.state === "watching" && part.next) return startsWords(team, st, part);
  if (part.outside) return startsWords(team, st, part);
  if (part.state === "watching") return part.phase?.why || "Watching";
  const status = row?.status || part.status;
  const ended = row?.ended_at || part.ended_at;
  if (part.accepted) return ended ? `Done · you accepted it at ${clock(ended)}` : "Done";
  if (status === "cancelled") return "Cancelled";
  if (status === "failed") return "Failed";
  return "Set aside";
}

// A part's time on the plan: how long it took, how long it has waited for the owner, how long
// it has been at work (elapsed, never an estimate).
export function partTime(part, now = Date.now()) {
  const row = part.row || {};
  const started = row.started_at || "";
  if (part.moved) return "";
  if (part.finished) {
    const ended = row.ended_at || part.ended_at;
    if (!started || !ended || ended <= started) return "";
    const took = minutesSince(started, Date.parse(ended));
    return took ? duration(took) : "<1 min";
  }
  if (part.state === "needs") {
    const minutes = minutesSince(needSince(part), now);
    if (needKind(part.marker) === "review") return minutes < 1 ? "just now" : `${duration(minutes)} ago`;
    return `waiting ${duration(minutes)}`;
  }
  if (part.state === "blocked") return `waiting ${duration(part.held.minutes)}`;
  if (part.state === "working") return started ? duration(minutesSince(started, now)) : "";
  return "";
}

// The plan's stages: a part's stage is how many parts it waits on, one after another (its
// `after` and the work it builds on); stage 0 those that wait on none. "Then", "Then, side by
// side" (several in a stage), "Side by side" (several first).
export function stagesOf(team, st) {
  const ids = new Set(st.parts.map((part) => part.id));
  const before = new Map(st.parts.map((part) => [part.id, []]));
  for (const edge of team.edges || []) if (ids.has(edge.from) && ids.has(edge.to) && edge.from !== edge.to) before.get(edge.to).push(edge.from);
  const depth = new Map();
  const deep = (id, seen = new Set()) => {
    if (depth.has(id)) return depth.get(id);
    if (seen.has(id)) return 0;
    seen.add(id);
    const found = Math.max(-1, ...before.get(id).map((other) => deep(other, seen))) + 1;
    depth.set(id, found);
    return found;
  };
  for (const part of st.parts) deep(part.id);
  const stages = [];
  for (const part of st.parts) (stages[depth.get(part.id)] ||= []).push(part);
  return stages.filter(Boolean).map((parts, index) => ({ index, parts, label: index ? (parts.length > 1 ? "Then, side by side" : "Then") : parts.length > 1 ? "Side by side" : "" }));
}
// The rail from one stage to the next: solid once every result into the next stage came, amber
// while one waits for the owner's OK, red when one won't come, dashed while one is to come.
export function railState(team, st, stage, next) {
  if (st.done) return "met";
  const into = new Set(next.parts.map((part) => part.id));
  const edges = (team.edges || []).filter((edge) => into.has(edge.to) && edge.kind === "after");
  if (edges.some((edge) => edge.state === "failed")) return "failed";
  if (edges.some((edge) => edge.state === "owner")) return "owner";
  return edges.every((edge) => edge.state === "met") ? "met" : "pending";
}

// What to watch out for, across the parts (team-page-v4 §3): a question holding a part up, a
// part waiting on work outside the team, a result that won't come to the part waiting on it, a
// changed decision a part has not confirmed yet. Each { key, parts, text, sub, question? }.
export function watchOf(team, st, decided = []) {
  const name = nameIn(st);
  const found = [];
  for (const part of st.parts) {
    if (part.held && !part.finished) found.push({ key: `w:q:${part.held.id}`, parts: [part.id, part.held.to], text: `${part.name} waits on ${name(part.held.to)}'s answer`, sub: `${duration(part.held.minutes)} · its work waits on it`, question: part.held });
    if (part.outside) found.push({ key: `w:o:${part.id}`, parts: [part.id], text: `${part.name} waits on “${part.outside.join("” and “")}”`, sub: "Outside this team: it starts when that is done" });
  }
  for (const edge of team.edges || []) {
    if (edge.state === "failed") found.push({ key: `w:f:${edge.from}:${edge.to}`, parts: [edge.from, edge.to], text: `${name(edge.from)}'s result won't come to ${name(edge.to)}`, sub: `${name(edge.from)} ended without finishing: retry it, or tell ${name(edge.to)} to go on without it` });
  }
  const open = new Set(st.parts.filter((part) => !part.finished).map((part) => part.id));
  for (const entry of decided) {
    const left = (entry.unconfirmed || []).filter((id) => open.has(id));
    if (entry.kind === "yours" && left.length) found.push({ key: `w:d:${entry.key}`, parts: left, text: `${listed(left.map(name))} ${left.length > 1 ? "haven't" : "hasn't"} confirmed your change yet`, sub: entry.text });
  }
  return found;
}

// Since you looked (team-page-v4 §2.4): what changed across the parts since the owner last had
// the page in sight (`looked`), oldest first, each a jump: a part that asked them, reported,
// finished or failed; a PR merged; an answer between parts (what it settled is in Decided).
// Not the notes parts tell each other (folded, for the curious). Nothing when never looked.
export function sinceOf(team, st, { looked = "", decided = [], made = [], brief = null } = {}) {
  if (!looked) return [];
  const name = nameIn(st);
  const after = (at) => Boolean(at) && at > looked;
  const found = [];
  for (const part of st.parts) {
    if (part.moved) continue;
    const row = part.row || {};
    if (part.state === "needs") {
      const at = needSince(part);
      const kind = needKind(part.marker);
      const text = kind === "review" ? `${part.name}'s result is ready` : kind === "ask" ? `${part.name} asked you` : `${part.name} needs you`;
      if (after(at)) found.push({ key: `n:${part.id}`, at, text, target: brief?.part?.id === part.id ? "brief" : `part:${part.id}` });
    } else if (part.finished) {
      const at = row.ended_at || part.ended_at;
      const status = row.status || part.status;
      const text = part.accepted ? `${part.name} is done` : status === "failed" ? `${part.name} failed` : status === "cancelled" ? `${part.name} was cancelled` : `${part.name} ended`;
      if (after(at)) found.push({ key: `e:${part.id}`, at, text, target: `part:${part.id}` });
    } else if (part.state === "blocked" && part.held && after(part.held.asked)) {
      // A question between parts, once it holds its asker up (a note never is news here).
      found.push({ key: `q:${part.held.id}`, at: part.held.asked, text: `${part.name} waits on ${name(part.held.to)}'s answer`, target: brief?.part?.id === part.id ? "brief" : `part:${part.id}` });
    }
  }
  for (const item of made) if (item.status === "merged" && after(item.at)) found.push({ key: `m:${item.key}`, at: item.at, text: `${name(item.part)}'s ${item.short} merged`, target: `line:${item.key}` });
  for (const entry of decided) if (entry.kind === "between" && after(entry.at)) found.push({ key: entry.key, at: entry.at, text: `${name(entry.from)} answered ${name(entry.to)}`, target: `line:${entry.key}` });
  return found.sort((a, b) => a.at.localeCompare(b.at) || a.key.localeCompare(b.key));
}

// Tell the team (team-page-v4 §2.3): how a part takes the owner's word, by where it stands, and
// in words. ["now" | "start" | "reopen" | "", words]; "" for a part that can't take it.
export function tellHow(team, st, part) {
  const status = part.row?.status || part.status || "";
  if (part.moved) return ["", "Went on outside this team: tell it in its own chat"];
  if (status === "cancelled") return ["", "Cancelled: retry it first"];
  if (status === "queued" || (part.state === "watching" && part.next)) return ["start", "Not started: reads it when it starts (nothing spent till then)"];
  if (part.finished || status === "done" || status === "failed") return ["reopen", `${status === "failed" ? "Failed" : "Done"}: this reopens it as a follow-up`];
  if (part.state === "needs") return ["now", needKind(part.marker) === "review" ? "Result ready: reads it now, before you accept" : "Waiting on you: reads it now, with your answer if you give one"];
  if (part.state === "blocked") return ["now", `Waits on ${nameIn(st)(part.held.to)}'s answer: reads it now`];
  if (part.state === "watching") return ["now", "Watching: reads it now"];
  return ["now", "At work: reads it at its next step"];
}
// Who gets it unless the owner changes it: every part not finished that can take it (a done
// part only when ticked: it reopens).
export const defaultRecipients = (team, st) => st.parts.filter((part) => !part.finished && ["now", "start"].includes(tellHow(team, st, part)[0])).map((part) => part.id);

// The newest change of a team that needs nothing of the owner (Home's row): an exchange between
// two parts, a part finishing or failing, a part's latest progress. { at, text }, or null.
export function newestOf(team, st) {
  const name = nameIn(st);
  const found = [];
  if (team.news) found.push({ at: team.news.at, text: `${name(team.news.from)} ${team.news.kind} ${name(team.news.to)}` });
  for (const part of st.parts) {
    const row = part.row || {};
    if (part.moved) continue;
    if (part.finished && (row.ended_at || part.ended_at)) {
      const status = row.status || part.status;
      found.push({ at: row.ended_at || part.ended_at, text: part.accepted ? `${part.name} is done` : status === "failed" ? `${part.name} failed` : `${part.name} ended` });
    } else if (part.state === "working" || part.state === "watching") {
      const update = row.progress_updates?.[0];
      if (update?.at && firstLineOf(update.text)) found.push({ at: update.at, text: `${part.name}: ${firstLineOf(update.text)}` });
    }
  }
  return found.filter((item) => item.at).sort((a, b) => a.at.localeCompare(b.at)).pop() || null;
}
// Home's Teams row (team-page-v4 §2.6): what needs the owner and a held question first, as
// before; done, the outcome (its PRs merged, how long); else the newest change and how long
// ago, in stronger ink while the owner has not looked since. `merged`: its parts' PRs merged.
export function rowWords(team, st, { now = Date.now(), merged = 0 } = {}) {
  if (st.state === "needs" || (st.state === "blocked" && st.held)) return { words: st.words, tone: st.tone };
  if (st.done) {
    if (!st.accepted) return { words: st.words, tone: st.tone };
    const took = st.endedAt ? duration(minutesSince(team.request.at, Date.parse(st.endedAt))) : "";
    const what = merged ? `${merged} PR${merged > 1 ? "s" : ""} merged` : `${st.total} parts`;
    return { words: ["Done", what, took].filter(Boolean).join(" · "), tone: "green" };
  }
  const news = newestOf(team, st);
  if (!news) return { words: st.words, tone: st.tone };
  return { words: `${news.text} · ${ago(news.at, now)}`, tone: team.looked && news.at <= team.looked ? "" : "fresh-news" };
}

// Done (team-page-v4 §2.5): the outcome in a line.
export function doneWords(team, st, { merged = 0, decisions = 0 } = {}) {
  const took = st.endedAt ? duration(minutesSince(team.request.at, Date.parse(st.endedAt))) : "";
  const pieces = [merged ? `${merged} PR${merged > 1 ? "s" : ""} merged` : "", decisions ? `${decisions} decision${decisions > 1 ? "s" : ""} kept` : ""].filter(Boolean);
  const accepted = st.parts.filter((part) => part.accepted).length;
  const what = pieces.length ? listed(pieces) : accepted === st.total ? `all ${st.total} parts accepted` : `${accepted} of ${st.total} parts accepted`;
  return `Done${took ? ` in ${took}` : ""}: ${what}.`;
}
// Copy summary: the PRs and documents, the decisions, a line per part, as Markdown (a release
// note, a stand-up line). `made`: { name, url, status, part }; `decided`: the page's entries.
export function summaryMarkdown(team, st, { made = [], decided = [] } = {}) {
  const name = nameIn(st);
  const lines = [`**${team.goal}**`, ""];
  if (st.done) lines.push(doneWords(team, st, { merged: made.filter((item) => item.status === "merged").length, decisions: decided.length }), "");
  if (made.length) {
    lines.push("Made:");
    for (const item of made) lines.push(`- ${item.url ? `[${item.name}](${item.url})` : item.name}${item.status ? ` (${item.status})` : ""}`);
    lines.push("");
  }
  if (decided.length) {
    lines.push("Decided:");
    for (const entry of decided) lines.push(`- ${entry.text} (${entry.kind === "yours" ? "you" : `${name(entry.from)} answered ${name(entry.to)}`})`);
    lines.push("");
  }
  lines.push("Parts:");
  for (const part of st.parts) {
    const lead = part.row?.lead || "";
    lines.push(`- ${part.name}: ${lead || partWords(team, st, part)}`);
  }
  return lines.join("\n").trim() + "\n";
}
