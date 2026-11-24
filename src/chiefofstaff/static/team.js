// Teams lite (teams-v2.md v3.1): the work several workers do for one of the owner's requests, as
// the owner sees it. The server says which tasks are a team's parts (state.snapshot.teams);
// where each stands is Home's own board (teamstate.js). Home's Teams rows, a team's page (its
// request, the one thing that needs you, its flow map, what its parts handed each other),
// the sidebar's team line and Needs you's card per held question. Workers' own lines and
// colors in chats (labelOf, avatar) are as before.

import { $, api, el, moreMenu, reconcile, toast } from "./dom.js";
import { clock, firstLine } from "./text.js";
import { byId, closedWorkers, current, HOME, loadState, state } from "./state.js";
import { select, setLayout } from "./session.js";
import { renderSidebar } from "./sidebar.js";
import { closeMentions, setRecipient } from "./mention.js";
import { lineageOf } from "./relations.js";
import { markOf } from "./agentpick.js";
import { needsYou, scheduleHome } from "./inbox.js";
import { duration, headerWords, latestOf, minutesSince, pendingFor, teamState } from "./teamstate.js";
import { flowMap, partAvatar } from "./teammap.js";

// A color per worker, from its id: the same on every page and after a reload.
const COLORS = 8;
export function colorOf(id = "") {
  let hash = 0;
  for (const char of id) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
  return hash % COLORS;
}
// A worker's short name, from its title: the part before ":", "·", "(" or " - ", cut at a word
// to 24 characters. (The owner's own name for it, when they gave one, comes from the server.)
export function shortLabel(title = "") {
  const whole = title.replace(/\s+/g, " ").trim();
  const head = whole.split(/\s*[:·|(]\s*|\s+[-\u2013\u2014]\s+/)[0].trim() || whole;
  if (head.length <= 24) return head;
  const cut = head.slice(0, 25);
  const at = cut.lastIndexOf(" ");
  return (at >= 8 ? cut.slice(0, at) : head.slice(0, 24)).replace(/[\s,;.]+$/, "");
}
const workerOf = (id) => byId(id) || closedWorkers().find((item) => item.id === id);
export function labelOf(id, title = "") {
  const worker = workerOf(id);
  return worker?.label || shortLabel(worker?.title || title) || "a worker";
}
export function initials(label) {
  const words = label.split(/[\s/_-]+/).filter((word) => /[\p{L}\p{N}]/u.test(word));
  if (!words.length) return "?";
  const first = [...words[0]];
  return (words.length > 1 ? first[0] + [...words[1]][0] : first.slice(0, 2).join("")).toUpperCase();
}
export function avatar(id, title = "") {
  const label = labelOf(id, title);
  return el("span", { class: `avatar c${colorOf(id)}`, text: initials(label), title: label });
}
// What a Router "peer" row says: who told, asked or answered whom, of which question, and the
// words; the tasks it went between, once stamped (Teams lite). Rows from before these were
// marked say it in their text only.
const PEER = /^“(.+?)” (told|asked|answered) (?:“(.+?)”|the Router): ([\s\S]*)$/;
export function peerOf(message) {
  const meta = message.meta || {};
  const found = (message.text || "").match(PEER);
  return {
    from: meta.peer_from || "",
    to: meta.peer_to || "",
    kind: meta.peer_kind || found?.[2] || "told",
    question: meta.question_id || "",
    fromTask: meta.from_task || "",
    toTask: meta.to_task || "",
    fromTitle: found?.[1] || "",
    toTitle: found?.[3] || "",
    text: found ? found[4] : message.text || "",
  };
}
export const peerLine = (peer) => `${labelOf(peer.from, peer.fromTitle)} ${peer.kind} ${labelOf(peer.to, peer.toTitle)}: ${firstLine(peer.text)}`;
// Questions workers wait on each other for, still open.
export const peerWaiting = () => state.snapshot?.peer_waiting || [];
export function ago(iso, now = Date.now()) {
  const minutes = Math.max(0, Math.round((now - new Date(iso)) / 60000));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours} h` : `${Math.floor(hours / 24)} d`;
}
// "1 waiting 9 min": how many of these questions are still open, and since when the oldest.
export function waitingWords(questions) {
  const open = peerWaiting().filter((item) => questions.has(item.id));
  return open.length ? `${open.length} waiting ${ago(open[0].asked)}` : "";
}

// Teams, as the server's candidates, and where each stands on Home now.
export const teams = () => state.snapshot?.teams || [];
export const teamById = (id) => teams().find((team) => team.id === id);
export const TEAM = "team:";
export const isTeamView = () => String(state.selected || "").startsWith(TEAM);
const ORDER = { needs: 0, blocked: 1, working: 2, watching: 3, done: 4 };
// Home's own data the teams are placed with: its board, its tasks, what its cards are for.
export function teamContext(items = needsYou(), extra = []) {
  const tasks = new Map([...extra, ...(state.snapshot?.tasks || [])].map((task) => [task.id, task]));
  return { phases: items.board.phases, tasks, needs: pendingFor(items), now: Date.now() };
}
const latest = ({ team, st }) => [team.request.at, ...st.parts.map((part) => latestOf(part.row))].filter(Boolean).sort().pop() || "";
// Every current team placed: the ones Home shows (a done one until it left), and those that left
// (Past teams).
export function placedTeams(items = needsYou()) {
  const ctx = teamContext(items);
  const all = teams().map((team) => ({ team, st: teamState(team, ctx) }));
  const shown = all.filter((item) => !item.st.left).sort((a, b) => ORDER[a.st.state] - ORDER[b.st.state] || latest(b).localeCompare(latest(a)));
  return { shown, left: all.filter((item) => item.st.left) };
}
// The team a worker's current task is a part of (not done: a done team leaves the sidebar).
export function teamOfWorker(sessionId, placed = placedTeams().shown) {
  const session = byId(sessionId);
  const task = session?.task?.task_id;
  return placed.find(({ team, st }) => !st.done && team.members.some((member) => member.session === sessionId && !member.moved_out && (!task || member.tasks.includes(task)))) || null;
}

// The owner's look at a done team: after 2 s in sight, in a visible, focused window, posted once
// per key; it leaves Teams 10 minutes later (one timer at the nearest deadline).
const looking = new Map();
let lookObserver = null;
function considerLooks() {
  const able = document.visibilityState === "visible" && document.hasFocus();
  for (const [node, look] of looking) {
    if (!node.isConnected) {
      clearTimeout(look.timer);
      looking.delete(node);
      continue;
    }
    if (able && look.inSight && !look.timer) look.timer = setTimeout(() => postSeen(look), 2000);
    if ((!able || !look.inSight) && look.timer) {
      clearTimeout(look.timer);
      look.timer = 0;
    }
  }
}
document.addEventListener("visibilitychange", considerLooks);
window.addEventListener("focus", () => {
  considerLooks();
  // Past a deadline while away: as it is now.
  scheduleHome();
});
window.addEventListener("blur", considerLooks);
function watchLook(node, team, st) {
  if (!st.done || (st.seen && st.seen.key === st.key)) return;
  lookObserver ||= new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const look = looking.get(entry.target);
      if (look) look.inSight = entry.isIntersecting;
    }
    considerLooks();
  });
  looking.set(node, { team: team.id, key: st.key, inSight: false, timer: 0 });
  lookObserver.observe(node);
}
async function postSeen(look, dismissed = false) {
  const team = teamById(look.team);
  if (!dismissed && team?.seen?.key === look.key) return;
  try {
    const mark = await api(`/api/teams/${encodeURIComponent(look.team)}/${dismissed ? "dismiss" : "seen"}`, { method: "POST", body: { key: look.key } });
    if (team) team.seen = mark;
    scheduleDeadline();
    if (dismissed) {
      scheduleHome();
      renderSidebar();
    }
  } catch (error) {
    if (dismissed) toast(error.message, "error");
  }
}
let deadline = 0;
function scheduleDeadline(shown = placedTeams().shown) {
  clearTimeout(deadline);
  const next = shown.map(({ st }) => st.leavesAt).filter((at) => at && at > Date.now()).sort((a, b) => a - b)[0];
  if (next) {
    deadline = setTimeout(() => {
      scheduleHome();
      renderSidebar();
    }, next - Date.now() + 500);
  }
}

// Home: Teams, above Running. A row per current team; Past teams · N closes it.
const STATE_DOT = { needs: "waiting", blocked: "working", working: "working", watching: "watching" };
// Its mark: amber while on the owner or a question has held a part up half an hour; else as
// its parts are (at work, watching).
const stateDot = (st) => (st.state === "needs" || (st.state === "blocked" && st.held) ? "waiting" : STATE_DOT[st.state] || "");
const SVG = "http://www.w3.org/2000/svg";
function checkMark() {
  const box = document.createElementNS(SVG, "svg");
  box.setAttribute("viewBox", "0 0 14 14");
  box.setAttribute("class", "check");
  box.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", "M3 7.4l2.6 2.6 5.4-6");
  box.append(path);
  return box;
}
function stateMark(st) {
  if (st.done) return el("span", { class: `state${st.accepted ? "" : " aside"}` }, st.accepted ? checkMark() : el("span", { class: "dot" }));
  return el("span", { class: `state dot ${stateDot(st)}` });
}
function teamRow({ team, st }) {
  const frac = st.done ? "" : `${st.finished}/${st.total}`;
  return {
    key: JSON.stringify(["team", team.id, team.goal, st.state, st.words, st.segments, frac, st.key]),
    id: `team:${team.id}`,
    build: () => {
      const words = `${team.goal}: ${st.words}${frac ? `, ${st.finished} of ${st.total} parts done` : ""}`;
      const row = el(
        "button",
        { type: "button", class: "team2-row", "data-team": team.id, "aria-label": words, onclick: () => showTeam(team.id) },
        stateMark(st),
        el("span", { class: "goal", text: team.goal }),
        el("span", { class: "frac", text: frac }),
        el("span", { class: `status${st.tone ? ` ${st.tone}` : ""}`, text: st.words }),
        el("span", { class: "segs", "aria-hidden": "true" }, ...st.segments.map((segment) => el("i", { class: segment }))),
      );
      const wrap = el("div", { class: `team2-wrap${st.done ? " done" : ""}` }, row);
      if (st.done) {
        wrap.append(el("button", { type: "button", class: "dismiss", text: "×", "aria-label": `Dismiss ${team.goal}`, title: "Dismiss: it goes to Past teams", onclick: () => postSeen({ team: team.id, key: st.key }, true) }));
        watchLook(row, team, st);
      }
      return wrap;
    },
  };
}
// Past teams: those that left (seen, then 10 min; or dismissed), and older ones a page at a time.
const past = { teams: [], next: "", loaded: false, loading: false, open: false };
async function loadPast(more = false) {
  if (past.loading) return;
  past.loading = true;
  try {
    const found = await api(`/api/teams/past?limit=20${more && past.next ? `&before=${encodeURIComponent(past.next)}` : ""}`);
    past.teams = [...(more ? past.teams : []), ...found.teams];
    past.next = found.next;
    past.loaded = true;
  } catch (error) {
    toast(error.message, "error");
  } finally {
    past.loading = false;
    scheduleHome();
  }
}
function pastWhen(at) {
  if (!at) return "Done";
  const date = new Date(at);
  return date.toDateString() === new Date().toDateString() ? "Done today" : `Done ${date.toLocaleDateString([], { month: "short", day: "numeric" })}`;
}
function pastRow(team, endedAt) {
  return el("button", { type: "button", class: "past-row", onclick: () => showTeam(team.id) }, el("span", { class: "goal", text: team.goal }), el("span", { class: "when", text: `${pastWhen(endedAt)} · ${team.members.length} parts` }));
}
export function teamsSection(items, editing) {
  const { shown, left } = placedTeams(items);
  const count = (state.snapshot?.past_teams || 0) + left.length;
  scheduleDeadline(shown);
  if (!shown.length && !count) return null;
  let node = document.getElementById("tasks-teams");
  if (!node) {
    const saved = localStorage.getItem("cos.section.tasks-teams");
    node = el(
      "details",
      { id: "tasks-teams", class: "panel-section area", open: saved !== "0" },
      el("summary", { "data-tip": "Work you asked for that several workers do together" }, el("h2", { class: "title", text: "Teams" }), el("span", { class: "count" })),
      el("div", { class: "items" }),
      el("details", { class: "past-teams" }, el("summary")),
    );
    node.addEventListener("toggle", (event) => event.target === node && localStorage.setItem("cos.section.tasks-teams", node.open ? "1" : "0"));
    const fold = node.querySelector(".past-teams");
    fold.addEventListener("toggle", (event) => {
      event.stopPropagation();
      past.open = fold.open;
      if (fold.open && !past.loaded) loadPast();
      else scheduleHome();
    });
  }
  reconcile(node.querySelector(":scope > .items"), shown.map(teamRow), editing);
  const fold = node.querySelector(".past-teams");
  fold.hidden = !count;
  const summary = fold.querySelector("summary");
  const words = `Past teams · ${count}`;
  if (summary.textContent !== words) summary.textContent = words;
  const ctx = teamContext(items);
  const older = past.teams.filter((team) => !shown.some((item) => item.team.id === team.id) && !left.some((item) => item.team.id === team.id));
  const rows = [...left.map(({ team, st }) => pastRow(team, st.endedAt)), ...older.map((team) => pastRow(team, teamState(team, ctx).endedAt))];
  const pastKey = JSON.stringify([past.open, rows.length, past.next, past.loading, left.map((item) => item.team.id), older.map((team) => team.id)]);
  if (fold.dataset.key !== pastKey) {
    fold.dataset.key = pastKey;
    for (const child of [...fold.children]) if (child !== summary) child.remove();
    if (past.open) {
      fold.append(...rows);
      if (past.next) fold.append(el("button", { type: "button", class: "link show-more", text: "Show more", onclick: () => loadPast(true) }));
      if (past.loading && !rows.length) fold.append(el("p", { class: "empty-hint", text: "Loading…" }));
    }
  }
  return node;
}

// Needs you: a question that holds its asker up half an hour, one card each, in a team or not;
// Nudge (the one asked is told) and Open team, or Open (the asker's chat at the question).
export const heldQuestions = () => state.snapshot?.held_questions || [];
function questionParts(question) {
  const team = question.team ? teamById(question.team) : null;
  const part = (id) => team?.members.find((member) => member.id === id);
  const from = part(question.from);
  const to = part(question.to);
  return { team, from, asker: from?.name || labelOf(question.asker), target: to?.name || labelOf(question.target) };
}
export function heldItem(question) {
  const nudged = question.nudged ? Math.round((Date.now() - new Date(question.nudged)) / 60000) : null;
  return {
    key: `teamwait:${question.id}:${question.minutes}:${nudged ?? ""}:${question.team}`,
    id: `teamwait:${question.id}`,
    build: () => {
      const { team, from, asker, target } = questionParts(question);
      const nudge = el("button", { type: "button", text: nudged === null ? `Nudge ${target}` : `Nudged ${nudged} min ago`, disabled: nudged !== null && nudged < 30, title: `Tell ${target} that ${asker} waits for its answer` });
      nudge.addEventListener("click", async () => {
        nudge.disabled = true;
        try {
          const { said } = await api(`/api/questions/${encodeURIComponent(question.id)}/nudge`, { method: "POST" });
          toast(said);
          await loadState();
        } catch (error) {
          toast(error.message, "error");
          nudge.disabled = false;
        }
      });
      const open = team
        ? el("button", { type: "button", class: "link", text: "Open team", onclick: () => showTeam(team.id) })
        : el("button", { type: "button", class: "link", text: "Open", title: "Its chat, at the question", onclick: () => select(question.asker, { task: question.from_task || "" }) });
      return el(
        "div",
        { class: "card team-wait", "data-id": `teamwait:${question.id}` },
        el("div", { class: "head" }, from ? partAvatar(from) : avatar(question.asker), el("span", { class: "label", text: `${asker} waits on ${target}'s answer · ${duration(question.minutes)}` })),
        question.text ? el("p", { class: "asked", text: `“${question.text}”`, title: question.text }) : null,
        el("div", { class: "actions" }, team ? el("span", { class: "in-team", text: team.goal }) : null, nudge, open),
      );
    },
  };
}

