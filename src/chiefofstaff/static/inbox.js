// Home: the conversation with the Router, and beside it what needs the owner and every task.

import { $, api, el, keepingFocus, moreMenu, reconcile, submits, toast } from "./dom.js";
import { markdown, shortPath } from "./text.js";
import { byId, DOT_HELP, HOME, loadState, ownerQuestions, pendingDrafts, routerId, sessions, state, uncertainDrafts } from "./state.js";
import { renderStatusLine } from "./sidebar.js";
import { renderHeader, scrollToBottom, select } from "./session.js";
import { draftCard, draftKey, uncertainCard } from "./drafts.js";
import { expandTask, followUpItem, inTerminal, ownTasks, taskItem, terminalItem, workerGroupItem } from "./tasks.js";
// What waits for the owner: cards to approve or answer, tasks waiting for an answer or failed,
// and a worker's question whose task does not show it (cancelled, finished, or none). A task
// waiting on a decision card shows as that card.
export function needsYou() {
  const drafts = pendingDrafts();
  const deciding = new Set(drafts.filter((draft) => draft.kind === "decision").flatMap((draft) => draft.target.askers.map((asker) => asker.task_id)));
  const waiting = ownTasks().filter((task) => (task.status === "waiting" && !deciding.has(task.id)) || task.status === "failed" || task.blocked);
  const shownTasks = new Set(waiting.map((task) => task.id));
  const questions = ownerQuestions().filter((question) => !shownTasks.has(question.task_id));
  return { drafts, uncertain: uncertainDrafts(), tasks: waiting, questions };
}
// What the badge counts: not the cards the owner just acted on.
export function needsCount() {
  const items = needsYou();
  const shown = (key) => !state.leaving.has(key);
  return items.drafts.filter((draft) => shown(`draft:${draft.id}`)).length + items.uncertain.filter((draft) => shown(`draft:${draft.id}`)).length + items.tasks.filter((task) => shown(`task:${task.id}`)).length + items.questions.filter((question) => shown(`question:${question.session_id}`)).length;
}
// Where a worker stands, as its sidebar dot says and as Home shows it: the same words in both.
export const WORKER_STATES = [
  ["waiting", "Needs you: it waits for your answer or approval (Home → Needs you)"],
  ["failed", "A task failed (Home → Needs you)"],
  ["working", "Working now (Home → Running)"],
  ["watching", "Running, between steps: waiting on CI, a review or a reply (Home → Running or Waiting on others)"],
  ["queued", "Queued: waits for a free slot or another task (Home → Queued)"],
  ["done", "Finished: ready to review (Home → Ready to review)"],
  ["idle", "Nothing open: its work is accepted, or it has none"],
];
export function workerStates() {
  const needs = needsYou();
  const asking = new Set([...needs.drafts.map((draft) => draft.session_id), ...needs.questions.map((question) => question.session_id), ...needs.tasks.filter((task) => task.status !== "failed").map((task) => task.session_id)]);
  const failed = new Set(needs.tasks.filter((task) => task.status === "failed").map((task) => task.session_id));
  const open = new Map();
  for (const task of ownTasks()) open.set(task.session_id, [...(open.get(task.session_id) || []), task]);
  const found = new Map();
  for (const session of sessions()) {
    const list = open.get(session.id) || [];
    let dot = "idle";
    if (session.elsewhere) dot = `open${session.terminal?.working ? " busy" : ""}`;
    else if (asking.has(session.id)) dot = "waiting";
    else if (failed.has(session.id)) dot = "failed";
    else if (["working", "starting"].includes(session.status)) dot = "working";
    else if (list.some((task) => task.status === "running")) dot = "watching";
    else if (list.some((task) => task.status === "queued")) dot = "queued";
    else if (list.some((task) => task.status === "review" || (task.status === "done" && task.news))) dot = "done";
    found.set(session.id, { dot, text: session.elsewhere ? DOT_HELP.open : Object.fromEntries(WORKER_STATES)[dot] });
  }
  return found;
}
const askingSessions = () => new Set(pendingDrafts().map((draft) => draft.session_id));
// A running task whose worker is between turns, watching a PR or a thread: it waits on others
// (CI, a review, a reply), not on its worker.
function waitsOnOthers(task) {
  const worker = byId(task.session_id);
  if (!worker || ["working", "starting"].includes(worker.status) || worker.elsewhere || !(worker.follows || []).length) return false;
  return !worker.task || worker.task.task_id === task.id || worker.task.status !== "running";
}
// The task board: each group newest first; queued in the order they will start.
function board() {
  const asking = askingSessions();
  const mine = ownTasks().filter((task) => !task.blocked);
  const newest = (a, b) => b.updated_at.localeCompare(a.updated_at);
  // Every task at work shows, even when its worker also waits on a card (marked so).
  const running = mine.filter((task) => task.status === "running").map((task) => (asking.has(task.session_id) ? { ...task, asking: true } : task)).sort(newest);
  const terminal = inTerminal().filter((session) => !running.some((task) => task.session_id === session.id));
  // At work, though its task is finished: news on a PR it follows, a question about its work.
  const followUps = sessions().filter((session) => session.role === "worker" && !session.helper && !session.closed_at && session.status === "working" && !running.some((task) => task.session_id === session.id) && !terminal.some((other) => other.id === session.id));
  // A worker with several open tasks (at work on one, the others waiting their turn, for the
  // owner or queued) is one card with a line per task.
  const open = mine.filter((task) => ["running", "waiting", "queued"].includes(task.status));
  // A result its worker drafted something for (a reply, a review) is reviewed on that card.
  // Not a Ready-to-merge card: merging is its own decision, and the result's receipts (tests,
  // CI) are what to look at before it.
  const onCards = new Set(pendingDrafts().filter((draft) => draft.kind !== "merge").map(draftTask).filter((task) => task?.status === "review").map((task) => task.id));
  const perWorker = new Map();
  for (const task of open) perWorker.set(task.session_id, [...(perWorker.get(task.session_id) || []), task]);
  const grouped = new Set([...perWorker].filter(([id, list]) => list.length > 1 && list.some((task) => task.status === "running") && byId(id)).map(([id]) => id));
  const workers = [...grouped].map((id) => [byId(id), perWorker.get(id)]);
  const quiet = ([worker, list]) => !["working", "starting"].includes(worker.status) && (worker.follows || []).length && list.some((task) => task.status === "running");
  const single = running.filter((task) => !grouped.has(task.session_id));
  return {
    running: single.filter((task) => !waitsOnOthers(task)),
    watching: single.filter(waitsOnOthers),
    workers: workers.filter((entry) => !quiet(entry)),
    watchingWorkers: workers.filter(quiet),
    terminal,
    followUps,
    queued: mine.filter((task) => task.status === "queued" && !grouped.has(task.session_id)).sort((a, b) => a.created_at.localeCompare(b.created_at)),
    // A result whose worker drafted something for the owner (a reply, a review) is reviewed on
    // that card: it shows the task, and sending it accepts the task.
    // With them, finished work its worker has news about (its PR approved or merged) until seen.
    review: mine.filter((task) => (task.status === "review" || (task.status === "done" && task.news)) && !onCards.has(task.id)).sort(newest),
    finished: mine.filter((task) => (task.status === "done" && !task.news) || task.status === "cancelled").sort((a, b) => (b.ended_at || "").localeCompare(a.ended_at || "")),
  };
}
export function homeItem() {
  const count = needsCount();
  const router = byId(routerId());
  const working = router?.status === "working";
  return el(
    "button",
    { type: "button", class: `item home-item${state.selected === HOME ? " selected" : ""}`, onclick: () => select(HOME) },
    el("span", { class: `dot${working ? " working" : ""}`, title: working ? "Your chief of staff is working" : "Your chief of staff" }),
    el("span", { class: "label", text: "Home" }),
    count ? el("span", { class: "badge amber", text: String(count), title: `${count} need${count === 1 ? "s" : ""} you` }) : null,
  );
}
function focusUntil() {
  const until = state.snapshot?.focus?.until;
  return until && new Date(until) > new Date() ? new Date(until) : null;
}
export function renderHomeHeader() {
  const until = focusUntil();
  const router = byId(routerId());
  $("#session-title").textContent = "Home";
  $("#session-meta").textContent = until ? `Focusing until ${until.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}: notifications wait` : "";
  $("#session-meta").title = "";
  const actions = $("#session-actions");
  // Rebuilt only when they change: an open Focus menu stays open as tasks move. (Stop is on
  // the "Thinking…" line, where the Router is seen working.)
  const key = `home:${Boolean(until)}:${state.snapshot.runtime.state}:${Boolean(router)}`;
  if (actions.dataset.key === key) return;
  actions.dataset.key = key;
  actions.replaceChildren(state.snapshot.runtime.state === "ready" ? focusControl() : "");
}
// Focus: notifications wait, the Router handles what it can, one summary at the end.
function focusControl() {
  const focus = (minutes) =>
    api("/api/focus", { method: "POST", body: { minutes } })
      .then((focusState) => {
        state.snapshot.focus = focusState;
        renderHeader();
        renderStatusLine();
        toast(minutes ? "Focusing: notifications wait until you are done." : "Focus ended.");
      })
      .catch((error) => toast(error.message, "error"));
  if (focusUntil()) {
    return el("button", { type: "button", class: "focus-on", text: "End focus", title: "Notifications are held; the summary comes when focus ends", onclick: () => focus(0) });
  }
  const durations = [
    [30, "30 minutes"],
    [60, "1 hour"],
    [120, "2 hours"],
    [240, "4 hours"],
  ];
  const menu = moreMenu(durations.map(([minutes, text]) => ({ text: `For ${text}`, onclick: () => focus(minutes) })), "Hold notifications while you focus; the Router handles what it can", "Focus");
  menu.classList.add("focus");
  return menu;
}
let homeFrame = 0;
export function scheduleHome() {
  if (state.selected !== HOME || homeFrame) return;
  homeFrame = requestAnimationFrame(() => {
    homeFrame = 0;
    renderHome();
  });
}
export function renderHome() {
  if (state.selected !== HOME || !state.snapshot) return;
  renderHeader();
  keepingFocus(() => {
    renderNotices();
    renderPanel();
  });
}
// On a narrow window Home shows one part at a time: the chat, Needs you or Tasks.
export function showHomeTab(tab, focus = false) {
  state.homeTab = tab;
  const main = $("#main");
  for (const name of ["chat", "needs", "tasks"]) main.classList.toggle(`tab-${name}`, name === tab);
  for (const button of document.querySelectorAll("#home-tabs button")) button.setAttribute("aria-selected", String(button.dataset.tab === tab));
  if (focus && tab === "chat") {
    scrollToBottom(true);
    $("#composer-input").focus();
  }
}
// Forms the owner has open on task cards, by task id; collected before any section changes.
function openForms() {
  return new Map([...document.querySelectorAll("#panel .card > .actions[data-editing]")].map((form) => [form.parentElement.dataset.id, form]));
}
function section(parent, id, title, { tag = "div", hint = "" } = {}) {
  let node = document.getElementById(id);
  if (!node) {
    const heading = tag === "details" ? el("summary") : el("h3");
    node = el(tag, { id, class: "task-group" }, heading, el("div", { class: "items" }));
    if (hint) node.append(el("p", { class: "empty-hint", text: hint }));
    parent.append(node);
  }
  node.firstElementChild.replaceChildren(el("span", { text: title }), el("span", { class: "count" }));
  return node;
}
function counted(node, count) {
  node.querySelector(".count").textContent = count ? String(count) : "";
}
function renderPanel() {
  const panel = $("#panel");
  const editing = openForms();
  const items = needsYou();
  const tasks = board();

  const needs = document.getElementById("needs") || el("section", { id: "needs", class: "panel-section" }, el("h2", {}, el("span", { text: "Needs you" }), el("span", { class: "count" })), el("div", { class: "items" }), el("p", { class: "empty-hint", text: "Nothing needs you." }));
  const needCount = needsCount();
  counted(needs, needCount);
  needs.querySelector(".empty-hint").hidden = Boolean(needCount);
  reconcile(needs.querySelector(".items"), [
    ...items.drafts.map((draft) => draftItem(draft)),
    ...items.uncertain.map((draft) => ({ key: `uncertain:${draft.id}:${draft.outcome}`, build: () => uncertainCard(draft) })),
    ...items.tasks.map((task) => taskItem(task)),
    ...items.questions.map(questionItem),
  ], editing);

  const taskBox = document.getElementById("tasks") || el("section", { id: "tasks", class: "panel-section" }, el("h2", {}, el("span", { text: "Tasks" }), el("span", { class: "count" })), el("p", { class: "empty-hint", text: "Nothing running. Ask for anything — e.g. “fix the flaky login test in api”." }));
  // Counted by task everywhere: a worker's card counts its tasks here (those waiting for the
  // owner count under Needs you).
  const inGroup = (list) => list.filter((task) => task.status !== "waiting").length;
  const counts = {
    running: tasks.running.length + tasks.workers.reduce((sum, [, list]) => sum + inGroup(list), 0) + tasks.terminal.length + tasks.followUps.length,
    watching: tasks.watching.length + tasks.watchingWorkers.reduce((sum, [, list]) => sum + inGroup(list), 0),
    queued: tasks.queued.length,
    review: tasks.review.length,
  };
  const active = counts.running + counts.watching + counts.queued + counts.review;
  counted(taskBox, active);
  taskBox.querySelector(".empty-hint").hidden = Boolean(active || tasks.finished.length || ownTasks().some((task) => !["done", "cancelled"].includes(task.status)));
  const groups = [
    ["tasks-running", "Running", counts.running, [...tasks.terminal.map(terminalItem), ...tasks.workers.map(([worker, list]) => workerGroupItem(worker, list)), ...tasks.running.map(taskItem), ...tasks.followUps.map(followUpItem)], ""],
    ["tasks-watching", "Waiting on others", counts.watching, [...tasks.watchingWorkers.map(([worker, list]) => workerGroupItem(worker, list)), ...tasks.watching.map(taskItem)], "Its worker is between steps, watching a PR or a thread: CI, a review or a reply comes next."],
    ["tasks-queued", "Queued", counts.queued, tasks.queued.map(taskItem), ""],
    ["tasks-review", "Ready to review", counts.review, tasks.review.map(taskItem), ""],
  ];
  for (const [id, title, count, list, help] of groups) {
    const group = section(taskBox, id, title);
    if (help) group.firstElementChild.title = help;
    counted(group, count);
    group.hidden = !list.length;
    reconcile(group.querySelector(".items"), list, editing);
  }
  const recent = section(taskBox, "tasks-recent", "Recently finished", { tag: "details" });
  counted(recent, tasks.finished.length);
  recent.hidden = !tasks.finished.length;
  reconcile(recent.querySelector(".items"), tasks.finished.slice(0, 30).map(taskItem), editing);
  // The recent group stays last as groups are added.
  if (taskBox.lastElementChild !== recent) taskBox.append(recent);

  if (panel.children[0] !== needs || panel.children[1] !== taskBox) panel.replaceChildren(needs, taskBox);
  const tabs = document.querySelectorAll("#home-tabs .count");
  tabs[0].textContent = needCount ? String(needCount) : "";
  tabs[1].textContent = active ? String(active) : "";
}
// The task a card is about: the one its worker is on (a decision card lists its own).
function draftTask(draft) {
  if (draft.kind === "decision") return null;
  const mine = ownTasks().filter((task) => task.session_id === draft.session_id);
  if (draft.target?.task_id) return mine.find((task) => task.id === draft.target.task_id) || null;
  const worker = byId(draft.session_id);
  // The task it is on now, else one still open.
  return mine.find((task) => task.id === worker?.task?.task_id && ["running", "waiting", "review", "queued"].includes(task.status)) || mine.find((task) => ["running", "waiting", "review", "queued"].includes(task.status));
}
function draftItem(draft) {
  const task = draftTask(draft);
  return {
    key: draftKey(draft),
    version: task ? `${task.id}:${task.status}:${task.updated_at}` : "",
    build: () => withTask(draftCard(draft), task, draft),
    refresh: (node) => withTask(node, task, draft),
  };
}
// Which task the card is about, with what it found folded under its title.
function withTask(node, task, draft) {
  const old = node.querySelector(":scope > .context");
  if (!task) {
    // Not about a task: which worker asks, when it is one (a decision names its tasks itself).
    const worker = byId(draft.session_id);
    const from = draft.kind !== "decision" && worker?.role === "worker" && !worker.helper ? el("div", { class: "context" }, el("span", { class: "muted", text: "From " }), el("strong", { text: worker.title })) : null;
    if (old && from) old.replaceWith(from);
    else if (old) old.remove();
    else if (from) node.querySelector(":scope > .head").after(from);
    return node;
  }
  const text = task.status === "review" ? task.result || task.progress : task.progress || task.goal || "";
  const title = [el("span", { class: "muted", text: task.status === "review" ? "Result of " : "For " }), el("strong", { text: task.title })];
  const context = text
    ? el("details", { class: "context", open: old?.open }, el("summary", {}, ...title), el("div", { class: "markdown", html: markdown(text) }))
    : el("div", { class: "context" }, ...title);
  if (old) old.replaceWith(context);
  // After what the card is (a review says which PR first).
  else (node.querySelector(":scope > .pr-line") || node.querySelector(":scope > .head")).after(context);
  node.dataset.task = task.id;
  return node;
}
const narrow = () => window.matchMedia("(max-width: 1099px)").matches;
// From the chat: the task's card in the panel, brought into view (its report open), or the
// card that asks the owner about it. Not on the board any more: its worker.
export function focusTask(taskId, sessionId = "") {
  const node = (taskId && (document.querySelector(`#panel .card.task[data-id="${taskId}"]`) || document.querySelector(`#panel .card[data-task="${taskId}"]`))) || (sessionId && document.querySelector(`#panel .card.task[data-session="${sessionId}"]`));
  if (!node) {
    if (sessionId) select(sessionId);
    return;
  }
  if (node.classList.contains("review")) expandTask(node, true);
  bringIntoView(node);
}
// A card waiting for the owner, from a draft in a session's timeline.
export async function focusDraft(draftId) {
  if (state.selected !== HOME) await select(HOME);
  const node = document.querySelector(`#needs .card[data-id="${draftId}"]`);
  if (node) bringIntoView(node);
}
export async function focusQuestion(sessionId) {
  if (state.selected !== HOME) await select(HOME);
  const node = document.querySelector(`#needs .card.question-card[data-session="${sessionId}"]`) || document.querySelector(`#needs .card.task[data-session="${sessionId}"]`);
  if (node) bringIntoView(node);
}
// A worker's question to the owner, kept here until answered: its task may have been cancelled
// or finished meanwhile (the task card shows it otherwise).
function questionItem(question) {
  return {
    key: `question:${question.session_id}:${question.at}`,
    version: `${question.task_status}:${question.closed}`,
    build: () => questionCard(question),
    refresh: (node) => {
      const meta = node.querySelector(".head .meta");
      if (meta) meta.textContent = questionMeta(question);
    },
  };
}
const questionMeta = (question) => [question.task_status && question.task_status !== "running" ? `task ${question.task_status}` : "", question.closed ? "worker closed" : ""].filter(Boolean).join(" · ");
function questionCard(question) {
  const key = `question:${question.session_id}`;
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  const worker = byId(question.session_id);
  const node = el(
    "div",
    { class: "card question-card", "data-session": question.session_id, "data-task": question.task_id || undefined },
    el(
      "div",
      { class: "head" },
      el("span", { class: "dot waiting" }),
      el("button", { type: "button", class: "title", text: question.task_title || worker?.title || question.worker, title: `Open “${question.worker}” at its question`, onclick: () => select(question.session_id, { task: question.task_id }) }),
      el("span", { class: "chip question", text: "Question" }),
      el("span", { class: "meta", text: questionMeta(question) }),
    ),
    el("div", { class: "body markdown", html: markdown(question.text) }),
    actions,
  );
  async function act(run, done) {
    error.textContent = "";
    for (const button of actions.querySelectorAll("button")) button.disabled = true;
    state.leaving.add(key);
    node.classList.add("leaving");
    renderStatusLine();
    scheduleHome();
    try {
      await run();
      if (done) toast(done);
      await loadState();
    } catch (failure) {
      state.leaving.delete(key);
      node.classList.remove("leaving");
      error.textContent = failure.message;
      for (const button of actions.querySelectorAll("button")) button.disabled = false;
      scheduleHome();
    }
    state.leaving.delete(key);
  }
  function show() {
    delete actions.dataset.editing;
    actions.replaceChildren(
      error,
      moreMenu([
        { text: "Open worker", onclick: () => select(question.session_id, { task: question.task_id }) },
        { text: "Dismiss", onclick: () => act(() => api(`/api/questions/${question.session_id}/dismiss`, { method: "POST" }), "Dismissed.") },
      ]) || "",
      el("button", { type: "button", text: "Reply", onclick: reply }),
    );
  }
  function reply() {
    const ended = ["cancelled", "done", "review", "failed"].includes(question.task_status);
    const input = el("textarea", { rows: 2, placeholder: ended ? `Your answer (its ${question.task_status} task goes on with it)` : "Your answer (goes to the worker)" });
    // Answered, its task goes on, also one cancelled or finished meanwhile (Dismiss if not).
    const send = () => input.value.trim() && act(() => api(`/api/questions/${question.session_id}/answer`, { method: "POST", body: { text: input.value.trim() } }), ended ? "Sent: its task goes on." : "Sent to the worker.");
    input.addEventListener("keydown", (event) => {
      if (submits(event) && !event.shiftKey) {
        event.preventDefault();
        send();
      }
    });
    actions.replaceChildren(input, error, el("button", { type: "button", class: "text", text: "Cancel", onclick: show }), el("button", { type: "button", text: "Send", onclick: send }));
    actions.dataset.editing = "1";
    input.focus();
  }
  show();
  return node;
}
function bringIntoView(node) {
  if (narrow()) showHomeTab(node.closest("#needs") ? "needs" : "tasks");
  const group = node.closest("details");
  if (group) group.open = true;
  node.scrollIntoView({ block: "nearest", behavior: "smooth" });
  node.classList.remove("flash");
  void node.offsetWidth;  // Restarts the highlight.
  node.classList.add("flash");
}
// Above the conversation: what must be fixed first, and the summary after focus.
function renderNotices() {
  const box = $("#notices");
  const nodes = [setupCard(), focusSummaryCard()].filter(Boolean);
  if (nodes.length !== box.children.length || nodes.some((node, index) => box.children[index] !== node)) box.replaceChildren(...nodes);
  box.hidden = !nodes.length;
}
// First run: what is missing before work can start.
const NO_PROJECTS = "cos.noProjectsCard";  // The owner chose to work without projects for now.

