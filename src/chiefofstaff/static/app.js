"use strict";

const $ = (selector) => document.querySelector(selector);
const STATUS_ORDER = { waiting: 0, working: 1, starting: 1, queued: 2, failed: 3, idle: 4, done: 5 };
const STATUS_TEXT = {
  working: "Working",
  starting: "Starting",
  queued: "Queued",
  idle: "Idle",
  waiting: "Waiting for an answer",
  done: "Done",
  failed: "Failed",
};

const state = {
  snapshot: null,
  selected: localStorage.getItem("wb.selected"),
  external: [],
  lastId: 0,
  connectedOnce: false,
  reloadTimer: null,
  models: [],
  viewing: null,
  usageTimer: null,
};

// Helpers

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

function errorText(data, status) {
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) {
    return data.detail.map((item) => String(item.msg).replace(/^Value error, /, "")).join("; ");
  }
  return `Request failed (${status})`;
}

async function api(path, { method = "GET", body } = {}) {
  const init = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    $("#application").hidden = true;
    $("#signin").hidden = false;
    throw new Error(errorText(data, 401));
  }
  // Starlette's own "Not Found": this page is newer than the server that is still running.
  if (response.status === 404 && data.detail === "Not Found") throw new Error("Chief of Staff was updated. Restart it to use this.");
  if (!response.ok) throw new Error(errorText(data, response.status));
  return data;
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// Markdown: a small, safe subset. Text is escaped first; only these tags are produced.

const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// A path segment ends at whitespace, punctuation (Western or CJK) or markup.
const SEGMENT = "[^\\s/,，。;；:：()（）\\[\\]{}<>`'\"、|&*!?！？]+";

function inlinePattern() {
  const roots = [state.snapshot?.data_directory, state.snapshot?.home].filter(Boolean);
  const key = roots.join("\n");
  if (inlinePattern.key !== key || !inlinePattern.re) {
    const known = [...roots.map((root) => escapeRegExp(escapeHtml(root))), "~"];
    const path =
      `(?:${known.join("|")})(?:\\/${SEGMENT})+` +
      `|(?<![\\w.~\\/-])\\/(?:Users|home|tmp|private|var|opt|etc|Volumes|workspace)(?:\\/${SEGMENT})+`;
    inlinePattern.re = new RegExp(
      [
        "`([^`\\n]+)`",
        "\\*\\*([^*\\n]+)\\*\\*",
        "~~([^~\\n]+)~~",
        "(?<![\\w*])\\*([^*\\s](?:[^*\\n]*[^*\\s])?)\\*(?![\\w*])",
        "\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)",
        "(https?:\\/\\/[^\\s<)，。、；：！？）」』】]+)",
        `(${path})`,
      ].join("|"),
      "g",
    );
    inlinePattern.path = new RegExp(`^(?:${path})$`);
    inlinePattern.key = key;
  }
  return inlinePattern.re;
}

