// The main pane for one session: header, model settings and its timeline.

import { $, api, el, toast } from "./dom.js";
import { clock, compactPaths, displayPath, markdown, threadLabel } from "./text.js";
import { byId, current, markRead, pendingDrafts, routerId, state, STATUS_TEXT, upsertSession } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { renderDrafts } from "./drafts.js";
import { INBOX, renderInboxHeader, showInbox } from "./inbox.js";
import { closeMentions, setRecipient } from "./mention.js";
export function renderHeader() {
  if (state.selected === INBOX) {
    renderInboxHeader();
    return;
  }
  const session = current();
  const actions = $("#session-actions");
  if (!session) return;
  $("#composer").hidden = Boolean(session.closed_at) && session.role !== "worker";
  let meta = [];
  if (session.closed_at) {
    meta = ["Closed", session.branch || displayPath(session.cwd)];
  } else if (session.role === "router") {
    meta = ["Coordinates your sessions", session.status === "working" ? "routing…" : ""];
  } else if (session.role === "slack") {
    meta = ["Drafts your Slack replies", STATUS_TEXT[session.status]];
  } else if (session.role === "watchdog") {
    meta = ["Checks workers for stalls and drift", STATUS_TEXT[session.status]];
  } else {
    const repos = session.checkouts?.length || 0;
    meta = [STATUS_TEXT[session.status], session.status === "queued" ? session.activity : "", session.branch || displayPath(session.cwd), repos > 1 ? `${repos} repos` : ""];
    if (session.yolo) meta.unshift("YOLO");
    if (session.elsewhere) {
      // What it does in the terminal, read from its log; Chief of Staff does not drive it now.
      const terminal = session.terminal || {};
      meta = [terminal.working ? "Working in a terminal" : "Open in a terminal", terminal.working ? terminal.activity : "", session.branch || displayPath(session.cwd)];
    }
  }
  if (session.credits >= 0.05) meta.push(`${session.credits.toFixed(1)} credits`);
  const follows = session.follows || [];
  if (follows.length && !session.closed_at) meta.push(`following ${follows.length}`);
  const titles = { router: "Chat with your chief of staff", slack: "Slack", watchdog: "Watchdog" };
  $("#session-title").textContent = titles[session.role] || session.title;
  $("#session-meta").textContent = meta.filter(Boolean).join(" · ");
  const where = session.role === "worker" ? (session.checkouts?.length ? session.checkouts.map((checkout) => `${checkout.path}${checkout.branch ? ` (${checkout.branch})` : ""}`) : [session.cwd]) : [];
  $("#session-meta").title = [...where, ...follows.map(threadLabel)].filter(Boolean).join("\n");
  $("#session-meta").classList.toggle("yolo", Boolean(session.yolo && !session.closed_at));
  const buttons = [];
  if (session.status === "working" && !session.closed_at) {
    buttons.push(el("button", { type: "button", class: "text", text: "Stop", title: "Interrupt the current turn", onclick: () => post(`/api/sessions/${session.id}/stop`) }));
  }
  if (session.role === "worker" && !session.closed_at) {
    const globalYolo = state.snapshot.settings.worker_yolo;
    const checks = el(
      "select",
      { class: `folder${session.yolo ? " yolo" : ""}`, "aria-label": "Permission checks", title: "Permission checks for this worker" },
      el("option", { value: "", text: `Checks: default (${globalYolo ? "yolo" : "on"})` }),
      el("option", { value: "off", text: "Checks: on" }),
      el("option", { value: "on", text: "Yolo: no checks" }),
    );
    checks.value = session.yolo_choice || "";
    checks.onchange = async () => {
      try {
        const view = await api(`/api/sessions/${session.id}/yolo`, { method: "POST", body: { mode: checks.value } });
        upsertSession(view);
        toast(view.yolo ? "Yolo: this worker runs without permission checks." : "Permission checks are on for this worker.");
      } catch (error) {
        toast(error.message, "error");
      }
    };
    buttons.push(checks);
  }
  if (session.role === "worker" && session.cwd) {
    const folder = el(
      "select",
      { class: "folder", "aria-label": "Folder", title: session.cwd },
      el("option", { value: "", text: "Folder" }),
      el("option", { value: "finder", text: "Open in Finder" }),
      el("option", { value: "vscode", text: "Open in VS Code" }),
      el("option", { value: "copy", text: "Copy path" }),
    );
    folder.onchange = async () => {
      const choice = folder.value;
      folder.value = "";
      try {
        if (choice === "copy") {
          await navigator.clipboard.writeText(session.cwd);
          toast("Path copied");
        } else if (choice) {
          await api(`/api/sessions/${session.id}/open`, { method: "POST", body: { app: choice } });
        }
      } catch (error) {
        toast(error.message, "error");
      }
    };
    buttons.push(folder);
  }
  if (session.role === "worker" && session.closed_at) {
    buttons.push(el("button", { type: "button", class: "text", text: "Reopen", title: "Continue with its conversation and checkout", onclick: () => reopen(session.id) }));
  }
  if (session.role === "worker" && !session.closed_at) {
    buttons.push(
      el("button", {
        type: "button",
        class: "text",
        text: "Close",
        onclick: () => {
          if (session.status !== "working" || confirm("This worker is still working. Close it?")) post(`/api/sessions/${session.id}/close`);
        },
      }),
    );
  }
  actions.replaceChildren(...buttons);
  renderTuning(session);
  const placeholders = {
    router: "Ask your chief of staff… (@ to message a worker directly)",
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
// The composer, or (locked) a note saying why there is none.
export function setComposer({ placeholder = "", locked = "" } = {}) {
  const input = $("#composer-input");
  $("#composer").classList.toggle("locked", Boolean(locked));
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
  model.replaceChildren(
    el("option", { value: "", text: `Default${fallback ? ` · ${fallback}` : ""}` }),
    ...state.models.map((item) => el("option", { value: item.id, text: item.name })),
  );
  model.value = session.model_choice || "";
  const info = modelInfo(session.model);
  const efforts = info?.efforts || [];
  effort.hidden = !efforts.length;
  effort.replaceChildren(
    el("option", { value: "", text: `Effort · default${session.effort && !session.effort_choice ? ` (${EFFORTS[session.effort] || session.effort})` : ""}` }),
    ...efforts.map((level) => el("option", { value: level, text: `Effort · ${EFFORTS[level] || level}` })),
  );
  effort.value = efforts.includes(session.effort_choice) ? session.effort_choice : "";
  context.hidden = !info?.long_context;
  if (info?.long_context) {
    context.replaceChildren(
      el("option", { value: "", text: `${tokens(info.context)} context` }),
      el("option", { value: "long_context", text: `${tokens(info.long_context)} context` }),
    );
    context.value = session.context || "";
  }
  const save = async (body, what) => {
    try {
      upsertSession(await api(`/api/sessions/${session.id}/model`, { method: "POST", body }));
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
    save({ model: session.model_choice, effort: keptEffort, context: context.value }, `Context window: ${context.selectedOptions[0].text.replace(" context", "")}`);
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
export async function select(sessionId) {
  if (!sessionId) return;
  closeMentions();
  // A worker chosen with @ is for messages sent from the Router chat or the inbox.
  if (sessionId !== INBOX && sessionId !== routerId()) setRecipient(null);
  if (sessionId === INBOX) {
    showInbox();
    return;
  }
  $("#inbox").hidden = true;
  $("#timeline").hidden = false;
  $("#drafts").hidden = false;
  $("#composer-input").placeholder = "";
  if (!byId(sessionId)) {
    try {
      state.viewing = await api(`/api/sessions/${sessionId}`);
    } catch {
      if (sessionId !== routerId()) await select(routerId());
      return;
    }
  }
  state.selected = sessionId;
  localStorage.setItem("cos.selected", sessionId);
  renderSidebar();
  renderHeader();
  renderDrafts();
  const timeline = $("#timeline");
  timeline.replaceChildren();
  state.lastId = 0;
  // Messages that come by event while the history loads are added after it.
  const loading = { id: sessionId, held: [] };
  state.loading = loading;
  let messages = [];
  try {
    messages = await api(`/api/sessions/${sessionId}/messages`);
  } catch (error) {
    toast(error.message, "error");
  }
  if (state.loading === loading) state.loading = null;
  if (state.selected !== sessionId) return;
  timeline.replaceChildren();
  state.lastId = 0;
  appendMessages(messages);
  appendMessages(loading.held);
  scrollToBottom(true);
  if (byId(sessionId)?.unread) markRead(sessionId);
  if (!$("#composer").hidden) $("#composer-input").focus();
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
function renderMessage(message) {
  const meta = message.meta || {};
  const time = message.created_at;
  switch (message.kind) {
    case "owner":
      if (meta.to) {
        // Sent from here straight to one worker.
        return el(
          "div",
          { class: "msg owner", title: clock(time) },
          el("button", { type: "button", class: "to", text: `→ ${meta.to_title}`, title: "Open this worker", onclick: () => select(meta.to) }),
          message.text,
        );
      }
      return el("div", { class: "msg owner", text: message.text, title: clock(time) });
    case "assistant":
      if (meta.internal) return el("div", { class: "line note", text: message.text, title: clock(time) });
      return el("div", { class: "msg agent markdown", html: markdown(message.text), title: clock(time) });
    case "router":
      return card({ label: "From the Router", body: message.text, className: "router", time });
    case "ask":
      return card({ label: `Asked ${meta.title}`, body: message.text, className: "ask", time, open: () => select(meta.target) });
    case "inbound": {
      if (meta.asked_by) {
        return card({ label: `Question from “${meta.title}”`, body: message.text, className: "inbound", time, open: () => select(meta.asked_by) });
      }
      if (meta.answered_by) {
        return card({ label: `Answer from ${meta.title}`, body: message.text, className: "inbound answer", time, open: () => select(meta.answered_by) });
      }
      if (meta.review) {
        return card({ label: `Review · ${meta.title}`, body: message.text, className: "inbound", time, open: () => select(meta.review) });
      }
      if (meta.thread) return card({ label: threadLabel(meta.thread), body: message.text, className: "inbound", time });
      if (meta.from) {
        return card({ label: meta.title, body: message.text, className: `report ${meta.status}`, status: meta.status, time, open: () => select(meta.from) });
      }
      const source = { slack: "Slack", github: "GitHub" }[meta.source] || "Event";
      return card({ label: meta.delegated ? `You handed this over from ${source}` : source, body: message.text, className: "inbound", time });
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
    case "draft": {
      const node = card({ label: meta.label || "Draft", body: message.text, className: "draft-record", time });
      node.dataset.draftId = meta.draft_id;
      node.dataset.label = meta.label || "Draft";
      return node;
    }
    case "notice": {
      const open = meta.session_id ? () => select(meta.session_id) : undefined;
      return el("div", { class: `line notice${open ? " link" : ""}`, title: clock(time), onclick: open }, el("span", { text: message.text }));
    }
    case "error":
      return el("div", { class: "line error", text: message.text, title: clock(time) });
    default:
      return el("div", { class: "line", text: message.text, title: clock(time) });
  }
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
    group.dataset.said = latest.replace(/[`*_#>]/g, "").trim();
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
    else if (entry.kind === "owner") box.append(el("div", { class: "msg owner", text: entry.text }));
    else box.append(el("div", { class: "msg agent markdown", html: markdown(entry.text) }));
  }
  return [...box.children];
}

export function appendMessages(messages) {
  const timeline = $("#timeline");
  timeline.querySelector(".empty")?.remove();
  timeline.querySelector(".working-line")?.remove();
  const worker = current()?.role === "worker";
  messages.forEach((message, index) => {
    if (message.id <= state.lastId || message.session_id !== state.selected) return;
    state.lastId = message.id;
    if (message.kind === "tool" || narration(message, messages[index + 1], worker)) {
      addWork(timeline, message.kind === "tool" ? "tool" : "narration", message.text, current()?.cwd);
      return;
    }
    timeline.append(renderMessage(message));
  });
  if (!timeline.children.length && state.selected === routerId()) {
    timeline.append(
      el(
        "div",
        { class: "empty" },
        el("strong", { text: "What should we work on?" }),
        "Tell your chief of staff what you need. It starts workers, watches Slack, and only interrupts you when something is ready.",
      ),
    );
  }
  updateWorkingLine();
  updateDraftRecords();
  scrollToBottom();
}
export function updateWorkingLine() {
  const timeline = $("#timeline");
  timeline.querySelector(".working-line")?.remove();
  const session = byId(state.selected);
  if (session?.status !== "working") return;
  timeline.append(
    el("div", { class: "working-line" }, el("span", { class: "dot working" }), el("span", { text: session.activity || "Thinking…" })),
  );
  scrollToBottom();
}
export function updateDraftRecords() {
  const pending = new Set(pendingDrafts().map((draft) => draft.id));
  for (const node of document.querySelectorAll(".draft-record")) {
    const label = node.dataset.label || "Draft";
    node.querySelector(".label").textContent = pending.has(node.dataset.draftId) ? `${label} · waiting for you` : label;
    node.classList.toggle("pending", pending.has(node.dataset.draftId));
  }
}