function setupCard() {
  const snap = state.snapshot;
  const key = `${snap.runtime.state}:${snap.runtime.error}:${snap.settings.projects.length}:${snap.settings.code_folder}`;
  let node = document.getElementById("home-setup");
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
      { id: "home-setup", class: "card setup" },
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
  } else if (snap.runtime.state === "ready" && !snap.demo && !snap.settings.projects.length && !snap.settings.code_folder && localStorage.getItem(NO_PROJECTS) !== "1") {
    const input = el("input", { placeholder: "~/workspace", autocomplete: "off", "aria-label": "Code folder" });
    const error = el("span", { class: "error" });
    const add = async () => {
      if (!input.value.trim()) return;
      error.textContent = "";
      try {
        const added = await api("/api/projects", { method: "POST", body: { path: input.value.trim() } });
        toast(
          added.kind === "code_folder"
            ? `Code folder: ${shortPath(added.path)}, ${added.repositories} ${added.repositories === 1 ? "repository" : "repositories"}. Others a task needs get cloned into it.`
            : `Added ${shortPath(added.path)}.`,
        );
        await loadState();
      } catch (failure) {
        error.textContent = failure.message;
      }
    };
    input.addEventListener("keydown", (event) => submits(event) && add());
    node = el(
      "div",
      { id: "home-setup", class: "card setup" },
      el("div", { class: "head" }, el("span", { class: "label", text: "Where is your code?" })),
      el("p", { text: "The folder your repositories live in, like ~/workspace. Workers use their own copies, so your checkouts stay as they are." }),
      el(
        "div",
        { class: "actions" },
        input,
        error,
        el("button", { type: "button", class: "text", text: "Not now", onclick: () => {
          localStorage.setItem(NO_PROJECTS, "1");
          node.remove();
          renderNotices();
          toast("Set your code folder any time in Settings.");
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
    el("div", { class: "head" }, el("span", { class: "label", text: "Focus is over" }), el("span", { class: "spacer" }), el("button", { type: "button", class: "text", text: "×", title: "Dismiss", onclick: () => { state.focusSummary = null; renderNotices(); } })),
    el("div", { class: "body markdown", html: markdown(state.focusSummary) }),
  );
  node.dataset.text = state.focusSummary;
  return node;
}
// An empty conversation: what to ask for.
export function welcomeCard() {
  const examples = [
    "Fix the flaky login test in the api repo and open a PR",
    "Research how other teams use feature flags and summarize the options",
    "Watch for review requests on my repos and draft reviews for me",
  ];
  return el(
    "div",
    { id: "home-welcome", class: "welcome empty" },
    el("h2", { text: "Ask for anything" }),
    el("p", { text: "Several things at once is fine. Each task gets its own worker and shows up under Tasks; anything sent in your name waits for your OK." }),
    el("div", { class: "examples" }, ...examples.map((text) => el("button", { type: "button", class: "secondary", text, onclick: () => { $("#composer-input").value = text; $("#composer-input").focus(); } }))),
  );
}
