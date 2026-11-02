// The inbox: what needs the owner, what is running, and results ready to review.

import { $, api, el, toast } from "./dom.js";
import { compactPaths, markdown, plural, shortPath, since } from "./text.js";
import { byId, loadState, pendingDrafts, routerId, state } from "./state.js";
import { renderSidebar, renderStatusLine } from "./sidebar.js";
import { select } from "./session.js";
import { draftCard } from "./drafts.js";
export const INBOX = "inbox";
const tasks = () => state.snapshot?.tasks || [];
export const uncertainDrafts = () => state.snapshot?.uncertain || [];
const TASK_LABEL = { queued: "Queued", running: "Running", waiting: "Needs you", review: "Ready to review", done: "Done", failed: "Failed", cancelled: "Cancelled" };
const TASK_CHIP = { queued: "", running: "progress", waiting: "waiting", review: "done", done: "done", failed: "failed", cancelled: "" };
function needsYou() {
  const waiting = tasks().filter((task) => task.status === "waiting" || task.status === "failed" || task.blocked);
  return { drafts: pendingDrafts(), uncertain: uncertainDrafts(), tasks: waiting };
}
function needsYouCount() {
  const items = needsYou();
  return items.drafts.length + items.uncertain.length + items.tasks.length;
}
function inboxCounts() {
  const running = tasks().filter((task) => (task.status === "running" || task.status === "queued") && !task.blocked).length;
  const review = tasks().filter((task) => task.status === "review").length;
  return { needs: needsYouCount(), running, review };
}
export function inboxItem() {
  const count = needsYouCount();
  return el(
    "button",
    { type: "button", class: `item inbox-item${state.selected === INBOX ? " selected" : ""}`, onclick: () => select(INBOX) },
    el("span", { class: `dot${count ? " waiting" : ""}`, title: count ? "Something needs you" : "Nothing needs you" }),
    el("span", { class: "label", text: "Inbox" }),
    count ? el("span", { class: "badge amber", text: String(count), title: "Needs you" }) : null,
  );
}
export function showInbox() {
  state.selected = INBOX;
  localStorage.setItem("cos.selected", INBOX);
  $("#inbox").hidden = false;
  $("#timeline").hidden = true;
  $("#drafts").hidden = true;
  $("#session-tuning").hidden = true;
  $("#composer").hidden = false;
  $("#composer-input").disabled = false;
  $("#composer-input").placeholder = "Ask your chief of staff…";
  renderSidebar();
  renderInboxHeader();
  renderInbox();
  $("#scroller").scrollTop = 0;
  $("#composer-input").focus();
}
function focusUntil() {
  const until = state.snapshot?.focus?.until;
  return until && new Date(until) > new Date() ? new Date(until) : null;
}
export function renderInboxHeader() {
  const { needs, running, review } = inboxCounts();
  const until = focusUntil();
  $("#session-title").textContent = "Inbox";
  const parts = [
    until ? `Focusing until ${until.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "",
    needs ? `${needs} need${needs === 1 ? "s" : ""} you` : "Nothing needs you",
    running ? `${running} running` : "",
    review ? `${review} to review` : "",
  ];
  $("#session-meta").textContent = parts.filter(Boolean).join(" · ");
  $("#session-meta").title = "";
  $("#session-meta").classList.remove("yolo");
  $("#session-actions").replaceChildren(state.snapshot.runtime.state === "ready" ? focusControl() : "");
}
// Focus: notifications wait, the Router handles what it can, one summary at the end.
function focusControl() {
  const focus = (minutes) =>
    api("/api/focus", { method: "POST", body: { minutes } })
      .then((focusState) => {
        state.snapshot.focus = focusState;
        renderInboxHeader();
        renderStatusLine();
        toast(minutes ? "Focusing: notifications wait until you are done." : "Focus ended.");
      })
      .catch((error) => toast(error.message, "error"));
  if (focusUntil()) {
    return el("button", { type: "button", class: "focus-on", text: "End focus", title: "Notifications are held; the summary comes when focus ends", onclick: () => focus(0) });
  }
  const select = el(
    "select",
    { class: "folder", "aria-label": "Focus", title: "Hold notifications while you focus; the Router handles what it can" },
    el("option", { value: "", text: "Focus…" }),
    ...[
      [30, "30 minutes"],
      [60, "1 hour"],
      [120, "2 hours"],
      [240, "4 hours"],
    ].map(([value, text]) => el("option", { value, text: `Focus for ${text}` })),
  );
  select.addEventListener("change", () => select.value && focus(Number(select.value)));
  return select;
}
let inboxFrame = 0;
export function scheduleInbox() {
  if (state.selected !== INBOX || inboxFrame) return;
  inboxFrame = requestAnimationFrame(() => {
    inboxFrame = 0;
    renderInboxHeader();
    renderInbox();
  });
}
// Keeps nodes whose key did not change; `refresh` updates their content when only their
// `version` did, so a button under the mouse is never swapped out mid-click. A card whose key
// changed is rebuilt, and a form the owner has open on it (a half-typed note) moves into the
// new card, with its text and focus, even when the card moves to another section.
function reconcile(container, items, editing = openForms()) {
  const existing = new Map([...container.children].map((node) => [node.dataset.key, node]));
  const nodes = items.map(({ key, id, version, build, refresh }) => {
    let node = existing.get(key);
    if (node && refresh && node.dataset.version !== version) refresh(node);
    if (!node) {
      node = build();
      const form = id && editing.get(id);
      if (form) {
        const fresh = node.querySelector(":scope > .actions");
        if (fresh) fresh.replaceWith(form);
        else node.append(form);
      }
    }
    node.dataset.key = key;
    if (id) node.dataset.id = id;
    if (version !== undefined) node.dataset.version = version;
    return node;
  });
  if (nodes.length !== container.children.length || nodes.some((node, index) => container.children[index] !== node)) {
    container.replaceChildren(...nodes);
  }
}
// Moving a focused field in the page blurs it: put the focus and cursor back afterwards.
function keepingFocus(change) {
  const active = document.activeElement;
  const selection = active && "selectionStart" in active ? [active.selectionStart, active.selectionEnd] : null;
  change();
  if (active?.isConnected && document.activeElement !== active) {
    active.focus();
    if (selection) active.setSelectionRange(...selection);
  }
}
// Forms the owner has open on inbox cards, by task id; collected before any section changes.
function openForms() {
  return new Map([...document.querySelectorAll("#inbox .card > .actions[data-editing]")].map((form) => [form.parentElement.dataset.id, form]));
}
function section(id, title, count, hint) {
  let node = document.getElementById(id);
  if (!node) {
    node = el("section", { id, class: "inbox-section" }, el("h2", {}, el("span", { class: "title" }), el("span", { class: "count" })), el("div", { class: "items" }), el("p", { class: "empty-hint" }));
  }
  node.querySelector(".title").textContent = title;
  node.querySelector(".count").textContent = count ? String(count) : "";
  node.querySelector(".empty-hint").textContent = count ? "" : hint;
  return node;
}
function renderInbox() {
  if (state.selected !== INBOX || !state.snapshot) return;
  keepingFocus(updateInbox);
}
function updateInbox() {
  const box = $("#inbox");
  const items = needsYou();
  const editing = openForms();
  // A worker waiting on a card for the owner shows as that card, under Needs you.
  const asking = new Set(items.drafts.map((draft) => draft.session_id));
  const running = tasks()
    .filter((task) => (task.status === "running" || task.status === "queued") && !task.blocked && !asking.has(task.session_id))
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  const review = tasks().filter((task) => task.status === "review").sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  const finished = tasks().filter((task) => task.status === "done" || task.status === "cancelled").sort((a, b) => (b.ended_at || "").localeCompare(a.ended_at || ""));

  const needs = section("inbox-needs", "Needs you", items.drafts.length + items.uncertain.length + items.tasks.length, "Nothing needs you right now.");
  reconcile(needs.querySelector(".items"), [
    ...items.drafts.map((draft) => ({ key: `draft:${draft.id}`, build: () => draftCard(draft) })),
    ...items.uncertain.map((draft) => ({ key: `uncertain:${draft.id}:${draft.outcome}`, build: () => uncertainCard(draft) })),
    ...items.tasks.map((task) => taskItem(task)),
  ], editing);
  const runningSection = section("inbox-running", "Running", running.length, "");
  runningSection.hidden = !running.length;
  reconcile(runningSection.querySelector(".items"), running.map((task) => taskItem(task)), editing);
  const reviewSection = section("inbox-review", "Ready to review", review.length, "Finished work shows up here with what was observed: tests, changes and pull requests.");
  reconcile(reviewSection.querySelector(".items"), review.map((task) => taskItem(task)), editing);

  let recent = document.getElementById("inbox-recent");
  if (!recent) {
    recent = el("details", { id: "inbox-recent", class: "inbox-section recent" }, el("summary"), el("div", { class: "items" }));
  }
  recent.hidden = !finished.length;
  recent.querySelector("summary").textContent = `Recently finished · ${finished.length}`;
  reconcile(recent.querySelector(".items"), finished.slice(0, 30).map((task) => taskItem(task)), editing);

  const first = !tasks().length && !items.drafts.length && !items.uncertain.length;
  const welcome = first ? document.getElementById("inbox-welcome") || welcomeCard() : null;
  const blocked = state.snapshot.runtime.state === "error";  // Only the way to fix it matters.
  const order = blocked ? [setupCard()] : [setupCard(), focusSummaryCard(), chiefStrip(), welcome, needs, runningSection, reviewSection, recent].filter(Boolean);
  if (order.some((node, index) => box.children[index] !== node) || box.children.length !== order.length) box.replaceChildren(...order);
}
// The key covers what changes a card's actions and layout; the version, what only its text.
function taskItem(task) {
  const worker = byId(task.session_id);
  return {
    // Receipts show on finished cards only; a running task's grow with every command.
    key: `task:${task.id}:${task.status}:${task.blocked ? 1 : 0}:${["review", "done"].includes(task.status) ? JSON.stringify(task.receipts || null) : ""}`,
    id: task.id,
    version: `${task.updated_at}:${task.title}:${worker?.status || ""}:${worker?.activity || ""}:${task.credits}:${task.runs}`,
    build: () => taskCard(task),
    refresh: (node) => refreshTaskCard(node, task),
  };
}
function refreshTaskCard(node, task) {
  const worker = byId(task.session_id);
  const title = node.querySelector(".head .title");
  title.textContent = task.title;
  title.title = task.goal;
  node.querySelector(".head .meta").textContent = taskMeta(task);
  const body = taskBody(task, worker);
  const old = node.querySelector(":scope > .task-body");
  if (old && body) old.replaceWith(body);
  else if (old) old.remove();
  else if (body) node.querySelector(":scope > .head").after(body);
}
function taskMeta(task) {
  const finishedAt = ["review", "done", "failed", "cancelled"].includes(task.status) ? task.ended_at || task.updated_at : "";
  return [task.worker && task.worker !== task.title ? task.worker : "", finishedAt ? `finished ${since(finishedAt)}` : `started ${since(task.created_at)}`, task.credits >= 0.05 ? `${task.credits.toFixed(1)} credits` : "", task.runs > 1 ? `${task.runs} runs` : ""].filter(Boolean).join(" · ");
}
// First run: what is missing before work can start.
const NO_PROJECTS = "cos.noProjectsCard";  // The owner chose to work without projects for now.

function setupCard() {
  const snap = state.snapshot;
  const key = `${snap.runtime.state}:${snap.runtime.error}:${snap.settings.projects.length}`;
  let node = document.getElementById("inbox-setup");
  if (node?.dataset.key === key) return node;
  if (snap.runtime.state === "error") {
    const retry = el("button", { type: "button", text: "Retry", onclick: async () => {
      retry.disabled = true;
      try {
        await api("/api/runtime/reconnect", { method: "POST" });
      } catch (error) {
        toast(error.message, "error");
      }
      retry.disabled = false;
    } });
    node = el(
      "div",
      { id: "inbox-setup", class: "card setup" },
      el("div", { class: "head" }, el("span", { class: "label", text: "Connect GitHub Copilot" })),
      el("p", { text: "Chief of Staff runs your work in GitHub Copilot sessions, and Copilot is not ready yet:" }),
      el("p", { class: "muted", text: snap.runtime.error }),
      el(
        "ol",
        {},
        el("li", {}, "Install the Copilot CLI: ", el("code", { text: "npm install -g @github/copilot" })),
        el("li", {}, "Sign in: ", el("code", { text: "copilot login" })),
        el("li", { text: "Retry here." }),
      ),
      el("div", { class: "actions" }, el("span", { class: "muted", text: "Or look around first: chiefofstaff --demo" }), retry),
    );
  } else if (snap.runtime.state === "ready" && !snap.demo && !snap.settings.projects.length && localStorage.getItem(NO_PROJECTS) !== "1") {
    const input = el("input", { placeholder: "~/code/my-app", autocomplete: "off" });
    const error = el("span", { class: "error" });
    const add = async () => {
      if (!input.value.trim()) return;
      error.textContent = "";
      try {
        const { path } = await api("/api/projects", { method: "POST", body: { path: input.value.trim() } });
        toast(`Added ${shortPath(path)}.`);
        await loadState();
      } catch (failure) {
        error.textContent = failure.message;
      }
    };
    input.addEventListener("keydown", (event) => event.key === "Enter" && add());
    node = el(
      "div",
      { id: "inbox-setup", class: "card setup" },
      el("div", { class: "head" }, el("span", { class: "label", text: "Add a project folder" })),
      el("p", { text: "Workers change code in their own copies (git worktrees) of these folders; your own checkout stays as it is. Research and questions need no project." }),
      el(
        "div",
        { class: "actions" },
        input,
        error,
        el("button", { type: "button", class: "text", text: "Not now", onclick: () => {
          localStorage.setItem(NO_PROJECTS, "1");
          node.remove();
          toast("Add projects any time in Settings.");
        } }),
        el("button", { type: "button", text: "Add", onclick: add }),
      ),
    );
  } else {
    return null;
  }
  node.dataset.key = key;
  return node;
}
function focusSummaryCard() {
  if (!state.focusSummary) return null;
  let node = document.getElementById("focus-summary");
  if (node?.dataset.text === state.focusSummary) return node;
  node = el(
    "div",
    { id: "focus-summary", class: "card update" },
    el("div", { class: "head" }, el("span", { class: "label", text: "Focus is over" }), el("span", { class: "spacer" }), el("button", { type: "button", class: "text", text: "×", title: "Dismiss", onclick: () => { state.focusSummary = null; node.remove(); } })),
    el("div", { class: "body", text: state.focusSummary }),
  );
  node.dataset.text = state.focusSummary;
  return node;
}
function welcomeCard() {
  const examples = [
    "Fix the flaky login test in the api repo and open a PR",
    "Research how other teams use feature flags and summarize the options",
    "Watch for review requests on my repos and draft reviews for me",
  ];
  return el(
    "div",
    { id: "inbox-welcome", class: "welcome" },
    el("h2", { text: "Give your chief of staff some work" }),
    el("p", { text: "Say what you need below, several things at once if you like. Each task gets its own worker in its own folder; you approve anything sent in your name, and finished work waits here for your review." }),
    el("div", { class: "examples" }, ...examples.map((text) => el("button", { type: "button", class: "secondary", text, onclick: () => { $("#composer-input").value = text; $("#composer-input").focus(); } }))),
  );
}
// The latest exchange with the Router, so a request from the inbox gets its answer here too.
function chiefStrip() {
  const chief = state.chief;
  if (!chief) return null;
  let node = document.getElementById("chief-strip");
  if (!node) node = el("div", { id: "chief-strip", class: "card chief" });
  const router = byId(routerId());
  const replies = chief.replies.slice(-2);
  node.replaceChildren(
    el(
      "div",
      { class: "head" },
      el("span", { class: "label", text: "You asked" }),
      el("span", { class: "spacer" }),
      el("button", { type: "button", class: "open", text: "Open chat", onclick: () => select(routerId()) }),
      el("button", { type: "button", class: "text", text: "×", title: "Hide", onclick: () => { state.chief = null; node.remove(); } }),
    ),
    el("div", { class: "asked", text: chief.asked }),
    ...replies.map((reply) => el("div", { class: "reply markdown", html: markdown(reply) })),
    !replies.length || router?.status === "working" ? el("div", { class: "working-line", text: router?.status === "working" ? "Working on it…" : "Sent." }) : null,
  );
  return node;
}
function taskCard(task) {
  const worker = byId(task.session_id);
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  const meta = taskMeta(task);
  const node = el(
    "div",
    { class: `card task ${task.status}${task.blocked ? " blocked" : ""}` },
    el(
      "div",
      { class: "head" },
      el("span", { class: "label title", text: task.title, title: task.goal }),
      el("span", { class: `chip ${task.blocked ? "blocked" : TASK_CHIP[task.status]}`, text: task.blocked ? "Blocked" : TASK_LABEL[task.status] }),
      el("span", { class: "spacer" }),
      el("span", { class: "meta", text: meta }),
      task.session_id ? el("button", { type: "button", class: "open", text: "Open", onclick: () => select(task.session_id) }) : null,
    ),
  );
  const body = taskBody(task, worker);
  if (body) node.append(body);
  if (task.receipts && (task.status === "review" || task.status === "done")) node.append(receiptsView(task.receipts));

  async function act(path, body, done) {
    for (const button of actions.querySelectorAll("button")) button.disabled = true;
    error.textContent = "";
    try {
      const view = await api(path, { method: "POST", body: body || {} });
      showActions();  // The form is done: the card may be rebuilt from now on.
      upsertTask(view);
      if (done) toast(done);
    } catch (failure) {
      error.textContent = failure.message;
      for (const button of actions.querySelectorAll("button")) button.disabled = false;
    }
  }
  function reply(label, placeholder) {
    const input = el("textarea", { rows: 2, placeholder });
    const send = () => input.value.trim() && act(`/api/tasks/${task.id}/follow-up`, { text: input.value.trim() }, "Sent to the worker.");
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        send();
      }
    });
    actions.replaceChildren(input, el("button", { type: "button", class: "text", text: "Cancel", onclick: showActions }), el("button", { type: "button", text: label, onclick: send }));
    actions.dataset.editing = "1";
    input.focus();
  }
  function confirmCancel() {
    const reason = el("input", { placeholder: "Why? (optional)" });
    actions.replaceChildren(reason, el("button", { type: "button", class: "text", text: "Keep it", onclick: showActions }), el("button", { type: "button", class: "danger", text: "Cancel task", onclick: () => act(`/api/tasks/${task.id}/cancel`, { text: reason.value.trim() }, "Task cancelled.") }));
    actions.dataset.editing = "1";
    reason.focus();
  }
  function showActions() {
    delete actions.dataset.editing;
    const host = actions.parentElement;
    if (host && host !== node) {
      // This form moved into a newer card for the task: that card shows its own actions.
      actions.remove();
      delete host.dataset.key;
      scheduleInbox();
      return;
    }
    const buttons = [];
    if (task.status === "review") {
      buttons.push(
        el("button", { type: "button", class: "text", text: "Copy report", onclick: () => copyReport(task) }),
        el("button", { type: "button", class: "secondary", text: "Follow up", onclick: () => reply("Send", "What else should it do?") }),
        el("button", { type: "button", text: "Accept", onclick: () => act(`/api/tasks/${task.id}/accept`) }),
      );
    } else if (task.status === "waiting") {
      buttons.push(
        el("button", { type: "button", class: "text", text: "Cancel task", onclick: confirmCancel }),
        el("button", { type: "button", text: "Reply", onclick: () => reply("Send", "Your answer") }),
      );
    } else if (task.status === "failed") {
      buttons.push(
        el("button", { type: "button", class: "text", text: "Cancel task", onclick: confirmCancel }),
        el("button", { type: "button", class: "secondary", text: "Retry with a note", onclick: () => retryWithNote() }),
        el("button", { type: "button", text: "Retry", onclick: () => act(`/api/tasks/${task.id}/retry`, {}, "Retrying.") }),
      );
    } else if (task.status === "running" || task.status === "queued") {
      buttons.push(
        el("button", { type: "button", class: "text", text: "Cancel", onclick: confirmCancel }),
        el("button", { type: "button", class: "text", text: "Add a note", onclick: () => reply("Send", "Tell the worker now (it reads it within its current turn)") }),
      );
    } else if (task.status === "done") {
      buttons.push(el("button", { type: "button", class: "text", text: "Copy report", onclick: () => copyReport(task) }));
    }
    actions.replaceChildren(error, ...buttons);
  }
  function retryWithNote() {
    const input = el("textarea", { rows: 2, placeholder: "What should it do differently?" });
    actions.replaceChildren(input, el("button", { type: "button", class: "text", text: "Cancel", onclick: showActions }), el("button", { type: "button", text: "Retry", onclick: () => act(`/api/tasks/${task.id}/retry`, { text: input.value.trim() }, "Retrying.") }));
    actions.dataset.editing = "1";
    input.focus();
  }
  showActions();
  if (actions.children.length > 1) node.append(actions);
  return node;
}
function taskBody(task, worker) {
  if (task.status === "running" || task.status === "queued") {
    const line = task.status === "queued" ? worker?.activity || "Waiting for a free worker slot" : worker?.activity || (worker?.status === "working" ? "Working…" : "Between turns");
    const command = line.replace(/^(bash|shell|powershell) /, "$ ");
    const nodes = [el("div", { class: "activity", text: compactPaths(command, worker?.cwd) })];
    if (task.progress) nodes.push(el("div", { class: "progress markdown", html: markdown(task.progress) }));
    return el("div", { class: "task-body" }, ...nodes);
  }
  const text = task.status === "review" || task.status === "done" ? task.result || task.progress : task.progress || (task.blocked ? "It waits for a task that ended without finishing." : "");
  if (!text) return null;
  const content = el("div", { class: "body markdown", html: markdown(text) });
  if (text.length > 700 || text.split("\n").length > 10) content.classList.add("folded");
  const wrapper = el("div", { class: "task-body" }, content);
  if (content.classList.contains("folded")) {
    const toggle = el("button", { type: "button", class: "more", text: "Show more" });
    toggle.addEventListener("click", () => {
      const folded = content.classList.toggle("folded");
      toggle.textContent = folded ? "Show more" : "Show less";
    });
    wrapper.append(toggle);
  }
  return wrapper;
}
// What was observed, next to what the worker reported.
function receiptsView(receipts) {
  const items = [];
  const result = (label, item) => {
    if (!item) return;
    items.push(el("span", { class: `receipt ${item.passed ? "pass" : "fail"}`, title: `${item.command}\n(${plural(item.runs, "run")})`, text: `${item.passed ? "✓" : "✗"} ${label}` }));
  };
  result("tests", receipts.tests);
  result("checks", receipts.checks);
  result("build", receipts.build);
  const changed = receipts.changed || {};
  if (changed.files) items.push(el("span", { class: "receipt", text: `+${changed.insertions} −${changed.deletions} in ${plural(changed.files, "file")}` }));
  if (changed.commits) items.push(el("span", { class: "receipt", text: plural(changed.commits, "commit") }));
  if (changed.uncommitted) items.push(el("span", { class: "receipt warn", text: `${changed.uncommitted} uncommitted` }));
  for (const pr of receipts.prs || []) {
    const checks = { passing: "✓", failing: "✗", pending: "…" }[pr.checks] || "";
    items.push(el("a", { class: `receipt pr ${pr.checks}`, href: pr.url, target: "_blank", rel: "noreferrer", text: `${pr.label}${pr.state && pr.state !== "OPEN" ? ` (${pr.state.toLowerCase()})` : ""} ${checks}`.trim() }));
  }
  if (!items.length) items.push(el("span", { class: "receipt none", text: receipts.observed ? `${plural(receipts.observed, "command")}, no tests or changes observed` : "Nothing observed" }));
  const view = el("div", { class: "receipts" }, el("span", { class: "observed", text: "Observed" }), ...items);
  const flags = (receipts.flags || []).map((flag) => el("div", { class: "flag", text: flag }));
  return el("div", { class: "evidence" }, view, ...flags);
}
async function copyReport(task) {
  const text = api(`/api/tasks/${task.id}/report`).then((report) => report.markdown);
  text.catch(() => {});
  try {
    // Started within the click, so the browser still counts it as the owner's action.
    if (window.ClipboardItem && navigator.clipboard?.write) {
      const blob = text.then((value) => new Blob([value], { type: "text/plain" }));
      await navigator.clipboard.write([new ClipboardItem({ "text/plain": blob })]);
    } else {
      await navigator.clipboard.writeText(await text);
    }
    toast("Report copied: paste it into a PR description or a message.");
  } catch {
    try {
      showCopy(await text);
    } catch (error) {
      toast(error.message, "error");
    }
  }
}
// Where the clipboard is not available: the text, selected, to copy by hand.
function showCopy(text) {
  const area = el("textarea", { class: "copy-area", rows: 14, readonly: true });
  area.value = text;
  const dialog = el("dialog", { class: "copy-dialog" }, el("p", { text: "Copy the report:" }), area, el("form", { method: "dialog" }, el("button", { type: "submit", text: "Done" })));
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  area.select();
}
// A draft whose sending a stop interrupted, and that could not be confirmed.
function uncertainCard(draft) {
  const target = draft.target;
  const where = draft.kind === "slack" ? `Slack reply to ${target.to || target.channel_id}` : `PR review on ${target.repo}#${target.number}`;
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  async function decide(body) {
    for (const button of actions.querySelectorAll("button")) button.disabled = true;
    try {
      await api(`/api/drafts/${draft.id}`, { method: "POST", body });
    } catch (failure) {
      error.textContent = failure.message;
      for (const button of actions.querySelectorAll("button")) button.disabled = false;
    }
  }
  actions.append(
    error,
    el("button", { type: "button", class: "text", text: "Drop it", onclick: () => decide({ action: "reject" }) }),
    el("button", { type: "button", class: "secondary", text: "It was sent", onclick: () => decide({ action: "mark_sent" }) }),
    el("button", { type: "button", text: "Send again", onclick: () => decide({ action: "send", text: draft.final_text || draft.text }) }),
  );
  return el(
    "div",
    { class: "card draft uncertain" },
    el("div", { class: "head" }, el("span", { class: "label", text: where }), el("span", { class: "chip waiting", text: "Sent?" })),
    el("div", { class: "note", text: draft.outcome || "Chief of Staff stopped while sending this. Check whether it arrived." }),
    el("div", { class: "body quoted", text: draft.final_text || draft.text }),
    actions,
  );
}
export function upsertTask(task) {
  if (!state.snapshot) return;
  const list = tasks().filter((item) => item.id !== task.id);
  list.push(task);
  state.snapshot.tasks = list;
  scheduleInbox();
  renderSidebar();
}
