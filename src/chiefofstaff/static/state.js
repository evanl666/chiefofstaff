// What the page knows, and the server events that keep it current.

import { $, api, el, withKey } from "./dom.js";
import { renderSidebar } from "./sidebar.js";
import { appendMessages, renderHeader, replaceMessage, select, updateDraftRecords, updateWorkingLine } from "./session.js";
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
// A worker by what it is on: its latest task while that is open (a worker given new work keeps
// the title it was started with), else that title.
export function workerTitle(session) {
  const task = session?.task;
  return task?.title && ["running", "waiting", "queued"].includes(task.status) ? task.title : session?.title || "";
}
export async function loadState() {
  const data = await api("/api/state");
  const first = !state.snapshot;
  state.snapshot = data;
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
  const session = byId(viewedId());
  if (!session) return;
  const messages = await api(`/api/sessions/${session.id}/messages?after=${state.lastId}`);
  appendMessages(messages);
}
function handleEvent({ type, data }) {
  if (type === "hello") {
    if (state.connectedOnce) resync().catch(() => {});
    state.connectedOnce = true;
  } else if (type === "message") {
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
  } else if (type === "state") {
    scheduleReload();
  }
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
  }
  renderSidebar();
  updateDraftRecords();
  scheduleHome();
}
export function upsertSession(session) {
  if (!state.snapshot) return;
  const list = sessions().filter((item) => item.id !== session.id);
  if (!session.closed_at) list.push(session);
  list.sort((a, b) => a.created_at.localeCompare(b.created_at));
  state.snapshot.sessions = list;
  const closed = closedWorkers().filter((item) => item.id !== session.id);
  if (session.closed_at && session.role === "worker") closed.unshift(session);
  state.snapshot.closed = closed.slice(0, 20);
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
