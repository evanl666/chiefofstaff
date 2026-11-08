// Home: the conversation with the Router, and beside it what needs the owner and every task.

import { $, api, checkTip, el, keepingFocus, moreMenu, once, reconcile, submits, toast } from "./dom.js";
import { clock, markdown, shortPath, slackText } from "./text.js";
import { byId, closedWorkers, DOT_HELP, HOME, loadState, ownerQuestions, ownerUpdates, pendingDrafts, routerId, sessions, state, uncertainDrafts } from "./state.js";
import { boardOf, countOf, followingUp, SECTIONS, workerSection } from "./board.js";
import { renderSidebar, renderStatusLine } from "./sidebar.js";
import { renderHeader, scrollToBottom, select } from "./session.js";
import { draftCard, draftKey, uncertainCard } from "./drafts.js";
import { expandTask, followUpItem, inTerminal, ownTasks, taskItem, terminalItem, unfoldTask, workerBoxItem } from "./tasks.js";
import { artifactItems } from "./artifacts.js";
// What waits for the owner: cards to approve or answer, and the tasks the board puts under
// Needs you (waiting for an answer, failed, a result to review, news, no progress); a
// worker's question whose task does not show it (cancelled, finished, or none); the Router's
// notes that need the owner, until seen. A task waiting on a decision card shows as that card.
export function needsYou() {
  const drafts = pendingDrafts();
  const found = board();
  const deciding = new Set(drafts.filter((draft) => draft.kind === "decision").flatMap((draft) => draft.target.askers.map((asker) => asker.task_id)));
  const waiting = found.sections.needs.flatMap((entry) => entry.tasks).filter((task) => !(task.status === "waiting" && deciding.has(task.id)));
  const shownTasks = new Set(waiting.map((task) => task.id));
  const questions = ownerQuestions().filter((question) => !shownTasks.has(question.task_id));
  return { drafts, uncertain: uncertainDrafts(), tasks: waiting, questions, updates: ownerUpdates(), board: found };
}
// What the badge counts: not the cards the owner just acted on.
export function needsCount() {
  const items = needsYou();
  const shown = (key) => !state.leaving.has(key);
  return items.drafts.filter((draft) => shown(`draft:${draft.id}`)).length + items.uncertain.filter((draft) => shown(`draft:${draft.id}`)).length + items.tasks.filter((task) => shown(`task:${task.id}`)).length + items.questions.filter((question) => shown(`question:${question.id}`)).length + items.updates.filter((update) => shown(`update:${update.id}`)).length;
}
// Where a worker stands, as its sidebar dot says and as Home shows it: the same four places.
export const WORKER_STATES = [
  ["waiting", "Needs you: one of its tasks waits for your answer, approval or review (Home → Needs you)"],
  ["working", "Running: at work on a task now (Home → Running)"],
  ["watching", "Monitoring: waiting on CI, a review, a reply, a run, a slot or its turn (Home → Monitoring)"],
  ["idle", "Nothing open: its tasks are finished"],
];
const MARKED = { queued: "queued", turn: "up next", paused: "paused", overdue: "overdue", idle: "nothing to wait on", "check-back": "checks back" };
export function workerStates() {
  const needs = needsYou();
  const { phases } = needs.board;
  // Every worker a decision card waits for, not only the first that asked.
  const askers = needs.drafts.flatMap((draft) => (draft.kind === "decision" ? (draft.target.askers || []).map((asker) => asker.session_id) : []));
  const asking = new Set([...needs.drafts.map((draft) => draft.session_id), ...askers, ...needs.questions.map((question) => question.session_id), ...needs.tasks.map((task) => task.session_id)]);
  const mine = new Map();
  for (const task of ownTasks()) mine.set(task.session_id, [...(mine.get(task.session_id) || []), task]);
  const found = new Map();
  for (const session of sessions()) {
    const list = mine.get(session.id) || [];
    const section = workerSection(phases, list);
    let dot = "idle";
    if (session.elsewhere) dot = `open${session.terminal?.working ? " busy" : ""}`;
    else if (asking.has(session.id)) dot = "waiting";
    else if (["working", "starting"].includes(session.status) || section === "running") dot = "working";
    else if (section === "monitoring") dot = "watching";
    // A marker in words, not a color of its own: queued, overdue, checks back…
    const markers = [...new Set(list.map((task) => MARKED[phases.get(task.id)?.marker]).filter(Boolean))];
    found.set(session.id, { dot, sub: dot === "watching" ? markers[0] || "" : "", text: session.elsewhere ? DOT_HELP.open : Object.fromEntries(WORKER_STATES)[dot] });
  }
  return found;
}
// The task board, task by task: each section holds a box per worker with its tasks there (a
// worker shows in every section one of its tasks is in). See board.js.
function board() {
  const mine = ownTasks();
  // A result its worker drafted something for (a reply, a review) is reviewed on that card: its
  // task's card goes in it (the first one about it). Not a Ready-to-merge card: merging is its
  // own decision, and the result's receipts (tests, CI) are what to look at before it.
  const onCards = new Set(pendingDrafts().filter((draft) => draft.kind !== "merge").filter((draft) => { const task = draftTask(draft); return task && (task.status === "review" || (task.status === "done" && task.news) || (task.status === "waiting" && task.question_draft === draft.id)); }).map((draft) => draftTask(draft).id));
  const workers = new Map([...sessions(), ...closedWorkers()].map((session) => [session.id, session]));
  const stallHours = state.snapshot.settings.stall_hours || 4;
  const found = boardOf(mine, workers, { stallHours, onCards });
  const running = new Set(found.sections.running.map((entry) => entry.sessionId));
  const terminal = inTerminal().filter((session) => !running.has(session.id));
  // At work, though none of its tasks is open (news on a PR it follows, a question about it).
  const own = sessions().filter((session) => session.role === "worker" && !session.helper && !session.closed_at);
  const followUps = followingUp(own, mine, running, new Set(terminal.map((session) => session.id)));
  return { ...found, terminal, followUps };
}
// The sections a worker's other tasks are in, for its box.
function elsewhereOf(sections, section, sessionId) {
  return SECTIONS.filter(([key]) => key !== section && key !== "finished" && (sections[key] || []).some((entry) => entry.sessionId === sessionId)).map(([, title]) => title);
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
  checkTip();
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
// A part of Home that folds (Needs you, Running, Monitoring, Artifacts, Recently finished, and
// the groups inside Monitoring): its title and count stay in sight folded; open unless the
// owner folded it (`shut`: folded until opened), remembered per part.
const EARLIER_KEYS = { "tasks-artifacts": "cos.artifactsOpen", "tasks-recent": "cos.recentOpen" };
function fold(id, title, { className = "task-group", shut = false, help = "" } = {}) {
  const saved = localStorage.getItem(`cos.section.${id}`) ?? localStorage.getItem(EARLIER_KEYS[id]);
  // A click in its list (Show more, Done…) redraws it at once, even with the mouse over it.
  const items = el("div", { class: "items" });
  items.addEventListener("click", () => (items.dataset.clicked = String(Date.now())), true);
  const node = el("details", { id, class: className, open: saved === null ? !shut : saved !== "0" }, el("summary", { title: help || undefined }, el("span", { class: "title", text: title }), el("span", { class: "count" })), items);
  node.addEventListener("toggle", () => localStorage.setItem(`cos.section.${id}`, node.open ? "1" : "0"));
  return node;
}
function section(parent, id, title, options = {}) {
  let node = document.getElementById(id);
  if (!node) {
    node = fold(id, title, options);
    parent.append(node);
  }
  return node;
}
function counted(node, count) {
  node.querySelector(":scope > summary .count, :scope > h2 .count").textContent = count ? String(count) : "";
}
// A list does not move under the mouse (a task that just finished would push the rows down,
// under the pointer): it is redrawn when the mouse leaves it, or at once after a click in it.
function holding(section) {
  const list = section.querySelector(":scope > .items");
  if (!list.matches(":hover") || Date.now() - Number(list.dataset.clicked || 0) < 3000) return false;
  if (!list.dataset.held) {
    list.dataset.held = "1";
    list.addEventListener("mouseleave", () => {
      delete list.dataset.held;
      scheduleHome();
    }, { once: true });
  }
  return true;
}
// Rows of tasks, in a group of their own inside Monitoring that folds.
function groupItem(id, title, list, phases, editing, options) {
  const items = list.map((task) => taskItem(task, phases.get(task.id)));
  const fill = (node) => {
    counted(node, list.length);
    reconcile(node.querySelector(".items"), items, editing);
  };
  return {
    key: `group:${id}`,
    version: items.map((item) => `${item.key}=${item.version}`).join("|"),
    build: () => {
      const node = fold(id, title, { className: "task-group sub", ...options });
      fill(node);
      return node;
    },
    refresh: fill,
  };
}
function renderPanel() {
  const panel = $("#panel");
  const editing = openForms();
  const items = needsYou();
  const tasks = items.board;

  let needs = document.getElementById("needs");
  if (!needs) {
    needs = fold("needs", "Needs you", { className: "panel-section" });
    needs.append(el("p", { class: "empty-hint", text: "Nothing needs you." }));
  }
  const needCount = needsCount();
  counted(needs, needCount);
  needs.querySelector(".empty-hint").hidden = Boolean(needCount);
  // Its tasks that wait for the owner (an answer, a retry, a result to review), in a box per
  // worker.
  const needBoxes = new Map();
  for (const task of items.tasks) needBoxes.set(task.session_id, [...(needBoxes.get(task.session_id) || []), task]);
  const embedded = new Set();
  const needItems = [
    ...items.updates.map(updateItem),
    ...items.drafts.map((draft) => draftItem(draft, tasks.phases, embedded)),
    ...items.uncertain.map((draft) => ({ key: `uncertain:${draft.id}:${draft.outcome}`, build: () => uncertainCard(draft) })),
    ...[...needBoxes].map(([sessionId, list]) => workerBoxItem("needs", sessionId, list, tasks.phases, editing, elsewhereOf(tasks.sections, "needs", sessionId))),
    ...items.questions.map(questionItem),
  ];
  // Folded, it opens by itself when something new needs the owner.
  const needKeys = [...items.updates.map((update) => `update:${update.id}`), ...items.drafts.map((draft) => `draft:${draft.id}`), ...items.uncertain.map((draft) => `draft:${draft.id}`), ...items.tasks.map((task) => `task:${task.id}`), ...items.questions.map((question) => `question:${question.id}`)];
  if (state.needKeys && needKeys.some((key) => !state.needKeys.has(key) && !state.leaving.has(key))) needs.open = true;
  state.needKeys = new Set(needKeys);
  reconcile(needs.querySelector(".items"), needItems, editing);

  const taskBox = document.getElementById("tasks") || el("section", { id: "tasks", class: "panel-section" }, el("h2", {}, el("span", { text: "Tasks" }), el("span", { class: "count" })), el("p", { class: "empty-hint", text: "Nothing running. Ask for anything — e.g. “fix the flaky login test in api”." }));
  // Counted by task everywhere, each task in one place.
  const counts = { running: countOf(tasks.sections.running) + tasks.terminal.length + tasks.followUps.length, monitoring: countOf(tasks.sections.monitoring), finished: tasks.finished.length };
  const active = counts.running + counts.monitoring;
  counted(taskBox, active);
  taskBox.querySelector(".empty-hint").hidden = Boolean(active || counts.finished || needCount);
  // What the tasks made or run, in one list with where each stands.
  const made = section(taskBox, "tasks-artifacts", "Artifacts", { help: "What your tasks made or run (PRs, docs, flows), with where each stands now: a PR as GitHub has it, the rest as its worker last said" });
  const artifacts = artifactItems(ownTasks(), focusTask, tasks.phases);
  counted(made, artifacts.shown);
  made.hidden = !artifacts.rows.length;
  reconcile(made.querySelector(".items"), artifacts.rows, editing);
  // Running: a box per worker at work, with the task it is on.
  const running = section(taskBox, "tasks-running", "Running", { help: "Being worked on now: its worker is on it (a worker does one task at a time)" });
  const runningItems = [...tasks.terminal.map(terminalItem), ...tasks.sections.running.map((entry) => workerBoxItem("running", entry.sessionId, entry.tasks, tasks.phases, editing, elsewhereOf(tasks.sections, "running", entry.sessionId))), ...tasks.followUps.map(followUpItem)];
  counted(running, counts.running);
  running.hidden = !runningItems.length;
  reconcile(running.querySelector(".items"), runningItems, editing);
  // Monitoring: a row per task, what went quiet first; the owner's deliveries waiting on others
  // and the PRs they reviewed in groups of their own (the reviewed ones folded at first).
  const monitoring = section(taskBox, "tasks-monitoring", "Monitoring", { help: "Not being worked on now; waiting on something that wakes it: a PR's CI or review, a thread, a time it checks back, a slot or its turn. Overdue ones first. Click a row to see its card." });
  const rows = tasks.monitoring;
  counted(monitoring, counts.monitoring);
  monitoring.hidden = !counts.monitoring;
  if (!holding(monitoring)) {
    reconcile(monitoring.querySelector(".items"), [
      ...rows.waiting.map((task) => taskItem(task, tasks.phases.get(task.id))),
      ...(rows.delivered.length ? [groupItem("tasks-monitoring-delivered", "Delivered, waiting on others", rows.delivered, tasks.phases, editing, { help: "Your work, delivered: waiting on a review, the merge or a reply" })] : []),
      ...(rows.reviewed.length ? [groupItem("tasks-monitoring-reviewed", "PRs you reviewed", rows.reviewed, tasks.phases, editing, { shut: true, help: "Others' PRs you reviewed, followed for their author's answer" })] : []),
    ], editing);
  }
  // Recently finished: a row per task, the newest first across all workers, a page at a time.
  const recent = section(taskBox, "tasks-recent", "Recently finished", { help: "Finished in the last three days, the newest first: reports to read, results accepted, tasks cancelled. Click a row to see its card." });
  const shown = Math.min(state.finishedShown || FINISHED_PAGE, counts.finished);
  recent.querySelector(":scope > summary .count").textContent = !counts.finished ? "" : shown < counts.finished ? `${shown} of ${counts.finished}` : String(counts.finished);
  recent.hidden = !counts.finished;
  const more = shown < counts.finished
    ? [{ key: `more:${shown}:${counts.finished}`, build: () => el("button", { type: "button", class: "link show-more", text: `Show ${Math.min(FINISHED_PAGE, counts.finished - shown)} more`, onclick: () => { state.finishedShown = shown + FINISHED_PAGE; renderHome(); } }) }]
    : [];
  if (!holding(recent)) {
    reconcile(recent.querySelector(".items"), [...tasks.finished.slice(0, shown).map((task) => taskItem(task, tasks.phases.get(task.id))), ...more], editing);
  }
  // The groups stay in order as they are added, the recent one last.
  const order = [made, running, monitoring, recent];
  if (order.some((node, index) => taskBox.children[index + 2] !== node)) taskBox.append(...order);
  for (const stale of ["tasks-queued", "tasks-review", "tasks-stalled"]) document.getElementById(stale)?.remove();

  if (panel.children[0] !== needs || panel.children[1] !== taskBox) panel.replaceChildren(needs, taskBox);
  const tabs = document.querySelectorAll("#home-tabs .count");
  tabs[0].textContent = needCount ? String(needCount) : "";
  tabs[1].textContent = active ? String(active) : "";
}
const FINISHED_PAGE = 30;
// The task a card is about: the one its worker is on (a decision card lists its own).
function draftTask(draft) {
  if (draft.kind === "decision") return null;
  const mine = ownTasks().filter((task) => task.session_id === draft.session_id);
  if (draft.target?.task_id) return mine.find((task) => task.id === draft.target.task_id) || null;
  const worker = byId(draft.session_id);
  // The task it is on now, else one still open.
  return mine.find((task) => task.id === worker?.task?.task_id && ["running", "waiting", "review", "queued"].includes(task.status)) || mine.find((task) => ["running", "waiting", "review", "queued"].includes(task.status));
}
function draftItem(draft, phases, embedded = new Set()) {
  const task = draftTask(draft);
  // A result shown on this card only (board.js: on its card): its own card goes in it, with its
  // receipts, its news and its actions (Accept, Create PR…); once, on the first card about it.
  const phase = task && !embedded.has(task.id) && phases.get(task.id);
  const inner = phase && !phase.section && phase.marker ? taskItem(task, { ...phase, section: phase.marker === "report" ? "" : "needs" }) : null;
  if (inner) embedded.add(task.id);
  return {
    key: draftKey(draft),
    version: task ? `${task.id}:${task.status}:${task.updated_at}${inner ? `:${inner.key}=${inner.version}` : ""}` : "",
    build: () => withTask(draftCard(draft), task, draft, inner),
    refresh: (node) => withTask(node, task, draft, inner),
  };
}
// Which task the card is about, with what it found folded under its title; a result it is the
// only card of, as that task's card (`inner`), redrawn in place (a form open on it stays).
function withTask(node, task, draft, inner = null) {
  const old = node.querySelector(":scope > .context, :scope > .card.task");
  if (task && inner) {
    if (old?.dataset.key === inner.key) {
      if (old.dataset.version !== inner.version) inner.refresh(old);
      old.dataset.version = inner.version;
    } else {
      const result = inner.build();
      result.classList.add("in-draft");
      result.querySelector(":scope > .head .title")?.before(el("span", { class: "of", text: "Result of" }));
      Object.assign(result.dataset, { key: inner.key, version: inner.version });
      if (old) old.replaceWith(result);
      else (node.querySelector(":scope > .pr-line") || node.querySelector(":scope > .head")).after(result);
    }
    node.dataset.task = task.id;
    return node;
  }
  if (!task) {
    // Not about a task: which worker asks, when it is one (a decision names its tasks itself).
    const worker = byId(draft.session_id);
    const from = draft.kind !== "decision" && worker?.role === "worker" && !worker.helper ? el("div", { class: "context" }, el("span", { class: "muted", text: "From " }), el("strong", { text: worker.display_title || worker.title })) : null;
    if (old && from) old.replaceWith(from);
    else if (old) old.remove();
    else if (from) node.querySelector(":scope > .head").after(from);
    return node;
  }
  // A finished task's own result, not what it was asked (the Router's instructions).
  const ended = ["review", "done"].includes(task.status);
  const text = ended ? task.result || task.progress : task.progress || task.goal || "";
  const title = [el("span", { class: "muted", text: ended ? "Result of " : "For " }), el("strong", { text: task.title })];
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
  const find = () => taskId && (document.querySelector(`#panel .card.task[data-id="${taskId}"]`) || document.querySelector(`#panel .card[data-task="${taskId}"]`));
  // A finished one further down than the page shows: the page grows to it.
  const index = taskId && !find() ? board().finished.findIndex((task) => task.id === taskId) : -1;
  if (index >= (state.finishedShown || FINISHED_PAGE)) {
    state.finishedShown = Math.ceil((index + 1) / FINISHED_PAGE) * FINISHED_PAGE;
    renderHome();
  }
  const node = find() || (sessionId && document.querySelector(`#panel .card.task[data-session="${sessionId}"]`));
  if (!node) {
    if (sessionId) select(sessionId);
    return;
  }
  if (node.classList.contains("row")) unfoldTask(node, true, true);
  if (node.classList.contains("review")) expandTask(node, true, true);
  bringIntoView(node);
}
// A card waiting for the owner, from a draft in a session's timeline.
export async function focusDraft(draftId) {
  if (state.selected !== HOME) await select(HOME);
  const node = document.querySelector(`#needs .card[data-id="${draftId}"]`);
  if (node) bringIntoView(node);
}
export async function focusQuestion(questionId) {
  if (state.selected !== HOME) await select(HOME);
  const sessionId = String(questionId).split(":")[0];
  const node = [...document.querySelectorAll("#needs .card.question-card")].find((card) => card.dataset.question === questionId) || document.querySelector(`#needs .card.task[data-session="${CSS.escape(sessionId)}"]`);
  if (node) bringIntoView(node);
}
// A worker's question to the owner, kept here until answered: its task may have been cancelled
// or finished meanwhile (the task card shows it otherwise).
function questionItem(question) {
  return {
    key: `question:${question.id}:${question.at}`,
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
  const key = `question:${question.id}`;
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  const worker = byId(question.session_id);
  const node = el(
    "div",
    { class: "card question-card", "data-session": question.session_id, "data-question": question.id, "data-task": question.task_id || undefined },
    el(
      "div",
      { class: "head" },
      el("span", { class: "dot waiting" }),
      el("button", { type: "button", class: "title", text: question.task_title || worker?.display_title || question.worker, title: `Open “${question.worker}” at its question`, onclick: () => select(question.session_id, { task: question.task_id }) }),
      el("span", { class: "chip question", text: "Question" }),
      el("span", { class: "meta", text: questionMeta(question) }),
    ),
    el("div", { class: "body markdown", html: markdown(worker?.role === "slack" ? slackText(question.text) : question.text) }),
    actions,
  );
  const act = (run, done) => once(key, null, () => answer(run, done));
  async function answer(run, done) {
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
        { text: "Dismiss", onclick: () => act(() => api(`/api/questions/${encodeURIComponent(question.id)}/dismiss`, { method: "POST" }), "Dismissed.") },
      ]) || "",
      el("button", { type: "button", text: "Reply", onclick: reply }),
    );
  }
  function reply() {
    const ended = ["cancelled", "done", "review", "failed"].includes(question.task_status);
    const input = el("textarea", { rows: 2, placeholder: ended ? `Your answer (its ${question.task_status} task goes on with it)` : "Your answer (goes to the worker)" });
    // Answered, its task goes on, also one cancelled or finished meanwhile (Dismiss if not).
    const send = () => input.value.trim() && act(() => api(`/api/questions/${encodeURIComponent(question.id)}/answer`, { method: "POST", body: { text: input.value.trim() } }), ended ? "Sent: its task goes on." : "Sent to the worker.");
    input.addEventListener("keydown", (event) => {
      if (submits(event) && !event.shiftKey) {
        event.preventDefault();
        send();
      }
      // Escape closes it, as Cancel does.
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        show();
        actions.querySelector("button")?.focus();
      }
    });
    actions.replaceChildren(input, error, el("button", { type: "button", class: "text", text: "Cancel", onclick: show }), el("button", { type: "button", text: "Send", onclick: send }));
    actions.dataset.editing = "1";
    input.focus();
  }
  show();
  return node;
}
// A note of the Router's that needs the owner (notify_owner), kept here until seen: Open shows
// it in the chat, Got it says it was read; writing to the Router after it does too.
function updateItem(update) {
  return { key: `update:${update.id}`, build: () => updateCard(update) };
}
function updateCard(update) {
  const key = `update:${update.id}`;
  const error = el("span", { class: "error" });
  const node = el(
    "div",
    { class: `card update-card${update.urgent ? " urgent" : ""}`, "data-update": update.id },
    el(
      "div",
      { class: "head" },
      el("span", { class: "dot waiting" }),
      el("span", { class: "title", text: "The Router" }),
      update.urgent ? el("span", { class: "chip blocked", text: "Urgent" }) : null,
      el("time", { text: clock(update.at), title: new Date(update.at).toLocaleString() }),
    ),
    el("div", { class: "body markdown", html: markdown(update.text) }),
    el(
      "div",
      { class: "actions" },
      error,
      el("button", { type: "button", class: "text", text: "Open", title: "Show it in the chat", onclick: () => {
        showUpdate(update.id);
        seen();
      } }),
      el("button", { type: "button", text: "Got it", onclick: () => seen() }),
    ),
  );
  const seen = () => once(key, node, async () => {
    error.textContent = "";
    state.leaving.add(key);
    node.classList.add("leaving");
    renderSidebar();
    try {
      await api(`/api/messages/${update.id}/seen`, { method: "POST" });
      state.snapshot.updates = ownerUpdates().filter((item) => item.id !== update.id);
    } catch (failure) {
      node.classList.remove("leaving");
      error.textContent = failure.message;
    }
    state.leaving.delete(key);
    renderSidebar();
    scheduleHome();
  });
  return node;
}
// The Router's line in the chat, brought into view (and opened when it folds).
function showUpdate(messageId) {
  if (narrow()) showHomeTab("chat");
  const line = document.querySelector(`#timeline [data-id="${messageId}"]`);
  if (!line) {
    scrollToBottom(true);
    return;
  }
  const toggle = line.querySelector(":scope > .row > button.line");
  if (toggle?.getAttribute("aria-expanded") === "false") toggle.click();
  line.scrollIntoView({ block: "center", behavior: "smooth" });
  line.classList.remove("flash");
  void line.offsetWidth;  // Restarts the highlight.
  line.classList.add("flash");
}
function bringIntoView(node) {
  if (narrow()) showHomeTab(node.closest("#needs") ? "needs" : "tasks");
  for (let group = node.closest("details"); group; group = group.parentElement.closest("details")) group.open = true;
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
