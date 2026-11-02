// The sidebar: the inbox, the Router's chat, sessions, closed workers and other Copilot sessions.

import { $, api, el, toast } from "./dom.js";
import { ago, shortPath } from "./text.js";
import { closedWorkers, draftsFor, loadState, sessions, state, STATUS_ORDER, STATUS_TEXT } from "./state.js";
import { select } from "./session.js";
import { inboxItem } from "./inbox.js";
function sessionItem(session, label) {
  const drafts = draftsFor(session.id).length;
  const badge = session.unread
    ? el("span", { class: "badge", text: String(session.unread) })
    : drafts
      ? el("span", { class: "badge amber", text: String(drafts), title: "Waiting for your approval" })
      : null;
  return el(
    "button",
    {
      type: "button",
      class: `item ${session.status}${state.selected === session.id ? " selected" : ""}`,
      title: `${STATUS_TEXT[session.status] || session.status}${session.activity ? ` · ${session.activity}` : ""}`,
      "data-id": session.id,
      onclick: () => select(session.id),
    },
    el("span", { class: `dot ${session.status}` }),
    el("span", { class: "label", text: label }),
    badge,
  );
}
export function renderSidebar() {
  const list = $("#session-list");
  const all = sessions();
  const router = all.find((session) => session.role === "router");
  const slack = all.find((session) => session.role === "slack");
  const watchdog = all.find((session) => session.role === "watchdog");
  const workers = all
    .filter((session) => session.role === "worker")
    .sort((a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) || b.created_at.localeCompare(a.created_at));
  const nodes = [inboxItem()];
  if (router) nodes.push(sessionItem(router, "Chat"));
  if (slack && state.snapshot.settings.slack_enabled) nodes.push(sessionItem(slack, "Slack"));
  if (watchdog && state.snapshot.settings.watchdog_enabled) nodes.push(sessionItem(watchdog, "Watchdog"));
  if (workers.length) {
    nodes.push(el("div", { class: "group", text: `Workers · ${workers.length}` }));
    nodes.push(...workers.map((worker) => sessionItem(worker, worker.title)));
  }
  list.replaceChildren(...nodes);
  renderClosed();
  renderExternal();
  renderStatusLine();
}
function renderClosed() {
  const closed = closedWorkers();
  $("#closed").hidden = !closed.length;
  $("#closed-count").textContent = `· ${closed.length}`;
  $("#closed-list").replaceChildren(
    ...closed.map((session) =>
      el(
        "button",
        {
          type: "button",
          class: `item${state.selected === session.id ? " selected" : ""}`,
          title: session.report || session.title,
          onclick: () => select(session.id),
        },
        el("span", { class: "dot" }),
        el("span", { class: "label", text: session.title }),
        el("span", { class: "sub", text: ago(session.closed_at) }),
      ),
    ),
  );
}
export function renderStatusLine() {
  const line = $("#status-line");
  const snap = state.snapshot;
  const nodes = [];
  if (snap.runtime.state === "starting") nodes.push(el("div", { text: "Connecting to Copilot…" }));
  if (snap.runtime.state === "error") {
    nodes.push(
      el("div", { class: "bad", text: `Copilot: ${snap.runtime.error}` }),
      el("button", { type: "button", class: "link", text: "Retry", onclick: reconnect }),
    );
  }
  if (snap.demo) nodes.push(el("div", { class: "demo", text: "Demo mode: scripted sessions, nothing leaves this machine" }));
  const until = snap.focus?.until && new Date(snap.focus.until) > new Date() ? new Date(snap.focus.until) : null;
  if (until) nodes.push(el("div", { class: "demo", text: `Focusing until ${until.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}: notifications wait` }));
  if (snap.runtime.warning) nodes.push(el("div", { class: "bad", text: snap.runtime.warning }));
  if (snap.settings.slack_enabled && snap.slack.error) nodes.push(el("div", { class: "bad", text: `Slack: ${snap.slack.error}` }));
  if (snap.settings.github_enabled && snap.github.error) nodes.push(el("div", { class: "bad", text: `GitHub: ${snap.github.error}` }));
  if (snap.settings.paused) nodes.push(el("div", { text: "Watchers paused" }));
  const sandboxed = snap.settings.worker_sandbox && snap.sandbox?.supported;
  if (snap.settings.worker_yolo) nodes.push(el("div", { class: "yolo", text: sandboxed ? "Yolo: no permission prompts (sandboxed)" : "Yolo: workers run without permission checks or sandbox" }));
  line.replaceChildren(...nodes);
}
async function reconnect() {
  try {
    state.snapshot = { ...state.snapshot, ...(await api("/api/runtime/reconnect", { method: "POST" })) };
    await loadState();
  } catch (error) {
    toast(error.message, "error");
  }
}
function renderExternal() {
  const open = state.external.filter((item) => item.open).length;
  $("#external-count").textContent = state.external.length ? `· ${state.external.length}${open ? ` · ${open} open` : ""}` : "";
  $("#external-list").replaceChildren(
    ...state.external.map((item) =>
      el(
        "button",
        {
          type: "button",
          class: `item${state.selected === item.id ? " selected" : ""}`,
          title: item.open ? `Open in a terminal now · ${item.cwd}` : item.cwd,
          onclick: () => showExternal(item),
        },
        el("span", { class: `dot${item.open ? " open" : ""}` }),
        el("span", { class: "label", text: item.summary }),
        el("span", { class: "sub", text: item.open ? "open" : ago(item.modified) }),
      ),
    ),
  );
}
export async function loadExternal(refresh = false) {
  try {
    state.external = await api(`/api/external${refresh ? "?refresh=true" : ""}`);
    renderExternal();
  } catch (error) {
    toast(error.message, "error");
  }
}
function showExternal(item) {
  state.selected = item.id;
  renderSidebar();
  $("#session-title").textContent = item.summary;
  $("#session-meta").textContent = [item.open ? "Open in a terminal now" : "", shortPath(item.cwd), item.branch, `updated ${ago(item.modified)} ago`].filter(Boolean).join(" · ");
  $("#session-actions").replaceChildren();
  $("#drafts").replaceChildren();
  $("#composer").hidden = true;
  const command = `copilot --resume ${item.id}`;
  const adopt = el("button", {
    type: "button",
    text: "Let the Router manage it",
    disabled: Boolean(item.open),
    onclick: async () => {
      adopt.disabled = true;
      try {
        await api(`/api/external/${item.id}/adopt`, { method: "POST" });
        state.external = state.external.filter((other) => other.id !== item.id);
        await loadState();
        select(item.id);
      } catch (error) {
        adopt.disabled = false;
        toast(error.message, "error");
      }
    },
  });
  $("#timeline").replaceChildren(
    el(
      "div",
      { class: "external-detail" },
      el("div", { text: "This Copilot session is not managed by Chief of Staff. Resume it in a terminal, or let the Router manage it as a worker." }),
      el("code", { text: command }),
      el(
        "div",
        { class: "actions" },
        adopt,
        el("button", { type: "button", class: "secondary", text: "Copy command", onclick: () => navigator.clipboard.writeText(command).then(() => toast("Copied")) }),
      ),
      el("small", {
        text: item.open
          ? "It is open in a terminal right now. Keep using it there, or exit it (/exit) to let the Router manage it."
          : "Its conversation so far comes along. While the Router manages it, do not also open it in a terminal.",
      }),
    ),
  );
}