// Trailing punctuation belongs to the sentence, not to a URL or path.
const TRAILING = /(?:[.,;:!?'"]|&quot;|&#39;)+$/;

function linkLabel(url) {
  let match;
  if ((match = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/(?:pull|issues)\/(\d+)(?:[/?#].*)?$/.exec(url))) return `${match[1]}#${match[2]}`;
  if (/^https:\/\/[^/]+\.slack\.com\/archives\//.test(url)) return "Slack message";
  const plain = url.replace(/^https?:\/\/(www\.)?/, "");
  return plain.length > 64 ? `${plain.slice(0, 61)}…` : plain;
}

function displayPath(full) {
  const data = state.snapshot?.data_directory;
  const home = state.snapshot?.home;
  const scratch = state.snapshot?.settings?.scratch_directory;
  let shown = full;
  if (scratch && full.startsWith(`${scratch}/`)) shown = full.slice(scratch.length + 1);
  else if (data && full.startsWith(`${data}/work/`)) shown = full.slice(data.length + 6).replace(/^(scratch|worktrees|workspaces)\//, "");
  else if (home && full.startsWith(`${home}/`)) shown = `~${full.slice(home.length)}`;
  const parts = shown.split("/");
  if (shown.length > 52 && parts.length > 3) shown = `…/${parts.slice(-2).join("/")}`;
  return shown;
}

function pathChip(path) {
  // Matched on escaped text without "&", so the path needs no further escaping.
  return `<span class="path" data-path="${path}" title="${path}\nClick to copy">${escapeHtml(displayPath(path))}</span>`;
}

function inlineEscaped(escaped) {
  return escaped.replace(inlinePattern(), (match, code, bold, strike, italic, label, href, bare, path) => {
    if (code !== undefined) return inlinePattern.path.test(code) ? pathChip(code) : `<code>${code}</code>`;
    if (bold !== undefined) return `<strong>${inlineEscaped(bold)}</strong>`;
    if (strike !== undefined) return `<s>${inlineEscaped(strike)}</s>`;
    if (italic !== undefined) return `<em>${inlineEscaped(italic)}</em>`;
    const raw = href ?? bare ?? path;
    const trail = href ? "" : raw.match(TRAILING)?.[0] || "";
    const value = trail ? raw.slice(0, -trail.length) : raw;
    if (path !== undefined) return pathChip(value) + trail;
    return `<a href="${value}" target="_blank" rel="noreferrer">${label ?? escapeHtml(linkLabel(value))}</a>${trail}`;
  });
}

// "Label: rest" at the start of a line: a short key (at most three words) is set in bold.
const KEY = /^([\p{L}\p{N}][\p{L}\p{N}\p{M} _-]{0,15}?)\s*([:：])(?!:|\/\/|\d)\s*/u;

function inline(text, keyed = false) {
  const match = keyed && !text.startsWith("**") ? KEY.exec(text) : null;
  if (match && /\p{L}/u.test(match[1]) && match[1].trim().split(/\s+/).length <= 3) {
    const rest = text.slice(match[0].length);
    return `<strong class="key">${escapeHtml(match[1] + match[2])}</strong>${rest ? ` ${inlineEscaped(escapeHtml(rest))}` : ""}`;
  }
  return inlineEscaped(escapeHtml(text));
}

const FENCE = /^\s*(```+|~~~+)\s*[\w+-]*\s*$/;
const HEADING = /^\s*(#{1,6})\s+(.*)$/;
const QUOTE_DEPTH = 6;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^\s*>/;
const ITEM = /^(\s*)([-*•+]|\d+[.)])\s+(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

const isTable = (lines, i) => lines[i].includes("|") && TABLE_RULE.test(lines[i + 1] || "") && (lines[i + 1] || "").includes("-");
const startsBlock = (lines, i) => [FENCE, HEADING, RULE, QUOTE, ITEM].some((re) => re.test(lines[i])) || isTable(lines, i);
const cells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());

function renderList(items) {
  let html = "";
  const stack = [];
  for (const item of items) {
    while (stack.length && item.indent < stack.at(-1).indent) html += `</li></${stack.pop().tag}>`;
    const top = stack.at(-1);
    if (!top || item.indent > top.indent) {
      const tag = item.number === null ? "ul" : "ol";
      html += item.number > 1 ? `<ol start="${item.number}">` : `<${tag}>`;
      stack.push({ indent: item.indent, tag });
    } else {
      html += "</li>";
    }
    html += `<li>${item.lines.map((line) => inline(line, true)).join("<br>")}`;
  }
  while (stack.length) html += `</li></${stack.pop().tag}>`;
  return html;
}

function markdown(text, depth = 0) {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
    } else if (FENCE.test(line)) {
      const fence = FENCE.exec(line)[1];
      const code = [];
      for (i += 1; i < lines.length && !lines[i].trim().startsWith(fence); i += 1) code.push(lines[i]);
      i += 1;
      out.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
    } else if (isTable(lines, i)) {
      const head = cells(line);
      const rows = [];
      for (i += 2; i < lines.length && lines[i].includes("|") && lines[i].trim(); i += 1) rows.push(cells(lines[i]));
      out.push(
        `<div class="table"><table><thead><tr>${head.map((cell) => `<th>${inline(cell)}</th>`).join("")}</tr></thead>` +
          `<tbody>${rows.map((row) => `<tr>${head.map((_, n) => `<td>${inline(row[n] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`,
      );
    } else if (HEADING.test(line)) {
      const title = HEADING.exec(line)[2].trimEnd().replace(/(^|\s)#+$/, "").trimEnd();
      out.push(`<h4>${inline(title)}</h4>`);
      i += 1;
    } else if (RULE.test(line)) {
      out.push("<hr>");
      i += 1;
    } else if (QUOTE.test(line)) {
      const quote = [];
      for (; i < lines.length && QUOTE.test(lines[i]); i += 1) quote.push(lines[i].replace(/^\s*>\s?/, ""));
      out.push(
        depth < QUOTE_DEPTH
          ? `<blockquote>${markdown(quote.join("\n"), depth + 1)}</blockquote>`
          : `<p>${quote.map((part) => inline(part.replace(/^[\s>]+/, ""))).join("<br>")}</p>`,
      );
    } else if (ITEM.test(line)) {
      const items = [];
      for (; i < lines.length && lines[i].trim(); i += 1) {
        const match = ITEM.exec(lines[i]);
        if (match) {
          const number = /\d/.test(match[2]) ? Number.parseInt(match[2], 10) : null;
          items.push({ indent: match[1].replace(/\t/g, "  ").length, number, lines: [match[3]] });
        } else if (/^\s+\S/.test(lines[i]) && !startsBlock(lines, i)) {
          items.at(-1).lines.push(lines[i].trim());
        } else {
          break;
        }
      }
      out.push(renderList(items));
    } else {
      const paragraph = [];
      for (; i < lines.length && lines[i].trim() && (!paragraph.length || !startsBlock(lines, i)); i += 1) paragraph.push(lines[i].trim());
      out.push(`<p>${paragraph.map((part) => inline(part, true)).join("<br>")}</p>`);
    }
  }
  return out.join("");
}

function clock(iso) {
  const date = new Date(iso);
  const time = date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (date.toDateString() === new Date().toDateString()) return time;
  return `${date.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
}

// Steps show paths relative to the session's directory, and home as "~".
function compactPaths(text, cwd) {
  let result = text;
  if (cwd) result = result.replaceAll(`${cwd}/`, "./").replaceAll(cwd, ".");
  const home = state.snapshot?.home;
  return home ? result.replaceAll(home, "~") : result;
}

function ago(iso) {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function threadLabel(key) {
  let match;
  if ((match = /^slack:([A-Z0-9]+):/.exec(key))) return `Slack thread in ${match[1]}`;
  if ((match = /^slack:([A-Z0-9]+)$/.exec(key))) return `Slack DM ${match[1]}`;
  if ((match = /^github:(.+)$/.exec(key))) return match[1];
  return key;
}

function shortPath(path) {
  const home = state.snapshot?.home;
  return home && path?.startsWith(home) ? `~${path.slice(home.length)}` : path || "";
}

const sessions = () => state.snapshot?.sessions || [];
const closedWorkers = () => state.snapshot?.closed || [];
const byId = (id) => sessions().find((session) => session.id === id);
const current = () => byId(state.selected) || (state.viewing?.id === state.selected ? state.viewing : null);
const routerId = () => state.snapshot?.router_id;
const pendingDrafts = () => state.snapshot?.drafts || [];

function draftsFor(sessionId) {
  return sessionId === routerId() ? pendingDrafts() : pendingDrafts().filter((d) => d.session_id === sessionId);
}

// State and events

async function loadState() {
  const data = await api("/api/state");
  state.snapshot = data;
  const known = current() || state.external.some((item) => item.id === state.selected);
  if (!known) {
    state.selected = data.router_id;
  }
  renderSidebar();
  renderHeader();
  renderDrafts();
}

async function loadModels() {
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

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onmessage = (event) => handleEvent(JSON.parse(event.data));
}

async function resync() {
  await loadState();
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
    if (data.session_id === state.selected) appendMessages([data]);
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
    renderDrafts();
    renderSidebar();
    updateDraftRecords();
  } else if (type === "toast") {
    // The Router view already shows updates and every pending draft.
    const visible = document.hasFocus() && [data.session_id, routerId()].includes(state.selected);
    if (!visible) toast(data.text, data.needs_action ? "attention" : "", data.session_id);
  } else if (type === "state") {
    scheduleReload();
  }
}

function upsertSession(session) {
  if (!state.snapshot) return;
  const list = sessions().filter((item) => item.id !== session.id);
  if (!session.closed_at) list.push(session);
  list.sort((a, b) => a.created_at.localeCompare(b.created_at));
  state.snapshot.sessions = list;
  const closed = closedWorkers().filter((item) => item.id !== session.id);
  if (session.closed_at && session.role === "worker") closed.unshift(session);
  state.snapshot.closed = closed.slice(0, 20);
  renderSidebar();
  if (session.id !== state.selected) return;
  if (session.closed_at) state.viewing = session;
  renderHeader();
  updateWorkingLine();
  if (session.unread && document.hasFocus()) markRead(session.id);
}

function markRead(sessionId) {
  api(`/api/sessions/${sessionId}/read`, { method: "POST" }).catch(() => {});
}

// Sidebar

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

function renderSidebar() {
  const list = $("#session-list");
  const all = sessions();
  const router = all.find((session) => session.role === "router");
  const slack = all.find((session) => session.role === "slack");
  const watchdog = all.find((session) => session.role === "watchdog");
  const workers = all
    .filter((session) => session.role === "worker")
    .sort((a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) || b.created_at.localeCompare(a.created_at));
  const nodes = [];
  if (router) nodes.push(sessionItem(router, "Router"));
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

function renderStatusLine() {
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
  if (snap.settings.slack_enabled && snap.slack.error) nodes.push(el("div", { class: "bad", text: `Slack: ${snap.slack.error}` }));
  if (snap.settings.github_enabled && snap.github.error) nodes.push(el("div", { class: "bad", text: `GitHub: ${snap.github.error}` }));
  if (snap.settings.paused) nodes.push(el("div", { text: "Watchers paused" }));
  if (snap.settings.worker_yolo) nodes.push(el("div", { class: "yolo", text: "Yolo: workers run without permission checks" }));
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
  $("#external-count").textContent = state.external.length ? `· ${state.external.length}` : "";
  $("#external-list").replaceChildren(
    ...state.external.map((item) =>
      el(
        "button",
        {
          type: "button",
          class: `item${state.selected === item.id ? " selected" : ""}`,
          title: item.cwd,
          onclick: () => showExternal(item),
        },
        el("span", { class: "dot" }),
        el("span", { class: "label", text: item.summary }),
        el("span", { class: "sub", text: ago(item.modified) }),
      ),
    ),
  );
}

async function loadExternal(refresh = false) {
  try {
    state.external = await api(`/api/external${refresh ? "?refresh=true" : ""}`);
    renderExternal();
  } catch (error) {
    toast(error.message, "error");
  }
}

// Main pane

function renderHeader() {
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
  }
  if (session.credits >= 0.05) meta.push(`${session.credits.toFixed(1)} credits`);
  const follows = session.follows || [];
  if (follows.length && !session.closed_at) meta.push(`following ${follows.length}`);
  const titles = { router: "Router", slack: "Slack", watchdog: "Watchdog" };
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
  const input = $("#composer-input");
  const placeholders = {
    router: "Tell the Router what you need…",
    slack: "Message the Slack session…",
    watchdog: "Message the Watchdog…",
  };
  input.placeholder = session.closed_at ? "Message to reopen this worker…" : placeholders[session.role] || "Message this worker directly…";
}

const EFFORTS = { none: "none", low: "low", medium: "medium", high: "high", xhigh: "extra high", max: "max" };
const modelInfo = (id) => state.models.find((model) => model.id === id);
const tokens = (count) => (count >= 1e6 ? `${+(count / 1e6).toFixed(1)}M` : `${Math.round(count / 1000)}k`);

// Model, thinking effort and context window of the selected session. Each applies from its next message.
function renderTuning(session) {
  const box = $("#session-tuning");
  box.hidden = Boolean(session.closed_at) || !state.models.length;
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

async function select(sessionId) {
  if (!sessionId) return;
  if (!byId(sessionId)) {
    try {
      state.viewing = await api(`/api/sessions/${sessionId}`);
    } catch {
      if (sessionId !== routerId()) await select(routerId());
      return;
    }
  }
  state.selected = sessionId;
  localStorage.setItem("wb.selected", sessionId);
  renderSidebar();
  renderHeader();
  renderDrafts();
  const timeline = $("#timeline");
  timeline.replaceChildren();
  state.lastId = 0;
  let messages = [];
  try {
    messages = await api(`/api/sessions/${sessionId}/messages`);
  } catch (error) {
    toast(error.message, "error");
  }
  if (state.selected !== sessionId) return;
  timeline.replaceChildren();
  appendMessages(messages);
  scrollToBottom(true);
  if (byId(sessionId)?.unread) markRead(sessionId);
  if (!$("#composer").hidden) $("#composer-input").focus();
}

function scrollToBottom(force = false) {
  const scroller = $("#scroller");
  if (force || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 160) {
    scroller.scrollTop = scroller.scrollHeight;
  }
}

const STATUS_CHIP = { done: "Done", progress: "Progress", question: "Question", blocked: "Blocked", failed: "Failed" };

function chip(status) {
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
      return card({ label: source, body: message.text, className: "inbound", time });
    }
    case "watchdog":
      return card({ label: "Watchdog", body: message.text, className: "update attention", time });
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

function appendMessages(messages) {
  const timeline = $("#timeline");
  timeline.querySelector(".empty")?.remove();
  timeline.querySelector(".working-line")?.remove();
  for (const message of messages) {
    if (message.id <= state.lastId || message.session_id !== state.selected) continue;
    state.lastId = message.id;
    if (message.kind === "tool") {
      let group = timeline.lastElementChild;
      if (!group?.classList.contains("steps")) {
        group = el("details", { class: "steps" }, el("summary"));
        timeline.append(group);
      }
      const step = compactPaths(message.text, current()?.cwd);
      group.append(el("div", { class: "step", text: step, title: message.text }));
      const count = group.querySelectorAll(".step").length;
      group.querySelector("summary").replaceChildren(
        el("span", { class: "count", text: `${count} step${count === 1 ? "" : "s"}` }),
        el("span", { class: "last", text: step }),
      );
      continue;
    }
    timeline.append(renderMessage(message));
  }
  if (!timeline.children.length && state.selected === routerId()) {
    timeline.append(
      el(
        "div",
        { class: "empty" },
        el("strong", { text: "What should we work on?" }),
        "Tell the Router. It starts workers, watches Slack, and only interrupts you when something is ready.",
      ),
    );
  }
  updateWorkingLine();
  updateDraftRecords();
  scrollToBottom();
}

function updateWorkingLine() {
  const timeline = $("#timeline");
  timeline.querySelector(".working-line")?.remove();
  const session = byId(state.selected);
  if (session?.status !== "working") return;
  timeline.append(
    el("div", { class: "working-line" }, el("span", { class: "dot working" }), el("span", { text: session.activity || "Thinking…" })),
  );
  scrollToBottom();
}

function updateDraftRecords() {
  const pending = new Set(pendingDrafts().map((draft) => draft.id));
  for (const node of document.querySelectorAll(".draft-record")) {
    const label = node.dataset.label || "Draft";
    node.querySelector(".label").textContent = pending.has(node.dataset.draftId) ? `${label} · waiting for you` : label;
    node.classList.toggle("pending", pending.has(node.dataset.draftId));
  }
}

// Drafts

function renderDrafts() {
  const box = $("#drafts");
  const visible = draftsFor(state.selected);
  const ids = new Set(visible.map((draft) => draft.id));
  for (const node of [...box.children]) {
    if (!ids.has(node.dataset.id)) node.remove();
  }
  for (const draft of visible) {
    if (!box.querySelector(`[data-id="${draft.id}"]`)) box.append(draftCard(draft));
  }
}

// The Router is not sure which folder a task belongs in: the owner picks one.
function workspaceCard(draft) {
  const target = draft.target;
  const error = el("span", { class: "error" });
  const other = el("input", { placeholder: "Another folder, e.g. ~/code/app" });
  const node = el(
    "div",
    { class: "card draft workspace", "data-id": draft.id },
    el(
      "div",
      { class: "head" },
      el("span", { class: "label", text: `Where should “${target.title}” work?` }),
      el("span", { class: "chip waiting", text: "Waiting for you" }),
    ),
    el("div", { class: "note", text: [draft.text, target.why].filter(Boolean).join(" — ") }),
  );
  const choices = el("div", { class: "choices" });
  async function decide(body) {
    for (const button of node.querySelectorAll("button")) button.disabled = true;
    error.textContent = "";
    try {
      await api(`/api/drafts/${draft.id}`, { method: "POST", body });
    } catch (failure) {
      error.textContent = failure.message;
      for (const button of node.querySelectorAll("button")) button.disabled = false;
    }
  }
  target.options.forEach((option, index) => {
    choices.append(
      el(
        "button",
        { type: "button", class: index ? "secondary" : "", title: option.path, onclick: () => decide({ action: "choose", choice: option.path }) },
        el("span", { text: option.path.split("/").pop() }),
        el("small", { text: `${displayPath(option.path)}${option.configured ? "" : " · new project"}` }),
      ),
    );
  });
  choices.append(
    el("button", { type: "button", class: "secondary", onclick: () => decide({ action: "choose", choice: "" }) }, el("span", { text: "No project" }), el("small", { text: "its own scratch folder" })),
  );
  const use = () => other.value.trim() && decide({ action: "choose", choice: other.value.trim() });
  other.addEventListener("keydown", (event) => event.key === "Enter" && use());
  node.append(
    choices,
    el(
      "div",
      { class: "actions" },
      other,
      el("button", { type: "button", class: "secondary", text: "Use", onclick: use }),
      el("button", { type: "button", class: "text", text: "Don't start", onclick: () => decide({ action: "reject" }) }),
    ),
    error,
  );
  return node;
}

const REQUEST_LABELS = { question: "Question", form: "Form", link: "Link to open", signin: "Sign-in" };
const SECRET_FIELD = /pass(word)?|secret|token|api[_-]?key/i;

// Fields for a form an MCP tool asks the owner to fill in (a flat JSON schema).
function formFields(schema) {
  const properties = schema?.properties || {};
  const required = new Set(schema?.required || []);
  const inputs = {};
  const rows = Object.entries(properties).map(([name, spec]) => {
    const title = `${spec.title || name}${required.has(name) ? "" : " (optional)"}`;
    let input;
    const options = (item) => (item?.enum || []).map((value) => ({ value, title: value })).concat((item?.oneOf || item?.anyOf || []).filter((option) => option && "const" in option).map((option) => ({ value: option.const, title: option.title || option.const })));
    const choices = options(spec).length ? options(spec) : options(spec.items);
    if (spec.type === "boolean") {
      input = el("input", { type: "checkbox", checked: spec.default === true });
      inputs[name] = () => input.checked;
      return el("label", { class: "check" }, input, el("span", { text: title }));
    }
    if (choices.length) {
      input = el("select", { multiple: spec.type === "array" }, ...choices.map((choice) => el("option", { value: choice.value, text: choice.title })));
      if (spec.default !== undefined) input.value = spec.default;
      inputs[name] = () => (spec.type === "array" ? [...input.selectedOptions].map((option) => option.value) : input.value);
    } else {
      const numeric = spec.type === "number" || spec.type === "integer";
      input = el("input", { type: numeric ? "number" : SECRET_FIELD.test(name) ? "password" : "text", value: spec.default ?? "", autocomplete: "off" });
      inputs[name] = () => input.value;
    }
    return el("label", {}, el("span", { text: title }), spec.description ? el("small", { text: spec.description }) : null, input);
  });
  return {
    node: el("div", { class: "fields" }, ...rows),
    values: () => Object.fromEntries(Object.entries(inputs).map(([name, read]) => [name, read()])),
  };
}

// Something a session needs from the owner: an answer, a form, a link to open or a sign-in.
function requestCard(draft) {
  const owner = byId(draft.session_id);
  const who = owner?.role === "worker" ? owner.title : "";
  const target = draft.target;
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  const node = el(
    "div",
    { class: `card draft request ${draft.kind}`, "data-id": draft.id },
    el(
      "div",
      { class: "head" },
      el("span", { class: "label", text: `${REQUEST_LABELS[draft.kind]}${who ? ` · ${who}` : ""}` }),
      el("span", { class: "chip waiting", text: "Waiting for you" }),
    ),
    el("div", { class: "body markdown", html: markdown(draft.text) }),
  );
  async function decide(body) {
    for (const button of node.querySelectorAll("button")) button.disabled = true;
    error.textContent = "";
    try {
      await api(`/api/drafts/${draft.id}`, { method: "POST", body });
    } catch (failure) {
      error.textContent = failure.message;
      for (const button of node.querySelectorAll("button")) button.disabled = false;
    }
  }
  const decline = (text) => el("button", { type: "button", class: "text", text, onclick: () => decide({ action: "reject" }) });
  const link = (url, text) => (/^https?:\/\//.test(url) ? el("a", { class: "button", href: url, target: "_blank", rel: "noreferrer", text }) : null);
  if (draft.kind === "question") {
    if (target.choices?.length) {
      node.append(el("div", { class: "choices" }, ...target.choices.map((choice) => el("button", { type: "button", class: "secondary", text: choice, onclick: () => decide({ action: "answer", text: choice }) }))));
    }
    if (target.allow_freeform || !target.choices?.length) {
      const input = el("input", { placeholder: "Your answer", autocomplete: "off" });
      input.addEventListener("keydown", (event) => event.key === "Enter" && decide({ action: "answer", text: input.value }));
      actions.append(input, el("button", { type: "button", text: "Answer", onclick: () => decide({ action: "answer", text: input.value }) }));
    }
    actions.append(decline("Skip"));
  } else if (draft.kind === "form") {
    const fields = formFields(target.schema);
    node.append(fields.node);
    actions.append(decline("Decline"), el("button", { type: "button", text: "Submit", onclick: () => decide({ action: "answer", content: fields.values() }) }));
  } else if (draft.kind === "link") {
    actions.append(link(target.url, "Open link"), el("button", { type: "button", class: "secondary", text: "Done", onclick: () => decide({ action: "answer" }) }), decline("Decline"));
  } else {
    node.append(el("div", { class: "note", text: "Sign in in the tab that opens. This card closes by itself once it worked; every session then uses the sign-in." }));
    actions.append(
      link(target.url, `Sign in to ${target.server}`),
      el("button", { type: "button", class: "secondary", text: "Done", title: "I signed in", onclick: () => decide({ action: "answer" }) }),
      decline("Not now"),
    );
  }
  node.append(actions, error);
  return node;
}

function draftCard(draft) {
  if (draft.kind === "workspace") return workspaceCard(draft);
  if (REQUEST_LABELS[draft.kind]) return requestCard(draft);
  const owner = byId(draft.session_id);
  const target = draft.target;
  const label =
    draft.kind === "slack"
      ? `Slack reply${target.thread_ts ? " in thread" : ""} · ${target.to || target.channel_id}`
      : `PR review · ${target.repo}#${target.number}`;
  const text = el("textarea", { rows: Math.min(10, draft.text.split("\n").length + 1) });
  text.value = draft.text;
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  const node = el(
    "div",
    { class: "card draft", "data-id": draft.id },
    el(
      "div",
      { class: "head" },
      el("span", { class: "label", text: `${label}${owner && owner.role === "worker" ? ` · from ${owner.title}` : ""}` }),
      el("span", { class: "chip waiting", text: "Waiting for you" }),
    ),
    draft.note ? el("div", { class: "note", text: draft.note }) : null,
    draft.kind === "github" ? el("a", { href: target.url, target: "_blank", rel: "noreferrer", text: target.url }) : null,
    text,
    actions,
  );

  async function decide(body) {
    for (const button of actions.querySelectorAll("button")) button.disabled = true;
    error.textContent = "";
    try {
      await api(`/api/drafts/${draft.id}`, { method: "POST", body });
    } catch (failure) {
      error.textContent = failure.message;
      for (const button of actions.querySelectorAll("button")) button.disabled = false;
    }
  }

  function showDefault() {
    actions.replaceChildren(
      error,
      el("button", { type: "button", class: "secondary", text: "Reject", onclick: showReject }),
      el("button", { type: "button", text: "Send", onclick: () => decide({ action: "send", text: text.value }) }),
    );
  }

  function showReject() {
    const reason = el("input", { placeholder: "Why? (optional, helps the next draft)" });
    actions.replaceChildren(
      reason,
      el("button", { type: "button", class: "text", text: "Cancel", onclick: showDefault }),
      el("button", { type: "button", class: "secondary", text: "Reject", onclick: () => decide({ action: "reject", reason: reason.value }) }),
    );
    reason.focus();
  }

  showDefault();
  return node;
}

// External sessions

function showExternal(item) {
  state.selected = item.id;
  renderSidebar();
  $("#session-title").textContent = item.summary;
  $("#session-meta").textContent = [shortPath(item.cwd), item.branch, `updated ${ago(item.modified)} ago`].filter(Boolean).join(" · ");
  $("#session-actions").replaceChildren();
  $("#drafts").replaceChildren();
  $("#composer").hidden = true;
  const command = `copilot --resume ${item.id}`;
  const adopt = el("button", {
    type: "button",
    text: "Let the Router manage it",
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
      el("small", { text: "Only adopt sessions that are not open in a terminal right now." }),
    ),
  );
}

// Usage

function startOfDay(date) {
  const day = new Date(date);
  day.setHours(0, 0, 0, 0);
  return day.getTime() / 1000;
}

async function refreshUsageLink() {
  try {
    const usage = await api(`/api/usage?since=${startOfDay(new Date())}`);
    $("#usage-button").textContent = `Today · ${usage.total.credits.toFixed(1)} credits`;
  } catch {
    $("#usage-button").textContent = "Usage";
  }
}

function usageTable(title, rows, label, onclick) {
  if (!rows.length) return null;
  return el(
    "div",
    {},
    el("h3", { text: title }),
    el(
      "table",
      {},
      ...rows.map((row) =>
        el(
          "tr",
          { class: onclick ? "link" : "", onclick: onclick ? () => onclick(row) : undefined },
          el("td", { text: label(row) }),
          el("td", { class: "num", text: `${row.calls} calls` }),
          el("td", { class: "num", text: `${row.credits.toFixed(1)} credits` }),
        ),
      ),
    ),
  );
}

async function showUsage(range) {
  const form = $("#usage-dialog form");
  let since;
  let until = Date.now() / 1000 + 1;
  if (range === "today") since = startOfDay(new Date());
  else if (range === "custom") {
    if (!form.from.value) return;
    since = startOfDay(new Date(`${form.from.value}T00:00`));
    if (form.to.value) until = startOfDay(new Date(`${form.to.value}T00:00`)) + 86400;
  } else since = Date.now() / 1000 - Number(range) * 86400;
  for (const button of form.querySelectorAll("[data-range]")) button.classList.toggle("active", button.dataset.range === range);
  try {
    const usage = await api(`/api/usage?since=${since}&until=${until}`);
    const total = usage.total;
    $("#usage-total").replaceChildren(
      el("strong", { text: `${total.credits.toFixed(1)} credits` }),
      el("span", { text: `${total.premium.toFixed(0)} premium requests` }),
      el("span", { text: `${total.calls} model calls` }),
      el("span", { text: `${Math.round((total.input_tokens + total.output_tokens) / 1000)}k tokens` }),
    );
    const roles = { router: "Router", slack: "Slack", worker: "Workers", watchdog: "Watchdog" };
    $("#usage-tables").replaceChildren(
      ...[
        usageTable("By session", usage.by_session, (row) => `${row.title || "Deleted session"}${row.closed_at ? " (closed)" : ""}`, (row) => {
          $("#usage-dialog").close();
          select(row.session_id);
        }),
        usageTable("By model", usage.by_model, (row) => row.model || "unknown"),
        usageTable("By role", usage.by_role, (row) => roles[row.role] || row.role),
      ].filter(Boolean),
    );
    if (!total.calls) $("#usage-tables").replaceChildren(el("div", { class: "line", text: "No model calls in this range." }));
  } catch (error) {
    toast(error.message, "error");
  }
}

// Toasts

function toast(text, kind = "", sessionId = null) {
  const node = el("div", {
    class: `toast ${kind}`,
    text,
    onclick: () => {
      if (sessionId && byId(sessionId)) select(sessionId);
      node.remove();
    },
  });
  $("#toasts").append(node);
  setTimeout(() => node.remove(), kind === "attention" ? 12000 : 6000);
}

// Settings

function lines(value) {
  return value.split("\n").map((line) => line.trim()).filter(Boolean);
}

function slackStatusText() {
  const slack = state.snapshot.slack;
  const parts = [slack.token_saved ? "Token saved." : "No token yet."];
  if (slack.error) parts.push(slack.error);
  else if (slack.connected) parts.push(`Watching${slack.last_checked ? `, checked ${ago(slack.last_checked)} ago` : ""}.`);
  if (slack.token_saved && slack.connected && !slack.can_send) parts.push("This token cannot send messages (chat:write).");
  return parts.join(" ");
}

function fillSettings() {
  const form = $("#settings-form");
  const settings = state.snapshot.settings;
  form.projects.value = settings.projects.join("\n");
  form.scratch_directory.value = settings.scratch_directory;
  form.scratch_directory.placeholder = `${state.snapshot.data_directory}/work/workspaces`;
  form.max_workers.value = settings.max_workers;
  form.worker_yolo.checked = settings.worker_yolo;
  form.worker_idle_hours.value = settings.worker_idle_hours;
  form.mac_notifications.checked = settings.mac_notifications;
  form.watchdog_enabled.checked = settings.watchdog_enabled;
  form.slack_enabled.checked = settings.slack_enabled;
  form.slack_token.value = "";
  form.slack_poll_seconds.value = settings.slack_poll_seconds;
  form.github_enabled.checked = settings.github_enabled;
  form.github_repos.value = settings.github_repos.join("\n");
  form.copilot_command.value = settings.copilot_command;
  form.model.value = settings.model;
  for (const role of ["router", "worker", "slack", "watchdog"]) {
    form[`model_${role}`].value = settings.role_models[role] || "";
    const select = form[`effort_${role}`];
    select.replaceChildren(
      el("option", { value: "", text: "Default" }),
      ...["low", "medium", "high", "xhigh", "max"].map((level) => el("option", { value: level, text: EFFORTS[level] })),
    );
    select.value = settings.role_efforts[role] || "";
  }
  form.worker_credit_limit.value = settings.worker_credit_limit || "";
  form.digest_time.value = settings.digest_time;
  form.profile.value = state.snapshot.profile || "";
  form.setup_notes.value = notesText();
  form.setup_notes.dataset.initial = form.setup_notes.value;
  $("#profile-draft").hidden = !state.snapshot.profile_draft;
  form.paused.checked = settings.paused;
  $("#slack-status").textContent = slackStatusText();
  $("#slack-token-remove").hidden = !state.snapshot.slack.token_saved;
  const github = state.snapshot.github;
  $("#github-status").textContent = github.error || (github.connected ? `Watching, checked ${ago(github.last_checked)} ago.` : "");
  $("#data-directory").textContent = `Data: ${state.snapshot.data_directory}`;
  $("#settings-error").hidden = true;
}

function notesText() {
  return Object.entries(state.snapshot.setup_notes || {})
    .map(([topic, entry]) => `${topic}: ${entry.note}`)
    .join("\n");
}

function settingsError(message) {
  const node = $("#settings-error");
  node.textContent = message;
  node.hidden = !message;
}

async function saveSettings(event) {
  event.preventDefault();
  const form = $("#settings-form");
  const body = {
    projects: lines(form.projects.value),
    scratch_directory: form.scratch_directory.value.trim(),
    max_workers: Number(form.max_workers.value),
    worker_yolo: form.worker_yolo.checked,
    worker_idle_hours: Number(form.worker_idle_hours.value || 24),
    mac_notifications: form.mac_notifications.checked,
    watchdog_enabled: form.watchdog_enabled.checked,
    slack_enabled: form.slack_enabled.checked,
    slack_poll_seconds: Number(form.slack_poll_seconds.value),
    github_enabled: form.github_enabled.checked,
    github_repos: lines(form.github_repos.value),
    copilot_command: form.copilot_command.value.trim(),
    model: form.model.value.trim(),
    role_models: Object.fromEntries(
      ["router", "worker", "slack", "watchdog"].map((role) => [role, form[`model_${role}`].value.trim()]).filter(([, value]) => value),
    ),
    role_efforts: Object.fromEntries(
      ["router", "worker", "slack", "watchdog"].map((role) => [role, form[`effort_${role}`].value]).filter(([, value]) => value),
    ),
    worker_credit_limit: Number(form.worker_credit_limit.value || 0),
    digest_time: form.digest_time.value,
    paused: form.paused.checked,
  };
  try {
    if (form.slack_token.value.trim()) await saveToken();
    let snapshot = await api("/api/settings", { method: "POST", body });
    const initialNotes = form.setup_notes.dataset.initial || "";
    if (form.setup_notes.value.trim() !== initialNotes.trim()) {
      snapshot = await api("/api/setup-notes", { method: "POST", body: { text: form.setup_notes.value, initial: initialNotes } });
    }
    if (form.profile.value.trim() !== (state.snapshot.profile || "")) {
      snapshot = await api("/api/profile", { method: "POST", body: { text: form.profile.value } });
    }
    state.snapshot = { ...state.snapshot, ...snapshot };
    $("#settings-dialog").close();
    renderSidebar();
    renderHeader();
  } catch (error) {
    settingsError(error.message);
  }
}

async function saveToken() {
  const form = $("#settings-form");
  const snapshot = await api("/api/slack/token", { method: "POST", body: { token: form.slack_token.value.trim() } });
  state.snapshot = { ...state.snapshot, ...snapshot };
  form.slack_token.value = "";
  $("#slack-status").textContent = slackStatusText();
  $("#slack-token-remove").hidden = false;
}

// Wiring

function bindUi() {
  const input = $("#composer-input");
  const autosize = () => {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("#composer").requestSubmit();
    }
  });
  $("#composer").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    const sessionId = state.selected;
    if (!text || !current()) return;
    input.value = "";
    autosize();
    try {
      appendMessages([await api(`/api/sessions/${sessionId}/messages`, { method: "POST", body: { text } })]);
    } catch (error) {
      input.value = text;
      autosize();
      toast(error.message, "error");
    }
  });
  $("#timeline").addEventListener("click", (event) => {
    const chip = event.target.closest(".path");
    if (!chip) return;
    navigator.clipboard.writeText(chip.dataset.path).then(
      () => toast("Path copied"),
      () => toast(chip.dataset.path),
    );
  });
  $("#settings-button").addEventListener("click", () => {
    fillSettings();
    $("#settings-dialog").showModal();
  });
  $("#settings-cancel").addEventListener("click", () => $("#settings-dialog").close());
  $("#profile-use").addEventListener("click", () => {
    $("#settings-form").profile.value = state.snapshot.profile_draft;
    $("#profile-draft").hidden = true;
    settingsError("Review the draft, edit it if needed, then Save to use it.");
  });
  $("#profile-discard").addEventListener("click", async () => {
    state.snapshot = { ...state.snapshot, ...(await api("/api/profile/discard", { method: "POST" })) };
    $("#profile-draft").hidden = true;
  });
  $("#profile-distill").addEventListener("click", async () => {
    try {
      await api("/api/profile/distill", { method: "POST" });
      $("#settings-dialog").close();
      await select(routerId());
      toast("The Router is building your profile. You review it before it is used.");
    } catch (error) {
      settingsError(error.message);
    }
  });
  $("#usage-button").addEventListener("click", () => {
    $("#usage-dialog").showModal();
    showUsage("today");
  });
  for (const button of document.querySelectorAll("#usage-dialog [data-range]")) {
    button.addEventListener("click", () => showUsage(button.dataset.range));
  }
  for (const input of document.querySelectorAll("#usage-dialog input[type=date]")) {
    input.addEventListener("change", () => showUsage("custom"));
  }
  setInterval(refreshUsageLink, 60000);
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#slack-token-save").addEventListener("click", async () => {
    try {
      settingsError("");
      await saveToken();
    } catch (error) {
      settingsError(error.message);
    }
  });
  $("#slack-token-remove").addEventListener("click", async () => {
    try {
      const snapshot = await api("/api/slack/token", { method: "DELETE" });
      state.snapshot = { ...state.snapshot, ...snapshot };
      $("#slack-status").textContent = slackStatusText();
      $("#slack-token-remove").hidden = true;
    } catch (error) {
      settingsError(error.message);
    }
  });
  $("#external").addEventListener("toggle", (event) => {
    if (event.target.open) loadExternal(true);
  });
  window.addEventListener("focus", () => {
    const session = byId(state.selected);
    if (session?.unread) markRead(session.id);
  });
  setInterval(() => {
    if ($("#external").open) loadExternal();
  }, 60000);
}

async function boot() {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (token) {
    await fetch("/api/auth", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
    history.replaceState(null, "", "/");
  }
  try {
    await loadState();
  } catch {
    return;
  }
  $("#application").hidden = false;
  bindUi();
  connectEvents();
  loadModels();
  refreshUsageLink();
  await select(state.selected || routerId());
}

boot();
