// Home: the conversation with the Router, and beside it what needs the owner and every task.

import { $, api, checkTip, el, keepingFocus, moreMenu, once, reconcile, submits, toast, watchPanel } from "./dom.js";
import { clock, markdown, shortPath, slackText } from "./text.js";
import { byId, closedWorkers, DOT_HELP, HOME, loadState, ownerQuestions, ownerUpdates, pendingDrafts, routerId, sessions, state, uncertainDrafts } from "./state.js";
import { boardOf, followingUp, workerSection } from "./board.js";
import { renderSidebar, renderStatusLine } from "./sidebar.js";
import { renderHeader, scrollToBottom, select } from "./session.js";
import { draftCard, draftKey, uncertainCard } from "./drafts.js";
import { expandTask, followUpItem, inTerminal, ownTasks, questionParts, taskItem, terminalItem, unfoldTask } from "./tasks.js";
import { artifactItems } from "./artifacts.js";
// What waits for the owner: cards to approve or answer, and the tasks the board puts under
// Needs you (waiting for an answer, failed, a result to accept, news, no progress); a
// worker's question whose task does not show it (cancelled, finished, or none); the Router's
// notes that need the owner, until seen. A task waiting on a decision card shows as that card.
export function needsYou() {
  const drafts = pendingDrafts();
  const found = board();
  const deciding = new Set(drafts.filter((draft) => draft.kind === "decision").flatMap((draft) => draft.target.askers.map((asker) => asker.task_id)));
  const waiting = found.sections.needs.filter((task) => !(task.status === "waiting" && deciding.has(task.id)));
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
// The task board, task by task: a row per task in each of its four places. See board.js.
function board() {
  const mine = ownTasks();
  // A result its worker drafted something for (a reply, a review) is accepted on that card: its
  // task's card goes in it (the first one about it). Not a Ready-to-merge card: merging is its
  // own decision, and the result's receipts (tests, CI) are what to look at before it.
  const onCards = new Set(pendingDrafts().filter((draft) => draft.kind !== "merge").filter((draft) => { const task = draftTask(draft); return task && (task.status === "review" || (task.status === "done" && task.news) || (task.status === "waiting" && task.question_draft === draft.id)); }).map((draft) => draftTask(draft).id));
  const workers = new Map([...sessions(), ...closedWorkers()].map((session) => [session.id, session]));
  const stallHours = state.snapshot.settings.stall_hours || 4;
  const found = boardOf(mine, workers, { stallHours, onCards });
  const running = new Set(found.sections.running.map((task) => task.session_id));
  const terminal = inTerminal().filter((session) => !running.has(session.id));
  // At work, though none of its tasks is open (news on a PR it follows, a question about it).
  const own = sessions().filter((session) => session.role === "worker" && !session.helper && !session.closed_at);
  const followUps = followingUp(own, mine, running, new Set(terminal.map((session) => session.id)));
  return { ...found, terminal, followUps };
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
// A part of Home (Needs you, Running, Monitoring, Recently finished, Artifacts): it folds, its
// title and count in sight folded; open unless the owner folded it, remembered per part. What
// it says when it holds nothing is said inside it: every part is always there, in its place.
const EARLIER_KEYS = { "tasks-artifacts": "cos.artifactsOpen", "tasks-recent": "cos.recentOpen" };
function fold(id, title, { help = "", empty = "" } = {}) {
  const saved = localStorage.getItem(`cos.section.${id}`) ?? localStorage.getItem(EARLIER_KEYS[id]);
  // A click in its list (Show more, Accept…) redraws it at once, even with the mouse over it.
  const items = el("div", { class: "items" });
  items.addEventListener("click", () => (items.dataset.clicked = String(Date.now())), true);
  const node = el("details", { id, class: "panel-section area", open: saved !== "0" }, el("summary", { "data-tip": help || undefined }, el("h2", { class: "title", text: title }), el("span", { class: "count" })), items, el("p", { class: "empty-hint", text: empty }));
  node.addEventListener("toggle", () => localStorage.setItem(`cos.section.${id}`, node.open ? "1" : "0"));
  return node;
}
function section(parent, id, title, options = {}) {
  let node = document.getElementById(id);
  // From an older page: a part of Tasks, without what it says when empty.
  if (node && !node.querySelector(":scope > .empty-hint")) {
    node.remove();
    node = null;
  }
  if (!node) {
    node = fold(id, title, options);
    parent.append(node);
  }
  return node;
}
// Only what changed is written: an unchanged Home is not touched (its tooltip stays).
function counted(node, count, words = count ? String(count) : "") {
  const shown = node.querySelector(":scope > summary .count");
  if (shown.textContent !== words) shown.textContent = words;
}
function shownIf(node, shown) {
  if (node.hidden === shown) node.hidden = !shown;
}
// A list does not move under the mouse (a task that just finished would push the rows down,
// under the pointer, and a row's Accept would land on another task): while the mouse is over
// it, its rows keep their places (updated in place: a step, a reason), and what is new comes in
// at its end, so nothing that needs the owner waits for the mouse to leave; it is sorted again
// when the mouse leaves, or at once after a click in it.
function steady(section, items) {
  const list = section.querySelector(":scope > .items");
  if (!list.matches(":hover") || Date.now() - Number(list.dataset.clicked || 0) < 3000) return items;
  if (!list.dataset.held) {
    list.dataset.held = "1";
    list.addEventListener("mouseleave", () => {
      delete list.dataset.held;
      scheduleHome();
    }, { once: true });
  }
  // A row is the same thing however its key changed (a task's status): by its id then.
  const byKey = new Map([...list.children].map((node, index) => [node.dataset.key, index]));
  const byId = new Map([...list.children].map((node, index) => [node.dataset.id, index]).filter(([id]) => id));
  const at = (item, index) => byKey.get(item.key) ?? (item.id ? byId.get(item.id) : undefined) ?? list.children.length + index;
  return items.map((item, index) => [item, at(item, index)]).sort((a, b) => a[1] - b[1]).map(([item]) => item);
}
// One of the four task places (or Artifacts): its rows, its count, what it says when empty.
function fill(node, items, editing, { count = items.length, words = undefined, hold = false } = {}) {
  counted(node, count, words);
  shownIf(node.querySelector(":scope > .empty-hint"), !items.length);
  reconcile(node.querySelector(":scope > .items"), hold ? steady(node, items) : items, editing);
}
function renderPanel() {
  const panel = $("#panel");
  // Its tooltips are Home's own, gone as soon as anything in it changes.
  if (!panel.dataset.watched) {
    panel.dataset.watched = "1";
    watchPanel(panel);
  }
  const editing = openForms();
  const items = needsYou();
  const tasks = items.board;

  let needs = document.getElementById("needs");
  if (!needs) needs = fold("needs", "Needs you", { help: "What waits for you: results to accept, questions, drafts to approve, decisions, what failed or stopped", empty: "Nothing needs you." });
  const needCount = needsCount();
  counted(needs, needCount);
  shownIf(needs.querySelector(".empty-hint"), !needCount);
  // A card per thing to do: a task's own row holds all it waits for (a result to accept, its
  // question), once.
  const embedded = new Set();
  const needItems = [
    ...items.updates.map(updateItem),
    ...items.drafts.map((draft) => draftItem(draft, tasks.phases, embedded)),
    ...items.uncertain.map((draft) => ({ key: `uncertain:${draft.id}:${draft.outcome}`, build: () => uncertainCard(draft) })),
    ...items.tasks.map((task) => taskItem(task, tasks.phases.get(task.id))),
    ...items.questions.map(questionItem),
  ];
  // Folded, it opens by itself when something new needs the owner.
  const needKeys = [...items.updates.map((update) => `update:${update.id}`), ...items.drafts.map((draft) => `draft:${draft.id}`), ...items.uncertain.map((draft) => `draft:${draft.id}`), ...items.tasks.map((task) => `task:${task.id}`), ...items.questions.map((question) => `question:${question.id}`)];
  if (state.needKeys && needKeys.some((key) => !state.needKeys.has(key) && !state.leaving.has(key))) needs.open = true;
  state.needKeys = new Set(needKeys);
  reconcile(needs.querySelector(".items"), steady(needs, needItems), editing);

  // The four parts below Needs you; on a narrow window, the Tasks tab.
  const taskBox = document.getElementById("tasks") || el("section", { id: "tasks", "aria-label": "Tasks" });
  taskBox.querySelector(":scope > h2, :scope > .empty-hint")?.remove();
  const sections = tasks.sections;
  // Running: a row per task its worker is on now, and per worker at work with no task there.
  const running = section(taskBox, "tasks-running", "Running", { help: "Being worked on now: its worker is on it (a worker does one task at a time)", empty: "Nothing running. Ask for anything — e.g. “fix the flaky login test in api”." });
  const runningItems = [...tasks.terminal.map(terminalItem), ...sections.running.map((task) => taskItem(task, tasks.phases.get(task.id))), ...tasks.followUps.map(followUpItem)];
  fill(running, runningItems, editing, { hold: true });
  // Monitoring: a row per task, what went quiet first.
  const monitoring = section(taskBox, "tasks-monitoring", "Monitoring", { help: "Not being worked on now; waiting on something that wakes it: a PR's CI or review, a thread, a time it checks back, a slot or its turn. Click a row to see all of it.", empty: "Nothing to wait on." });
  fill(monitoring, sections.monitoring.map((task) => taskItem(task, tasks.phases.get(task.id))), editing, { hold: true });
  // Recently finished: a row per task, the newest first across all workers, a page at a time.
  const recent = section(taskBox, "tasks-recent", "Recently finished", { help: "Finished in the last three days, the newest first: results you accepted, tasks cancelled or set aside. Click a row to see all of it.", empty: "Nothing finished lately." });
  const finished = sections.finished.length;
  const shown = Math.min(state.finishedShown || FINISHED_PAGE, finished);
  const more = shown < finished
    ? [{ key: `more:${shown}:${finished}`, build: () => el("button", { type: "button", class: "link show-more", text: `Show ${Math.min(FINISHED_PAGE, finished - shown)} more`, onclick: () => { state.finishedShown = shown + FINISHED_PAGE; renderHome(); } }) }]
    : [];
  fill(recent, [...sections.finished.slice(0, shown).map((task) => taskItem(task, tasks.phases.get(task.id))), ...more], editing, { count: finished, words: !finished ? "" : shown < finished ? `${shown} of ${finished}` : String(finished), hold: true });
  // Artifacts, last: what the tasks delivered (PRs, docs, flows), apart from the tasks.
  const made = section(taskBox, "tasks-artifacts", "Artifacts", { help: "What your tasks delivered (PRs they opened, pushed to or reviewed, docs they wrote, flows they ran), with where each stands now: a PR as GitHub has it, the rest as its worker last said", empty: "No PRs, docs or flows yet." });
  const artifacts = artifactItems(ownTasks(), focusTask, tasks.phases);
  fill(made, artifacts.rows, editing, { count: artifacts.shown });
  // In this order, whatever an older page had.
  const order = [running, monitoring, recent, made];
  if (order.some((node, index) => taskBox.children[index] !== node) || taskBox.children.length !== order.length) taskBox.replaceChildren(...order);
  if (panel.children[0] !== needs || panel.children[1] !== taskBox) panel.replaceChildren(needs, taskBox);
  const active = runningItems.length + sections.monitoring.length;
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
  const inner = phase && !phase.section && phase.marker ? taskItem(task, { ...phase, section: "" }) : null;
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
// Scrolled to, not glided to, when the system asks for less motion.
const glide = () => (window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");
// From the chat: the task's card in the panel, brought into view (its report open), or the
// card that asks the owner about it. Not on the board any more: its worker.
export function focusTask(taskId, sessionId = "") {
  const find = () => taskId && (document.querySelector(`#panel .card.task[data-id="${taskId}"]`) || document.querySelector(`#panel .card[data-task="${taskId}"]`));
  // A finished one further down than the page shows: the page grows to it.
  const index = taskId && !find() ? board().sections.finished.findIndex((task) => task.id === taskId) : -1;
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
    version: `${question.task_status}:${question.closed}:${JSON.stringify([question.why, question.options, question.next])}`,
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
  );
  const act = (run, done) => once(key, null, () => answer(run, done));
  const ended = ["cancelled", "done", "review", "failed"].includes(question.task_status);
  const sent = ended ? "Sent: its task goes on." : "Sent to the worker.";
  // Why it asks, the answers it offers (one click sends one) and what it does then.
  const say = (text) => act(() => api(`/api/questions/${encodeURIComponent(question.id)}/answer`, { method: "POST", body: { text } }), sent);
  const asked = questionParts(question, say);
  if (asked.length) node.append(el("div", { class: "asked" }, ...asked));
  node.append(actions);
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
    const input = el("textarea", { rows: 2, placeholder: ended ? `Your answer (its ${question.task_status} task goes on with it)` : "Your answer (goes to the worker)" });
    // Answered, its task goes on, also one cancelled or finished meanwhile (Dismiss if not).
    const send = () => input.value.trim() && say(input.value.trim());
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
  line.scrollIntoView({ block: "center", behavior: glide() });
  line.classList.remove("flash");
  void line.offsetWidth;  // Restarts the highlight.
  line.classList.add("flash");
}
function bringIntoView(node) {
  if (narrow()) showHomeTab(node.closest("#needs") ? "needs" : "tasks");
  for (let group = node.closest("details"); group; group = group.parentElement.closest("details")) group.open = true;
  node.scrollIntoView({ block: "nearest", behavior: glide() });
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
