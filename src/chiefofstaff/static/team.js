// Teams lite (teams-v2.md v3.1) and the team page (team-page-v4.md): the work several workers
// do for one of the owner's requests, as the owner sees it. The server says which tasks are a
// team's parts (state.snapshot.teams); where each stands is Home's own board (teamstate.js).
// Home's Teams rows (the newest change, the outcome when done, live segments) and Needs you's
// card per held question; a team's page is teampage.js; never the sidebar, which lists workers
// only. Workers' own lines and colors in chats (labelOf, avatar) are as before.

import { $, api, el, reconcile, toast } from "./dom.js";
import { firstLine } from "./text.js";
import { byId, closedWorkers, current, loadState, state } from "./state.js";
import { select } from "./session.js";
import { renderSidebar } from "./sidebar.js";
import { lineageOf } from "./relations.js";
import { needsYou, scheduleHome } from "./inbox.js";
import { deliverablesOf } from "./artifacts.js";
import { duration, latestOf, pendingFor, rowWords, teamState } from "./teamstate.js";
import { refreshTeam, showTeam } from "./teampage.js";

export { refreshTeam, showTeam };

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
export function watchLook(node, team, st) {
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
export async function postSeen(look, dismissed = false) {
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
export const stateDot = (st) => (st.state === "needs" || (st.state === "blocked" && st.held) ? "waiting" : STATE_DOT[st.state] || "");
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
// What a team's parts merged (its Done row's outcome), from Home's own task rows.
function mergedOf(team) {
  const tasks = new Map((state.snapshot?.tasks || []).map((task) => [task.id, task]));
  const seen = new Set();
  for (const member of team.members) {
    const task = tasks.get(member.task);
    if (!task) continue;
    for (const item of deliverablesOf(task)) if (item.kind === "PR" && item.status === "merged") seen.add(item.key);
  }
  return seen.size;
}
// Segments as they were last drawn, by team: one that just filled (its part finished) fills
// in, from the left; a working part's has a slow sheen (style.css).
const drawnSegments = new Map();
function teamRow({ team, st }) {
  const frac = st.done ? "" : `${st.finished}/${st.total}`;
  const { words, tone } = rowWords(team, st, { merged: st.done ? mergedOf(team) : 0 });
  return {
    key: JSON.stringify(["team", team.id, team.goal, st.state, words, tone, st.segments, frac, st.key]),
    id: `team:${team.id}`,
    build: () => {
      const label = `${team.goal}: ${words}${frac ? `, ${st.finished} of ${st.total} parts done` : ""}`;
      const before = drawnSegments.get(team.id) || [];
      drawnSegments.set(team.id, st.segments);
      const moving = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const row = el(
        "button",
        { type: "button", class: "team2-row", "data-team": team.id, "aria-label": label, onclick: () => showTeam(team.id) },
        stateMark(st),
        el("span", { class: "goal", text: team.goal }),
        el("span", { class: "frac", text: frac }),
        el("span", { class: `status${tone ? ` ${tone}` : ""}`, text: words, title: words }),
        el("span", { class: "segs", "aria-hidden": "true" }, ...st.segments.map((segment, index) => el("i", { class: `${segment}${moving && before.length && before[index] !== "done" && segment === "done" ? " filled" : ""}` }))),
      );
      row.style.setProperty("--sync", `-${Date.now() % 2400}ms`);
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

// A part's avatar: its name's first character in its color, its state's ring, a tick when done,
// a "!" when it needs the owner.
const SVG_NS = "http://www.w3.org/2000/svg";
export function partAvatar(part, { large = false, mark = part.cls } = {}) {
  const node = el("span", { class: `pa ${part.color}${large ? " lg" : ""}`, "aria-hidden": "true" }, part.avatar, el("span", { class: "ring" }));
  if (/\bis-done\b/.test(mark || "") && !/\bis-ended\b/.test(mark || "")) {
    const tick = document.createElementNS(SVG_NS, "svg");
    tick.setAttribute("viewBox", "0 0 10 10");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", "M2.2 5.3l1.9 1.9 3.8-4.2");
    tick.append(path);
    node.append(el("span", { class: "tick" }, tick));
  }
  if (/\bis-review\b/.test(mark || "")) node.append(el("span", { class: "bang" }));
  return node;
}