// The sidebar: a team's line (its goal, its state, how many parts done) over its workers, each
// with a rail in its part's color, while one of them is in the open list.
export function sidebarTeams(placed = placedTeams().shown) {
  return placed.filter(({ st }) => !st.done);
}
export function teamLine({ team, st }) {
  const frac = `${st.finished}/${st.total}`;
  return el("button", { type: "button", class: `team-line${state.selected === TEAM + team.id ? " selected" : ""}`, title: `${team.goal}: ${st.words}`, onclick: () => showTeam(team.id) }, el("span", { class: `dot ${stateDot(st)}` }), el("span", { class: "name", text: team.goal }), el("span", { class: "frac", text: frac }));
}

// A team's page (#team): its request, the one thing that needs you (or its calm Done row), its
// flow map, and what its parts handed each other, newest first.
let page = null;
export async function showTeam(teamId) {
  closeMentions();
  setRecipient(null);
  state.selected = TEAM + teamId;
  setLayout(false);
  renderSidebar();
  $("#back-home").hidden = false;
  $("#session-menu").hidden = true;
  $("#worker-tabs").hidden = true;
  const actions = $("#session-actions");
  delete actions.dataset.key;
  actions.replaceChildren();
  $("#composer").hidden = true;
  page = { id: teamId, data: null, filter: "all", items: [], next: "", loadingMore: false };
  $("#session-title").textContent = teamById(teamId)?.goal || "Team";
  $("#session-meta").textContent = "";
  $("#timeline").replaceChildren(el("div", { class: "line note", text: "Loading…" }));
  await refreshTeam();
}
export async function refreshTeam() {
  const view = page;
  if (!view || state.selected !== TEAM + view.id) return;
  let data;
  try {
    data = await api(`/api/teams/${encodeURIComponent(view.id)}`);
  } catch (error) {
    if (state.selected === TEAM + view.id) $("#timeline").replaceChildren(el("div", { class: "line error", text: error.message }));
    return;
  }
  if (state.selected !== TEAM + view.id) return;
  // Its id when it grew (a bridge made two groups one).
  if (data.team.id !== view.id) {
    view.id = data.team.id;
    state.selected = TEAM + view.id;
  }
  view.data = data;
  view.items = data.items;
  view.next = data.next;
  renderPage();
}
async function earlier() {
  const view = page;
  if (!view?.next || view.loadingMore) return;
  view.loadingMore = true;
  try {
    const data = await api(`/api/teams/${encodeURIComponent(view.id)}?before=${encodeURIComponent(view.next)}`);
    if (page !== view) return;
    view.items = [...view.items, ...data.items];
    view.next = data.next;
    renderPage();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    view.loadingMore = false;
  }
}
let refreshing = 0;
export function scheduleTeam() {
  if (refreshing) return;
  if (!isTeamView()) {
    if (worker.session && worker.session === current()?.id) {
      refreshing = setTimeout(() => {
        refreshing = 0;
        workerLineage(worker.session);
      }, 300);
    }
    return;
  }
  refreshing = setTimeout(() => {
    refreshing = 0;
    refreshTeam();
  }, 300);
}
const NODE_CLASS = { needs: "is-review", blocked: "is-waiting", working: "is-working", watching: "is-watching" };
function nodeClass(part) {
  if (part.finished) return part.accepted ? "is-done" : "is-done is-ended";
  if (part.state === "watching" && part.next) return "is-queued";
  return NODE_CLASS[part.state] || "";
}
// A part's one status line, in plain words (no internal word: not handed, forked, an agent).
function partStatus(part, st, team) {
  const name = (id) => st.parts.find((other) => other.id === id)?.name || "";
  const row = part.row;
  if (part.moved) return `Continued in “${part.moved_out.title}” outside this team`;
  if (part.state === "blocked") return `Waits on ${name(part.held.to)}'s answer · ${duration(part.held.minutes)}`;
  if (part.state === "needs") {
    if (["review", "update"].includes(part.marker)) return `Result ready for you · ${duration(minutesSince(row?.updated_at, Date.now()))}`;
    if (["question", "waiting", ""].includes(part.marker || "")) return "Asks you something";
    return part.phase?.why || "Needs you";
  }
  if (part.state === "working") return firstLine(row?.progress || "") || "Working";
  if (part.outside) return `Waiting on “${part.outside.join("” and “")}” (outside this team)`;
  if (part.next) {
    const order = (id) => st.parts.findIndex((other) => other.id === id);
    const before = team.edges.filter((edge) => edge.kind === "after" && edge.to === part.id && edge.state !== "met").sort((a, b) => order(a.from) - order(b.from));
    const owner = before.filter((edge) => edge.state === "owner").map((edge) => name(edge.from));
    if (owner.length) return `Starts after your OK on ${owner.join(" and ")}`;
    if (before.length) return `Starts after ${before.map((edge) => name(edge.from)).join(" and ")}`;
    return "Up next";
  }
  if (part.state === "watching") return part.phase?.why || "Watching";
  const status = row?.status || part.status;
  if (part.accepted) return row?.ended_at || part.ended_at ? `Done · you accepted it at ${clock(row?.ended_at || part.ended_at)}` : "Done";
  if (status === "cancelled") return "Cancelled";
  if (status === "failed") return "Failed";
  return "Set aside";
}
const FLOW_WORDS = {
  met: ["result ✓", "", (a, b) => `${a} handed its result to ${b}`],
  pending: ["result to come", "", (a, b) => `${b} starts once ${a}'s result comes`],
  owner: ["after your OK", "amber", (a, b) => `${b} starts after your OK on ${a}`],
  failed: ["result won't come", "red", (a, b) => `${a}'s result won't come to ${b}`],
};
// One line per pair of parts, one label saying all it carries, the piece that needs attention
// first: a question waiting, a result, notes, questions answered.
function linesOf(team, st, pairs, shown) {
  const name = (id) => st.parts.find((part) => part.id === id)?.name || "";
  const lines = new Map();
  const lineOf = (a, b) => {
    const key = [a, b].sort().join("|");
    if (!lines.has(key)) lines.set(key, { id: `l:${key}`, kind: "pair", parts: [a, b], from: a, to: b, pieces: [], says: [], reached: new Set(), flow: null, open: false });
    return lines.get(key);
  };
  for (const edge of team.edges) {
    if (!shown.has(edge.from) || !shown.has(edge.to)) continue;
    const line = lineOf(edge.from, edge.to);
    Object.assign(line, { kind: "flow", from: edge.from, to: edge.to, flow: edge.kind === "context" ? line.flow || "met" : edge.state });
    line.reached.add(edge.to);
    if (edge.kind === "context") {
      line.pieces.push({ rank: 1, text: "context", tone: "" });
      line.says.push(`${name(edge.to)} builds on ${name(edge.from)}'s work`);
      continue;
    }
    const [text, tone, say] = FLOW_WORDS[edge.state] || FLOW_WORDS.pending;
    line.pieces.push({ rank: edge.state === "met" ? 1 : 0, text, tone });
    line.says.push(say(name(edge.from), name(edge.to)));
  }
  for (const pair of pairs) {
    const [a, b] = pair.parts;
    if (!shown.has(a) || !shown.has(b)) continue;
    const line = lineOf(a, b);
    for (const sender of pair.from) line.reached.add(sender === a ? b : a);
    for (const question of pair.open) {
      line.open = true;
      line.pieces.push({ rank: 0, text: `asked · ${duration(question.minutes || 0)}`, tone: "amber" });
      line.says.push(`${name(question.from)} asked ${name(question.to)}, waiting ${duration(question.minutes || 0)}`);
    }
    const told = pair.told.reduce((sum, item) => sum + item.n, 0);
    if (told) {
      line.pieces.push({ rank: 2, text: told > 1 ? `told ${told}` : "told", tone: "" });
      line.says.push(...pair.told.filter((item) => item.n).map((item) => `${name(item.from)} told ${name(item.from === a ? b : a)} ${item.n === 1 ? "1 thing" : `${item.n} things`}`));
    }
    if (pair.answered) {
      line.pieces.push({ rank: 3, text: pair.answered > 1 ? `asked ${pair.answered} ✓` : "asked ✓", tone: "" });
      line.says.push(`${pair.answered === 1 ? "a question" : `${pair.answered} questions`} answered`);
    }
  }
  return [...lines.values()].map((line) => {
    line.pieces.sort((x, y) => x.rank - y.rank);
    line.say = line.says.join("; ");
    line.style = line.open ? "open" : line.flow === "owner" ? "owner" : line.flow === "failed" ? "failed" : line.flow === "pending" ? "pending" : line.flow ? "done" : "msg";
    return line;
  });
}
const MAP_PARTS = 8;
const URGENCY = { needs: 0, blocked: 1, working: 2, watching: 3, done: 4 };
function mapOf(team, st, pairs) {
  // At most 8 nodes: beyond, the unfinished that need attention first and "+N more".
  let parts = st.parts;
  let more = null;
  if (parts.length > MAP_PARTS) {
    const first = new Set([...parts].sort((a, b) => URGENCY[a.state] - URGENCY[b.state]).slice(0, MAP_PARTS - 1));
    parts = st.parts.filter((part) => first.has(part));
    more = el("button", { type: "button", class: "tv2-node tv2-more", text: `+${st.parts.length - parts.length} more`, title: "All its parts, in a list", onclick: () => document.querySelector(".tv2-all")?.setAttribute("open", "") });
  }
  const shown = new Set(parts.map((part) => part.id));
  const model = {
    parts: parts.map((part) => ({
      id: part.id,
      name: part.name,
      avatar: part.avatar,
      color: part.color,
      cls: nodeClass(part),
      status: partStatus(part, st, team),
      mark: markOf(workerOf(part.moved_out?.session || part.session)),
      open: () => select(part.moved_out?.session || part.session, { task: part.moved_out?.task || part.task }),
    })),
    lines: linesOf(team, st, pairs, shown),
  };
  return flowMap(model, { more });
}
// All N parts (beyond 8): a table, each part with its worker and where it stands.
function allParts(team, st) {
  const rows = st.parts.map((part) => el("tr", {}, el("th", { scope: "row", text: part.name }), el("td", { text: workerOf(part.session)?.display_title || part.title }), el("td", { text: partStatus(part, st, team) })));
  return el(
    "details",
    { class: "tv2-all" },
    el("summary", { text: `All ${st.parts.length} parts` }),
    el("table", {}, el("caption", { class: "visually-hidden", text: `The ${st.parts.length} parts of ${team.goal}` }), el("thead", {}, el("tr", {}, el("th", { scope: "col", text: "Part" }), el("th", { scope: "col", text: "Worker" }), el("th", { scope: "col", text: "Where it stands" }))), el("tbody", {}, ...rows)),
  );
}
// The one thing that needs the owner, with one button; done instead, one calm row.
function callout(team, st) {
  const name = (id) => st.parts.find((part) => part.id === id)?.name || "";
  if (st.done) {
    const look = el(
      "div",
      { class: "tv2-done", role: "status" },
      el("span", { class: "done-mark", "aria-hidden": "true" }, checkMark()),
      el("div", { class: "what" }, el("strong", { text: st.words }), el("span", { text: "Leaves Teams 10 min after you’ve seen it; it stays under Past teams." })),
      el("button", { type: "button", class: "link", text: "Dismiss", title: "Dismiss now: it goes to Past teams", onclick: () => postSeen({ team: team.id, key: st.key }, true).then(() => select(HOME)) }),
    );
    watchLook(look, team, st);
    return look;
  }
  const part = st.needing;
  let strong;
  let span = "";
  let action;
  let who = part;
  if (part) {
    const open = () => select(part.session, { task: part.task });
    if (["review", "update"].includes(part.marker)) {
      strong = `${part.name}'s result is ready for you.`;
      const waits = team.edges.filter((edge) => edge.from === part.id && edge.state === "owner").map((edge) => name(edge.to));
      span = waits.length ? `${waits.join(" and ")} ${waits.length > 1 ? "start" : "starts"} once you accept it.` : "";
      action = [`Review ${part.name}`, open];
    } else if (["question", "waiting", ""].includes(part.marker || "")) {
      strong = `${part.name} asks you something.`;
      action = [`Answer ${part.name}`, open];
    } else {
      strong = part.marker === "failed" ? `${part.name} failed.` : `${part.name} needs you.`;
      span = part.phase?.why || "";
      action = ["Open", open];
    }
  } else if (st.held) {
    who = st.held;
    const to = name(st.held.held.to);
    strong = `${st.held.name} has waited ${duration(st.held.held.minutes)} for ${to}'s answer.`;
    span = "Its work waits on it.";
    action = [
      `Nudge ${to}`,
      async (event) => {
        event.currentTarget.disabled = true;
        try {
          toast((await api(`/api/questions/${encodeURIComponent(st.held.held.id)}/nudge`, { method: "POST" })).said);
        } catch (error) {
          toast(error.message, "error");
        }
      },
    ];
  } else return null;
  return el("div", { class: "tv2-callout", role: "status" }, partAvatar({ ...who, cls: nodeClass(who) }, { large: true }), el("div", { class: "what" }, el("strong", { text: strong }), span ? el("span", { text: span }) : null), el("button", { type: "button", text: action[0], onclick: action[1] }));
}
// What they handed each other: newest first; filters only with 8 entries or more and two kinds
// or more, never a zero count.
const FILTERS = {
  all: ["All", () => true],
  waiting: ["Waiting", (item) => item.kind === "ask" && item.status === "open"],
  questions: ["Questions", (item) => item.kind === "ask"],
  notes: ["Notes", (item) => item.kind === "tell"],
};
function history(team, st, view) {
  const items = view.items;
  const talk = items.filter((item) => item.kind === "tell" || item.kind === "ask");
  const kinds = ["questions", "notes"].filter((key) => talk.some(FILTERS[key][1])).length;
  const filtered = items.length >= 8 && kinds >= 2 && !st.done;
  if (!filtered) view.filter = "all";
  const counts = Object.fromEntries(Object.entries(FILTERS).map(([key, [, test]]) => [key, items.filter(test).length]));
  const title = [el("span", { text: "What they handed each other" }), filtered ? null : el("span", { class: "count", text: talk.length ? String(talk.length) : "" })];
  const filters = filtered
    ? el(
        "div",
        { class: "filters", role: "group", "aria-label": "Show" },
        ...Object.entries(FILTERS)
          .filter(([key]) => counts[key] > 0)
          .map(([key, [word]]) => el("button", { type: "button", "aria-pressed": String(view.filter === key), onclick: () => { view.filter = key; renderPage(); } }, word, el("span", { class: "n", text: String(counts[key]) }))),
      )
    : null;
  const part = (id) => st.parts.find((one) => one.id === id) || { name: "", avatar: "?", color: "" };
  const list = el("div", { class: "list" }, ...items.filter(FILTERS[view.filter][1]).map((item) => entry(item, part, team)));
  const box = st.done ? el("details", { class: "tv2-history" }, el("summary", {}, ...title)) : el("section", { class: "tv2-history", "aria-label": "What they handed each other" }, el("h2", {}, ...title, filters));
  box.append(list);
  if (view.next) box.append(el("button", { type: "button", class: "link show-more", text: "Show earlier", onclick: earlier }));
  // "Show all" only where the text is cut short.
  const trim = () => {
    for (const text of box.querySelectorAll(".tv2-item .text")) {
      const more = text.nextElementSibling;
      if (text.clientHeight && more?.classList.contains("more") && text.scrollHeight <= text.clientHeight + 1) more.remove();
    }
  };
  requestAnimationFrame(trim);
  box.addEventListener("toggle", () => requestAnimationFrame(trim));
  return box;
}
const stamp = (at) => (at ? el("time", { text: clock(at), title: new Date(at).toLocaleString(), datetime: at }) : null);
const names = (list) => (list.length > 1 ? `${list.slice(0, -1).join(", ")} and ${list.at(-1)}` : list[0] || "");
function entry(item, part, team) {
  if (!["tell", "ask"].includes(item.kind)) {
    const who = part(item.part).name;
    const [mark, text, done] = {
      request: ["·", `Your request, in ${team.members.length} parts: ${names(team.members.map((member) => member.name))}`],
      review: ["!", `${who} finished: its result waits for your review`],
      done: ["✓", `You accepted ${who}`, true],
      cancelled: ["·", `${who} was cancelled`],
      failed: ["!", `${who} failed`],
      moved: ["→", `${who} continued in “${item.to?.title || ""}” outside this team`],
    }[item.kind] || ["·", ""];
    return el("div", { class: `tv2-event${done ? " done" : ""}` }, el("span", { class: "mark", "aria-hidden": "true", text: mark }), el("span", { class: "what", text }), stamp(item.at));
  }
  const from = part(item.from);
  const to = part(item.to);
  const open = item.kind === "ask" && item.status === "open";
  let pill = null;
  if (item.kind === "ask") {
    if (open) pill = el("span", { class: "pill waiting", text: `waiting ${duration(item.minutes || 0)}${item.blocks ? ` · holds ${from.name}` : ""}` });
    else if (item.status === "answered") pill = el("span", { class: "pill answered", text: `answered in ${duration(item.minutes || 0)}` });
    else pill = el("span", { class: "pill ended", text: item.status === "expired" ? "no answer · expired" : "no answer · closed" });
  }
  const node = el(
    "article",
    { class: `tv2-item${open ? " waiting" : ""}`, "data-id": item.id },
    el("div", { class: "top" }, partAvatar(from), el("span", { class: "who" }, from.name, el("span", { class: "arrow", text: "→", "aria-label": "to" }), to.name), el("span", { class: "kind", text: item.kind === "ask" ? "· question" : "· note" }), pill, stamp(item.at)),
    el("div", { class: "about", text: item.title }),
  );
  if (item.rest) {
    const text = el("div", { class: "text", text: item.rest });
    const more = el("button", { type: "button", class: "more", text: "Show all" });
    more.addEventListener("click", () => {
      text.classList.toggle("whole");
      more.textContent = text.classList.contains("whole") ? "Show less" : "Show all";
    });
    node.append(text, more);
  }
  if (item.kind === "ask" && item.status === "answered") {
    node.append(el("div", { class: "answer" }, partAvatar(to), el("div", { class: "a-top" }, el("strong", { text: to.name }), ` answered · ${clock(item.answered_at)}`), el("div", { class: "a-text", text: item.answer })));
  }
  return node;
}
function renderPage() {
  const view = page;
  const { data } = view;
  const team = data.team;
  const st = teamState(team, teamContext(needsYou(), data.tasks));
  const title = $("#session-title");
  title.textContent = team.goal;
  title.title = "";
  const meta = $("#session-meta");
  meta.replaceChildren(...(st.done ? [] : [el("span", { class: `dot ${stateDot(st)}`, "aria-hidden": "true" })]), headerWords(team, st, clock));
  meta.title = "";
  const actions = $("#session-actions");
  const menuKey = `team:${team.id}:${st.done}`;
  if (actions.dataset.key !== menuKey) {
    actions.dataset.key = menuKey;
    const copy = { text: "Copy link", onclick: () => navigator.clipboard?.writeText(`${location.origin}${location.pathname}#${TEAM}${team.id}`).then(() => toast("Link copied.")) };
    const dismiss = st.done ? { text: "Dismiss", onclick: () => postSeen({ team: team.id, key: st.key }, true).then(() => select(HOME)) } : null;
    actions.replaceChildren(moreMenu([dismiss, copy], "Dismiss, copy link", "⋯", "Team actions"));
  }
  const scroller = $("#scroller");
  const top = scroller.scrollTop;
  const root = el(
    "div",
    { class: "team-page" },
    el("p", { class: "tv2-why" }, el("span", { class: "why-label", text: `For your request at ${clock(team.request.at)}:` }), el("button", { type: "button", class: "quote", text: `“${team.request.text}”`, title: "Show it in your chat", onclick: () => select(HOME) })),
    callout(team, st),
    mapOf(team, st, data.pairs || []),
    st.parts.length > MAP_PARTS ? allParts(team, st) : null,
    history(team, st, view),
  );
  $("#timeline").replaceChildren(root);
  scroller.scrollTop = top;
}

