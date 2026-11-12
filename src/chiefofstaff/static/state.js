// What the page knows, and the server events that keep it current.

import { $, api, el, toast, withKey } from "./dom.js";
import { firstLine, shorten } from "./text.js";
import { renderOthers, renderSidebar } from "./sidebar.js";
import { appendMessages, PAGE, refreshChips, renderHeader, replaceMessage, select, updateDraftRecords, updateWorkingLine } from "./session.js";
import { scheduleHome } from "./inbox.js";
import { upsertTask } from "./tasks.js";
import { refreshUsageLink, renderConnections, renderLearning } from "./settings.js";
// Home: the Router's conversation, with what needs the owner and every task beside it.
export const HOME = "home";
// Workers in the sidebar: running first.
export const STATUS_ORDER = { working: 0, starting: 0, waiting: 1, queued: 2, failed: 3, idle: 4, done: 5 };
export const STATUS_TEXT = {
  working: "Working",
  starting: "Starting",
  queued: "Queued",
  idle: "Idle",
  waiting: "Waiting for an answer",
  done: "Done",
  failed: "Failed",
};
// What each sidebar dot means, shown when hovering it.
export const DOT_HELP = {
  working: "Working",
  starting: "Starting",
  queued: "Queued: waits for a free worker slot, or for another task",
  idle: "Idle: between tasks",
  waiting: "Waiting for your answer",
  done: "Done: finished, nothing running",
  failed: "Failed",
  open: "Open in a terminal right now (shown read-only)",
};
// The inbox and the Router's chat of earlier versions are both Home now.
function savedView() {
  const saved = localStorage.getItem("cos.selected");
  return !saved || saved === "inbox" ? HOME : saved;
}
export const state = {
  snapshot: null,
  selected: savedView(),
  homeTab: "chat",  // On a narrow window, which part of Home shows.
  // Cards the owner just acted on: gone at once, back with the error if it failed.
  leaving: new Set(),
  external: [],
  // With the setting on (others.py): the owner's other Copilot sessions the index follows, by
  // id, each with where it stands; why none can say it, when none can; one found by its id
  // alone (a selection kept across a reload), for its view.
  others: new Map(),
  othersWhy: "",
  otherShown: null,
  lastId: 0,
  loading: null,
  connectedOnce: false,
  reloadTimer: null,
  models: [],
  viewing: null,
  usageTimer: null,
  // Drafts gone without being sent (withdrawn by their worker, replaced by a newer one): what
  // their lines in a timeline say.
  draftOutcomes: new Map(),
};
export const sessions = () => state.snapshot?.sessions || [];
export const closedWorkers = () => state.snapshot?.closed || [];
export const byId = (id) => sessions().find((session) => session.id === id);
export const current = () => byId(state.selected) || (state.viewing?.id === state.selected ? state.viewing : null);
export const routerId = () => state.snapshot?.router_id;
// The session whose timeline is shown: the Router's on Home.
export const viewedId = () => (state.selected === HOME ? routerId() : state.selected);
export const pendingDrafts = () => state.snapshot?.drafts || [];
export const uncertainDrafts = () => state.snapshot?.uncertain || [];
export const tasks = () => state.snapshot?.tasks || [];
export const ownerQuestions = () => state.snapshot?.questions || [];
// The Router's notes that need the owner (notify_owner with needs_action or urgent), until seen.
export const ownerUpdates = () => state.snapshot?.updates || [];
export const focusing = () => new Date(state.snapshot?.focus?.until || 0) > new Date();
// A worker by what it is on (the server's one rule: its current task while open, else its
// newest open task, else its newest task); the first thing it was asked only when it has none.
export function workerTitle(session) {
  return session?.display_title || session?.title || "";
}
// Where a message sent to a session stands (`delivery`: the owner's, the Router's, a notice),
// in words for the chat and for the row the owner sent it from: queued, sent, its conversation
// shows it (received), the turn it was in ended (handled), sent again for want of a sign it got
// there, or given up on.
export const DELIVERY_WORDS = {
  queued: ["Waiting to be sent", "wait"],
  sent: ["Sent", ""],
  received: ["Received", "good"],
  processed: ["Handled", "good"],
  resending: ["Not received yet — sending again", "warn"],
  undelivered: ["Not delivered", "bad"],
};
// The latest few such messages of each session, as their events said (any session's, not only
// the one shown: a row on Home says how what was sent from it is doing).
const deliveries = new Map();
export function noteDelivery(message) {
  if (!message?.delivery || !message.session_id) return false;
  const known = deliveries.get(message.session_id) || [];
  const before = known.find((item) => item.id === message.id);
  const list = known.filter((item) => item.id !== message.id);
  // When it came to where it stands (a row shows "Handled" for a while, then nothing).
  const changed = before?.delivery === message.delivery ? before.changed : Date.now();
  list.push({ id: message.id, kind: message.kind, delivery: message.delivery, at: Date.parse(message.created_at) || Date.now(), changed });
  deliveries.set(message.session_id, list.sort((a, b) => a.id - b.id).slice(-8));
  return true;
}
// The latest of the owner's messages to a session since a moment (ms): not the Router's, which
// it may get meanwhile.
export const latestDelivery = (sessionId, since = 0) => (deliveries.get(sessionId) || []).filter((item) => item.at >= since && item.kind === "owner").pop() || null;
// `again`: after the event stream was away (resync), what the index says is read again too.
export async function loadState(again = false) {
  const data = await api("/api/state");
  const first = !state.snapshot;
  state.snapshot = data;
  // Drafts gone without being sent (withdrawn, replaced) in the last days: their timeline lines
  // say so after a reload too.
  for (const [id, outcome] of Object.entries(data.draft_outcomes || {})) state.draftOutcomes.set(id, outcome);
  if (state.selected === routerId()) state.selected = HOME;
  // Where the owner's other sessions stand, before the selection is checked: one of them may be
  // the one shown.
  if (othersOn() && (first || again)) await syncOthers();
  let known =
    state.selected === HOME || current() || closedWorkers().some((item) => item.id === state.selected) || state.external.some((item) => item.id === state.selected);
  // One of them in neither list (closed for long, the group never unfolded): asked for by its
  // id before it is given up for Home.
  if (!known && othersOn()) known = otherItem(state.selected) || (await findOther(state.selected));
  if (!known) {
    // The session shown is gone: Home instead (on the first load, the page opens it anyway).
    state.selected = HOME;
    if (!first) select(HOME);
  }
  followOthers();
  renderSidebar();
  renderHeader();
  scheduleHome();
  updateDraftRecords();
  refreshChips();
  if ($("#settings-dialog").open) {
    renderLearning();
    renderConnections();
  }
}
export async function loadModels() {
  try {
    state.models = await api("/api/models");
  } catch {
    return;
  }
  $("#model-options").replaceChildren(...state.models.map((model) => el("option", { value: model.id, text: model.name })));
  renderHeader();
}
function scheduleReload() {
  clearTimeout(state.reloadTimer);
  state.reloadTimer = setTimeout(() => loadState().catch(() => {}), 150);
}
// The server's events. Kept: with the setting on, a stream that breaks drops every status.
let events = null;
export function connectEvents() {
  const source = new EventSource(withKey("/api/events"));
  source.onmessage = (event) => handleEvent(JSON.parse(event.data));
  events = source;
  followOthers();
}
async function resync() {
  await loadState(true);
  const sessionId = viewedId();
  if (!byId(sessionId)) return;
  // What came while the page was away, page by page from the last message shown: a busy chat
  // can have more than a page of it. Far behind, the chat loads again from its latest.
  for (let pages = 0; pages < 25; pages += 1) {
    const page = await api(`/api/sessions/${sessionId}/messages?after=${state.lastId}&limit=${PAGE}`);
    if (viewedId() !== sessionId || state.loading) return;
    appendMessages(page);
    if (page.length < PAGE) return;
  }
  await select(state.selected);
}
function handleEvent({ type, data }) {
  if (type === "hello") {
    if (state.connectedOnce) resync().catch(() => {});
    // What changed between the first read and the stream starting is read again.
    else if (othersOn()) syncOthers();
    state.connectedOnce = true;
  } else if (type === "others") {
    heardOthers(data);
  } else if (type === "message") {
    noteUpdate(data);
    // Where a message sent to a worker stands: the row it was sent from says it too.
    if (noteDelivery(data) && data.session_id !== viewedId()) scheduleHome();
    if (data.amended) {
      if (data.session_id === viewedId()) replaceMessage(data);
      return;
    }
    if (state.loading?.id === data.session_id) state.loading.held.push(data);
    else if (data.session_id === viewedId()) appendMessages([data]);
  } else if (type === "task") {
    upsertTask(data);
  } else if (type === "focus_summary") {
    state.focusSummary = data.text;
    scheduleHome();
  } else if (type === "session") {
    upsertSession(data);
    // Credits change as sessions work: refresh today's total at most every 5 seconds.
    state.usageTimer ||= setTimeout(() => {
      state.usageTimer = null;
      refreshUsageLink();
    }, 5000);
  } else if (type === "draft") {
    applyDraft(data);
  } else if (type === "toast") {
    notify(data);
  } else if (type === "state") {
    scheduleReload();
  }
}
// A notification while the page is open: none while the owner focuses (unless urgent), nor where
// the owner is looking and sees it already: the session it is from (on Home, the Router's: its
// chat and Needs you), or Home's chat line for news that needs nothing of them.
function notify({ text = "", session_id: sessionId, needs_action: needsAction, urgent }) {
  const looking = document.hasFocus() && (sessionId === viewedId() || (state.selected === HOME && !needsAction));
  if (!urgent && (focusing() || looking)) return;
  toast(shorten(firstLine(text) || text, 200), needsAction || urgent ? "attention" : "", sessionId);
}
// A note of the Router's that needs the owner waits under Needs you; what the owner writes to
// the Router after it answers it (as the server counts it; not what they told one worker).
function noteUpdate(message) {
  if (!state.snapshot || message.amended || message.session_id !== routerId()) return;
  const meta = message.meta || {};
  if (message.kind === "owner") {
    if (meta.to || !ownerUpdates().length) return;
    state.snapshot.updates = ownerUpdates().filter((update) => update.id > message.id);
  } else if (message.kind === "update" && (meta.needs_action || meta.urgent)) {
    const update = { id: message.id, text: message.text, urgent: Boolean(meta.urgent), at: message.created_at };
    state.snapshot.updates = [...ownerUpdates().filter((item) => item.id !== message.id), update];
  } else {
    return;
  }
  renderSidebar();
  scheduleHome();
}
// A draft as it is now (from an event, or the answer to the owner's decision on it).
export function applyDraft(data) {
  if (!state.snapshot) return;
  const drafts = pendingDrafts().filter((draft) => draft.id !== data.id);
  if (data.status === "pending") drafts.push(data);
  state.snapshot.drafts = drafts;
  const uncertain = uncertainDrafts().filter((draft) => draft.id !== data.id);
  if (data.status === "uncertain") uncertain.push(data);
  state.snapshot.uncertain = uncertain;
  if (data.status !== "pending" && data.status !== "uncertain") {
    state.leaving.delete(`draft:${data.id}`);
    // Its review view, if one was opened, goes with it.
    document.querySelector(`.review-dialog[data-draft="${data.id}"]`)?.remove();
    if (/^(Withdrawn|Replaced by)/.test(data.outcome || "")) state.draftOutcomes.set(data.id, data.outcome);
  }
  renderSidebar();
  updateDraftRecords();
  scheduleHome();
}
export function upsertSession(session) {
  if (!state.snapshot) return;
  const was = sessions().some((item) => item.id === session.id) ? "open" : closedWorkers().some((item) => item.id === session.id) ? "closed" : "";
  const list = sessions().filter((item) => item.id !== session.id);
  if (!session.closed_at) list.push(session);
  list.sort((a, b) => a.created_at.localeCompare(b.created_at));
  state.snapshot.sessions = list;
  const closed = closedWorkers().filter((item) => item.id !== session.id);
  if (session.closed_at && session.role === "worker") closed.unshift(session);
  state.snapshot.closed = closed.slice(0, 20);
  // The sidebar's count of closed workers, until the next snapshot has it.
  const now = session.closed_at ? "closed" : "open";
  if (typeof state.snapshot.closed_count === "number" && session.role === "worker" && !session.helper && was && was !== now) {
    state.snapshot.closed_count = Math.max(0, state.snapshot.closed_count + (now === "closed" ? 1 : -1));
  }
  renderSidebar();
  scheduleHome();
  // Settings shows whether Chief of Staff's learning worker is at it.
  if (session.helper && $("#settings-dialog").open) renderLearning();
  if (session.id !== viewedId()) return;
  if (session.closed_at) state.viewing = session;
  renderHeader();
  updateWorkingLine();
  if (session.unread && document.hasFocus()) markRead(session.id);
}
export function markRead(sessionId) {
  api(`/api/sessions/${sessionId}/read`, { method: "POST" }).catch(() => {});
}

