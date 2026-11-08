// The main pane: Home or one session, with its header, model settings and timeline.

import { $, api, el, foldable, toast, withKey } from "./dom.js";
import { clock, compactPaths, firstLine, gist, markdown, slackText, threadLabel } from "./text.js";
import { byId, current, HOME, markRead, pendingDrafts, routerId, state, STATUS_TEXT, upsertSession, viewedId, workerTitle } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { focusDraft, focusTask, renderHome, renderHomeHeader, showHomeTab, welcomeCard, workerStates } from "./inbox.js";
import { closeMentions, setRecipient } from "./mention.js";
import { openSettings } from "./settings.js";
// A worker's state in the header, as its sidebar dot and Home have it.
// The four places, as the sidebar dot and Home say them.
const SHORT_STATE = { waiting: "Needs you", working: "Running", watching: "Monitoring", idle: "Nothing open" };
const ROUTER_PLACEHOLDER = "Ask for anything… (@ to message one worker)";
// Every view but Home has "← Home" at the top left.
function backHome(shown) {
  $("#back-home").hidden = !shown;
}
// The "⋯" of a session: its model settings, and what is done with it less often.
function sessionMenu(session, items = []) {
  // Not rebuilt while open: a choice the owner is making stays where it is.
  if (!$("#session-more").hidden && $("#session-menu").dataset.for === session?.id) return;
  $("#session-menu").dataset.for = session?.id || "";
  $("#session-more").hidden = true;
  if (session) renderTuning(session);
  else $("#session-tuning").hidden = true;
  $("#session-menu-items").replaceChildren(...items.filter(Boolean));
  $("#session-menu").hidden = $("#session-tuning").hidden && !$("#session-menu-items").children.length;
}
const menuButton = (text, onclick, className = "") => el("button", { type: "button", class: className, text, onclick });
export function renderHeader() {
  if (state.selected === HOME) {
    backHome(false);
    renderHomeHeader();
    const router = byId(routerId());
    sessionMenu(router);
    $("#composer").hidden = false;
    const waiting = !router && state.snapshot.runtime.state !== "ready";
    setComposer(router?.elsewhere ? { locked: IN_TERMINAL } : waiting ? { locked: "You can ask once Copilot is connected." } : { placeholder: ROUTER_PLACEHOLDER });
    return;
  }
  const session = current();
  const actions = $("#session-actions");
  if (!session) return;
  backHome(true);
  delete actions.dataset.key;
  $("#composer").hidden = Boolean(session.closed_at) && session.role !== "worker";
  let meta = [];
  if (session.closed_at) {
    meta = ["Closed", session.branch];
  } else if (session.role === "router") {
    meta = ["Coordinates your sessions", session.status === "working" ? "routing…" : ""];
  } else if (session.role === "slack") {
    meta = ["Drafts your Slack replies", STATUS_TEXT[session.status]];
  } else if (session.role === "watchdog") {
    meta = ["Checks workers for stalls and drift", STATUS_TEXT[session.status]];
  } else {
    // Its state and branch; the folders it works in are in the tooltip and its ⋯ menu.
    const repos = session.checkouts?.length || 0;
    // Where it stands, in the words of its sidebar dot and of Home.
    const found = workerStates().get(session.id);
    const where = (found?.dot || "idle").split(" ")[0];
    meta = [SHORT_STATE[where] || STATUS_TEXT[session.status], found?.sub || "", session.status === "queued" ? session.activity : "", session.branch, repos > 1 ? `${repos} repos` : ""];
    if (session.elsewhere) {
      // What it does in the terminal, read from its log; Chief of Staff does not drive it now.
      const terminal = session.terminal || {};
      meta = [terminal.working ? "Working in a terminal" : "Open in a terminal", terminal.working ? terminal.activity : "", session.branch];
    }
  }
  if (session.credits >= 0.05) meta.push(`${session.credits.toFixed(1)} credits`);
  const follows = session.follows || [];
  if (follows.length && !session.closed_at) meta.push(`following ${follows.length}`);
  const titles = { router: "Chat with your chief of staff", slack: "Slack", watchdog: "Watchdog" };
  $("#session-title").textContent = titles[session.role] || workerTitle(session);
  $("#session-title").title = session.role === "worker" && session.title !== workerTitle(session) ? `Started as “${session.title}”` : "";
  $("#session-meta").textContent = meta.filter(Boolean).join(" · ");
  const where = session.role === "worker" ? (session.checkouts?.length ? session.checkouts.map((checkout) => `${checkout.path}${checkout.branch ? ` (${checkout.branch})` : ""}`) : [session.cwd]) : [];
  $("#session-meta").title = [...where, ...follows.map(threadLabel)].filter(Boolean).join("\n");
  // Stop is on the line that shows it working, at the end of its timeline.
  const buttons = [];
  if (session.role === "worker" && session.closed_at) {
    buttons.push(el("button", { type: "button", class: "secondary", text: "Reopen", title: "Continue with its conversation and checkout", onclick: () => reopen(session.id) }));
  }
  actions.replaceChildren(...buttons);
  const items = [];
  if (session.role === "worker" && !session.closed_at) {
    const globalYolo = state.snapshot.settings.worker_yolo;
    const checks = el(
      "select",
      { class: session.yolo ? "yolo" : "", title: "Permission checks for this worker" },
      el("option", { value: "", text: `Default: ${globalYolo && !session.slack ? "yolo" : "ask first"}` }),
      el("option", { value: "on", text: "Yolo (no permission checks)" }),
      el("option", { value: "off", text: "Ask first (permission checks)" }),
    );
    checks.value = session.yolo_choice || "";
    checks.onchange = async () => {
      try {
        const view = await api(`/api/sessions/${session.id}/yolo`, { method: "POST", body: { mode: checks.value } });
        upsertSession(view);
        toast(view.yolo ? "Yolo: this worker runs without permission checks." : "Ask first: permission checks are on for this worker.");
      } catch (error) {
        toast(error.message, "error");
      }
    };
    items.push(el("label", {}, el("span", { text: "Permission checks" }), checks));
  }
  if (session.role === "worker" && session.cwd) {
    const open = (app) => api(`/api/sessions/${session.id}/open`, { method: "POST", body: { app } }).catch((error) => toast(error.message, "error"));
    items.push(
      el("hr"),
      menuButton("Open in Finder", () => open("finder")),
      menuButton("Open in VS Code", () => open("vscode")),
      menuButton("Copy folder path", () => navigator.clipboard.writeText(session.cwd).then(() => toast("Path copied."), (error) => toast(error.message, "error"))),
    );
  }
  if (session.role === "worker" && !session.closed_at) {
    items.push(
      el("hr"),
      session.status === "working" ? menuButton("Stop what it is doing", () => post(`/api/sessions/${session.id}/stop`)) : "",
      menuButton(
        "Close worker",
        () => {
          if (session.status !== "working" || confirm("This worker is still working. Close it?")) post(`/api/sessions/${session.id}/close`);
        },
        "danger",
      ),
    );
  }
  sessionMenu(session, items);
  const placeholders = {
    router: ROUTER_PLACEHOLDER,
    slack: "Message the Slack session…",
    watchdog: "Message the Watchdog…",
  };
  setComposer(
    session.elsewhere
      ? { locked: IN_TERMINAL }
      : { placeholder: session.closed_at ? "Message to reopen this worker…" : placeholders[session.role] || "Message this worker directly…" },
  );
}
// Two programs must not write to one Copilot session: one open in a terminal is used there.
export const IN_TERMINAL = "Open in a terminal right now. Continue there, or exit it there (/exit) to message it here.";
// The composer, or (locked) a note saying why there is none. `attach`: images can go along.
export function setComposer({ placeholder = "", locked = "", attach = true } = {}) {
  const input = $("#composer-input");
  $("#composer").classList.toggle("locked", Boolean(locked));
  $("#composer").classList.toggle("no-attach", !attach);
  $("#composer-note").textContent = locked;
  input.disabled = Boolean(locked);
  input.placeholder = placeholder;
}
export const EFFORTS = { none: "none", low: "low", medium: "medium", high: "high", xhigh: "extra high", max: "max" };
const modelInfo = (id) => state.models.find((model) => model.id === id);
const tokens = (count) => (count >= 1e6 ? `${+(count / 1e6).toFixed(1)}M` : `${Math.round(count / 1000)}k`);
// Model, thinking effort and context window of the selected session. Each applies from its next message.
function renderTuning(session) {
  const box = $("#session-tuning");
  box.hidden = Boolean(session.closed_at || session.elsewhere) || !state.models.length;
  if (box.hidden) return;
  const model = $("#session-model");
  const effort = $("#session-effort");
  const context = $("#session-context");
  const fallback = session.model_choice ? "" : modelInfo(session.model)?.name || session.model;
  const show = (select, shown) => {
    select.closest("label").hidden = !shown;
  };
  model.replaceChildren(
    el("option", { value: "", text: `Default${fallback ? ` · ${fallback}` : ""}` }),
    ...state.models.map((item) => el("option", { value: item.id, text: item.name })),
  );
  model.value = session.model_choice || "";
  const info = modelInfo(session.model);
  const efforts = info?.efforts || [];
  show(effort, efforts.length);
  effort.replaceChildren(
    el("option", { value: "", text: `Default${session.effort && !session.effort_choice ? ` (${EFFORTS[session.effort] || session.effort})` : ""}` }),
    ...efforts.map((level) => el("option", { value: level, text: EFFORTS[level] || level })),
  );
  effort.value = efforts.includes(session.effort_choice) ? session.effort_choice : "";
  show(context, info?.long_context);
  if (info?.long_context) {
    context.replaceChildren(
      el("option", { value: "", text: `${tokens(info.context)} tokens` }),
      el("option", { value: "long_context", text: `${tokens(info.long_context)} tokens` }),
    );
    context.value = session.context || "";
  }
  const save = async (body, what) => {
    try {
      const view = await api(`/api/sessions/${session.id}/model`, { method: "POST", body });
      upsertSession(view);
      renderTuning(view);  // Its efforts and context windows are the new model's, menu open or not.
      toast(`${what}. Applies from the next message.`);
    } catch (error) {
      toast(error.message, "error");
      renderTuning(session);
    }
  };
  // A new model starts from its own default effort and context window.
  model.onchange = () => save({ model: model.value }, `Model: ${model.value ? modelInfo(model.value)?.name || model.value : "default"}`);
  // Resend only what the current model supports: a choice left from another model is dropped.
  const keptEffort = efforts.includes(session.effort_choice) ? session.effort_choice : "";
  const keptContext = info?.long_context ? session.context : "";
  effort.onchange = () =>
    save({ model: session.model_choice, effort: effort.value, context: keptContext }, `Thinking effort: ${EFFORTS[effort.value] || "default"}`);
  context.onchange = () =>
    save({ model: session.model_choice, effort: keptEffort, context: context.value }, `Context window: ${context.selectedOptions[0].text.replace(" tokens", "")}`);
}
async function reopen(sessionId) {
  try {
    upsertSession(await api(`/api/sessions/${sessionId}/reopen`, { method: "POST" }));
  } catch (error) {
    toast(error.message, "error");
  }
}
async function post(path) {
  try {
    await api(path, { method: "POST" });
  } catch (error) {
    toast(error.message, "error");
  }
}
// Home has the Router's conversation and a panel beside it; a session, its timeline alone.
export function setLayout(home) {
  $("#main").classList.toggle("home", home);
  $("#panel").hidden = !home;
  $("#home-tabs").hidden = !home;
  $("#notices").hidden = !home || !$("#notices").children.length;
  $("#timeline").hidden = false;
  if (home) showHomeTab(state.homeTab);
}
// `task`: the conversation opens at that task's latest report (or what it was given), not at
// its end: the worker may have gone on to other work since.
export async function select(sessionId, { task = "" } = {}) {
  if (!sessionId) return;
  // The Router's chat and the inbox of earlier versions are Home.
  if (sessionId === "inbox" || sessionId === routerId()) sessionId = HOME;
  closeMentions();
  // A worker chosen with @ is for messages sent from Home.
  if (sessionId !== HOME) setRecipient(null);
  if (sessionId !== HOME && !byId(sessionId)) {
    try {
      state.viewing = await api(`/api/sessions/${sessionId}`);
    } catch {
      await select(HOME);
      return;
    }
  }
  state.selected = sessionId;
  localStorage.setItem("cos.selected", sessionId);
  setLayout(sessionId === HOME);
  renderSidebar();
  renderHeader();
  renderHome();
  const timeline = $("#timeline");
  timeline.replaceChildren();
  state.lastId = 0;
  const viewed = viewedId();
  let messages = [];
  // Messages that come by event while the history loads are added after it.
  const loading = { id: viewed, held: [] };
  if (viewed) {
    state.loading = loading;
    try {
      messages = await api(`/api/sessions/${viewed}/messages`);
    } catch (error) {
      toast(error.message, "error");
    }
    if (state.loading === loading) state.loading = null;
    if (state.selected !== sessionId) return;
  }
  timeline.replaceChildren();
  state.lastId = 0;
  appendMessages(messages);
  appendMessages(loading.held);
  scrollToBottom(true);
  if (task) showTaskInTimeline(task);
  if (viewed && byId(viewed)?.unread) markRead(viewed);
  if (!$("#composer").hidden) $("#composer-input").focus();
}
function showTaskInTimeline(taskId) {
  const nodes = [...$("#timeline").querySelectorAll(`[data-task="${taskId}"]`)];
  const node = nodes.filter((item) => item.dataset.kind === "report").pop() || nodes.pop();
  if (!node) return;
  node.scrollIntoView({ block: "start" });
  node.classList.remove("flash");
  void node.offsetWidth;  // Restarts the highlight.
  node.classList.add("flash");
}
export function scrollToBottom(force = false) {
  const scroller = $("#scroller");
  if (force || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 160) {
    scroller.scrollTop = scroller.scrollHeight;
  }
}
const STATUS_CHIP = { done: "Done", progress: "Progress", question: "Question", blocked: "Blocked", failed: "Failed" };
export function chip(status) {
  return STATUS_CHIP[status] ? el("span", { class: `chip ${status}`, text: STATUS_CHIP[status] }) : null;
}
// A card: a header (what, status, time, optional "Open"), then the markdown body. Long bodies fold.
function card({ label, body, className = "", status, time, open }) {
  const content = el("div", { class: "body markdown", html: markdown(body) });
  const node = el(
    "div",
    { class: `card ${className}` },
    el(
      "div",
      { class: "head" },
      el("span", { class: "label", text: label }),
      chip(status),
      el("span", { class: "spacer" }),
      time ? el("time", { text: clock(time), title: new Date(time).toLocaleString() }) : null,
      open ? el("button", { type: "button", class: "open", text: "Open", onclick: open }) : null,
    ),
    content,
  );
  if (body.length > 900 || body.split("\n").length > 14) {
    content.classList.add("folded");
    const toggle = el("button", { type: "button", class: "more", text: "Show more" });
    toggle.addEventListener("click", () => {
      const folded = content.classList.toggle("folded");
      toggle.textContent = folded ? "Show more" : "Show less";
    });
    node.append(toggle);
  }
  return node;
}
// One line in the Home chat: a mark, what happened, a link to where it is; with `body`, it opens
// in place to read it all.
function eventLine({ mark = "·", kind = "", text, time, view = null, viewText = "View", body = "" }) {
  const parts = [
    el("span", { class: "mark", text: mark }),
    el("span", { class: "what", text, title: text }),
    view ? el("button", { type: "button", class: "link", text: viewText, onclick: (event) => { event.preventDefault(); event.stopPropagation(); view(); } }) : null,
    time ? el("time", { text: clock(time), title: new Date(time).toLocaleString() }) : null,
  ];
  if (!body) return el("div", { class: `event ${kind}`.trim() }, ...parts);
  // Its line opens it; its View is beside that toggle, not in it (no button in a button).
  const content = el("div", { class: "markdown", html: markdown(body), hidden: true });
  const node = el("div", { class: `event opens ${kind}`.trim() });
  const toggle = el("button", { type: "button", class: "line", "aria-expanded": "false", onclick: () => {
    content.hidden = !content.hidden;
    node.classList.toggle("open", !content.hidden);
    toggle.setAttribute("aria-expanded", String(!content.hidden));
  } }, parts[0], parts[1]);
  node.append(el("div", { class: "row" }, toggle, ...parts.slice(2)), content);
  return node;
}
// What a worker's report says, in a word: the report itself is on the task's card.
const REPORTED = {
  done: ["✓", "done", "finished"],
  question: ["?", "waiting", "has a question"],
  blocked: ["!", "waiting", "is blocked"],
  failed: ["✗", "failed", "failed"],
  progress: ["…", "", "reported progress"],
};
// Where something that reached the Router came from: a watch's finding is the watch's, whatever
// source it found it in.
const SOURCES = { slack: "Slack", github: "GitHub", watch: "a watch" };
// Slack's text with names for its ids (from Slack, a thread followed, a watch finding there).
const readable = (meta, text) => (meta.source === "slack" || meta.source === "watch" || meta.thread?.startsWith("slack:") ? slackText(text) : text);
// Home's conversation is the owner and the Router. Everything else that reaches the Router is
// one line: a task's news points at its card beside the chat; the rest opens in place.
function homeEntry(message) {
  const meta = message.meta || {};
  const time = message.created_at;
  const text = message.kind === "inbound" ? readable(meta, message.text || "") : message.text || "";
  switch (message.kind) {
    case "inbound": {
      if (meta.from && meta.status?.startsWith("watchdog")) {
        return eventLine({ mark: "⚠", kind: "waiting", text: `Watchdog: “${meta.title}” looks ${meta.status.replace("watchdog: ", "").replace("_", " ")}`, time, body: text });
      }
      if (meta.from && meta.status) {
        const [mark, kind, what] = REPORTED[meta.status] || ["·", "", meta.status];
        // What it found, in a line (the whole report opens here; its card has the receipts).
        const said = meta.status === "done" ? gist(text) : "";
        return eventLine({ mark, kind, text: `${meta.title} ${what}${said ? `: ${said}` : ""}`, time, body: said ? text : "", view: () => focusTask(meta.task_id, meta.from) });
      }
      if (meta.asked_by) return eventLine({ mark: "?", text: `Question from “${meta.title}”: ${firstLine(text)}`, time, body: text, view: () => select(meta.asked_by), viewText: "Open" });
      if (meta.answered_by) return eventLine({ mark: "↩", text: `Answer from ${meta.title}: ${firstLine(text)}`, time, body: text, view: () => select(meta.answered_by), viewText: "Open" });
      if (meta.review) return eventLine({ mark: "·", text: `Review · ${meta.title}: ${firstLine(text)}`, time, body: text, view: () => select(meta.review), viewText: "Open" });
      if (meta.thread) return eventLine({ mark: "↘", text: `${threadLabel(meta.thread)}: ${firstLine(text)}`, time, body: text });
      const source = SOURCES[meta.source] || "Event";
      return eventLine({ mark: "↘", text: `${meta.delegated ? `Handed over from ${source}` : `From ${source}`}: ${firstLine(text)}`, time, body: text });
    }
    case "ask":
      return eventLine({ mark: "→", text: `Asked ${meta.title}: ${firstLine(text)}`, time, body: text, view: () => select(meta.target), viewText: "Open" });
    case "peer":
      // What workers that work together told each other: the Router's copy, in a line.
      return eventLine({ mark: "↔", text: firstLine(text), time, body: text, view: () => select(meta.peer_to), viewText: "Open" });
    case "watchdog": {
      const verdict = { stuck: "looks stuck", looping: "looks like it is looping", off_track: "looks off track" }[meta.verdict];
      return eventLine({ mark: "⚠", kind: "waiting", text: `Watchdog${verdict ? ` · ${verdict}` : ""}: ${firstLine(text)}`, time, body: `${text}${meta.reason ? `\n\nWhy it stepped in: ${meta.reason}` : ""}` });
    }
    case "answer":
      // A worker's answer to what the owner wrote it on Home (@): there too, in a line.
      return eventLine({ mark: "↩", text: `${meta.from_title}: ${gist(text) || firstLine(text)}`, time, body: text, view: () => select(meta.session_id, meta.task_id ? { task: meta.task_id } : {}), viewText: "Open" });
    case "update":
      if (meta.digest) return null;
      return eventLine({ mark: meta.needs_action ? "!" : "·", kind: meta.needs_action ? "waiting" : "", text: firstLine(text), time, body: text.includes("\n") || text.length > 140 ? text : "" });
    case "draft": {
      const node = eventLine({ mark: "✎", text: `${meta.label || "Draft"}: ${firstLine(text)}`, time, body: text });
      node.classList.add("draft-record");
      node.dataset.draftId = meta.draft_id;
      node.dataset.label = meta.label || "Draft";
      return node;
    }
    case "notice": {
      if (meta.settings) return eventLine({ mark: "✦", text, time, view: () => openSettings(meta.settings), viewText: "Open Settings" });
      if (meta.away_reply) return eventLine({ mark: "↩", text, time });
      // "Started “X” · new because …": the why is on hover.
      const started = /^(Started|New task|Continued)/.test(text);
      const node = eventLine({ mark: started ? "" : "·", kind: started ? "started" : "", text: text.split(" · ")[0], time, view: meta.session_id ? () => focusTask(meta.task_id || "", meta.session_id) : null });
      node.querySelector(".what").title = text;
      return node;
    }
    default:
      return null;
  }
}
function renderMessage(message) {
  const meta = message.meta || {};
  const time = message.created_at;
  if (state.selected === HOME && message.session_id === routerId()) {
    const entry = homeEntry(message);
    if (entry) return entry;
  }
  switch (message.kind) {
    case "owner":
      return ownerMessage(message.text, meta, time);
    case "assistant":
      if (meta.internal) return el("div", { class: "line note", text: message.text, title: clock(time) });
      return el("div", { class: "msg agent markdown", html: markdown(message.text), title: clock(time) });
    case "router":
      return card({ label: "From the Router", body: message.text, className: "router", time });
    case "ask":
      return card({ label: `Asked ${meta.title}`, body: message.text, className: "ask", time, open: () => select(meta.target) });
    case "tell":
      return card({ label: `Told “${meta.title}”`, body: message.text, className: "ask", time, open: () => select(meta.target) });
    case "peer":
      return el("div", { class: "line notice link", title: clock(time), onclick: () => select(meta.peer_to) }, el("span", { text: message.text }));
    case "inbound": {
      if (meta.told_by) {
        return card({ label: meta.finished ? `“${meta.title}” finished` : `From “${meta.title}”`, body: message.text, className: "inbound", time, open: () => select(meta.told_by) });
      }
      if (meta.asked_by) {
        return card({ label: `Question from “${meta.title}”`, body: message.text, className: "inbound", time, open: () => select(meta.asked_by) });
      }
      if (meta.answered_by) {
        return card({ label: `Answer from ${meta.title}`, body: message.text, className: "inbound answer", time, open: () => select(meta.answered_by) });
      }
      if (meta.review) {
        return card({ label: `Review · ${meta.title}`, body: message.text, className: "inbound", time, open: () => select(meta.review) });
      }
      if (meta.thread) return card({ label: threadLabel(meta.thread), body: readable(meta, message.text), className: "inbound", time });
      if (meta.from) {
        return card({ label: meta.title, body: message.text, className: `report ${meta.status}`, status: meta.status, time, open: () => select(meta.from) });
      }
      const source = SOURCES[meta.source] || "Event";
      return card({ label: meta.delegated ? `You handed this over from ${source}` : `From ${source}`, body: readable(meta, message.text), className: "inbound", time });
    }
    case "watchdog": {
      const verdict = { stuck: "looks stuck", looping: "looks like it is looping", off_track: "looks off track" }[meta.verdict];
      const node = card({ label: verdict ? `Watchdog · ${verdict}` : "Watchdog", body: message.text, className: "update attention", time });
      if (meta.reason) node.append(el("div", { class: "why-line", text: `Why it stepped in: ${meta.reason}` }));
      return node;
    }
    case "update": {
      if (meta.digest) {
        return card({ label: "Daily summary", body: message.text.replace(/^Daily summary\n/, ""), className: "update", time });
      }
      const label = meta.needs_action ? "Needs you" : "Update";
      return card({ label, body: message.text, className: meta.needs_action ? "update attention" : "update", time });
    }
    case "report":
      return card({ label: "Report", body: message.text, className: `report ${meta.status}`, status: meta.status, time });
    case "answer":
      return card({ label: `Answer from “${meta.from_title}”`, body: message.text, className: "inbound answer", time, open: () => select(meta.session_id) });
    case "draft": {
      const node = card({ label: meta.label || "Draft", body: message.text, className: "draft-record", time });
      node.dataset.draftId = meta.draft_id;
      node.dataset.label = meta.label || "Draft";
      return node;
    }
    case "notice": {
      const open = meta.session_id ? () => select(meta.session_id) : undefined;
      // What Chief of Staff learned or changed on its own: where to see it in Settings.
      const settings = meta.settings
        ? el("button", { type: "button", class: "link", text: "Open Settings", onclick: (event) => { event.stopPropagation(); openSettings(meta.settings); } })
        : null;
      return el("div", { class: `line notice${open ? " link" : ""}${settings ? " wide" : ""}`, title: clock(time), onclick: open }, el("span", { text: message.text }), settings);
    }
    case "error":
      return el("div", { class: "line error", text: message.text, title: clock(time) });
    default:
      return el("div", { class: "line", text: message.text, title: clock(time) });
  }
}
// The owner's message: long ones fold, images show small and open full size.
function ownerMessage(text, meta = {}, time = "") {
  const node = el("div", { class: "msg owner", title: time ? clock(time) : undefined });
  // Sent from Home straight to one worker.
  if (meta.to) node.append(el("button", { type: "button", class: "to", text: `→ ${meta.to_title}`, title: "Open this worker", onclick: () => select(meta.to) }));
  const files = meta.attachments || [];
  if (files.length) {
    node.append(
      el(
        "div",
        { class: "attachments" },
        ...files.map((file) =>
          el(
            "a",
            { href: withKey(file.url), target: "_blank", rel: "noreferrer", title: file.name },
            file.type?.startsWith("image/") ? el("img", { src: withKey(file.url), alt: file.name, loading: "lazy" }) : el("span", { class: "file", text: file.name }),
          ),
        ),
      ),
    );
  }
  if (text) foldable(node, el("div", { class: "text", text }), text);
  return node;
}
// The work between messages, folded into one line: its steps, and what the session said on the
// way to them ("Now the tests:"), which is narration, not an answer. The line shows the latest.
function addWork(container, kind, text, cwd) {
  let group = container.lastElementChild;
  if (!group?.classList.contains("steps")) {
    group = el("details", { class: "steps" }, el("summary"));
    container.append(group);
  }
  let latest;
  if (kind === "tool") {
    latest = compactPaths(text, cwd);
    group.append(el("div", { class: "step", text: latest, title: text }));
  } else {
    group.append(el("div", { class: "narration markdown", html: markdown(text) }));
    latest = text.split("\n").find((line) => line.trim()) || text;
    // Its words in a line, without markup (and past a lead-in like "Done:").
    group.dataset.said = gist(text) || firstLine(text);
  }
  const count = group.querySelectorAll(".step").length;
  const said = group.dataset.said;
  group.querySelector("summary").replaceChildren(
    count ? el("span", { class: "count", text: `${count} step${count === 1 ? "" : "s"}` }) : "",
    el("span", { class: said ? "said" : "last", text: said || latest, title: said || latest }),
  );
}
// Narration: flagged as it arrives; in older timelines, a worker's message right before a step.
function narration(message, next, worker) {
  if (message.kind !== "assistant") return false;
  // The Router's remarks after events and reports: the cards are what matters to the owner.
  if (message.meta?.internal) return true;
  return Boolean(message.meta?.narration) || (worker && next?.kind === "tool");
}
// A session Chief of Staff does not drive, as read from its log: messages and folded work.
export function readOnlyTimeline(entries, cwd) {
  const box = el("div");
  for (const entry of entries) {
    if (entry.kind === "tool" || entry.kind === "narration") addWork(box, entry.kind, entry.text, cwd);
    else if (entry.kind === "owner") box.append(ownerMessage(entry.text));
    else box.append(el("div", { class: "msg agent markdown", html: markdown(entry.text) }));
  }
  return [...box.children];
}

