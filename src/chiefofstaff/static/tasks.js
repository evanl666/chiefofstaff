// Task cards on Home: what runs and what it does now, what waits, what is finished.

import { api, el, foldable, moreMenu, once, submits, toast } from "./dom.js";
import { monitorParts, obligationsOf, PR_TO_FIX, SECTION_DOT, statusWord, taskPhase, workerContext } from "./board.js";
import { ago, changesText, compactPaths, displayPath, gist, linkName, markdown, notCountedText, plural, prTitle, shorten, since, taskLinks, threadLabel } from "./text.js";
import { deliverablesOf, isHidden, PR_ROLES, prStatus, setHidden, toneOf } from "./artifacts.js";
import { openFile } from "./files.js";
import { byId, closedWorkers, DELIVERY_WORDS, latestDelivery, loadState, ownerQuestions, pendingDrafts, sessions, state, tasks } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { refreshChips, select } from "./session.js";
import { focusDraft, focusQuestion, scheduleHome } from "./inbox.js";
import { showChanges } from "./diff.js";
// Chief of Staff's own helpers (learning how the owner works, checks, the profile): their
// results come as cards to approve; the work itself is not the owner's.
const HELPER_TITLES = new Set(["Learn how you work", "Learn checks from mistakes", "Distill owner profile"]);
const sessionById = (id) => byId(id) || closedWorkers().find((session) => session.id === id);
export function isHelperTask(task) {
  if (task.helper) return true;
  const session = sessionById(task.session_id);
  return session ? Boolean(session.helper) : HELPER_TITLES.has(task.title);
}
export const ownTasks = () => tasks().filter((task) => !isHelperTask(task));
// Workers taking a slot: in a turn (working, finishing one after reporting, or waiting in it
// for the owner's answer), as the server counts them.
const busy = (session) => session.role === "worker" && !session.closed_at && (session.slot ?? (["working", "starting"].includes(session.status) || session.activity?.startsWith("Waiting for you")));
// Why a queued task has not started: a task it waits for, or every worker slot is taken.
export function queuedReason(task) {
  const activity = byId(task.session_id)?.activity || "";
  if (/^waiting for /i.test(activity)) return { text: `W${activity.slice(1)}` };
  const limit = state.snapshot.settings.max_workers;
  // Chief of Staff's own helpers take no slot.
  const own = sessions().filter((session) => busy(session) && !session.helper).length;
  const help = `At most ${limit} worker${limit === 1 ? "" : "s"} run at once: Settings → General → Parallel workers.`;
  return { text: `Waiting for a worker slot · ${Math.min(own, limit)} of ${limit} busy`, help };
}
// Workers the owner is using in a terminal right now: working, though no task says so.
export const inTerminal = () => sessions().filter((session) => session.role === "worker" && !session.helper && session.elsewhere && session.terminal?.working);
// A row of Running for a worker with no task row there (a terminal, a follow-up): as a task's
// row is, its worker a reference at its foot.
function workerRow({ key, className, dot, dotTip, name, status, statusTip, line, session, taskId = "" }) {
  return {
    key,
    version: `${name}:${status}:${line}`,
    build: () =>
      el(
        "div",
        { class: `card task row in-running ${className}`, "data-session": session.id },
        el(
          "div",
          { class: "head" },
          el("span", { class: `dot ${dot}`, "data-tip": dotTip }),
          el("span", { class: "title", text: name, "data-tip": name }),
          el("span", { class: "meta", "data-tip": statusTip }, el("span", { class: "marker", text: status })),
        ),
        el("div", { class: "row-line" }, el("span", { class: "latest step", text: line, "data-tip": line })),
        el("div", { class: "row-foot" }, workerRef(session, name, taskId)),
      ),
    refresh: (node) => {
      node.querySelector(".row-line .latest").textContent = line;
      node.querySelector(".row-line .latest").dataset.tip = line;
      node.querySelector(".head .title").textContent = name;
      node.querySelector(".head .meta .marker").textContent = status;
    },
  };
}
export function terminalItem(session) {
  return workerRow({ key: `terminal:${session.id}`, className: "terminal", dot: "open busy", dotTip: "Working in a terminal", name: session.display_title || session.title, status: "In a terminal", statusTip: "You are using this worker in a terminal: shown read-only", line: compactPaths(session.terminal?.activity || "Working…", session.cwd), session });
}
// Workers at work whose task is finished (news on a PR they follow, a question about it): they
// are running too. The row is named for that task.
export function followUpItem(session) {
  const status = `Following up${session.step_since ? ` · ${ago(session.step_since)}` : ""}`;
  return workerRow({ key: `followup:${session.id}`, className: "followup", dot: "working", dotTip: "Working", name: session.display_title || session.task?.title || session.title, status, statusTip: "Its task is finished; it works on what came up about it (news on its PR, a question)", line: stepText(session.step || session.activity || "Working…", session.cwd), session, taskId: session.task?.task_id || "" });
}
// What a row calls its worker: what the worker is on now (the server's `display_title`) when
// that is another task, else nothing (its "worker chat"). Never the task again, and never the
// first thing it was asked (its `title`): a worker given new work keeps that, and it may be
// about something long done.
export function workerName(session, title) {
  const named = session?.display_title || "";
  return named !== title ? named : "";
}
// The worker a row's task is with, as a small reference at its foot: it opens that worker's
// chat at the task.
export function workerRef(session, title, taskId = "") {
  if (!session) return "";
  const name = workerName(session, title);
  const tip = [`Open its worker's chat${taskId ? " at this task" : ""}`, name ? `Worker: “${name}”` : ""].filter(Boolean).join("\n");
  return el(
    "button",
    { type: "button", class: "worker-ref link", "data-tip": tip, "aria-label": `Open the worker of “${title}”`, onclick: (event) => {
      event.stopPropagation();
      select(session.id, taskId ? { task: taskId } : {});
    } },
    el("span", { class: "icon", "aria-hidden": "true" }),
    el("span", { class: "who", text: "Worker" }),
    el("span", { class: "name", text: name || "chat" }),
  );
}
// Where a task is when no board placed it (a card elsewhere): as the board would place it.
const phaseOf = (task) => {
  const worker = byId(task.session_id);
  const list = tasks().filter((other) => other.session_id === task.session_id);
  return taskPhase(task, worker, { ...workerContext(worker, list), stallHours: state.snapshot?.settings?.stall_hours || 4 });
};
// Every task on the board is a row (Needs you, Running, Monitoring, Recently finished): a click
// shows its whole card in place. One inside a draft's card is that card's part, shown whole.
const isRow = (phase) => Boolean(phase.section);
// A task split off another can go back into it for a while.
const undoUntil = (task) => (task.split_from?.until && Date.parse(task.split_from.until) > Date.now() ? task.split_from.until : "");
// A result to accept the owner has not opened yet.
const unread = (task, phase) => task.status === "review" && phase.marker === "review" && !task.read;
// Its "⋯", told apart from every other card's by its task.
const moreName = (task) => `More actions for “${task.title}”`;
// The longest title the server keeps (clean_title).
const TITLE_MAX = 80;
// What cancelling came to, as the server has the task after it: one that had finished is set
// aside (its result kept), not cancelled.
export const cancelledWords = (view) => (view?.set_aside ? "It had finished: set aside, its result is kept." : "Task cancelled.");
// Runs its worker said go on that nothing told of for a day (see showActions): their links.
const unknownRuns = (task) => (Array.isArray(task.monitoring) ? task.monitoring : []).filter((item) => item.kind === "flow" && item.unknown).map((item) => item.url);
// The key covers what changes a card's actions and layout; the version, what only its text.
// `phase`: where it is on the board ({section, marker, why}: see board.js).
export function taskItem(task, phase = phaseOf(task)) {
  const worker = byId(task.session_id);
  const live = ["running", "queued"].includes(task.status) || phase.section === "monitoring";
  const [meta, help] = taskMeta(task, phase);
  const made = [task.artifacts, task.links, task.files, task.made_links, task.link_names, task.watch_kind, task.watch_changed_at, task.deliverables, task.monitoring, state.snapshot?.artifacts_hidden];
  const asked = askedOf(task);
  return {
    key: `task:${task.id}:${task.status}:${phase.section}:${phase.marker}:${task.stopped ? 1 : 0}:${task.blocked ? 1 : 0}:${createsPr(task) ? 1 : 0}:${task.news?.at || ""}:${task.splittable ? 1 : 0}:${undoUntil(task) ? 1 : 0}${unknownRuns(task).map((url) => `:${url}`).join("")}`,
    id: task.id,
    // For live rows the minute too: "6m" and "3m ago" move on by themselves.
    version: `${task.updated_at}:${task.title}:${phase.why}:${meta.join("|")}:${help}:${task.read ? 1 : 0}:${worker?.status || ""}:${worker?.activity || ""}:${worker?.step || ""}:${worker?.step_since || ""}:${worker?.turn_task || ""}:${worker?.background ? 1 : 0}:${worker?.display_title || ""}:${task.credits}:${task.runs}:${JSON.stringify(task.receipts?.prs || null)}:${task.status === "review" ? JSON.stringify(task.receipts || null) : ""}:${live ? `${Math.floor(Date.now() / 60000)}:${queuedKey()}` : ""}:${waitsOnOwner(task, worker)}:${(task.watching || []).join(",")}:${(task.watch_failing || []).join(",")}:${(task.watch_unreadable || []).join(",")}:${JSON.stringify(task.watch_failing_why || null)}:${JSON.stringify(task.check_back || null)}:${task.nudged_at || ""}:${JSON.stringify(task.receipts?.changed || null)}:${JSON.stringify(made)}:${JSON.stringify(asked)}:${deliveryKey(task)}:${earlierResults(task).length}`,
    build: () => taskCard(task, phase),
    refresh: (node) => refreshTaskCard(node, task, phase),
  };
}
// A queued card's reason changes as other workers start and stop.
const queuedKey = () => `${sessions().filter(busy).length}/${state.snapshot.settings.max_workers}`;
// A row's whole card (its body, receipts, report) is drawn once it is shown, and only then: a
// folded row has its own short lines instead, never both (so nothing on it is there twice).
const whole = (node, phase) => !isRow(phase) || node.classList.contains("unfolded");
function refreshTaskCard(node, task, phase) {
  node.redraw = () => refreshTaskCard(node, task, phase);
  const shown = whole(node, phase);
  const title = node.querySelector(".head .title");
  title.textContent = task.title;
  title.dataset.tip = titleTip(task, phase);
  node.querySelector(".more-button")?.setAttribute("aria-label", moreName(task));
  const [meta, help] = taskMeta(task, phase);
  node.querySelector(".head .meta").replaceChildren(...metaParts(meta));
  node.querySelector(".head .meta").dataset.tip = help;
  const dot = node.querySelector(".head .dot");
  dot.className = dotClass(task, phase);
  dot.dataset.tip = unread(task, phase) ? "Not read yet" : "";
  const review = task.status === "review";
  const id = node.querySelector(".head .tid");
  if (id) id.textContent = task.id;
  const worker = byId(task.session_id);
  for (const part of [".row-line", ".asked", ".row-foot", ".task-body", ".news", ".chips", ".report"]) {
    const old = node.querySelector(`:scope > ${part}`);
    const fresh = {
      ".row-line": () => (isRow(phase) && !shown ? rowLine(task, worker, phase) : null),
      ".asked": () => askedBlock(task, node, phase),
      ".row-foot": () => (isRow(phase) ? rowFoot(task, worker, phase, shown) : null),
      ".task-body": () => (shown ? taskBody(task, phase) : null),
      ".news": () => (shown ? newsBlock(task) : null),
      ".chips": () => (review && shown ? chips(task, phase) : null),
      ".report": () => (review && shown ? report(task) : null),
    }[part]();
    if (old && fresh) old.replaceWith(fresh);
    else if (old) old.remove();
    else if (fresh) placePart(node, part, fresh);
  }
  expandTask(node, review && openReports.has(task.id));
}
// A part of a card in its place: a row's own lines after its head (in order), the card's after
// them, its actions last.
const PARTS = [".row-line", ".asked", ".row-foot", ".task-body", ".news", ".chips", ".report"];
function placePart(node, part, fresh) {
  const after = PARTS.slice(PARTS.indexOf(part) + 1).map((name) => node.querySelector(`:scope > ${name}`)).find(Boolean) || node.querySelector(":scope > .actions:not(.inline)");
  if (after) after.before(fresh);
  else node.append(fresh);
}
const dotClass = (task, phase) => `dot ${SECTION_DOT[phase.section] ?? ""}${unread(task, phase) ? " unread" : ""}`;
// What a click on its title does, said on hover: a row shows all of it (or folds it back); in a
// draft's card it opens its worker.
const titleTip = (task, phase) => (isRow(phase) ? `${task.title}\nClick to see it all, or to fold it back` : task.session_id ? `${task.title}\nOpen its worker at this task` : task.goal || "");
// Short on the row: its status in a word (where it is, what kind of "needs you" or "finished")
// and since when; the rest on hover.
function taskMeta(task, phase = {}) {
  const ended = ["review", "done", "failed", "cancelled"].includes(task.status);
  const earlier = (task.earlier_titles || []).map((item) => `“${item.title}”`).join(", ");
  // Short: a few lines, each a fact.
  const help = [
    phase.marker === "review" && phase.why ? phase.why : "",
    [`Task ${task.id}`, ended ? `finished ${since(task.ended_at || task.updated_at)}` : `started ${since(task.created_at)}`].join(" · "),
    task.worker && task.worker !== task.title ? `Worker: ${shorten(task.worker, 60)}` : "",
    earlier ? `Earlier names: ${shorten(earlier, 80)}` : "",
    [task.credits >= 0.05 ? `${task.credits.toFixed(1)} credits` : "", task.runs > 1 ? `${task.runs} runs` : ""].filter(Boolean).join(" · "),
  ];
  let when = since(task.ended_at || task.updated_at);
  if (phase.section === "running" && task.status === "running") when = ago(task.created_at) === "now" ? "just now" : ago(task.created_at);
  else if (phase.marker === "check-back" && task.check_back?.at) when = clockTime(task.check_back.at);
  else if (phase.section === "monitoring") when = since(task.active_at || task.updated_at);
  else if (phase.marker === "queued" || phase.marker === "turn") when = "";
  return [[statusWord(phase), when], help.filter(Boolean).join("\n")];
}
// The meta's parts: the status, and when (left out on a narrow row: it is in the tooltip).
const metaParts = ([marker, when]) => [marker ? el("span", { class: "marker", text: marker }) : "", when ? el("span", { class: "when", text: `${marker ? " · " : ""}${when}` }) : ""];
const clockTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
// Commits on a branch of its own checkout (a chief/… one, or one the owner named) that no PR is
// from yet: Create PR opens one. Not on the default branch, and not on a PR it checked out to
// review (someone else's branch: its PR is known, and it made no commits there).
const DEFAULT_BRANCHES = new Set(["main", "master", "trunk", "develop", "development", "default", "HEAD"]);
function createsPr(task) {
  const receipts = task.receipts;
  if (!receipts) return false;
  const prs = receipts.prs || [];
  if (prs.some((pr) => pr.own)) return false;
  // Not from a folder of the owner's (shared): its branch is theirs, not the task's.
  return (receipts.checkouts || []).some((checkout) => !checkout.shared && checkout.commits > 0 && checkout.branch && !DEFAULT_BRANCHES.has(checkout.branch) && !prs.some((pr) => pr.branch === checkout.branch));
}
// A result to accept shows its full report on demand; an open one stays open while the card is
// redrawn (new receipts, a fresh PR state).
// `seen`: the owner opened it (it is read now).
const openReports = new Set();
export function expandTask(node, open = !node.classList.contains("expanded"), seen = false) {
  node.classList.toggle("expanded", open);
  const toggle = node.querySelector(".expand");
  if (toggle) toggle.textContent = open ? "Hide report" : "Show report";
  if (node.dataset.task) openReports[open ? "add" : "delete"](node.dataset.task);
  if (open && seen) readTask(node.dataset.task);
}
// A row shows its whole card in place (its title whole), and stays so while redrawn.
const unfolded = new Set();
export function unfoldTask(node, open = !node.classList.contains("unfolded"), seen = false) {
  const changed = node.classList.contains("unfolded") !== open;
  node.classList.toggle("unfolded", open);
  // Its whole card drawn (or its short lines again).
  if (changed) node.redraw?.();
  // Its title says whether it is shown whole (to screen readers too).
  node.querySelector(":scope > .head .title")?.setAttribute("aria-expanded", String(open));
  if (node.dataset.task) unfolded[open ? "add" : "delete"](node.dataset.task);
  if (open && seen) readTask(node.dataset.task);
}
// A result the owner opened is read: its unread dot goes (asked once per task here).
const reading = new Set();
function readTask(taskId) {
  const task = tasks().find((item) => item.id === taskId);
  if (!task || task.read || task.status !== "review" || reading.has(taskId)) return;
  reading.add(taskId);
  // Only that it is read: the rest of the answer may be older than what came since.
  api(`/api/tasks/${taskId}/read`, { method: "POST", body: {} })
    .then((view) => {
      const now = tasks().find((item) => item.id === taskId);
      if (now && view?.read !== false) upsertTask({ ...now, read: true });
    })
    .catch(() => {});
}
// What the owner sent a task's worker from its row (Go on, an answer, a follow-up, changes to
// make), by task: its row says whether it got there (sent, received, sent again, not
// delivered), as the chat does, until a while after it was handled.
const sentFrom = new Map();
const SENT_SHOWN = 10 * 60000;
// "Handled" shows this long, then the row says nothing more of it.
const HANDLED_SHOWN = 60000;
const SENDING_SHOWN = 30000;
// `at`: taken before the request, which stores the message before it returns (it may take a
// while: its worker's session is opened first).
export function noteSent(task, at) {
  sentFrom.set(task.id, { sessionId: task.session_id, at });
}
export function sentState(task, now = Date.now()) {
  const sent = sentFrom.get(task.id);
  if (!sent || now - sent.at > SENT_SHOWN) return "";
  const found = latestDelivery(sent.sessionId, sent.at - 5000);
  if (found?.delivery === "processed" && now - found.changed > HANDLED_SHOWN) return "";
  // Nothing of it heard of in half a minute (it sent nothing new: a Go on clicked twice): no
  // "Sending…" left behind.
  if (!found && now - sent.at > SENDING_SHOWN) return "";
  return found?.delivery || "sending";
}
const deliveryKey = (task) => sentState(task);
function deliveryNote(task) {
  const shown = sentState(task);
  if (!shown) return null;
  const [text, tone] = DELIVERY_WORDS[shown] || ["Sending…", ""];
  return el("span", { class: `delivery ${tone}`.trim(), text, "data-tip": "What you sent its worker from here" });
}
// A waiting task's question to the owner, when its worker asked one: what it asks, why, the
// answers it offers (a click sends one) and what it does then. Free text is its Reply.
const questionOf = (task) => (task.stopped ? null : ownerQuestions().find((question) => question.task_id === task.id) || task.question || null);
const askedOf = (task) => {
  const question = questionOf(task);
  return question ? [question.id, question.text, question.why, question.options, question.next] : null;
};
export function questionParts(question, answer) {
  const options = (Array.isArray(question.options) ? question.options : []).map((option) => String(option?.choice ?? option?.text ?? option ?? "").trim()).filter(Boolean);
  return [
    question.why ? el("div", { class: "q-why" }, el("span", { class: "muted", text: "Why: " }), question.why) : "",
    options.length ? el("div", { class: "q-options", role: "group", "aria-label": "Answers it offers" }, ...options.map((option) => el("button", { type: "button", class: "secondary q-option", text: option, "data-tip": `Answer “${option}”`, onclick: () => answer(option) }))) : "",
    question.next ? el("div", { class: "q-next" }, el("span", { class: "muted", text: "Then: " }), question.next) : "",
  ].filter(Boolean);
}
function askedBlock(task, node, phase) {
  const question = phase.section === "needs" ? questionOf(task) : null;
  if (!question) return null;
  const error = el("span", { class: "error" });
  const key = `task:${task.id}`;
  const answer = (text) => once(key, null, async () => {
    error.textContent = "";
    state.leaving.add(key);
    node.classList.add("leaving");
    renderSidebar();
    scheduleHome();
    try {
      const at = Date.now();
      await api(`/api/questions/${encodeURIComponent(question.id)}/answer`, { method: "POST", body: { text } });
      noteSent(task, at);
      toast("Sent to the worker.");
      await loadState();
    } catch (failure) {
      node.classList.remove("leaving");
      error.textContent = failure.message;
    }
    state.leaving.delete(key);
    renderSidebar();
    scheduleHome();
  });
  const text = question.text || "";
  const parts = questionParts(question, answer);
  return el("div", { class: "asked" }, text ? el("div", { class: "q-text markdown", html: markdown(text) }) : "", ...parts, error);
}
function taskCard(task, phase = phaseOf(task)) {
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  const [meta, help] = taskMeta(task, phase);
  const worker = byId(task.session_id);
  // In a draft's card its title opens its worker at this task's report: it may be on other
  // work since. On the board a row's title shows it whole (its worker is at its foot).
  const openAt = task.session_id ? () => select(task.session_id, { task: task.id }) : null;
  const row = isRow(phase);
  // A row's "⋯" stays in its head, on one line with its title; what to do about it (and a form
  // the owner opens) takes a line of its own below.
  const menu = row ? el("span", { class: "actions inline row-menu" }) : null;
  const node = el(
    "div",
    { class: `card task ${task.status} in-${phase.section || "card"}${phase.marker ? ` marker-${phase.marker}` : ""}${task.blocked ? " blocked" : ""}${row ? " row" : ""}${row && unfolded.has(task.id) ? " unfolded" : ""}`, "data-session": task.session_id || undefined, "data-task": task.id },
    el(
      "div",
      { class: "head" },
      el("span", { class: dotClass(task, phase), "data-tip": unread(task, phase) ? "Not read yet" : undefined }),
      el("button", { type: "button", class: "title", text: task.title, "data-tip": titleTip(task, phase), "aria-expanded": row ? String(unfolded.has(task.id)) : undefined, onclick: () => (row ? unfoldTask(node, undefined, true) : openAt?.()), disabled: !openAt && !row }),
      el("span", { class: "tid", text: task.id, "data-tip": "Task id" }),
      el("span", { class: "meta", "data-tip": help }, ...metaParts(meta)),
      menu,
      // A row shown whole folds back here, with its title or with Escape.
      row ? el("button", { type: "button", class: "fold icon", text: "▴", "data-tip": "Fold it back into a row (Esc)", "aria-label": "Fold", onclick: () => foldBack() }) : null,
    ),
  );
  function foldBack() {
    unfoldTask(node, false);
    node.querySelector(":scope > .head .title")?.focus();
  }
  const shown = !row || unfolded.has(task.id);
  node.redraw = () => refreshTaskCard(node, task, phase);
  if (row) {
    if (!shown) node.append(rowLine(task, worker, phase));
    const asked = askedBlock(task, node, phase);
    if (asked) node.append(asked);
    node.append(rowFoot(task, worker, phase, shown));
    node.addEventListener("click", (event) => {
      // A click on the row (not on what is in it to use: a link, a button, a form) shows it.
      if (!node.classList.contains("unfolded") && !event.target.closest("a, button, .menu-wrap, .actions, .asked, textarea, input, select, summary")) unfoldTask(node, true, true);
    });
    node.addEventListener("keydown", (event) => {
      // Not while typing in a form on it, nor to close its open menu.
      if (event.key !== "Escape" || !node.classList.contains("unfolded") || event.target.closest("input, textarea, select") || node.querySelector(".menu:not([hidden])")) return;
      event.preventDefault();
      foldBack();
    });
  }
  if (shown) {
    for (const part of [taskBody(task, phase), newsBlock(task)]) if (part) node.append(part);
    if (task.status === "review") node.append(...[chips(task, phase), report(task)].filter(Boolean));
  }
  expandTask(node, task.status === "review" && openReports.has(task.id));
  const key = `task:${task.id}`;

  // The row leaves its place at once (`moves`); the task's new state puts it where it goes.
  // One request at a time: a second click (or Enter) while it is out does nothing.
  const act = (path, payload, done, moves = true, sends = false) => once(key, null, () => send(path, payload, done, moves, sends));
  async function send(path, payload, done, moves, sends) {
    for (const button of actions.querySelectorAll("button")) button.disabled = true;
    error.textContent = "";
    if (moves) {
      state.leaving.add(key);
      node.classList.add("leaving");
      renderSidebar();
      scheduleHome();
    }
    try {
      const at = Date.now();
      const view = await api(path, { method: "POST", body: payload || {} });
      state.leaving.delete(key);
      // What it sent its worker: its row says whether it got there.
      if (sends) noteSent(task, at);
      showActions();  // The form is done: the card may be rebuilt from now on.
      upsertTask(view);
      // After the board is redrawn: a card that moved is gone by then; one that stays shows.
      requestAnimationFrame(() => requestAnimationFrame(() => node.classList.remove("leaving")));
      // What it came to, as the server has it now (a cancelled task that had finished is set
      // aside, say), when `done` asks.
      const said = typeof done === "function" ? done(view) : done;
      if (said) toast(said);
    } catch (failure) {
      state.leaving.delete(key);
      node.classList.remove("leaving");
      error.textContent = failure.message;
      // Redrawn meanwhile (it finished as the owner cancelled it, say): why, where it is seen.
      if (!error.isConnected) toast(failure.message, "error");
      for (const button of actions.querySelectorAll("button")) button.disabled = false;
      renderSidebar();
      scheduleHome();
    }
  }
  function form(input, label, send) {
    input.addEventListener("keydown", (event) => {
      if (submits(event) && !event.shiftKey) {
        event.preventDefault();
        send();
      }
    });
    actions.replaceChildren(input, error, el("button", { type: "button", class: "text", text: "Cancel", onclick: showActions }), el("button", { type: "button", text: label, onclick: send }));
    actions.dataset.editing = "1";
    place();
    input.focus();
  }
  // Escape closes a form opened on it (an answer, why to cancel, a new title), as Cancel does.
  actions.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !actions.dataset.editing) return;
    event.preventDefault();
    event.stopPropagation();
    showActions();
    actions.querySelector("button:not(:disabled)")?.focus();
  });
  // With only its "⋯", that stays in the head, on one line with the title; a main action (and
  // a form the owner opens) takes a line of its own. A row's "⋯" is in its head always.
  function place() {
    const inline = !row && !actions.querySelector(":scope > button:not(.more-button)") && !actions.dataset.editing;
    actions.classList.toggle("inline", inline);
    const parent = inline ? node.querySelector(":scope > .head") : node;
    // In the head, before a row's fold button (that stays at the end).
    if (actions.parentElement !== parent) parent.insertBefore(actions, inline ? parent.querySelector(":scope > .fold") : null);
  }
  function reply(placeholder, done, moves = true, path = "follow-up") {
    const input = el("textarea", { rows: 2, placeholder });
    form(input, "Send", () => input.value.trim() && act(`/api/tasks/${task.id}/${path}`, { text: input.value.trim() }, done, moves, true));
  }
  function confirmCancel() {
    const reason = el("input", { placeholder: "Why? (optional)" });
    form(reason, "Cancel task", () => act(`/api/tasks/${task.id}/cancel`, { text: reason.value.trim() }, cancelledWords));
    actions.lastElementChild.className = "danger";
    actions.querySelector(".text").textContent = "Keep it";
  }
  function rename() {
    // As long as the server keeps a title: not cut off there mid-word.
    const input = el("input", { value: task.title, "aria-label": "Title", placeholder: "What it delivers · repo#PR", maxlength: TITLE_MAX, title: `At most ${TITLE_MAX} characters` });
    form(input, "Rename", () => input.value.trim() && act(`/api/tasks/${task.id}/rename`, { text: input.value.trim() }, "Renamed.", false));
    input.select();
  }
  // Its latest result is about something else (the server says when: `splittable`): after a
  // look at what stays and what moves, it becomes a task of its own.
  async function split() {
    const box = el("div", { class: "split-preview", text: "Looking at what would move…" });
    const cancel = el("button", { type: "button", class: "text", text: "Cancel", onclick: showActions });
    actions.replaceChildren(box, error, cancel);
    actions.dataset.editing = "1";
    place();
    let preview;
    try {
      preview = await api(`/api/tasks/${task.id}/split-preview`);
    } catch (failure) {
      box.textContent = failure.message;
      return;
    }
    if (!box.isConnected) return;
    if (!preview.allowed) {
      box.textContent = preview.why || "Its results are one piece of work: nothing to move.";
      return;
    }
    const moves = preview.moves || {};
    const messages = moves.messages || [];
    const input = el("input", { placeholder: "What the new task delivers", "aria-label": "Title of the new task", autocomplete: "off" });
    box.replaceChildren(
      el("div", { class: "hint", text: "Its latest result becomes a task of its own; this one goes back to the result before it. You can undo it for a while." }),
      // The server's doubt (its results look like one piece of work): said before it is done.
      preview.why ? el("div", { class: "doubt", text: preview.why }) : "",
      el("div", { class: "keeps" }, el("strong", { text: "Stays here: " }), gist(preview.keeps?.result || "") || "no earlier result"),
      el("div", { class: "moves" }, el("strong", { text: "Moves: " }), gist(moves.result || "") || "its latest result"),
      messages.length
        ? el("details", { class: "moved" }, el("summary", { text: `and ${plural(messages.length, "message")} after it` }), el("ul", {}, ...messages.map((message) => el("li", { text: shorten(gist(message.text || ""), 140) }))))
        : el("div", { class: "moved", text: "No messages move with it." }),
    );
    const go = () => input.value.trim() && act(`/api/tasks/${task.id}/split`, { text: input.value.trim() }, "Moved to a new task. Undo split is on its card for a while.", false);
    input.addEventListener("keydown", (event) => {
      if (submits(event)) {
        event.preventDefault();
        go();
      }
    });
    actions.replaceChildren(box, input, error, cancel, el("button", { type: "button", text: "Move it", onclick: go }));
    input.focus();
  }
  function retryWithNote() {
    const input = el("textarea", { rows: 2, placeholder: "What should it do differently?" });
    form(input, "Retry", () => act(`/api/tasks/${task.id}/retry`, { text: input.value.trim() }, "Retrying."));
  }
  // Its whole result and what it delivered, in place.
  function viewReport() {
    if (row) unfoldTask(node, true, true);
    expandTask(node, true, true);
    node.querySelector(":scope > .report")?.scrollIntoView({ block: "nearest" });
  }
  function showActions() {
    delete actions.dataset.editing;
    const host = actions.closest(".card");
    if (host && host !== node) {
      // This form moved into a newer card for the task: that card shows its own actions.
      actions.remove();
      delete host.dataset.key;
      scheduleHome();
      return;
    }
    // Its worker is at the foot of its row; in a draft's card, here.
    const worker = !row && task.session_id ? { text: "Open worker", onclick: openAt } : null;
    const cancel = { text: "Cancel task…", onclick: confirmCancel, danger: true };
    const note = { text: "Add a note…", onclick: () => reply("Tell the worker now (it reads it within its current turn)", "Sent to the worker.", false) };
    const copy = { text: "Copy report", onclick: () => copyReport(task) };
    const pr = createsPr(task) ? { text: "Create PR", onclick: () => act(`/api/tasks/${task.id}/pr`, {}, "PR opened.", false) } : null;
    const named = { text: "Rename…", onclick: rename };
    const splitOff = task.splittable ? { text: "Move the latest result to a new task…", onclick: split } : null;
    // Split off another task a moment ago: back into it, until the server's time is up.
    const until = undoUntil(task);
    const from = until ? tasks().find((item) => item.id === task.split_from.task_id)?.title || "the task it came from" : "";
    const undo = until ? el("button", { type: "button", class: "secondary", text: "Undo split", "data-tip": `Back into “${from}” (until ${clockTime(until)})`, onclick: () => act(`/api/tasks/${task.id}/merge-back`, {}, `Back in “${from}”.`) }) : null;
    const followUp = { text: "Follow up…", onclick: () => reply("What else should it do?", "Sent to the worker.") };
    const accept = () => act(`/api/tasks/${task.id}/accept`, {}, "Accepted.");
    // A result to accept: its report in sight, a follow-up, changes to make, or Accept.
    const quiet = (text, onclick, tip = "") => el("button", { type: "button", class: "text", text, "data-tip": tip || undefined, onclick });
    const viewing = quiet("View report", viewReport, "Its whole result, and what it delivered");
    const following = quiet("Follow up", () => reply("What else should it do?", "Sent to the worker."), "Ask more about it; its result stays yours to accept");
    const changes = quiet("Request changes", () => reply("What should change?", "Sent back with your changes.", true, "request-changes"), "Send it back to its worker with what should change");
    const nudge = el("button", { type: "button", text: "Nudge", "data-tip": "Ask its worker where this task stands", onclick: () => act(`/api/tasks/${task.id}/nudge`, {}, "Asked its worker where it stands.", false) });
    let main = null;
    let shown = [];
    let more = [];
    if (task.status === "done" && PR_TO_FIX.includes(phase.marker)) {
      // Its PR has failing CI, merge conflicts or changes asked for: tell its worker what to do
      // about it (a result not accepted yet can be, as it is).
      const about = { "pr-failing": "the failing checks", "pr-conflicts": "the merge conflicts", "pr-changes": "the changes asked for" }[phase.marker];
      main = el("button", { type: "button", text: "Ask it to fix…", onclick: () => reply(`What should it do about ${about}?`, "Sent to the worker.") });
      more = [followUp, copy, named, worker];
    } else if (task.status === "review") {
      // Every result waits for the owner's Accept, a report that only informs too.
      main = el("button", { type: "button", text: "Accept", onclick: accept });
      shown = [viewing, following, changes];
      more = [copy, pr, named, splitOff, worker];
    } else if (task.status === "waiting" && task.stopped) {
      // Stopped mid-work: go on where it stopped, or with a word on what to do instead.
      main = el("button", { type: "button", text: "Go on", onclick: () => act(`/api/tasks/${task.id}/follow-up`, { text: "Go on where you stopped." }, "Sent to the worker.", true, true) });
      more = [{ text: "Go on with a note…", onclick: () => reply("What should it do now?", "Sent to the worker.") }, named, worker, cancel];
    } else if (task.status === "waiting") {
      main = el("button", { type: "button", text: "Reply", onclick: () => reply("Your answer", "Sent to the worker.") });
      more = [named, worker, cancel];
    } else if (task.status === "failed") {
      main = el("button", { type: "button", text: "Retry", onclick: () => act(`/api/tasks/${task.id}/retry`, {}, "Retrying.") });
      more = [{ text: "Retry with a note…", onclick: retryWithNote }, named, worker, cancel];
    } else if (task.status === "running" && ["stalled", "overdue", "idle", "check-failed"].includes(phase.marker)) {
      // Nothing moves on it: ask its worker where it stands, or end it.
      main = nudge;
      more = [note, named, worker, cancel];
    } else if (task.status === "running" || task.status === "queued") {
      more = [note, task.status === "running" ? { text: "Nudge", onclick: () => nudge.click() } : null, named, worker, cancel];
    } else if (task.status === "done" && task.news) {
      // News about finished work: seen, it goes back where it was.
      main = el("button", { type: "button", text: "Got it", onclick: () => act(`/api/tasks/${task.id}/news-seen`, {}, "") });
      more = [{ text: "Follow up…", onclick: () => reply("What should it do about it?", "Sent to the worker.") }, copy, pr, named, worker];
    } else if (task.status === "done") {
      more = [followUp, copy, pr, named, splitOff, worker];
    } else {
      more = [named, worker];
    }
    // Runs its worker said go on that nothing told of for a day: the owner says what became of
    // them (ended, or no longer to follow), in its menu.
    const urls = unknownRuns(task);
    if (urls.length && ["review", "done"].includes(task.status)) {
      const what = urls.length > 1 ? "Runs marked ended." : "Marked ended.";
      more = [{ text: "Mark ended", onclick: () => act(`/api/tasks/${task.id}/runs`, { how: "ended", urls }, what) }, { text: "Stop tracking", onclick: () => act(`/api/tasks/${task.id}/runs`, { how: "untracked", urls }, "No longer tracked.") }, ...more];
    }
    const others = moreMenu(more, "More actions", "⋯", moreName(task)) || "";
    if (menu) menu.replaceChildren(others);
    actions.replaceChildren(error, menu ? "" : others, undo || "", ...shown, main || "");
    place();
  }
  showActions();
  return node;
}
function taskBody(task, phase = {}) {
  const worker = byId(task.session_id);
  if (task.status === "queued" && !task.blocked) {
    const reason = queuedReason(task);
    return el("div", { class: "task-body" }, el("div", { class: "reason", text: reason.text, title: reason.help || "" }));
  }
  if (task.status === "running") return runningBody(task, worker, phase);
  if (phase.section === "running" && task.status === "review") {
    // Its worker is at it again, on what the owner asked about it.
    return el("div", { class: "task-body" }, activityLine(worker), el("div", { class: "reason", text: phase.why }));
  }
  if (task.status === "done" || task.status === "cancelled") {
    // Over: what it came to, and what it delivered in reach (its PRs, its documents, the run
    // it made, the threads it still follows); on a row, what it reported before, folded.
    const first = isRow(phase) ? gist(task.result || "") : "";
    // Monitoring: what it waits on; under Needs you for its PR: what is wrong there.
    const waits = phase.section === "monitoring" || PR_TO_FIX.includes(phase.marker) ? waitLine(task, worker, phase) : null;
    const found = artifacts(task, worker, { active: phase.section === "monitoring" || phase.marker === "still-following" });
    const nodes = [first ? el("div", { class: "outcome", text: first }) : null, waits, found, isRow(phase) ? earlierBlock(task) : null].filter(Boolean);
    return nodes.length ? el("div", { class: "task-body" }, ...nodes) : null;
  }
  if (task.status === "review") {
    // What came out, in its first sentences (past a "## Summary" title); the report on demand.
    const first = gist(task.result || task.progress || "");
    // Under Needs you for its PR: what is wrong there.
    const waits = phase.section === "monitoring" || PR_TO_FIX.includes(phase.marker) ? waitLine(task, worker, phase) : null;
    const nodes = [first ? el("div", { class: "outcome", text: first }) : null, waits].filter(Boolean);
    return nodes.length ? el("div", { class: "task-body" }, ...nodes) : null;
  }
  const text = task.progress || (task.blocked ? "It waits for a task that ended without finishing." : "");
  const found = artifacts(task, worker);
  if (!text && !found) return null;
  const wrapper = el("div", { class: "task-body" });
  if (text) foldable(wrapper, el("div", { class: "body markdown", html: markdown(text) }), text, { chars: 360, lines: 5 });
  if (found) wrapper.append(found);
  return wrapper;
}
// A step as a line on a card: what it is for (a command's description, when it gave one), or
// the command without the "cd" into its own folder.
function stepText(text, cwd) {
  let line = text.replace(/^(bash|shell|powershell|zsh) /, "$ ");
  if (cwd) line = line.replace(`cd ${cwd} && `, "").replace(`cd "${cwd}" && `, "");
  return compactPaths(line, cwd);
}
// Its latest report on this task, in its first sentences: two lines, the rest on a click; when
// it said it, below, never cut off. Stays open across redraws; old news shows gray.
const openProgress = new Set();
function progressLine(text, at, taskId = "", stale = false) {
  const words = gist(text);
  if (!words) return null;
  const line = el("div", { class: `progress-line${openProgress.has(taskId) ? " open" : ""}`, title: "Its latest report on this task · click to read it all" }, el("span", { class: "words", text: words }));
  line.addEventListener("click", () => {
    line.classList.toggle("open");
    if (taskId) openProgress[line.classList.contains("open") ? "add" : "delete"](taskId);
  });
  return el("div", { class: `progress${stale ? " stale" : ""}` }, line, at ? el("div", { class: "when", text: `Reported ${since(at)}` }) : "");
}
// Its links that are not what it delivered, folded away by what each is to the owner: what else
// it made (a file it wrote, a mockup, a plain link it declared), what is under a run (a Spark
// application, logs, a CI run), what it only points to (a document it mentions, an issue, a PR
// it did not make or review, a thread) and what was taken off. `shown`: keys on chips already
// (its deliverables); `mentioned`, `off`: chips of PRs it only mentions, of deliverables taken
// off.
function linkGroups(task, shown, mentioned = [], off = []) {
  const more = { made: [], diagnostic: [], reference: [...mentioned], off: [...off] };
  for (const link of taskLinks(task)) {
    const { found, said } = link;
    if (shown.has(link.key) || (found.key && shown.has(found.key))) continue;
    shown.add(link.key);
    if (found.key) shown.add(found.key);
    const role = link.role === "pr" ? "reference" : link.role;
    const made = ["output", "run"].includes(role);
    const taken = made && isHidden(link.key, toneOf(said?.status));
    const chip = linkChip(task, link, role, made && !taken);
    if (taken) more.off.push(chip);
    else if (made) more.made.push(chip);
    else (more[role] || more.reference).push(chip);
  }
  const folded = [...more.made, ...more.diagnostic, ...more.reference, ...more.off];
  if (!folded.length) return null;
  const words = [more.made.length ? `${more.made.length} more made` : "", more.diagnostic.length ? `${more.diagnostic.length} for diagnosis` : "", more.reference.length ? `${more.reference.length} referenced` : "", more.off.length ? `${more.off.length} taken off` : ""].filter(Boolean).join(" · ");
  return el("details", { class: "more-links" }, el("summary", { text: `More links: ${words}` }), el("div", { class: "receipts" }, ...folded));
}
// A link as a chip: its name (and where it stands, as its worker said); a file opens here.
function linkChip(task, link, role, removable) {
  const { found, said } = link;
  const name = linkName(link);
  const file = link.path && found.kind === "file";
  const label = `${file ? "file · " : ""}${name}${said?.status ? ` · ${said.status}` : ""}`;
  const size = link.file?.size ? `${Math.max(1, Math.round(link.file.size / 1024))} KB` : "";
  // What it is and its id (a document's) on hover, never as its name.
  const help = [said?.status ? `${name} · ${said.status}` : name, found.id ? `${found.label} ${found.id}` : link.name && found.title ? found.title : "", size, link.path || link.url].filter(Boolean).join("\n");
  const open = file
    ? el("button", { type: "button", class: "chip-link", text: label, "data-tip": `${help}\nClick to open it`, onclick: () => openFile(link.path, task.session_id) })
    : el("a", { class: "chip-link", href: link.url, target: "_blank", rel: "noreferrer", "data-tip": help, text: label });
  const chip = el("span", { class: `receipt link-chip kind-${found.kind} ${role}${said ? ` tone-${toneOf(said.status)}` : ""}` }, open);
  if (removable) chip.append(offButton(link.key, name, toneOf(said?.status)));
  return chip;
}
const offButton = (key, name, tone) => el("button", { type: "button", class: "chip-off", text: "✕", "data-tip": "Take it off this card and Artifacts (it comes back if it fails)", "aria-label": `Take ${name} off`, onclick: () => setHidden(key, true, tone) });
// What a task delivered, as chips: the same as Home's Artifacts has of it (its PRs, the docs it
// wrote, the flows it ran: deliverablesOf), each PR as GitHub has it (one it follows for this
// task can be let go from its chip), a doc or a flow with ✕ to take it off. `shown`: the keys
// on these chips; `off`: chips of those taken off, for the fold.
function deliverableChips(task, phase) {
  const shown = new Set();
  const list = [];
  const off = [];
  const followed = new Set([...(task.watching || []), ...(task.watch_unreadable || [])].map((key) => key.toLowerCase()));
  for (const item of deliverablesOf(task, phase)) {
    if (item.kind === "PR") {
      const label = item.key.slice("pr:".length);
      shown.add(label);
      shown.add(`github.com/${label.replace("#", "/pull/")}`);
      const chip = item.pr ? prChip(item.pr, item.role) : deliveredChip(item, false);
      const key = `github:${label}`;
      list.push(followed.has(key) ? el("span", { class: "pr-followed" }, chip, stopButton(key, label.replace(/^[^/]+\//, ""), task)) : chip);
      continue;
    }
    shown.add(item.key);
    if (isHidden(item.key, item.tone)) off.push(deliveredChip(item, false));
    else list.push(deliveredChip(item, true));
  }
  return { list, shown, off };
}
// A doc or a flow it delivered (or a PR GitHub was not read for yet): its kind, its title and
// where it stands as its worker said (its task's state is on its row already).
function deliveredChip(item, removable) {
  const role = PR_ROLES[item.role] ? ` · ${PR_ROLES[item.role].toLowerCase()}` : "";
  const help = [`${item.kind}${role}: ${item.name}`, item.status ? `${item.status} (${item.said ? "as its worker said" : "as its task stands"})` : "", item.url].filter(Boolean).join("\n");
  const chip = el("span", { class: `receipt link-chip kind-${item.kind.toLowerCase()} output tone-${item.tone}` }, el("a", { class: "chip-link", href: item.url, target: "_blank", rel: "noreferrer", "data-tip": help, text: `${item.kind === "PR" ? "" : `${item.kind} · `}${item.name}${item.status && item.said ? ` · ${item.status}` : ""}` }));
  if (removable) chip.append(offButton(item.key, item.name, item.tone));
  return chip;
}
const WAITS_WORDS = { review: "Review draft", github: "Reply draft", slack: "Slack draft", reply: "Reply draft", merge: "Ready to merge", question: "Question", form: "Form", link: "Link to open", signin: "Sign-in", workspace: "Where to work" };
// Cards under Needs you from this task's worker about this task (one it names, else the task it
// is on now).
function ownerItems(task, worker) {
  const about = (taskId) => (taskId ? taskId === task.id : worker?.task?.task_id === task.id);
  const drafts = pendingDrafts().filter((draft) => draft.session_id === task.session_id && draft.kind !== "decision" && about(draft.target?.task_id));
  const questions = ownerQuestions().filter((question) => question.session_id === task.session_id && about(question.task_id));
  return { drafts, questions };
}
const waitsOnOwner = (task, worker) => {
  const { drafts, questions } = ownerItems(task, worker);
  return [...drafts.map((draft) => draft.id), ...questions.map((question) => question.at)].join(",");
};
// Stop following a thread for this task (its worker's other tasks keep following it).
async function stopFollowing(key, label, task) {
  try {
    const { all } = await api(`/api/sessions/${task.session_id}/unfollow`, { method: "POST", body: { text: key, task_id: task.id } });
    toast(unfollowedWords(key, label, all, task));
  } catch (error) {
    toast(error.message, "error");
  }
}
// What stopping comes to: a Slack thread's new messages go to the Router; a PR is read only
// while someone follows it, so with nobody left its news reaches no one.
export function unfollowedWords(key, label, all, task) {
  if (!all) return `Stopped following ${label} for this task; its worker still follows it for another task.`;
  if (!/^github:[^/#]+\/[^#]+#\d+$/i.test(key)) return `Stopped following ${label}: new messages there go to the Router.`;
  const same = (watched) => watched.toLowerCase() === key.toLowerCase();
  const others = tasks().some((other) => other.session_id !== task.session_id && (other.watching || []).some(same));
  return others ? `Stopped following ${label}; another worker still follows it.` : `Stopped following ${label}. Nobody follows it now: its news reaches no one.`;
}
const stopButton = (key, label, task) => el("button", { type: "button", class: "link unfollow", text: "✕", "data-tip": `Stop following ${label}`, "aria-label": `Stop following ${label}`, onclick: () => stopFollowing(key, label, task) });
// A thread its worker follows, as a link to it: a PR on GitHub, a Slack conversation when its
// link is known.
function watchingChip(key, task) {
  const pr = /^github:([^/#]+\/[^#]+)#(\d+)$/.exec(key);
  const title = pr ? prTitle(`${pr[1]}#${pr[2]}`) : "";
  const label = pr ? `${pr[1].split("/")[1]}#${pr[2]}${title ? ` · ${shorten(title, 36)}` : ""}` : threadWords(key);
  const href = pr ? `https://github.com/${pr[1]}/pull/${pr[2]}` : state.snapshot?.thread_links?.[key] || "";
  const help = `It follows ${key}${title ? ` (“${title}”)` : ""}: what happens there comes to it`;
  const name = href ? el("a", { class: "watch-name", href, target: "_blank", rel: "noreferrer", text: `Watching ${label}` }) : el("span", { class: "watch-name", text: `Watching ${label}` });
  // Any thread or PR it follows can be let go (not only one it cannot read).
  return el("span", { class: "receipt watching", "data-tip": help }, name, stopButton(key, label, task));
}
// Cards under Needs you about this task (a draft its worker wrote, its question when its row
// does not show it), each a way there.
function ownerChips(task, worker, phase) {
  const { drafts, questions } = ownerItems(task, worker);
  // A review to post is not a reply (a reply answers a comment, or is its own PR's).
  const waitsKind = (draft) => (draft.kind === "github" && !(draft.target?.in_reply_to || draft.target?.reply_to_comment || draft.target?.own) ? "review" : draft.kind);
  return [
    ...drafts.map((draft) => el("button", { type: "button", class: "receipt needs", "data-tip": "Waiting for you under Needs you", text: `${WAITS_WORDS[waitsKind(draft)] || "Draft"} · Needs you`, onclick: () => focusDraft(draft.id) })),
    ...questions.filter((item) => item.task_id !== task.id || phase?.section !== "needs").map((question) => el("button", { type: "button", class: "receipt needs", "data-tip": question.text, text: "Question · Needs you", onclick: () => focusQuestion(question.id) })),
  ];
}
export function artifacts(task, worker = byId(task.session_id), { active = true, phase } = {}) {
  const items = [];
  const delivered = deliverableChips(task, phase);
  items.push(...delivered.list);
  const mentioned = (task.receipts?.prs || []).filter((pr) => !delivered.shown.has(pr.label.toLowerCase())).map((pr) => {
    delivered.shown.add(pr.label.toLowerCase());
    return prChip(pr);
  });
  const more = linkGroups(task, delivered.shown, mentioned, delivered.off);
  if (active && worker) {
    for (const key of followedOnce(task.watching || [])) {
      const pr = /^github:(.+)$/.exec(key)?.[1];
      if (pr && delivered.shown.has(pr.toLowerCase())) continue;
      items.push(watchingChip(key, task));
    }
  }
  // A thread it cannot read is not something it waits on: said, with what to do about it.
  for (const key of followedOnce(task.watch_failing || [])) items.push(failingChip(key, task));
  items.push(...ownerChips(task, worker, phase));
  if (!items.length && !more) return null;
  return el("div", { class: "artifacts" }, items.length ? el("div", { class: "receipts" }, ...items) : "", more || "");
}
// What could not be read, as GitHub said it, by kind: no access (another signed-in account may
// have it), not found, GitHub's rate limit or GitHub not answering (both tried again by
// themselves: Retry tries now), or a part of the PR only (its CI checks, say). `error`: the
// server's sentence for the owner (an older server's: what GitHub said).
const READ_PARTS = { account: "account", comments: "comments", "review comments": "review comments", reviews: "reviews", "CI checks": "CI checks" };
// Kinds that pass by themselves: tried again (Retry now), not something to stop following for.
export const PASSING = new Set(["transient", "rate_limited"]);
export function failingWords(rawKey, why = {}) {
  const label = threadWords(rawKey);
  // In the tooltip too, the PR as the owner reads it ("acme/app#72"), not its key.
  const key = rawKey.startsWith("github:") ? rawKey.slice("github:".length) : threadLabel(rawKey);
  const said = why.error ? ` GitHub said: ${why.error}` : "";
  const sentence = why.error ? ` ${why.error}` : "";
  const again = why.retry_at ? ` It tries again at ${clockTime(why.retry_at)}.` : " It keeps trying.";
  if (why.step === "delivery") return [`Can't pass on news from ${label}`, `It reads ${key}, but what it found could not be passed to its worker.${said}`];
  if (why.kind === "rate_limited") return [`GitHub's rate limit for ${label}`, `GitHub asked to wait before ${key} is read again.${sentence}${again}`];
  if (why.kind === "transient") return [`GitHub not answering for ${label}`, `Reading ${key} failed several times in a row.${sentence || said}${again}`];
  if (why.kind === "not_found") return [`${label} not found`, `GitHub does not find ${key} (deleted, moved, or not visible to your accounts).${sentence}`];
  const part = READ_PARTS[why.step];
  if (part && part !== "account") return [`Can't read ${label}'s ${part}`, `It reads the PR, but not its ${part}: news of that kind does not reach it.${why.kind === "permission" ? sentence : said}`];
  if (why.kind === "permission") return [`No access to ${label}`, `None of your GitHub accounts signed in here can read ${key}: sign in with one that can (gh auth login), or stop following it.${sentence}`];
  if (why.kind === "access") return [`No access to ${label}`, `None of your GitHub accounts signed in here can read ${key}: sign in with one that can (gh auth login), or stop following it.${said}`];
  return [`Can't read ${label}`, `It follows ${key} for this task, but it could not be read several times in a row: nothing there reaches it.${said || " The reason is not known yet."}`];
}
// A follow that cannot be read: what is wrong, in its words. One that passes by itself (GitHub
// slow, its rate limit) offers Retry and the details; one that will not (no access, not found)
// offers to stop following it.
function failingChip(key, task) {
  const label = threadWords(key);
  const why = task.watch_failing_why?.[key] || {};
  const [words, help] = failingWords(key, why);
  const chip = el("span", { class: "receipt warn failing", "data-tip": help }, el("span", { class: "words", text: words }));
  if (PASSING.has(why.kind)) {
    const retry = el("button", { type: "button", class: "link retry", text: "Retry", onclick: async () => {
      retry.disabled = true;
      try {
        await api(`/api/sessions/${task.session_id}/follow-retry`, { method: "POST", body: { key } });
        toast(`Reading ${label} again.`);
      } catch (error) {
        toast(error.message, "error");
      }
      retry.disabled = false;
    } });
    const details = [help, why.step ? `Step: ${why.step}` : "", why.detail ? `GitHub said: ${why.detail}` : "", why.at ? `Last tried ${since(why.at)}` : ""].filter(Boolean).join("\n");
    chip.append(" · ", retry, " · ", el("details", { class: "why-details" }, el("summary", { text: "details" }), el("div", { class: "detail", text: details })));
  } else {
    chip.append(" · ", el("button", { type: "button", class: "link", text: "stop following", onclick: () => stopFollowing(key, label, task) }));
  }
  return chip;
}
// What a worker does right now: the step it is in and for how long, or how long it has been
// quiet and what it watches meanwhile; its turn over while a command it left running goes on,
// that. What it said last is in the tooltip.
function activityLine(worker) {
  const working = worker?.status === "working";
  const raw = worker?.step || worker?.activity || "";
  const background = !working && worker?.background;
  const line = background ? BACKGROUND : raw ? stepText(raw, worker?.cwd) : working ? "Thinking…" : quiet(worker);
  const long = !background && worker?.step_since && ago(worker.step_since) !== "now";
  const said = worker?.said ? `\n\nSaid last: ${worker.said.replace(/\s+/g, " ").slice(0, 400)}` : "";
  // What it runs, in the tooltip: the line says what it is for.
  const command = worker?.step_command && worker.step_command !== raw ? `\n${worker.step_command}` : "";
  // The step is cut to fit; how long it has run stays in sight beside it.
  return el("div", { class: "activity", "data-tip": `${background ? `${BACKGROUND_HELP}\n` : ""}${raw || line}${command}${said}` }, el("span", { class: "step", text: `${worker?.step && !background ? "Now: " : ""}${line}` }), long ? el("span", { class: "when", text: ` · ${ago(worker.step_since)}` }) : "");
}
// A worker whose turn is over while a command it left running (a dev server) goes on.
export const BACKGROUND = "Waiting for background process";
const BACKGROUND_HELP = "Its turn is over; a command it started (a dev server, a watcher) still runs";
// A running task at a glance, by where it stands: at work (the step its worker is in and for
// how long), watching (what, and when it checks back), waiting its turn, or stalled; then
// this task's own latest progress and what it made.
function runningBody(task, worker, phase = {}) {
  const nodes = [];
  const section = phase.section || "running";
  if (section === "running") nodes.push(phase.marker === "terminal" ? el("div", { class: "activity" }, el("span", { class: "step", text: compactPaths(worker?.terminal?.activity || "Working in a terminal", worker?.cwd) })) : activityLine(worker));
  else nodes.push(waitLine(task, worker, phase));
  // Its own progress: what it last reported on this task (not its asides while it works);
  // gray once it is old news.
  const stale = section !== "running" && Date.now() - Date.parse(task.updated_at) > 3600000;
  const progress = progressLine(task.progress, task.progress ? task.updated_at : "", task.id, stale);
  if (progress) nodes.push(progress);
  const found = artifacts(task, worker, { active: true, phase });
  if (found) nodes.push(found);
  return el("div", { class: "task-body" }, ...nodes.filter(Boolean));
}
// What a task that is not being worked on waits for, and what wakes it: the PR or thread it
// follows for this task (and where the PR stands), the time it checks back, a slot, its turn,
// or nothing (then it says so): { text, help, warn }.
function waitWords(task, worker, phase) {
  const marker = phase.marker;
  let text = phase.why;
  let help = "";
  if (marker === "check-back" && task.check_back) {
    const back = task.check_back;
    text = `Checks back at ${clockTime(back.at)}: ${back.what}`;
    if (back.error) text += ` · could not wake it last time (${back.error}); trying again`;
    help = "It said when to look again; it is woken then";
  } else if (["watching", "delivered", "mergeable"].includes(marker) && obligationsOf(task).length) {
    // What it waits for and what wakes it, each thing it waits on (one it cannot read now is
    // said so: its card says why).
    const unreadable = new Set(task.watch_unreadable || []);
    // On its card, in a line: for what, on what, how long quiet.
    const bits = monitorBits(task);
    if (bits) text = [bits.first.unknown ? `${bits.label}: status unknown` : `${bits.what} ${bits.what === "Following" ? "" : "on "}${bits.label}`, bits.quiet].filter(Boolean).join(" · ");
    const waits = (item) => (item.unknown ? ": status unknown" : item.waiting_for ? `: waiting for ${item.waiting_for}` : "");
    help = [text, ...obligationsOf(task).map((item) => `${item.label || item.key}${waits(item)}${unreadable.has(item.key) ? " (can't read it now)" : ""}${item.trigger ? `\n  ${item.trigger}` : ""}`)].join("\n");
  } else if (marker === "queued") {
    const reason = queuedReason(task);
    text = reason.text;
    help = reason.help || "";
  } else if (marker === "paused") {
    help = "Its worker took it up, then went on to another task: it reports this one (done, a question) or goes back to it after";
  }
  if (PR_TO_FIX.includes(marker)) help = "Its PR, as GitHub has it now: tell its worker what to do about it here, or act on GitHub";
  else if (marker === "mergeable") help = `${help ? `${help}\n` : ""}GitHub would merge it now; you can merge it on GitHub (Merge cards come once per new commit)`;
  return { text, help: help || text, warn: ["idle", "overdue", "stalled", "check-failed", ...PR_TO_FIX].includes(marker) };
}
function waitLine(task, worker, phase) {
  const { text, help, warn } = waitWords(task, worker, phase);
  return text ? el("div", { class: `reason${warn ? " warn" : ""}`, text, "data-tip": help }) : null;
}
// What a monitored task waits on, in a few words with a link to it: the first thing (a PR, a
// thread, a flow), when it was last looked at and when it is next ("checked 5 min ago · next in
// 10 min"; a run Chief of Staff does not read itself, when its worker said where it stands:
// "reported running 3h ago"); the trigger on hover.
export function monitorBits(task) {
  const parts = monitorParts(task);
  if (!parts) return null;
  const { first } = parts;
  const label = first.kind === "pr" ? shortPr(first.label || first.key) : first.kind === "thread" ? threadWords(first.key) : first.label || first.key;
  const href = first.url || (first.kind === "thread" ? state.snapshot?.thread_links?.[first.key] || "" : "");
  const checked = first.last_check ? ago(first.last_check) : "";
  const reported = !checked && !first.unknown && first.kind === "flow" && first.reported_at ? ago(first.reported_at) : "";
  const said = reported === "now" ? `reported ${first.status} just now` : reported ? `reported ${first.status} ${spelled(reported)} ago` : "";
  const checks = [checked === "now" ? "checked just now" : checked ? `checked ${spelled(checked)} ago` : said, first.next_check ? nextWords(first.next_check) : ""].filter(Boolean).join(" · ");
  return { ...parts, label, href, checks };
}
// "o/app#7" as "app#7" (the server's label is that already, with its title).
const shortPr = (label) => String(label).replace(/^github:/, "").replace(/^[^/\s#]+\/(?=[^/\s#]+#\d)/, "");
const spelled = (passed) => passed.replace(/m$/, " min");
function nextWords(iso) {
  const left = (Date.parse(iso) - Date.now()) / 60000;
  if (!(left > 0)) return "next check due";
  const minutes = Math.max(1, Math.floor(left));
  return minutes < 60 ? `next in ${minutes} min` : minutes < 2880 ? `next in ${Math.floor(minutes / 60)}h` : `next in ${Math.floor(minutes / 1440)}d`;
}
// A row's second line: its reason (what it waits on, what it came to, why it needs the owner)
// and its latest progress beside it.
function rowLine(task, worker, phase) {
  const why = rowWhy(task, worker, phase);
  const latest = rowLatest(task, worker, phase);
  const watched = ["watching", "delivered", "mergeable"].includes(phase.marker) ? monitorBits(task) : null;
  const tip = (text, help) => (help && help !== text ? `${text}\n${help}` : text || undefined);
  const parts = watched
    ? [
        el("span", { class: "why", text: watched.what, "data-tip": why.help || watched.what }),
        watched.href ? el("a", { class: "object", href: watched.href, target: "_blank", rel: "noreferrer", text: watched.label, "data-tip": `${watched.first.label || watched.first.key}${watched.first.trigger ? `\n${watched.first.trigger}` : ""}` }) : el("span", { class: "object", text: watched.label }),
        watched.more ? el("span", { class: "also", text: `+${watched.more}`, "data-tip": why.help }) : "",
        watched.quiet ? el("span", { class: "quiet", text: watched.quiet, "data-tip": why.help || watched.quiet }) : "",
      ]
    : [why.text ? el("span", { class: `why${why.warn ? " warn" : ""}`, text: why.text, "data-tip": tip(why.text, why.help) }) : ""];
  const session = worker || closedWorkers().find((item) => item.id === task.session_id);
  return el("div", { class: "row-line" }, ...parts, latest.text ? el("span", { class: "latest", text: latest.text, "data-tip": latest.help || latest.text }) : "", workerRef(session, task.title, task.id));
}
// A row's foot: when its watch is looked at, whether what the owner sent from here reached its
// worker, cards about it under Needs you and what it delivered. Shown whole, the card has
// those: its worker (its second line's, folded) is here then.
function rowFoot(task, worker, phase, shown = false) {
  const delivered = deliveryNote(task) || "";
  if (shown) return el("div", { class: "row-foot" }, workerRef(worker || closedWorkers().find((item) => item.id === task.session_id), task.title, task.id), delivered);
  const watched = ["watching", "delivered", "mergeable"].includes(phase.marker) ? monitorBits(task) : null;
  const { list } = deliverableChips(task, phase);
  const extra = list.length > 2 ? el("span", { class: "more-made", text: `+${list.length - 2}`, "data-tip": "Show it all to see every one" }) : "";
  const waiting = ownerChips(task, worker, phase);
  return el(
    "div",
    { class: "row-foot" },
    watched?.checks ? el("span", { class: "checks", text: watched.checks, "data-tip": watched.first.trigger || "" }) : "",
    delivered,
    waiting.length ? el("span", { class: "row-chips row-needs" }, ...waiting) : "",
    list.length ? el("span", { class: "row-chips" }, ...list.slice(0, 2), extra) : "",
  );
}
// What a row says of its reason: what it waits on, why it needs the owner, or what it came to.
function rowWhy(task, worker, phase) {
  if (phase.section === "monitoring") {
    // "Paused: its worker is on …", "Checks back at 3:15 PM: …" beside the status "Paused",
    // "Checks back": said once.
    const words = waitWords(task, worker, phase);
    const said = new RegExp(`^${statusWord(phase).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?::\\s*|\\s+)`, "i");
    return { ...words, text: words.text.replace(said, "") };
  }
  if (phase.section === "finished") return { text: finishedWords(task, phase), help: "" };
  if (phase.section === "needs") {
    // A question is said in full on the row; a result to accept says what it came to (its
    // status says it waits for Accept), and why it needs a look when something does: its PR's
    // changes requested or failing CI, news on it since.
    const words = phase.marker === "question" ? "" : phase.why;
    const flagged = phase.marker === "review" && Boolean(phase.why) && !phase.why.startsWith("News: ");
    return { text: words, help: "", warn: flagged || ["failed", "blocked", "stalled", "check-failed", ...PR_TO_FIX].includes(phase.marker) };
  }
  return { text: phase.marker === "background" ? "" : phase.why, help: "" };
}
// A row's latest progress: what its worker does now (Running), else the latest it said of this
// task: its question or why it failed, its result (the newest: a newer report on it took the
// place of the one before, which its card keeps folded), what came of it.
export function rowLatest(task, worker, phase) {
  if (phase.section === "running") {
    if (phase.marker === "terminal") return { text: compactPaths(worker?.terminal?.activity || "Working in a terminal", worker?.cwd) };
    const line = activityLine(worker);
    return { text: line.textContent, help: line.dataset.tip };
  }
  if (phase.section === "finished") return { text: "" };
  if (phase.marker === "update") return { text: gist(task.news?.text || ""), help: task.news?.text };
  // Its question is said in full on the row.
  if (phase.section === "needs" && questionOf(task)) return { text: "" };
  // Failed after a retry, a result from before it is older than why it failed.
  const said = ["waiting", "failed"].includes(task.status) ? task.progress || task.result : ["review", "done"].includes(task.status) ? task.result || task.progress : task.progress;
  return { text: gist(said || ""), help: "" };
}
// What a finished task came to, in its first words ("Cancelled by you." is not said twice);
// never as if done when it was cancelled or set aside.
export function finishedWords(task, phase) {
  if (phase.marker === "still-following" || phase.marker === "reviewed") return phase.why;
  const said = gist(task.result || task.progress || "");
  if (task.status === "cancelled") return !said ? "Cancelled" : /^cancel/i.test(said) ? said : `Cancelled: ${said}`;
  if (phase.marker === "set-aside") return said ? `Set aside, not accepted: ${said}` : phase.why;
  return said || phase.why || "Accepted";
}
// A worker between turns on a running task: how long it has been quiet, and what it follows.
function quiet(worker) {
  if (!worker) return "Starting…";
  const follows = followedOnce(worker.follows || []).map(threadWords);
  return [`Quiet since ${since(worker.updated_at)}`, follows.length ? `follows ${follows.slice(0, 2).join(", ")}` : ""].filter(Boolean).join(" · ");
}
// A thread it follows in a few words: a PR as "app#7"; a Slack conversation by its name, as
// Slack shows it ("DM with Bob", "a thread in #eng").
export function threadWords(key) {
  if (!key.startsWith("slack:")) return key.replace(/^github:[^/]+\//, "");
  const label = threadLabel(key);
  return label === "Slack DM" ? "a Slack DM" : label.replace(/^Slack thread in /, "a thread in ").replace(/^Slack /, "");
}
// The threads it follows, each once: a thread in a Slack conversation it follows as a whole is
// that conversation, and two Slack threads that read the same are one.
export function followedOnce(keys) {
  const whole = new Set(keys.filter((key) => /^slack:[^:]+$/.test(key)));
  const seen = new Set();
  return keys.filter((key) => {
    const conversation = /^slack:([^:]+):/.exec(key)?.[1];
    if (conversation && whole.has(`slack:${conversation}`)) return false;
    const words = key.startsWith("slack:") ? threadWords(key) : key;
    if (seen.has(words)) return false;
    seen.add(words);
    return true;
  });
}
// News about finished work (its PR approved or merged, a reply handled), in sight until seen.
function newsBlock(task) {
  if (!task.news || !["review", "done"].includes(task.status)) return null;
  const wrapper = el("div", { class: "news" }, el("div", { class: "news-head" }, el("span", { class: "chip progress", text: "Update" }), el("span", { class: "when", text: since(task.news.at) })));
  foldable(wrapper, el("div", { class: "body markdown", html: markdown(task.news.text) }), task.news.text, { chars: 360, lines: 4 });
  return wrapper;
}
// Someone else's PR it reviewed: whether that review was sent, or waits on its card (rejected
// there or not written yet, nothing).
export const reviewedWords = (pr) => (!pr.reviewed || pr.own ? "" : { sent: "You reviewed it", drafted: "Its review waits on its card" }[pr.review_state] || "");
// A PR as GitHub has it now, in a few words: "app#7 · ✓ CI · approved"; its part in the task
// (opened, pushed to, reviewed) on hover, and in sight when it is not the task's own.
function prChip(pr, role = "") {
  const short = pr.label.replace(/^[^/]+\//, "");
  const named = pr.title || prTitle(pr.label);
  const state = pr.state === "MERGED" ? "merged" : pr.state === "CLOSED" ? "closed" : "";
  const ci = state ? "" : { passing: "✓ CI", failing: "✗ CI", pending: "CI running" }[pr.checks] || "";
  // Where it stands in a word, as Artifacts and Monitoring say it (changes requested wins over
  // merge conflicts and failing CI); a word only about CI is not said again beside the CI mark,
  // and "in review" (nothing to say yet) is left out.
  const [word] = prStatus(pr);
  const stands = word === "in review" || (word === "CI failing" && ci) || (word === "CI running" && ci) ? "" : word;
  const review = { APPROVED: "approved", CHANGES_REQUESTED: "changes requested" }[pr.review] || "";
  const failing = pr.checks === "failing" && pr.failing?.length ? `\nFailing: ${pr.failing.join(", ")}` : "";
  const ciWords = { passing: "Checks pass", failing: "Checks fail", pending: "Checks are running", none: "No checks" }[pr.checks] || "";
  const seen = pr.at ? `\nAs of ${new Date(pr.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "";
  const title = [pr.label, named ? `“${named}”` : "", ciWords + failing, !state && pr.awaiting?.length && `Waiting for approval: ${pr.awaiting.join(", ")}`, review && `Review: ${review}`, word === "ready to merge" && "GitHub would merge it now", word === "merge conflicts" && "It conflicts with its base branch: GitHub cannot merge it", state && `PR ${state}`, reviewedWords(pr), PR_ROLES[role] && `${PR_ROLES[role]} by this task`].filter(Boolean).join("\n") + seen;
  // Its number and title (shortened to fit), then where it stands (always in sight).
  const where = [state || ci, stands !== state ? stands : ""].filter(Boolean).join(" · ");
  return el(
    "a",
    { class: `receipt pr ${state || pr.checks}`, href: pr.url, target: "_blank", rel: "noreferrer", title },
    el("span", { class: "pr-name", text: named ? `${short} · ${shorten(named, 60)}` : short }),
    where ? el("span", { class: "pr-state", text: ` · ${where}` }) : "",
    ["modified", "reviewed"].includes(role) ? el("span", { class: "pr-role", text: ` · ${role}` }) : "",
  );
}
// What was observed while it worked, at a glance: the verdict, tests, changes, and what it
// delivered (as Artifacts has it).
export function chips(task, phase) {
  const receipts = task.receipts;
  const items = [];
  if (receipts?.verdict) {
    const [kind, text, help] = receipts.stale ? ["warn", "Out of date", receipts.stale] : VERDICT[receipts.verdict];
    const done = receipts.criteria.filter((item) => item.state === "verified").length;
    items.push(el("span", { class: `receipt verdict ${kind}`, title: help, text: receipts.verdict === "partial" ? `${text} · ${done}/${receipts.criteria.length}` : text }));
  }
  const result = (label, item) => {
    if (!item) return;
    if (item.no_tests) {
      // It ran, and found no test to run: no pass, and no failure.
      const again = item.running ? "\nRunning again now" : "";
      items.push(el("span", { class: "receipt warn", title: `${item.command}\n(${plural(item.runs, "run")})\nNo test ran${again}`, text: `no ${label} ran${item.running ? " · running again" : ""}` }));
      return;
    }
    if (item.passed === null) {
      items.push(el("span", { class: "receipt warn", title: `${item.command}\nRunning now`, text: `… ${label} running` }));
      return;
    }
    const mark = item.passed ? (item.failed_before ? "✗→✓" : "✓") : "✗";
    const before = item.failed_before ? "\nFailed first, then passed" : "";
    const again = item.running ? "\nRunning again now" : "";
    // The PR's checks passed after this run failed here: CI has the newer word.
    const ci = !item.passed && item.ci_passing?.length;
    const why = ci ? `\nThe checks on ${item.ci_passing.join(", ")} pass now` : "";
    items.push(el("span", { class: `receipt ${item.passed ? "pass" : ci ? "warn" : "fail"}`, title: `${item.command}\n(${plural(item.runs, "run")})${before}${again}${why}`, text: `${mark} ${label}${ci ? " locally" : ""}${item.running ? " · running again" : ""}` }));
  };
  // Its tests, when it ran them on its own work (its checkout); tests run elsewhere (someone's
  // source, to check a claim) are evidence in its report, not this.
  if (receipts?.tests?.own) result("tests", receipts.tests);
  result("lint", receipts?.checks);
  result("build", receipts?.build);
  const changed = receipts?.changed || {};
  // What others changed in the owner's folder meanwhile is not its own: said quietly beside it.
  const others = notCountedText(changed);
  const othersHelp = others ? `\n${others[0].toUpperCase()}${others.slice(1)} changed there meanwhile by you or other work: not this task's` : "";
  if (changed.files) items.push(el("button", { type: "button", class: "receipt changes", title: `See the changes and comment on them${othersHelp}`, onclick: () => showChanges(task, upsertTask) }, changesText(changed), others ? el("span", { class: "not-counted", text: ` · ${others}` }) : ""));
  const delivered = deliverableChips(task, phase);
  items.push(...delivered.list);
  const mentioned = (task.receipts?.prs || []).filter((pr) => !delivered.shown.has(pr.label.toLowerCase())).map((pr) => {
    delivered.shown.add(pr.label.toLowerCase());
    return prChip(pr);
  });
  const more = linkGroups(task, delivered.shown, mentioned, delivered.off);
  const flags = (receipts?.flags || []).map((flag) => el("div", { class: "flag", text: flag }));
  const toggle = el("button", { type: "button", class: "expand link", text: "Show report", onclick: (event) => expandTask(event.currentTarget.closest(".card"), undefined, true) });
  return el("div", { class: "chips" }, el("div", { class: "receipts", "data-tip": "Observed while it worked, not just reported" }, ...items, toggle), more || "", ...flags);
}
// The whole report: what the worker wrote, each criterion and how it was checked.
function report(task) {
  const receipts = task.receipts || {};
  const text = task.result || task.progress || "";
  const more = [];
  const changed = receipts.changed || {};
  if (changed.commits) more.push(el("span", { class: "receipt", text: plural(changed.commits, "commit") }));
  if (changed.uncommitted) more.push(el("span", { class: "receipt warn", text: `${changed.uncommitted} uncommitted` }));
  // No changes of its own, but others changed its folder meanwhile: said, not counted.
  if (!changed.files && notCountedText(changed)) more.push(el("span", { class: "receipt none", title: "Changed in your folder meanwhile by you or other work: not this task's", text: notCountedText(changed) }));
  if (receipts.tests && !receipts.tests.own) more.push(testsElsewhere(receipts.tests));
  for (const item of receipts.version || []) {
    more.push(el("span", { class: "receipt none", title: "The code these results are about", text: `at ${item.commit.slice(0, 7)}${item.edited ? " + edits" : ""}` }));
  }
  if (!receipts.verdict && !receipts.tests && !changed.files && !(receipts.prs || []).length) {
    more.push(el("span", { class: "receipt none", text: receipts.observed ? `${plural(receipts.observed, "command")}, no tests or changes observed` : "Nothing observed" }));
  }
  const criteria = (receipts.criteria || []).length
    ? el(
        "ul",
        { class: "criteria" },
        ...receipts.criteria.map((item) => {
          const [kind, mark, help] = CRITERION[item.state] || CRITERION.not_checked;
          const how = item.command ? `\n${item.command}${item.exit_code ? ` (exit ${item.exit_code})` : ""}` : item.evidence ? `\n${item.evidence}` : "";
          // `code` in a criterion shows as code.
          const words = item.criterion.split("`").map((part, index) => (index % 2 ? el("code", { text: part }) : part));
          return el("li", { class: kind, title: `${help}${how}` }, el("span", { class: "mark", text: mark }), el("span", {}, ...words));
        }),
      )
    : null;
  return el("div", { class: "report" }, text ? el("div", { class: "body markdown", html: markdown(text) }) : null, criteria, more.length ? el("div", { class: "receipts" }, ...more) : null, earlierBlock(task));
}
// What it reported before its result (a newer report on the same task took their place), as
// the server keeps them (`earlier_results`): newest first, each when it was said and what.
export function earlierResults(task) {
  const list = Array.isArray(task?.earlier_results) ? task.earlier_results : [];
  return list.filter((item) => typeof item?.text === "string" && item.text.trim()).map((item) => ({ at: typeof item.at === "string" && !Number.isNaN(Date.parse(item.at)) ? item.at : "", text: item.text }));
}
// Its earlier reports, folded under its result and read as it is; open, they stay open while
// the card is redrawn.
const openEarlier = new Set();
function earlierBlock(task) {
  const list = earlierResults(task);
  if (!list.length) return null;
  const fold = el(
    "details",
    { class: "earlier-reports", open: openEarlier.has(task.id) },
    el("summary", { text: `Earlier reports (${list.length})`, "data-tip": "What it reported on this task before its latest result, newest first" }),
    ...list.map((item) => {
      const entry = el("div", { class: "earlier-report" }, item.at ? el("div", { class: "when", text: `Reported ${since(item.at)}`, "data-tip": new Date(item.at).toLocaleString() }) : "");
      foldable(entry, el("div", { class: "body markdown", html: markdown(item.text) }), item.text, { chars: 360, lines: 5 });
      return entry;
    }),
  );
  fold.addEventListener("toggle", () => openEarlier[fold.open ? "add" : "delete"](task.id));
  return fold;
}
// Tests it ran outside its own checkout (someone's source, to check what it reports): what they
// found, as evidence; where, and how they ended, on hover.
function testsElsewhere(item) {
  const where = item.cwd ? displayPath(item.cwd).split("/").filter(Boolean).pop() : "";
  const outcome = item.passed === null ? "running" : item.summary || (item.no_tests ? "no tests ran" : item.passed ? "passed" : `failed${item.exit_code ? ` (exit ${item.exit_code})` : ""}`);
  const help = [`Ran in ${item.cwd || "a folder that is not its checkout"}: not tests of this task's own changes`, item.summary || "", item.command, plural(item.runs || 1, "run")].filter(Boolean).join("\n");
  return el("span", { class: `receipt tests-elsewhere ${item.passed === false ? "warn" : "none"}`, title: help, text: `Ran tests${where ? ` in ${where}` : ""} · ${outcome}` });
}
// What was observed, next to what the worker reported.
const VERDICT = {
  verified: ["pass", "✓ Verified", "Every acceptance criterion is backed by a check it was seen running"],
  partial: ["warn", "Partly verified", "Some criteria are backed by a check it was seen running; others are only reported"],
  unverified: ["warn", "Not verified", "No criterion is backed by a check it was seen running"],
  failed: ["fail", "✗ Not met", "A criterion is not met, or the check behind it failed"],
};
const CRITERION = {
  verified: ["pass", "✓", "Verified: it ran this check and it passed"],
  claimed: ["warn", "~", "Reported met; no check was seen running"],
  contradicted: ["fail", "✗", "Reported met, but the check failed"],
  not_met: ["fail", "✗", "Not met"],
  not_checked: ["none", "–", "Not checked"],
};
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
export function upsertTask(task) {
  if (!state.snapshot) return;
  const list = tasks().filter((item) => item.id !== task.id);
  list.push(task);
  state.snapshot.tasks = list;
  scheduleHome();
  renderSidebar();
  refreshChips();
}
