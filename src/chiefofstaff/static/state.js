// What the page knows, and the server events that keep it current.

import { $, api, el, toast, withKey } from "./dom.js";
import { firstLine, shorten } from "./text.js";
import { renderSidebar } from "./sidebar.js";
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
export async function loadState() {
  const data = await api("/api/state");
  const first = !state.snapshot;
  state.snapshot = data;
  // Drafts gone without being sent (withdrawn, replaced) in the last days: their timeline lines
  // say so after a reload too.
  for (const [id, outcome] of Object.entries(data.draft_outcomes || {})) state.draftOutcomes.set(id, outcome);
  if (state.selected === routerId()) state.selected = HOME;
  const known =
    state.selected === HOME || current() || closedWorkers().some((item) => item.id === state.selected) || state.external.some((item) => item.id === state.selected);
  if (!known) {
    // The session shown is gone: Home instead (on the first load, the page opens it anyway).
    state.selected = HOME;
    if (!first) select(HOME);
  }
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
export function connectEvents() {
  const source = new EventSource(withKey("/api/events"));
  source.onmessage = (event) => handleEvent(JSON.parse(event.data));
}
async function resync() {
  await loadState();
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
    state.connectedOnce = true;
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
