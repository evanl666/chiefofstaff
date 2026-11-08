// Home: the conversation with the Router, and beside it what needs the owner and every task.

import { $, api, el, keepingFocus, moreMenu, reconcile, submits, toast } from "./dom.js";
import { markdown, shortPath } from "./text.js";
import { byId, closedWorkers, DOT_HELP, HOME, loadState, ownerQuestions, pendingDrafts, routerId, sessions, state, uncertainDrafts } from "./state.js";
import { boardOf, countOf, finishedBoxes, followingUp, SECTIONS, workerSection } from "./board.js";
import { renderStatusLine } from "./sidebar.js";
import { renderHeader, scrollToBottom, select } from "./session.js";
import { draftCard, draftKey, uncertainCard } from "./drafts.js";
import { expandTask, followUpItem, inTerminal, ownTasks, terminalItem, workerBoxItem } from "./tasks.js";
import { artifactItems } from "./artifacts.js";
// What waits for the owner: cards to approve or answer, and the tasks the board puts under
// Needs you (waiting for an answer, failed, a result to review, news, no progress); a
// worker's question whose task does not show it (cancelled, finished, or none). A task waiting
// on a decision card shows as that card.
export function needsYou() {
  const drafts = pendingDrafts();
  const found = board();
  const deciding = new Set(drafts.filter((draft) => draft.kind === "decision").flatMap((draft) => draft.target.askers.map((asker) => asker.task_id)));
  const waiting = found.sections.needs.flatMap((entry) => entry.tasks).filter((task) => !(task.status === "waiting" && deciding.has(task.id)));
  const shownTasks = new Set(waiting.map((task) => task.id));
  const questions = ownerQuestions().filter((question) => !shownTasks.has(question.task_id));
  return { drafts, uncertain: uncertainDrafts(), tasks: waiting, questions, board: found };
}
// What the badge counts: not the cards the owner just acted on.
export function needsCount() {
  const items = needsYou();
  const shown = (key) => !state.leaving.has(key);
  return items.drafts.filter((draft) => shown(`draft:${draft.id}`)).length + items.uncertain.filter((draft) => shown(`draft:${draft.id}`)).length + items.tasks.filter((task) => shown(`task:${task.id}`)).length + items.questions.filter((question) => shown(`question:${question.id}`)).length;
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
  // A result its worker drafted something for (a reply, a review) is reviewed on that card.
  // Not a Ready-to-merge card: merging is its own decision, and the result's receipts (tests,
  // CI) are what to look at before it.
  const onCards = new Set(pendingDrafts().filter((draft) => draft.kind !== "merge").map(draftTask).filter((task) => task?.status === "review").map((task) => task.id));
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
  const tasks = items.board;

  const needs = document.getElementById("needs") || el("section", { id: "needs", class: "panel-section" }, el("h2", {}, el("span", { text: "Needs you" }), el("span", { class: "count" })), el("div", { class: "items" }), el("p", { class: "empty-hint", text: "Nothing needs you." }));
  const needCount = needsCount();
  counted(needs, needCount);
  needs.querySelector(".empty-hint").hidden = Boolean(needCount);
  // Its tasks that wait for the owner (an answer, a retry, a result to review), in a box per
  // worker.
  const needBoxes = new Map();
  for (const task of items.tasks) needBoxes.set(task.session_id, [...(needBoxes.get(task.session_id) || []), task]);
  reconcile(needs.querySelector(".items"), [
    ...items.drafts.map((draft) => draftItem(draft)),
    ...items.uncertain.map((draft) => ({ key: `uncertain:${draft.id}:${draft.outcome}`, build: () => uncertainCard(draft) })),
    ...[...needBoxes].map(([sessionId, list]) => workerBoxItem("needs", sessionId, list, tasks.phases, editing, elsewhereOf(tasks.sections, "needs", sessionId))),
    ...items.questions.map(questionItem),
  ], editing);

  const taskBox = document.getElementById("tasks") || el("section", { id: "tasks", class: "panel-section" }, el("h2", {}, el("span", { text: "Tasks" }), el("span", { class: "count" })), el("p", { class: "empty-hint", text: "Nothing running. Ask for anything — e.g. “fix the flaky login test in api”." }));
  // Counted by task everywhere (a box counts its tasks), each task in one place.
  const boxes = (section) => tasks.sections[section].map((entry) => workerBoxItem(section, entry.sessionId, entry.tasks, tasks.phases, editing, elsewhereOf(tasks.sections, section, entry.sessionId)));
  const counts = { running: countOf(tasks.sections.running) + tasks.terminal.length + tasks.followUps.length, monitoring: countOf(tasks.sections.monitoring), finished: tasks.finished.length };
  const active = counts.running + counts.monitoring;
  counted(taskBox, active);
  taskBox.querySelector(".empty-hint").hidden = Boolean(active || counts.finished || needCount);
  const HELP = {
    running: "Being worked on now: its worker is on it (a worker does one task at a time)",
    monitoring: "Not being worked on now; waiting on something that wakes it: a PR's CI or review, a thread, a time it checks back, a slot or its turn. Overdue and queued ones are marked so.",
  };
  // What the tasks made or run, in one list with where each stands (open until closed).
  const fresh = !document.getElementById("tasks-artifacts");
  const made = section(taskBox, "tasks-artifacts", "Artifacts", { tag: "details" });
  if (fresh) {
    made.open = localStorage.getItem("cos.artifactsOpen") !== "0";
    made.firstElementChild.title = "What your tasks made or run (PRs, docs, flows), with where each stands now: a PR as GitHub has it, the rest as its worker last said";
    made.addEventListener("toggle", () => localStorage.setItem("cos.artifactsOpen", made.open ? "1" : "0"));
  }
  const artifacts = artifactItems(ownTasks(), focusTask);
  counted(made, artifacts.shown);
  made.hidden = !artifacts.rows.length;
  reconcile(made.querySelector(".items"), artifacts.rows, editing);
  const groupNodes = [made];
  for (const id of ["running", "monitoring"]) {
    const list = id === "running" ? [...tasks.terminal.map(terminalItem), ...boxes("running"), ...tasks.followUps.map(followUpItem)] : boxes(id);
    const group = section(taskBox, `tasks-${id}`, SECTIONS.find(([key]) => key === id)[1]);
    groupNodes.push(group);
    group.firstElementChild.title = HELP[id];
    counted(group, counts[id]);
    group.hidden = !list.length;
    reconcile(group.querySelector(".items"), list, editing);
  }
  // Recently finished: the newest first across all workers, a page at a time.
  const firstRecent = !document.getElementById("tasks-recent");
  const recent = section(taskBox, "tasks-recent", "Recently finished", { tag: "details" });
  if (firstRecent) {
    // Open until the owner closes it: answers that only inform are here, read, not accepted.
    recent.open = localStorage.getItem("cos.recentOpen") !== "0";
    recent.addEventListener("toggle", () => localStorage.setItem("cos.recentOpen", recent.open ? "1" : "0"));
  }
  const shown = Math.min(state.finishedShown || FINISHED_PAGE, counts.finished);
  recent.querySelector(".count").textContent = !counts.finished ? "" : shown < counts.finished ? `${shown} of ${counts.finished}` : String(counts.finished);
  recent.hidden = !counts.finished;
  const more = shown < counts.finished
    ? [{ key: `more:${shown}:${counts.finished}`, build: () => el("button", { type: "button", class: "link show-more", text: `Show ${Math.min(FINISHED_PAGE, counts.finished - shown)} more`, onclick: () => { state.finishedShown = shown + FINISHED_PAGE; renderHome(); } }) }]
    : [];
  reconcile(recent.querySelector(".items"), [...finishedBoxes(tasks.finished, shown).map((box) => workerBoxItem("finished", box.sessionId, box.tasks, tasks.phases, editing, [], box.key)), ...more], editing);
  // The groups stay in order as they are added, the recent one last.
  const order = [...groupNodes, recent];
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
    const from = draft.kind !== "decision" && worker?.role === "worker" && !worker.helper ? el("div", { class: "context" }, el("span", { class: "muted", text: "From " }), el("strong", { text: worker.display_title || worker.title })) : null;
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
  if (node.classList.contains("review")) expandTask(node, true);
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