// The owner's other Copilot sessions: where each stands, as the index (others.py) says while the
// setting is on. Without it, none of what follows runs: the sidebar lists them as before.
export const othersOn = () => Boolean(state.snapshot?.settings?.other_sessions);
// One of them by its id: the index's (the latest), the sidebar's list, or the one found by its
// id; never one continued here since (a worker now).
export function otherItem(id) {
  if (!othersOn() || !id || byId(id) || closedWorkers().some((item) => item.id === id)) return undefined;
  return state.others.get(id) || state.external.find((item) => item.id === id) || (state.otherShown?.id === id ? state.otherShown : undefined);
}
async function findOther(id) {
  try {
    state.otherShown = await api(`/api/others/${encodeURIComponent(id)}`);
  } catch {
    return null;
  }
  return state.otherShown;
}
// A status is shown only while its lease holds: every pass of the index renews it for what is
// left of its 15 seconds, and it runs out on whichever of the page's two clocks passes its
// deadline first, so neither a clock stopped in sleep nor one moved by hand keeps it. One timer,
// to the earliest deadline, draws what ran out as it looks without the setting.
const leases = new Map();  // Session id -> { mono, wall }: its deadlines (ms).
let leaseTimer = null;
const holds = (lease) => performance.now() < lease.mono && Date.now() < lease.wall;
// The one way to where an other session stands: nothing once its lease is over (or with the
// setting off). Its time counts on from when the page heard it.
export function otherStatus(id) {
  const row = othersOn() ? state.others.get(id) : null;
  const lease = leases.get(id);
  if (!row?.status || !lease || !holds(lease)) return null;
  const { elapsed } = row.status;
  return elapsed === null || elapsed === undefined ? row.status : { ...row.status, elapsed: elapsed + (performance.now() - row.heard) / 1000 };
}
function lease(id, seconds) {
  if (seconds > 0) leases.set(id, { mono: performance.now() + seconds * 1000, wall: Date.now() + seconds * 1000 });
  else leases.delete(id);
}
function armLeases() {
  clearTimeout(leaseTimer);
  leaseTimer = null;
  if (!leases.size) return;
  const left = Math.min(...[...leases.values()].map((item) => Math.min(item.mono - performance.now(), item.wall - Date.now())));
  leaseTimer = setTimeout(() => {
    expireLeases();
    armLeases();
  }, Math.max(0, left) + 10);
}
// Whether any ran out (and went).
function expireLeases() {
  const over = [...leases].filter(([, item]) => !holds(item));
  for (const [id] of over) leases.delete(id);
  if (over.length) renderOthers();
  return over.length > 0;
}
// The latest pass heard of (`run`: the server's; with another run, passes count again).
const heard = { run: "", pass: -1 };
// Rows as they are now: a status changed holds only by a lease given with it.
function take(rows) {
  const now = performance.now();
  for (const row of rows || []) {
    state.others.set(row.id, { ...row, heard: now });
    leases.delete(row.id);
  }
}
// Since the stream broke (or the page was away) and until /api/others answers, events wait: those
// older than its answer were sent before it, and go.
let waiting = null;
let syncing = null;
let again = false;
let retry = null;
export function syncOthers() {
  if (syncing) {
    again = true;
    return syncing;
  }
  waiting ??= [];
  syncing = readOthers().finally(() => {
    syncing = null;
    if (again && othersOn()) {
      again = false;
      syncOthers();
    }
  });
  return syncing;
}
async function readOthers() {
  let view;
  try {
    view = await api("/api/others");
  } catch {
    // Read again later (the stream's own reconnection reads it too).
    clearTimeout(retry);
    retry = setTimeout(() => othersOn() && waiting && syncOthers(), 5000);
    return;
  }
  if (!othersOn()) return;
  state.others = new Map();
  leases.clear();
  take(view.rows);
  for (const row of view.rows || []) if (row.status) lease(row.id, row.valid_for);
  state.othersWhy = view.why || "";
  Object.assign(heard, { run: view.run, pass: view.pass });
  const held = waiting || [];
  waiting = null;
  for (const data of held) applyOthers(data);
  armLeases();
  renderOthers();
}
function heardOthers(data) {
  if (!othersOn()) return;
  // Woken from sleep, or the clock moved: this may be a renewal sent before.
  if (jumped()) loseOthers();
  if (waiting) {
    waiting.push(data);
    if (waiting.length > 200) waiting.shift();
    return;
  }
  applyOthers(data);
  armLeases();
  renderOthers();
}
function applyOthers({ run, pass, rows, gone, leases: renewed }) {
  if (run === heard.run && pass < heard.pass) return;  // Sent before what the page read since.
  if (run !== heard.run) {
    // Another server: what the last one said goes (the stream's reconnection reads it all).
    state.others = new Map();
    leases.clear();
  }
  Object.assign(heard, { run, pass });
  take(rows);
  for (const id of gone || []) {
    state.others.delete(id);
    leases.delete(id);
  }
  for (const [id, seconds] of Object.entries(renewed || {})) if (state.others.get(id)?.status) lease(id, seconds);
}
// The stream reported an error, the page was shown again or came back from the back-forward
// cache, the browser came back online, or the wall clock jumped: what the page was told may be
// out of date and renewals may come late. Every status goes at once, until /api/others answers.
function loseOthers() {
  if (!othersOn()) return;
  leases.clear();
  armLeases();
  skew = Date.now() - performance.now();
  renderOthers();
  syncOthers();
}
// The wall clock against the monotonic one, checked at each event and every second while the
// page is shown: more than 2 s apart since the last look, the computer slept or the clock moved.
let skew = null;
function jumped() {
  const now = Date.now() - performance.now();
  const moved = skew !== null && Math.abs(now - skew) > 2000;
  skew = now;
  return moved;
}
// The handlers that drop the leases, only while the setting is on (and the stream exists);
// gone, with what the index said, once it is off.
let watching = null;
export function followOthers() {
  const on = othersOn() && Boolean(events);
  if (on === Boolean(watching)) return;
  if (!on) {
    watching.abort();
    clearInterval(watching.timer);
    clearTimeout(retry);
    watching = null;
    const shown = state.others.get(state.selected) || state.otherShown;
    state.others = new Map();
    state.otherShown = null;
    leases.clear();
    armLeases();
    waiting = null;
    // The view of one of them, as it looks without the setting.
    if (shown) renderOthers(shown);
    return;
  }
  watching = new AbortController();
  const { signal } = watching;
  events.addEventListener("error", loseOthers, { signal });
  document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && loseOthers(), { signal });
  window.addEventListener("pageshow", (event) => event.persisted && loseOthers(), { signal });
  window.addEventListener("online", loseOthers, { signal });
  let ticks = 0;
  skew = null;
  jumped();
  watching.timer = setInterval(() => {
    if (document.visibilityState !== "visible") return;
    if (jumped()) loseOthers();
    // The timer follows the monotonic clock: a wall clock that crept ahead is checked here.
    else if (expireLeases()) armLeases();
    // How long each has waited, in its row and the view's meta line.
    else if (++ticks % 10 === 0 && leases.size) renderOthers();
  }, 1000);
  if (!state.connectedOnce) return;
  // Turned on in this page: read now.
  syncOthers();
}