// A report completed after it was shown (its worker wrote the answer in its chat).
export function replaceMessage(message) {
  const old = $("#timeline").querySelector(`[data-id="${message.id}"]`);
  const node = old && renderMessage(message);
  if (!node?.dataset) return;
  Object.assign(node.dataset, { ...old.dataset, id: message.id });
  old.replaceWith(node);
}

export function appendMessages(messages) {
  const timeline = $("#timeline");
  timeline.querySelector(".empty")?.remove();
  timeline.querySelector(".working-line")?.remove();
  const shown = state.selected === HOME ? byId(routerId()) : current();
  const worker = shown?.role === "worker";
  const viewed = viewedId();
  messages.forEach((message, index) => {
    if (message.id <= state.lastId || message.session_id !== viewed) return;
    state.lastId = message.id;
    if (message.kind === "tool" || narration(message, messages[index + 1], worker)) {
      addWork(timeline, message.kind === "tool" ? "tool" : "narration", message.text, shown?.cwd);
      return;
    }
    const node = renderMessage(message);
    if (node?.dataset) {
      node.dataset.id = message.id;
      node.dataset.kind = message.kind;
      if (message.meta?.task_id) node.dataset.task = message.meta.task_id;
    }
    timeline.append(node);
  });
  // Nothing said yet: what to ask for (unless Copilot must be set up first).
  if (!timeline.children.length && state.selected === HOME && state.snapshot?.runtime.state !== "error") timeline.append(welcomeCard());
  updateWorkingLine();
  updateDraftRecords();
  scrollToBottom();
}
export function updateWorkingLine() {
  const timeline = $("#timeline");
  timeline.querySelector(".working-line")?.remove();
  const session = byId(viewedId());
  if (session?.status !== "working") return;
  timeline.append(
    el(
      "div",
      { class: "working-line" },
      el("span", { class: "dot working" }),
      el("span", { text: session.activity || "Thinking…" }),
      el("button", { type: "button", class: "link", text: "Stop", title: "Interrupt what it is doing now", onclick: () => post(`/api/sessions/${session.id}/stop`) }),
    ),
  );
  scrollToBottom();
}
// A draft in a timeline: while it waits for the owner, a way to its card under Needs you.
export function updateDraftRecords() {
  const pending = new Set(pendingDrafts().map((draft) => draft.id));
  for (const node of document.querySelectorAll(".draft-record")) {
    const waiting = pending.has(node.dataset.draftId);
    node.classList.toggle("pending", waiting);
    // A card's head, or a chat line's row (the line itself when it does not open).
    const head = node.querySelector(":scope > .head, :scope > .row") || node;
    let link = head.querySelector(".goto");
    if (waiting && !link) {
      link = el("button", { type: "button", class: "link goto", text: "Waiting for you · Needs you", onclick: (event) => { event.preventDefault(); event.stopPropagation(); focusDraft(node.dataset.draftId); } });
      const time = head.querySelector("time");
      if (time) time.before(link);
      else head.append(link);
    } else if (!waiting) {
      link?.remove();
    }
    // Gone without being sent: it says so (withdrawn, or a newer draft took its place).
    const outcome = state.draftOutcomes.get(node.dataset.draftId);
    if (outcome && !head.querySelector(".outcome")) {
      const note = el("span", { class: "outcome", text: outcome, title: outcome });
      const time = head.querySelector("time");
      if (time) time.before(note);
      else head.append(note);
    }
  }
}
