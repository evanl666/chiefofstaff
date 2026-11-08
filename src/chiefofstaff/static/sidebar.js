// The sidebar: Home, sessions, closed workers and other Copilot sessions.

import { $, api, el, toast } from "./dom.js";
import { ago, shortPath, since } from "./text.js";
import { closedWorkers, DOT_HELP, HOME, loadState, sessions, state, workerTitle } from "./state.js";
import { IN_TERMINAL, readOnlyTimeline, scrollToBottom, select, setComposer, setLayout } from "./session.js";
import { homeItem, needsCount, WORKER_STATES, workerStates } from "./inbox.js";
import { closeMentions, setRecipient } from "./mention.js";
import { openSettings } from "./settings.js";
// One count in the whole sidebar, on Home: what needs the owner. Workers show only their dot,
// which says where each stands as Home does (Needs you, Running, Monitoring).
function sessionItem(session, label, where) {
  const dot = where?.dot || "idle";
  const named = label !== session.title ? `\nStarted as “${session.title}”` : "";
  return el(
    "button",
    {
      type: "button",
      class: `item ${dot.split(" ")[0]}${state.selected === session.id ? " selected" : ""}`,
      title: [where?.text, session.activity].filter(Boolean).join(" · ") + named,
      "data-id": session.id,
      onclick: () => select(session.id),
    },
    el("span", { class: `dot ${dot}`, title: where?.text || "" }),
    el("span", { class: "label", text: label }),
    // A marker in words (queued, overdue…), not a color of its own.
    where?.sub ? el("span", { class: "sub", text: where.sub }) : null,
  );
}
const ORDER = Object.fromEntries([...WORKER_STATES.map(([dot]) => dot), "open"].map((dot, index) => [dot, index]));
// What the dots mean, under "Workers" when asked.
function legend() {
  return el("div", { class: "legend" }, ...WORKER_STATES.map(([dot, text]) => el("div", { class: "legend-row" }, el("span", { class: `dot ${dot}` }), el("span", { text }))), el("div", { class: "legend-row" }, el("span", { class: "dot open" }), el("span", { text: DOT_HELP.open })));
}
export function renderSidebar() {
  const list = $("#session-list");
  const all = sessions();
  const where = workerStates();
  const rank = (session) => ORDER[(where.get(session.id)?.dot || "idle").split(" ")[0]] ?? 9;
  // Chief of Staff's own helpers (learning, checks, the profile) are not the owner's workers.
  const workers = all.filter((session) => session.role === "worker" && !session.helper).sort((a, b) => rank(a) - rank(b) || b.created_at.localeCompare(a.created_at));
  const nodes = [homeItem()];
  if (workers.length) {
    const toggle = el("button", { type: "button", class: "link legend-toggle", text: "?", title: "What the dots mean", "aria-expanded": String(Boolean(state.legend)), onclick: () => {
      state.legend = !state.legend;
      renderSidebar();
    } });
    nodes.push(el("div", { class: "group" }, el("span", { text: "Workers" }), toggle));
    if (state.legend) nodes.push(legend());
    // Workers with nothing open fold away: the ones that need you, work or wait come first.
    const idle = (worker) => (where.get(worker.id)?.dot || "idle") === "idle" && state.selected !== worker.id;
    const busy = workers.filter((worker) => !idle(worker));
    const resting = workers.filter(idle);
    nodes.push(...busy.map((worker) => sessionItem(worker, workerTitle(worker), where.get(worker.id))));
    if (resting.length) {
      const fold = el("details", { class: "idle-workers", open: Boolean(state.idleOpen) }, el("summary", { text: `Nothing open · ${resting.length}`, title: "Workers whose tasks are all finished" }), ...resting.map((worker) => sessionItem(worker, workerTitle(worker), where.get(worker.id))));
      fold.addEventListener("toggle", () => {
        state.idleOpen = fold.open;
      });
      nodes.push(fold);
    }
  }
  list.replaceChildren(...nodes);
  const count = needsCount();
  document.title = count ? `(${count}) Chief of Staff` : "Chief of Staff";
  renderClosed();
  renderExternal();
  renderStatusLine();
}
function renderClosed() {
  const closed = closedWorkers().filter((session) => !session.helper);
  $("#closed").hidden = !closed.length;
  $("#closed-count").textContent = `· ${closed.length}`;
  $("#closed-list").replaceChildren(
    ...closed.map((session) =>
      el(
        "button",
        {
          type: "button",
          class: `item${state.selected === session.id ? " selected" : ""}`,
          title: [workerTitle(session) !== session.title ? `Started as “${session.title}”` : "", session.report].filter(Boolean).join("\n\n") || session.title,
          onclick: () => select(session.id),
        },
        el("span", { class: "dot" }),
        el("span", { class: "label", text: workerTitle(session) }),
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
  // On Home the setup card says it, with what to do.
  if (snap.runtime.state === "error" && state.selected !== HOME) {
    nodes.push(
      el("div", { class: "bad", text: `Copilot: ${snap.runtime.error}` }),
      el("button", { type: "button", class: "link", text: "Retry", onclick: reconnect }),
    );
  }
  if (snap.demo) nodes.push(el("div", { class: "demo", text: "Demo mode: scripted sessions, nothing leaves this machine" }));
  const until = snap.focus?.until && new Date(snap.focus.until) > new Date() ? new Date(snap.focus.until) : null;
  if (until) nodes.push(el("div", { class: "demo", text: `Focusing until ${until.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}: notifications wait` }));
  if (snap.runtime.warning) nodes.push(el("div", { class: "bad", text: snap.runtime.warning }));
  if (snap.settings.slack_enabled && snap.slack.error) {
    // What to do about it, here: fix it in Settings, or stop watching Slack.
    nodes.push(
      el(
        "div",
        { class: "bad" },
        `Slack: ${snap.slack.error} `,
        el("button", { type: "button", class: "link", text: "Settings", onclick: () => openSettings("connections") }),
        " · ",
        el("button", { type: "button", class: "link", text: "Turn off", title: "Stop watching Slack (turn it on again in Settings → Connections)", onclick: turnOffSlack }),
      ),
    );
  }
  if (snap.settings.github_enabled && snap.github.error) nodes.push(el("div", { class: "bad", text: `GitHub: ${snap.github.error}` }));
  // A watch whose last scan could not see all of its source: "0 found" means nothing there.
  const partial = snap.connector?.last?.incomplete || [];
  if (partial.length) {
    nodes.push(
      el(
        "div",
        { class: "bad", title: partial.map((item) => `Watch ${item.watch}: ${item.reason}`).join("\n") },
        `Watches: ${partial.length === 1 ? "one was" : `${partial.length} were`} only partly covered `,
        el("button", { type: "button", class: "link", text: "Settings", onclick: () => openSettings("connections") }),
      ),
    );
  }
  if (snap.settings.paused) nodes.push(el("div", { text: "Watchers paused" }));
  line.replaceChildren(...nodes);
}
async function turnOffSlack() {
  try {
    state.snapshot = { ...state.snapshot, ...(await api("/api/settings", { method: "POST", body: { ...state.snapshot.settings, slack_enabled: false } })) };
    renderStatusLine();
    toast("Slack watching is off. Turn it on in Settings → Connections.");
  } catch (error) {
    toast(error.message, "error");
  }
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
        el("span", { class: `dot${item.open ? " open" : ""}`, title: item.open ? DOT_HELP.open : "Not open anywhere" }),
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
// A Copilot session Chief of Staff does not manage: its latest conversation, read from its log.
// A message continues it here (the Router manages it from then on, in its own folder).
export const externalItem = () => state.external.find((item) => item.id === state.selected);
function showExternal(item) {
  // A worker chosen with @ is for messages sent from Home, not for this session.
  closeMentions();
  setRecipient(null);
  state.selected = item.id;
  setLayout(false);
  renderSidebar();
  $("#back-home").hidden = false;
  $("#session-menu").hidden = true;
  $("#session-title").textContent = item.summary;
  $("#session-meta").textContent = [item.open ? "Open in a terminal" : "Not managed yet", shortPath(item.cwd), item.branch, `updated ${since(item.modified)}`].filter(Boolean).join(" · ");
  $("#session-meta").title = item.cwd;
  const command = `copilot --resume ${item.id}`;
  delete $("#session-actions").dataset.key;
  $("#session-actions").replaceChildren(
    el("button", { type: "button", class: "text", text: "Copy resume command", title: command, onclick: () => navigator.clipboard.writeText(command).then(() => toast("Copied")) }),
  );
  $("#composer").hidden = false;
  setComposer(item.open ? { locked: IN_TERMINAL } : { placeholder: "Message to continue this session here…", attach: false });
  const history = el("div", { class: "external-history" }, el("div", { class: "line note", text: "Loading its conversation…" }));
  $("#timeline").replaceChildren(history);
  loadHistory(item, history, true);
}

// Read-only, from the session's log; refreshed while it is open in a terminal.
async function loadHistory(item, box, first = false) {
  if (state.selected !== item.id || !box.isConnected) return;
  try {
    const { entries, working, open } = await api(`/api/external/${item.id}/history`);
    if (state.selected !== item.id || !box.isConnected) return;
    if (item.open && !open) {
      // Exited in the terminal while the owner watched: it can be continued here now.
      item.open = false;
      $("#session-meta").textContent = $("#session-meta").textContent.replace("Open in a terminal", "Not managed yet");
      setComposer({ placeholder: "Message to continue this session here…", attach: false });
    }
    const nodes = readOnlyTimeline(entries, item.cwd);
    if (!nodes.length) nodes.push(el("div", { class: "line note", text: "No conversation yet." }));
    if (working) nodes.push(el("div", { class: "working-line", text: "Working in a terminal…" }));
    else if (!open) nodes.push(el("div", { class: "line notice" }, el("span", { text: `A message continues it here, still in ${shortPath(item.cwd) || "its folder"}` })));
    box.replaceChildren(...nodes);
    scrollToBottom(first);
    if (open) setTimeout(() => loadHistory(item, box), 4000);
  } catch (error) {
    box.replaceChildren(el("div", { class: "line error", text: error.message }));
  }
}