// A worker's Team tab: its current task's lineage at the top (as 0.1.56 computes it: the
// relations of the Router's team of its worker, its view's `team`).
const SETTLE_MS = 90_000;
function takeRelations(view, data) {
  Object.assign(view, { tasks: data.tasks || [], relations: data.relations || [], groups: data.groups || [], anchors: data.anchors || [], summary: data.summary || {}, earlier: data.earlier || 0, byTask: null });
  const cleared = new Set();
  for (const edge of view.relations) {
    if (edge.kind !== "after") continue;
    if (view.seen.get(edge.id) === "pending" && edge.state === "resolved") cleared.add(edge.to_task);
    view.seen.set(edge.id, edge.state);
  }
  view.cleared = cleared;
  const just = view.tasks.filter((row) => row.relation?.just).map((row) => SETTLE_MS - (Date.now() - new Date(row.relation.resolved_at)));
  clearTimeout(view.settle);
  if (just.length) view.settle = setTimeout(() => scheduleTeam(), Math.max(1000, Math.min(...just) + 500));
}
const worker = { session: "", view: null };
export async function workerLineage(sessionId) {
  const timeline = $("#timeline");
  const session = current();
  const team = session?.id === sessionId ? session.team : "";
  const taskId = session?.id === sessionId ? session.task?.task_id : "";
  worker.session = team && taskId ? sessionId : "";
  if (!worker.session) {
    timeline.querySelector(":scope > .team-lineage")?.remove();
    return;
  }
  let data;
  try {
    data = await api(`/api/teams/${encodeURIComponent(team)}/thread`);
  } catch {
    return;
  }
  if (current()?.id !== sessionId) return;
  const view = worker.view?.session === sessionId ? worker.view : { session: sessionId, open: taskId, peek: "", lit: "", litTasks: [], seen: new Map(), cleared: new Set() };
  worker.view = view;
  takeRelations(view, data);
  const row = view.tasks.find((item) => item.id === taskId);
  const old = timeline.querySelector(":scope > .team-lineage");
  if (!row || (!row.lineage && !row.relation)) {
    old?.remove();
    return;
  }
  const draw = () => {
    const node = el("section", { class: "team-lineage", "aria-label": "Its task's lineage" }, lineageOf(view, row, draw));
    view.root = node;
    const there = timeline.querySelector(":scope > .team-lineage");
    if (there) there.replaceWith(node);
    else timeline.prepend(node);
  };
  draw();
}
// The team a run of team messages on Home is about: all its rows between parts of one team.
export function teamOfPeers(peers) {
  const found = new Set();
  // (An answer belongs to its question: its row names no task.)
  for (const peer of peers.filter((one) => one.kind !== "answered" || one.fromTask)) {
    const team = teams().find((one) => one.members.some((member) => member.tasks.includes(peer.fromTask)) && one.members.some((member) => member.tasks.includes(peer.toTask)));
    found.add(team?.id || "");
  }
  return found.size === 1 && !found.has("") ? teamById([...found][0]) : null;
}
