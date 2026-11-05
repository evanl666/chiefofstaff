// Home: the conversation with the Router, and beside it what needs the owner and every task.

import { $, api, el, keepingFocus, moreMenu, reconcile, submits, toast } from "./dom.js";
import { markdown, shortPath } from "./text.js";
import { byId, HOME, loadState, pendingDrafts, routerId, state, uncertainDrafts } from "./state.js";
import { renderStatusLine } from "./sidebar.js";
import { renderHeader, scrollToBottom, select } from "./session.js";
import { draftCard, draftKey, uncertainCard } from "./drafts.js";
import { expandTask, inTerminal, ownTasks, taskItem, terminalItem } from "./tasks.js";
// What waits for the owner. A task whose worker asks something on a card shows as that card.
export function needsYou() {
  const drafts = pendingDrafts();
  const deciding = new Set(drafts.filter((draft) => draft.kind === "decision").flatMap((draft) => draft.target.askers.map((asker) => asker.task_id)));
  const asking = askingSessions();
  const waiting = ownTasks().filter((task) => (task.status === "waiting" && !deciding.has(task.id) && !asking.has(task.session_id)) || task.status === "failed" || task.blocked);
  return { drafts, uncertain: uncertainDrafts(), tasks: waiting };
}
// What the badge counts: not the cards the owner just acted on.
export function needsCount() {
  const items = needsYou();
  const shown = (key) => !state.leaving.has(key);
  return items.drafts.filter((draft) => shown(`draft:${draft.id}`)).length + items.uncertain.filter((draft) => shown(`draft:${draft.id}`)).length + items.tasks.filter((task) => shown(`task:${task.id}`)).length;
}
const askingSessions = () => new Set(pendingDrafts().map((draft) => draft.session_id));
// The task board: each group newest first; queued in the order they will start.
function board() {
  const asking = askingSessions();
  const mine = ownTasks().filter((task) => !task.blocked);
  const newest = (a, b) => b.updated_at.localeCompare(a.updated_at);
  const running = mine.filter((task) => task.status === "running" && !asking.has(task.session_id)).sort(newest);
  return {
    running,
    terminal: inTerminal().filter((session) => !running.some((task) => task.session_id === session.id)),
    queued: mine.filter((task) => task.status === "queued").sort((a, b) => a.created_at.localeCompare(b.created_at)),
    // A result whose worker drafted something for the owner (a reply, a review) is reviewed on
    // that card: it shows the task, and sending it accepts the task.
    review: mine.filter((task) => task.status === "review" && !asking.has(task.session_id)).sort(newest),
    finished: mine.filter((task) => task.status === "done" || task.status === "cancelled").sort((a, b) => (b.ended_at || "").localeCompare(a.ended_at || "")),
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
  ], editing);

  const taskBox = document.getElementById("tasks") || el("section", { id: "tasks", class: "panel-section" }, el("h2", {}, el("span", { text: "Tasks" }), el("span", { class: "count" })), el("p", { class: "empty-hint", text: "Nothing running. Ask for anything — e.g. “fix the flaky login test in api”." }));
  const active = tasks.running.length + tasks.terminal.length + tasks.queued.length + tasks.review.length;
  counted(taskBox, active);
  taskBox.querySelector(".empty-hint").hidden = Boolean(active || tasks.finished.length || ownTasks().some((task) => !["done", "cancelled"].includes(task.status)));
  const groups = [
    ["tasks-running", "Running", [...tasks.terminal.map(terminalItem), ...tasks.running.map(taskItem)]],
    ["tasks-queued", "Queued", tasks.queued.map(taskItem)],
    ["tasks-review", "Ready to review", tasks.review.map(taskItem)],
  ];
  for (const [id, title, list] of groups) {
    const group = section(taskBox, id, title);
    counted(group, list.length);
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
  return ownTasks().find((task) => task.session_id === draft.session_id && ["running", "waiting", "review", "queued"].includes(task.status));
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
  else node.querySelector(":scope > .head").after(context);
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
