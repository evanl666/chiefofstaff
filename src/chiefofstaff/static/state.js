// What the page knows, and the server events that keep it current.

import { $, api, el, toast } from "./dom.js";
import { renderSidebar } from "./sidebar.js";
import { appendMessages, renderHeader, replaceMessage, updateDraftRecords, updateWorkingLine } from "./session.js";
import { renderDrafts } from "./drafts.js";
import { INBOX, scheduleInbox, uncertainDrafts, upsertTask } from "./inbox.js";
import { refreshUsageLink } from "./settings.js";
export const STATUS_ORDER = { waiting: 0, working: 1, starting: 1, queued: 2, failed: 3, idle: 4, done: 5 };
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
export const state = {
  snapshot: null,
  selected: localStorage.getItem("cos.selected") || "inbox",
  chief: null,
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
export const pendingDrafts = () => state.snapshot?.drafts || [];
export function draftsFor(sessionId) {
  return sessionId === routerId() ? pendingDrafts() : pendingDrafts().filter((d) => d.session_id === sessionId);
}
export async function loadState() {
  const data = await api("/api/state");
  state.snapshot = data;
  const known = state.selected === INBOX || current() || state.external.some((item) => item.id === state.selected);
  if (!known) {
    state.selected = INBOX;
  }
  renderSidebar();
  renderHeader();
  renderDrafts();
  scheduleInbox();
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
  const source = new EventSource("/api/events");
  source.onmessage = (event) => handleEvent(JSON.parse(event.data));
}
async function resync() {
  await loadState();
  if (state.selected === INBOX) return;
  const session = byId(state.selected);
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
      if (data.session_id === state.selected) replaceMessage(data);
      return;
    }
    if (state.loading?.id === data.session_id) state.loading.held.push(data);
    else if (data.session_id === state.selected) appendMessages([data]);
    const meta = data.meta || {};
    if (state.chief && data.session_id === routerId() && ((data.kind === "assistant" && !meta.internal) || data.kind === "update")) {
      state.chief.replies.push(data.text);
      scheduleInbox();
    }
  } else if (type === "task") {
    upsertTask(data);
  } else if (type === "focus_summary") {
    state.focusSummary = data.text;
    scheduleInbox();
  } else if (type === "session") {
    upsertSession(data);
    // Credits change as sessions work: refresh today's total at most every 5 seconds.
    state.usageTimer ||= setTimeout(() => {
      state.usageTimer = null;
      refreshUsageLink();
    }, 5000);
  } else if (type === "draft") {
    const drafts = pendingDrafts().filter((draft) => draft.id !== data.id);
    if (data.status === "pending") drafts.push(data);
    state.snapshot.drafts = drafts;
    const uncertain = uncertainDrafts().filter((draft) => draft.id !== data.id);
    if (data.status === "uncertain") uncertain.push(data);
    state.snapshot.uncertain = uncertain;
    renderDrafts();
    renderSidebar();
    updateDraftRecords();
    scheduleInbox();
  } else if (type === "toast") {
    // The Router view already shows updates and every pending draft.
    const visible = document.hasFocus() && [data.session_id, routerId(), INBOX].includes(state.selected);
    if (!visible) toast(data.text, data.needs_action ? "attention" : "", data.session_id);
  } else if (type === "state") {
    scheduleReload();
  }
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
  scheduleInbox();
  if (session.id !== state.selected) return;
  if (session.closed_at) state.viewing = session;
  renderHeader();
  updateWorkingLine();
  if (session.unread && document.hasFocus()) markRead(session.id);
}
export function markRead(sessionId) {
  api(`/api/sessions/${sessionId}/read`, { method: "POST" }).catch(() => {});
}
