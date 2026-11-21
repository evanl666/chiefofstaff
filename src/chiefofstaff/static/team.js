// Teams (E129, E130): workers that work together. Each worker has a short name and a color,
// the same everywhere; what they tell and ask each other is folded on Home.

import { $, api, el, foldable } from "./dom.js";
import { clock, firstLine, markdown } from "./text.js";
import { byId, closedWorkers, state } from "./state.js";
import { select, setLayout } from "./session.js";
import { renderSidebar } from "./sidebar.js";
import { closeMentions, setRecipient } from "./mention.js";

// A color per worker (and per team), from its id: the same on every page and after a reload.
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
  if (worker?.label) return worker.label;
  // A closed worker's, as its team has it.
  const named = teams().find((team) => team.labels?.[id])?.labels[id];
  return named || shortLabel(worker?.title || title) || "a worker";
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
// words. Rows from before these were marked say it in their text only.
const PEER = /^“(.+?)” (told|asked|answered) (?:“(.+?)”|the Router): ([\s\S]*)$/;
export function peerOf(message) {
  const meta = message.meta || {};
  const found = (message.text || "").match(PEER);
  return {
    from: meta.peer_from || "",
    to: meta.peer_to || "",
    kind: meta.peer_kind || found?.[2] || "told",
    question: meta.question_id || "",
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

// Teams (from the server: workers linked as working together), and the one a worker is in.
export const teams = () => state.snapshot?.teams || [];
export const teamById = (id) => teams().find((team) => team.id === id);
export const teamOf = (sessionId) => teams().find((team) => team.members.includes(sessionId));
export const TEAM = "team:";
export const isTeamView = () => String(state.selected || "").startsWith(TEAM);
const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
const minutes = (count) => (count < 60 ? `${count} min` : count < 1440 ? `${Math.floor(count / 60)} h` : `${Math.floor(count / 1440)} d`);
// "3 workers · 12 messages · 1 waiting 9 min" (or "all answered").
export function teamWords(team) {
  const messages = (team.told || 0) + (team.asked || 0) + (team.answered || 0);
  const waiting = team.waiting || [];
  const wait = waiting.length ? `${waiting.length} waiting ${minutes(Math.max(...waiting.map((item) => item.minutes)))}` : team.asked ? "all answered" : "";
  return [plural(team.members.length, "worker"), plural(messages, "message"), wait].filter(Boolean).join(" · ");
}
export function avatars(ids, most = 5) {
  const shown = ids.slice(0, most);
  return el("span", { class: "avatars" }, ...shown.map((id) => avatar(id)), ids.length > shown.length ? el("span", { class: "more", text: `+${ids.length - shown.length}` }) : null);
}

// A team's own view (#team): who talks to whom, and its thread, a question with its answer.
let shown = null;
const FILTERS = {
  all: ["All", () => true],
  waiting: ["Waiting", (item) => item.kind === "ask" && item.status === "open"],
  questions: ["Questions", (item) => item.kind === "ask"],
  told: ["Told", (item) => item.kind === "tell"],
};
export async function showTeam(teamId, { focus = "" } = {}) {
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
  shown = { id: teamId, data: null, filter: "all", focus };
  teamHeader(teamById(teamId));
  $("#timeline").replaceChildren(el("div", { class: "line note", text: "Loading its thread…" }));
  await refreshTeam();
}
// Again from the server (a new message between its workers, an answer): as it is now.
export async function refreshTeam() {
  const view = shown;
  if (!view || state.selected !== TEAM + view.id) return;
  let data;
  try {
    data = await api(`/api/teams/${encodeURIComponent(view.id)}/thread`);
  } catch (error) {
    if (state.selected === TEAM + view.id) $("#timeline").replaceChildren(el("div", { class: "line error", text: error.message }));
    return;
  }
  if (state.selected !== TEAM + view.id) return;
  view.data = data;
  teamHeader(data.team);
  renderTeam();
}
let refreshing = 0;
export function scheduleTeam() {
  if (!isTeamView() || refreshing) return;
  refreshing = setTimeout(() => {
    refreshing = 0;
    refreshTeam();
  }, 300);
}
function teamHeader(team) {
  $("#session-title").textContent = team ? `Team · ${team.name}` : "Team";
  $("#session-title").title = "";
  $("#session-meta").textContent = team ? teamWords(team) : "";
  $("#session-meta").title = "";
}
function renderTeam() {
  const { data, filter, focus } = shown;
  const { team, items } = data;
  const [, keep] = FILTERS[filter];
  const listed = items.filter(keep);
  const filters = el(
    "div",
    { class: "team-filters", role: "group", "aria-label": "Show" },
    ...Object.entries(FILTERS).map(([key, [label, test]]) =>
      el("button", { type: "button", class: "filter", "aria-pressed": String(key === filter), onclick: () => { shown.filter = key; renderTeam(); } }, label, el("span", { class: "count", text: String(items.filter(test).length) })),
    ),
  );
  const empty = { all: "Nothing told or asked between them yet.", waiting: "No question waits for an answer.", questions: "No questions between them.", told: "Nothing told yet." }[filter];
  const scroller = $("#scroller");
  const atEnd = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 160;
  $("#timeline").replaceChildren(
    el(
      "div",
      { class: "team-view" },
      el("div", { class: "team-top" }, members(team, focus), graph(team, items)),
      filters,
      listed.length ? el("div", { class: "team-thread" }, ...listed.map((item) => teamItem(item, focus))) : el("p", { class: "empty-hint", text: empty }),
    ),
  );
  if (atEnd) scroller.scrollTop = scroller.scrollHeight;
}
function members(team, focus) {
  const open = new Set(team.open || []);
  return el(
    "div",
    { class: "team-members" },
    ...team.members.map((id) =>
      el("button", { type: "button", class: `member${id === focus ? " focus" : ""}${open.has(id) ? "" : " closed"}`, title: open.has(id) ? "Open its chat" : "Closed: open its chat", onclick: () => select(id) }, avatar(id, team.labels?.[id] || ""), el("span", { text: team.labels?.[id] || labelOf(id) })),
    ),
  );
}
// Who talks to whom: a node per worker, a line per pair that exchanged anything, wider the more
// they did, amber while a question between them waits.
const SVG = "http://www.w3.org/2000/svg";
function svg(tag, attributes = {}, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  node.append(...children.filter(Boolean));
  return node;
}
function graph(team, items) {
  const ids = team.members;
  if (ids.length < 2) return null;
  const width = 240;
  const height = ids.length === 2 ? 72 : 168;
  const at = new Map(
    ids.map((id, index) => {
      if (ids.length === 2) return [id, [48 + index * 144, 36]];
      const angle = -Math.PI / 2 + (index * 2 * Math.PI) / ids.length;
      return [id, [width / 2 + 64 * Math.cos(angle), height / 2 + 60 * Math.sin(angle)]];
    }),
  );
  const pairs = new Map();
  for (const item of items) {
    if (!at.has(item.from) || !at.has(item.to)) continue;
    const key = [item.from, item.to].sort().join(" ");
    const pair = pairs.get(key) || { count: 0, open: false };
    pair.count += item.kind === "ask" && item.status === "answered" ? 2 : 1;
    pair.open ||= item.kind === "ask" && item.status === "open";
    pairs.set(key, pair);
  }
  const label = (id) => team.labels?.[id] || labelOf(id);
  const edges = [...pairs].map(([key, pair]) => {
    const [one, two] = key.split(" ");
    const [x1, y1] = at.get(one);
    const [x2, y2] = at.get(two);
    const stroke = Math.min(5, 1.5 + Math.log2(pair.count + 1));
    return svg("line", { class: `edge${pair.open ? " open" : ""}`, x1, y1, x2, y2, "stroke-width": stroke.toFixed(1) }, svg("title", {}, `${label(one)} and ${label(two)}: ${plural(pair.count, "message")}${pair.open ? ", a question waiting" : ""}`));
  });
  const nodes = ids.map((id) => {
    const [x, y] = at.get(id);
    return svg("g", { class: `node c${colorOf(id)}` }, svg("title", {}, label(id)), svg("circle", { cx: x, cy: y, r: 16 }), svg("text", { x, y, dy: "0.35em", "text-anchor": "middle" }, initials(label(id))));
  });
  return svg("svg", { class: "team-graph", viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-label": `Who talks to whom: ${pairs.size ? [...pairs].map(([key, pair]) => `${key.split(" ").map(label).join(" and ")}, ${plural(pair.count, "message")}`).join("; ") : "nothing yet"}` }, ...edges, ...nodes);
}
function pill(item) {
  if (item.kind !== "ask") return null;
  if (item.status === "open") return el("span", { class: `pill waiting${item.blocks ? " blocks" : ""}`, text: `waiting ${minutes(item.minutes)}`, title: item.blocks ? "Its task waits on the answer" : "" });
  if (item.status === "answered") return el("span", { class: "pill answered", text: `answered in ${minutes(item.minutes)}` });
  return el("span", { class: "pill ended", text: item.status === "expired" ? "no answer · expired" : "no answer · closed" });
}
function said(text) {
  const box = el("div", { class: "said" });
  foldable(box, el("div", { class: "markdown", html: markdown(text) }), text, { chars: 600, lines: 8 });
  return box;
}
function stamp(at) {
  return at ? el("time", { text: clock(at), title: new Date(at).toLocaleString() }) : null;
}
function teamItem(item, focus) {
  const verb = item.kind === "tell" ? "told" : "asked";
  const node = el(
    "div",
    { class: `team-item ${item.kind}${focus && (item.from === focus || item.to === focus) ? " mine" : ""}`, "data-id": item.id },
    el("div", { class: "head" }, avatar(item.from), el("span", { class: "who", text: `${labelOf(item.from)} ${verb} ${labelOf(item.to)}` }), pill(item), stamp(item.at)),
    said(item.text),
  );
  if (item.kind === "ask" && item.status === "answered") {
    node.append(el("div", { class: "answer" }, el("div", { class: "head" }, avatar(item.to), el("span", { class: "who", text: `${labelOf(item.to)} answered` }), stamp(item.answered_at)), said(item.answer)));
  }
  return node;
}

// Home: a line per team with a worker open, above Running: its workers, its name, what waits.
export function teamRows() {
  return teams().map((team) => ({
    key: JSON.stringify(["team", team.id, team.name, teamWords(team), team.labels]),
    build: () =>
      el(
        "button",
        { type: "button", class: `team-row${team.waiting?.length ? " waiting" : ""}`, "data-team": team.id, title: "Open what they told and asked each other", onclick: () => showTeam(team.id) },
        avatars(team.members),
        el("span", { class: "name", text: team.name }),
        el("span", { class: "words", text: teamWords(team) }),
      ),
  }));
}
// Needs you (phase 4): a question held up 30 minutes or more, its asker's task waiting on it:
// one line per team, the longest waiting.
export const BLOCKING_MINUTES = 30;
export function blockedTeams() {
  return teams()
    .map((team) => ({ team, question: (team.waiting || []).filter((item) => item.blocks && item.minutes >= BLOCKING_MINUTES).sort((a, b) => b.minutes - a.minutes)[0] }))
    .filter((found) => found.question);
}
