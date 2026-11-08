// Task cards on Home: what runs and what it does now, what waits, what is finished.

import { api, el, foldable, moreMenu, once, reconcile, submits, toast } from "./dom.js";
import { MARKERS, SECTION_DOT, SECTIONS, taskPhase, workerContext } from "./board.js";
import { ago, changesText, compactPaths, displayPath, gist, linkName, markdown, notCountedText, plural, prTitle, shorten, since, taskLinks, threadLabel } from "./text.js";
import { isHidden, prStatus, setHidden, toneOf } from "./artifacts.js";
import { openFile } from "./files.js";
import { byId, closedWorkers, ownerQuestions, pendingDrafts, sessions, state, tasks } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { select } from "./session.js";
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
export function terminalItem(session) {
  const line = () => compactPaths(session.terminal?.activity || "Working…", session.cwd);
  return {
    key: `terminal:${session.id}`,
    version: line(),
    build: () =>
      el(
        "div",
        { class: "card task running terminal" },
        el(
          "div",
          { class: "head" },
          el("span", { class: "dot open busy", title: "Working in a terminal" }),
          el("button", { type: "button", class: "title", text: session.display_title || session.title, title: `Open its worker (started as “${session.title}”)`, onclick: () => select(session.id) }),
          el("span", { class: "meta", text: "In a terminal" }),
        ),
        el("div", { class: "task-body" }, el("div", { class: "activity", text: line() })),
      ),
    refresh: (node) => {
      node.querySelector(".activity").textContent = line();
    },
  };
}
// Workers at work whose task is finished (news on a PR they follow, a question about it): they
// are running too. The card is named for that task; the meta stays short so the title shows.
export function followUpItem(session) {
  const line = () => stepText(session.step || session.activity || "Working…", session.cwd);
  const name = session.display_title || session.task?.title || session.title;
  const meta = () => `Following up${session.step_since ? ` · ${ago(session.step_since)}` : ""}`;
  return {
    key: `followup:${session.id}`,
    version: `${line()}:${name}:${meta()}`,
    build: () =>
      el(
        "div",
        { class: "card task running followup" },
        el(
          "div",
          { class: "head" },
          el("span", { class: "dot working", title: "Working" }),
          el("button", { type: "button", class: "title", text: name, title: `Open its worker at this task (started as “${session.title}”)`, onclick: () => select(session.id, { task: session.task?.task_id }) }),
          el("span", { class: "meta", text: meta(), title: "Its task is finished; it works on what came up about it (news on its PR, a question)" }),
        ),
        el("div", { class: "task-body" }, el("div", { class: "activity", text: line() })),
      ),
    refresh: (node) => {
      node.querySelector(".activity").textContent = line();
      node.querySelector(".head .title").textContent = name;
      node.querySelector(".head .meta").textContent = meta();
    },
  };
}
// Where a task is when no board placed it (a card elsewhere): as the board would place it.
const phaseOf = (task) => {
  const worker = byId(task.session_id);
  const list = tasks().filter((other) => other.session_id === task.session_id);
  return taskPhase(task, worker, { ...workerContext(worker, list), stallHours: state.snapshot?.settings?.stall_hours || 4 });
};
// Monitoring and Recently finished show a row per task: a click shows its whole card in place.
const isRow = (phase) => ["monitoring", "finished"].includes(phase.section);
// A task split off another can go back into it for a while.
const undoUntil = (task) => (task.split_from?.until && Date.parse(task.split_from.until) > Date.now() ? task.split_from.until : "");
// A report that only informs, not read yet.
const unread = (task, phase) => phase.marker === "report" && !task.read;
const REPORT_HELP = "Only a report: nothing to accept. Done once read.";
// The longest title the server keeps (clean_title).
const TITLE_MAX = 80;
// What cancelling came to, as the server has the task after it: one that had finished is set
// aside (its result kept), not cancelled.
export const cancelledWords = (view) => (view?.set_aside ? "It had finished: set aside, its result is kept." : "Task cancelled.");
// The key covers what changes a card's actions and layout; the version, what only its text.
// `phase`: where it is on the board ({section, marker, why}: see board.js).
export function taskItem(task, phase = phaseOf(task)) {
  const worker = byId(task.session_id);
  const live = ["running", "queued"].includes(task.status);
  const [meta, help] = taskMeta(task, phase);
  const made = [task.artifacts, task.links, task.files, task.made_links, task.link_names, task.watch_kind, task.watch_changed_at, state.snapshot?.artifacts_hidden];
  return {
    key: `task:${task.id}:${task.status}:${phase.section}:${phase.marker}:${task.stopped ? 1 : 0}:${task.blocked ? 1 : 0}:${createsPr(task) ? 1 : 0}:${task.news?.at || ""}:${task.splittable ? 1 : 0}:${undoUntil(task) ? 1 : 0}`,
    id: task.id,
    // For running cards the minute too: "6m" and "3m ago" move on by themselves.
    version: `${task.updated_at}:${task.title}:${phase.why}:${meta.join("|")}:${help}:${task.read ? 1 : 0}:${worker?.status || ""}:${worker?.activity || ""}:${worker?.step || ""}:${worker?.step_since || ""}:${worker?.turn_task || ""}:${task.credits}:${task.runs}:${JSON.stringify(task.receipts?.prs || null)}:${task.status === "review" ? JSON.stringify(task.receipts || null) : ""}:${live ? `${Math.floor(Date.now() / 60000)}:${queuedKey()}` : ""}:${waitsOnOwner(task, worker)}:${(task.watching || []).join(",")}:${(task.watch_failing || []).join(",")}:${JSON.stringify(task.check_back || null)}:${task.nudged_at || ""}:${JSON.stringify(task.receipts?.changed || null)}:${JSON.stringify(made)}`,
    build: () => taskCard(task, phase),
    refresh: (node) => refreshTaskCard(node, task, phase),
  };
}
// A queued card's reason changes as other workers start and stop.
const queuedKey = () => `${sessions().filter(busy).length}/${state.snapshot.settings.max_workers}`;
function refreshTaskCard(node, task, phase) {
  const title = node.querySelector(".head .title");
  title.textContent = task.title;
  title.dataset.tip = titleTip(task, phase);
  const [meta, help] = taskMeta(task, phase);
  node.querySelector(".head .meta").replaceChildren(...metaParts(meta));
  node.querySelector(".head .meta").dataset.tip = help;
  const dot = node.querySelector(".head .dot");
  dot.className = dotClass(task, phase);
  dot.dataset.tip = unread(task, phase) ? "Not read yet" : "";
  const review = task.status === "review";
  const id = node.querySelector(".head .tid");
  if (id) id.textContent = task.id;
  for (const part of [".row-line", ".task-body", ".news", ".chips", ".report"]) {
    const old = node.querySelector(`:scope > ${part}`);
    const fresh = part === ".row-line" ? (isRow(phase) ? rowLine(task, byId(task.session_id), phase) : null) : part === ".task-body" ? taskBody(task, phase) : part === ".news" ? newsBlock(task) : !review ? null : part === ".chips" ? chips(task) : report(task);
    if (old && fresh) old.replaceWith(fresh);
    else if (old) old.remove();
    else if (fresh && part === ".row-line") node.querySelector(":scope > .head").after(fresh);
    else if (fresh) (node.querySelector(":scope > .actions") ? node.querySelector(":scope > .actions").before(fresh) : node.append(fresh));
  }
  expandTask(node, review && openReports.has(task.id));
}
const dotClass = (task, phase) => `dot ${SECTION_DOT[phase.section] ?? ""}${unread(task, phase) ? " unread" : ""}`;
// What a click on its title does, said on hover: a row shows its card; a card opens its worker.
const titleTip = (task, phase) => (task.session_id ? `${task.title}\n${isRow(phase) ? "Click to see it all; its title then opens its worker" : "Open its worker at this task"}` : task.goal || "");
// Short on the card: what kind of place it is in (queued, overdue, to review…) and since when;
// the rest on hover.
function taskMeta(task, phase = {}) {
  const ended = ["review", "done", "failed", "cancelled"].includes(task.status);
  const earlier = (task.earlier_titles || []).map((item) => `“${item.title}”`).join(", ");
  // Short: a few lines, each a fact.
  const help = [
    phase.marker === "report" ? REPORT_HELP : "",
    [`Task ${task.id}`, ended ? `finished ${since(task.ended_at || task.updated_at)}` : `started ${since(task.created_at)}`].join(" · "),
    task.worker && task.worker !== task.title ? `Worker: ${shorten(task.worker, 60)}` : "",
    earlier ? `Earlier names: ${shorten(earlier, 80)}` : "",
    [task.credits >= 0.05 ? `${task.credits.toFixed(1)} credits` : "", task.runs > 1 ? `${task.runs} runs` : ""].filter(Boolean).join(" · "),
  ];
  const marker = MARKERS[phase.marker] || "";
  let when = since(task.ended_at || task.updated_at);
  if (phase.section === "running" && task.status === "running") when = ago(task.created_at) === "now" ? "just now" : ago(task.created_at);
  else if (phase.marker === "check-back" && task.check_back?.at) when = clockTime(task.check_back.at);
  else if (phase.section === "monitoring") when = since(task.active_at || task.updated_at);
  else if (phase.marker === "queued" || phase.marker === "turn") when = "";
  // On a row whose second line says it already ("Checks back at 3:15 PM: …", "Paused: …"), not
  // twice: the meta says when it was last active, and the title keeps its room.
  if (marker && isRow(phase) && rowWhy(task, byId(task.session_id), phase).text.toLowerCase().startsWith(marker.toLowerCase())) {
    return [["", phase.section === "monitoring" ? since(task.active_at || task.updated_at) : when], help.filter(Boolean).join("\n")];
  }
  return [[marker, when], help.filter(Boolean).join("\n")];
}
// The meta's parts: the marker, and when (left out on a narrow card: it is in the tooltip).
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
// A result to review shows its full report on demand; an open one stays open while the card
// is redrawn (new receipts, a fresh PR state).
// `seen`: the owner opened it (it is read now).
const openReports = new Set();
export function expandTask(node, open = !node.classList.contains("expanded"), seen = false) {
  node.classList.toggle("expanded", open);
  const toggle = node.querySelector(".expand");
  if (toggle) toggle.textContent = open ? "Hide report" : "Show report";
  if (node.dataset.task) openReports[open ? "add" : "delete"](node.dataset.task);
  if (open && seen) readTask(node.dataset.task);
}
// A row (Monitoring, Recently finished) shows its whole card in place, and stays so while
// redrawn.
const unfolded = new Set();
export function unfoldTask(node, open = !node.classList.contains("unfolded"), seen = false) {
  node.classList.toggle("unfolded", open);
  // Folded, its title shows it whole (said to screen readers); shown, the title opens its worker.
  const title = node.querySelector(":scope > .head .title");
  if (open) title?.removeAttribute("aria-expanded");
  else title?.setAttribute("aria-expanded", "false");
  if (node.dataset.task) unfolded[open ? "add" : "delete"](node.dataset.task);
  if (open && seen) readTask(node.dataset.task);
}
// A result the owner opened is read: a report's unread dot goes (asked once per task here).
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
function taskCard(task, phase = phaseOf(task)) {
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  const [meta, help] = taskMeta(task, phase);
  const open = task.session_id ? () => select(task.session_id) : null;
  // The title opens its worker at this task's report: it may be on other work since.
  const openAt = task.session_id ? () => select(task.session_id, { task: task.id }) : null;
  const row = isRow(phase);
  const node = el(
    "div",
    { class: `card task ${task.status} in-${phase.section || "card"}${phase.marker ? ` marker-${phase.marker}` : ""}${task.blocked ? " blocked" : ""}${row ? " row" : ""}${row && unfolded.has(task.id) ? " unfolded" : ""}`, "data-session": task.session_id || undefined, "data-task": task.id },
    el(
      "div",
      { class: "head" },
      el("span", { class: dotClass(task, phase), "data-tip": unread(task, phase) ? "Not read yet" : undefined }),
      // On a row its title shows the card first (the row's click); then it opens its worker.
      el("button", { type: "button", class: "title", text: task.title, "data-tip": titleTip(task, phase), "aria-expanded": row && !unfolded.has(task.id) ? "false" : undefined, onclick: () => (row && !node.classList.contains("unfolded") ? null : openAt?.()), disabled: !openAt && !row }),
      el("span", { class: "tid", text: task.id, title: "Task id" }),
      el("span", { class: "meta", "data-tip": help }, ...metaParts(meta)),
      // A row shown whole folds back here (or with Escape): its title opens its worker then.
      row ? el("button", { type: "button", class: "fold icon", text: "▴", title: "Fold it back into a row (Esc)", "aria-label": "Fold", onclick: () => foldBack() }) : null,
    ),
  );
  function foldBack() {
    unfoldTask(node, false);
    node.querySelector(":scope > .head .title")?.focus();
  }
  if (row) {
    node.append(rowLine(task, byId(task.session_id), phase));
    node.addEventListener("click", (event) => {
      if (!node.classList.contains("unfolded")) {
        if (!event.target.closest("a, .menu-wrap, .fold")) unfoldTask(node, true, true);
      } else if (event.target.closest(".head") && !event.target.closest("button, a, .menu-wrap")) {
        unfoldTask(node, false);
      }
    });
    node.addEventListener("keydown", (event) => {
      // Not while typing in a form on it, nor to close its open menu.
      if (event.key !== "Escape" || !node.classList.contains("unfolded") || event.target.closest("input, textarea, select") || node.querySelector(".menu:not([hidden])")) return;
      event.preventDefault();
      foldBack();
    });
  }
  const body = taskBody(task, phase);
  if (body) node.append(body);
  const news = newsBlock(task);
  if (news) node.append(news);
  if (task.status === "review") node.append(...[chips(task), report(task)].filter(Boolean));
  expandTask(node, task.status === "review" && openReports.has(task.id));
  const key = `task:${task.id}`;

  // The card leaves its place at once (`moves`); the task's new state puts it where it goes.
  // One request at a time: a second click (or Enter) while it is out does nothing.
  const act = (path, payload, done, moves = true) => once(key, null, () => send(path, payload, done, moves));
  async function send(path, payload, done, moves) {
    for (const button of actions.querySelectorAll("button")) button.disabled = true;
    error.textContent = "";
    if (moves) {
      state.leaving.add(key);
      node.classList.add("leaving");
      renderSidebar();
      scheduleHome();
    }
    try {
      const view = await api(path, { method: "POST", body: payload || {} });
      state.leaving.delete(key);
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
  // Live and finished cards keep their "⋯" in the head, on one line with the title; a form
  // the owner opens takes a line of its own.
  function place() {
    const inline = !actions.querySelector(":scope > button:not(.more-button)") && !actions.dataset.editing;
    actions.classList.toggle("inline", inline);
    const parent = inline ? node.querySelector(":scope > .head") : node;
    // In the head, before a row's fold button (that stays at the end).
    if (actions.parentElement !== parent) parent.insertBefore(actions, inline ? parent.querySelector(":scope > .fold") : null);
  }
  function reply(placeholder, done, moves = true) {
    const input = el("textarea", { rows: 2, placeholder });
    form(input, "Send", () => input.value.trim() && act(`/api/tasks/${task.id}/follow-up`, { text: input.value.trim() }, done, moves));
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
      preview.why ? el("div", { class: "doubt", text: preview.why }) : null,
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
    const worker = open ? { text: "Open worker", onclick: open } : null;
    const cancel = { text: "Cancel task…", onclick: confirmCancel, danger: true };
    const note = { text: "Add a note…", onclick: () => reply("Tell the worker now (it reads it within its current turn)", "Sent to the worker.", false) };
    const copy = { text: "Copy report", onclick: () => copyReport(task) };
    const pr = createsPr(task) ? { text: "Create PR", onclick: () => act(`/api/tasks/${task.id}/pr`, {}, "PR opened.", false) } : null;
    const named = { text: "Rename…", onclick: rename };
    const splitOff = task.splittable ? { text: "Move the latest result to a new task…", onclick: split } : null;
    // Split off another task a moment ago: back into it, until the server's time is up.
    const until = undoUntil(task);
    const from = until ? tasks().find((item) => item.id === task.split_from.task_id)?.title || "the task it came from" : "";
    const undo = until ? el("button", { type: "button", class: "secondary", text: "Undo split", title: `Back into “${from}” (until ${clockTime(until)})`, onclick: () => act(`/api/tasks/${task.id}/merge-back`, {}, `Back in “${from}”.`) }) : null;
    const followUp = { text: "Follow up…", onclick: () => reply("What else should it do?", "Sent to the worker.") };
    const nudge = el("button", { type: "button", text: "Nudge", title: "Ask its worker where this task stands", onclick: () => act(`/api/tasks/${task.id}/nudge`, {}, "Asked its worker where it stands.", false) });
    let main = null;
    let more = [];
    if (task.status === "review" && task.news) {
      main = el("button", { type: "button", text: "Got it", onclick: () => act(`/api/tasks/${task.id}/news-seen`, {}, "") });
      more = [{ text: "Accept", onclick: () => act(`/api/tasks/${task.id}/accept`, {}, "Accepted.") }, followUp, copy, pr, named, splitOff, worker];
    } else if (task.status === "review" && phase.section === "needs") {
      main = el("button", { type: "button", text: "Accept", onclick: () => act(`/api/tasks/${task.id}/accept`, {}, "Accepted.") });
      more = [followUp, copy, pr, named, splitOff, worker];
    } else if (task.status === "review") {
      // A report to read (or one still followed): nothing to accept, done once read.
      main = el("button", { type: "button", text: "Done", title: REPORT_HELP, onclick: () => act(`/api/tasks/${task.id}/accept`, {}, "Marked done.") });
      more = [followUp, copy, pr, named, splitOff, worker];
    } else if (task.status === "waiting" && task.stopped) {
      // Stopped mid-work: go on where it stopped, or with a word on what to do instead.
      main = el("button", { type: "button", text: "Go on", onclick: () => act(`/api/tasks/${task.id}/follow-up`, { text: "Go on where you stopped." }, "Sent to the worker.") });
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
      // News about finished work: seen, it goes back to the finished ones.
      main = el("button", { type: "button", text: "Got it", onclick: () => act(`/api/tasks/${task.id}/news-seen`, {}, "") });
      more = [{ text: "Follow up…", onclick: () => reply("What should it do about it?", "Sent to the worker.") }, copy, pr, named, worker];
    } else if (task.status === "done") {
      more = [followUp, copy, pr, named, splitOff, worker];
    } else {
      more = [named, worker];
    }
    actions.replaceChildren(error, moreMenu(more) || "", undo || "", main || "");
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
    // it made, the threads it still follows).
    const first = isRow(phase) ? gist(task.result || "") : "";
    const waits = phase.section === "monitoring" ? waitLine(task, worker, phase) : null;
    const found = artifacts(task, worker, { active: phase.section === "monitoring" || phase.marker === "still-following" });
    const nodes = [first ? el("div", { class: "outcome", text: first }) : null, waits, found].filter(Boolean);
    return nodes.length ? el("div", { class: "task-body" }, ...nodes) : null;
  }
  if (task.status === "review") {
    // What came out, in its first sentences (past a "## Summary" title); the report on demand.
    const first = gist(task.result || task.progress || "");
    const waits = phase.section === "monitoring" ? waitLine(task, worker, phase) : null;
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
// What a task has made or points to, by what it is to the owner: what it delivered (what its
// worker declared or its reports say it made, the files it wrote, the top-level run it made)
// first, a few at most, each with ✕ to take it off; what is under a run (a Spark application,
// logs, a debug page), what it only points to (a document it mentions, an issue, a PR it did
// not make or review, a thread) and what was taken off folded away. `shown`: PRs on chips of
// their own; `mentioned`: chips of PRs it only mentions.
function linkGroups(task, shown, mentioned = []) {
  const main = [];
  const more = { made: [], diagnostic: [], reference: [...mentioned], off: [] };
  for (const link of taskLinks(task)) {
    const { found, said } = link;
    if (found.key && shown.has(found.key)) continue;
    if (found.key) shown.add(found.key);
    const role = link.role === "pr" ? "reference" : link.role;
    const made = ["output", "run"].includes(role);
    const off = made && isHidden(link.key, toneOf(said?.status));
    const chip = linkChip(task, link, role, made && !off);
    if (off) more.off.push(chip);
    else if (made) (main.length < 3 ? main : more.made).push(chip);
    else (more[role] || more.reference).push(chip);
  }
  const folded = [...more.made, ...more.diagnostic, ...more.reference, ...more.off];
  if (!folded.length) return { main, more: null };
  const words = [more.made.length ? `${more.made.length} more made` : "", more.diagnostic.length ? `${more.diagnostic.length} for diagnosis` : "", more.reference.length ? `${more.reference.length} referenced` : "", more.off.length ? `${more.off.length} taken off` : ""].filter(Boolean).join(" · ");
  const details = el("details", { class: "more-links" }, el("summary", { text: `More links: ${words}` }), el("div", { class: "receipts" }, ...folded));
  return { main, more: details };
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
    ? el("button", { type: "button", class: "chip-link", text: label, title: `${help}\nClick to open it`, onclick: () => openFile(link.path, task.session_id) })
    : el("a", { class: "chip-link", href: link.url, target: "_blank", rel: "noreferrer", title: help, text: label });
  const chip = el("span", { class: `receipt link-chip kind-${found.kind} ${role}${said ? ` tone-${toneOf(said.status)}` : ""}` }, open);
  if (removable) {
    chip.classList.add("removable");
    chip.append(el("button", { type: "button", class: "chip-off", text: "✕", title: "Take it off this card and Artifacts (it comes back if it fails)", "aria-label": `Take ${name} off`, onclick: () => setHidden(link.key, true, toneOf(said?.status)) }));
  }
  return chip;
}
const WAITS_WORDS = { github: "Reply draft", slack: "Slack draft", reply: "Reply draft", merge: "Ready to merge", question: "Question", form: "Form", link: "Link to open", signin: "Sign-in", workspace: "Where to work" };
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
// A thread its worker follows, as a link to it: a PR on GitHub, a Slack conversation when its
// link is known.
function watchingChip(key) {
  const pr = /^github:([^/#]+\/[^#]+)#(\d+)$/.exec(key);
  const title = pr ? prTitle(`${pr[1]}#${pr[2]}`) : "";
  const label = pr ? `${pr[1].split("/")[1]}#${pr[2]}${title ? ` · ${shorten(title, 36)}` : ""}` : threadWords(key);
  const href = pr ? `https://github.com/${pr[1]}/pull/${pr[2]}` : state.snapshot?.thread_links?.[key] || "";
  const help = `It follows ${key}${title ? ` (“${title}”)` : ""}: what happens there comes to it`;
  if (!href) return el("span", { class: "receipt watching", title: help, text: `Watching ${label}` });
  return el("a", { class: "receipt watching", href, target: "_blank", rel: "noreferrer", title: help, text: `Watching ${label}` });
}
export function artifacts(task, worker = byId(task.session_id), { active = true } = {}) {
  const items = [];
  const { shown, own, mentioned } = prChips(task);
  items.push(...own);
  const links = linkGroups(task, shown, mentioned);
  items.push(...links.main);
  if (active && worker) {
    const onChips = new Set((task.receipts?.prs || []).filter((pr) => pr.own || pr.reviewed).map((pr) => pr.label.toLowerCase()));
    for (const key of followedOnce(task.watching || [])) {
      const pr = /^github:(.+)$/.exec(key)?.[1];
      if (pr && onChips.has(pr.toLowerCase())) continue;
      items.push(watchingChip(key));
    }
  }
  // A thread it cannot read is not something it waits on: said, with a way to let it go.
  for (const key of followedOnce(task.watch_failing || [])) items.push(failingChip(key, task));
  const { drafts, questions } = ownerItems(task, worker);
  for (const draft of drafts) items.push(el("button", { type: "button", class: "receipt needs", title: "Waiting for you under Needs you", text: `${WAITS_WORDS[draft.kind] || "Draft"} · Needs you`, onclick: () => focusDraft(draft.id) }));
  for (const question of questions) items.push(el("button", { type: "button", class: "receipt needs", title: question.text, text: "Question · Needs you", onclick: () => focusQuestion(question.id) }));
  if (!items.length && !links.more) return null;
  return el("div", { class: "artifacts" }, items.length ? el("div", { class: "receipts" }, ...items) : "", links.more || "");
}
// Its PRs as chips: those it made or reviewed in sight, those it only mentions folded with the
// references.
function prChips(task) {
  const shown = new Set();
  const own = [];
  const mentioned = [];
  for (const pr of task.receipts?.prs || []) {
    shown.add(pr.label.toLowerCase());
    (pr.own || pr.reviewed ? own : mentioned).push(prChip(pr));
  }
  return { shown, own, mentioned };
}
function failingChip(key, task) {
  const label = threadWords(key);
  const chip = el("span", { class: "receipt warn failing", title: `It follows ${key} for this task, but it could not be read several times in a row: nothing there reaches it` }, `Can't read ${label} · `);
  chip.append(
    el("button", {
      type: "button",
      class: "link",
      text: "stop following",
      onclick: async () => {
        try {
          await api(`/api/sessions/${task.session_id}/unfollow`, { method: "POST", body: { text: key } });
          toast(`Stopped following ${label}.`);
        } catch (error) {
          toast(error.message, "error");
        }
      },
    }),
  );
  return chip;
}
// A worker on the board (Needs you, Running): one box per section it has tasks in, holding
// those tasks. The same worker shows in each section one of its tasks is in. The tasks are what
// to read; the worker is their source, in small type.
export function workerBoxItem(section, sessionId, list, phases, editing, elsewhere = []) {
  const worker = sessionById(sessionId);
  const items = list.map((task) => taskItem(task, phases.get(task.id)));
  const head = boxHead(section, worker, sessionId, list, elsewhere);
  return {
    key: `box:${section}:${sessionId}`,
    version: `${head.version}|${items.map((item) => `${item.key}=${item.version}`).join("|")}`,
    build: () => {
      const node = el("div", { class: `card worker-box in-${section}`, "data-session": sessionId }, head.build(), el("div", { class: "box-tasks" }));
      reconcile(node.querySelector(".box-tasks"), items, editing);
      return node;
    },
    // In place: an open menu, an open report or a typed note stays.
    refresh: (node) => {
      node.querySelector(":scope > .box-head").replaceWith(head.build());
      reconcile(node.querySelector(":scope > .box-tasks"), items, editing);
    },
  };
}
// A worker's state in its box's head, in words: in a turn waiting on the owner, it waits for
// them, it is not idle.
const BOX_STATUS = { queued: "queued", waiting: "waits for you", done: "done", failed: "failed" };
function boxHead(section, worker, sessionId, list, elsewhere) {
  const name = worker?.display_title || worker?.title || "";
  // Its name only when it says more than its tasks' titles below it.
  const named = name && !list.some((task) => task.title === name) ? name : "";
  const short = sessionId.slice(0, 6);
  const working = worker && ["working", "starting"].includes(worker.status);
  const status = !worker ? "" : worker.closed_at ? "closed" : working ? "working" : worker.elsewhere ? "in a terminal" : BOX_STATUS[worker.status] || "idle";
  const also = elsewhere.length ? `also: ${elsewhere.join(", ")}` : "";
  return {
    version: `${named}:${status}:${also}`,
    build: () =>
      el(
        "div",
        { class: "box-head", title: SECTIONS.find(([key]) => key === section)?.[1] || "" },
        el("span", { class: "who", text: "Worker" }),
        el("button", { type: "button", class: "title", text: named ? `${short} · ${named}` : short, title: `Open the worker${name ? ` “${name}”` : ""}${worker?.title && worker.title !== name ? ` (started as “${worker.title}”)` : ""}`, onclick: () => worker && select(worker.id), disabled: !worker }),
        el("span", { class: "meta", text: [status, also].filter(Boolean).join(" · "), title: also ? "Its other tasks are in these sections" : "" }),
      ),
  };
}
// What a worker does right now: the step it is in and for how long, or how long it has been
// quiet and what it watches meanwhile. What it said last is in the tooltip.
function activityLine(worker) {
  const working = worker?.status === "working";
  const raw = worker?.step || worker?.activity || "";
  const line = raw ? stepText(raw, worker?.cwd) : working ? "Thinking…" : quiet(worker);
  const long = worker?.step_since && ago(worker.step_since) !== "now";
  const said = worker?.said ? `\n\nSaid last: ${worker.said.replace(/\s+/g, " ").slice(0, 400)}` : "";
  // What it runs, in the tooltip: the line says what it is for.
  const command = worker?.step_command && worker.step_command !== raw ? `\n${worker.step_command}` : "";
  return el("div", { class: "activity", title: `${raw || line}${command}${said}` }, worker?.step ? "Now: " : "", line, long ? el("span", { class: "when", text: ` · ${ago(worker.step_since)}` }) : "");
}
// A running task at a glance, by where it stands: at work (the step its worker is in and for
// how long), watching (what, and when it checks back), waiting its turn, or stalled; then
// this task's own latest progress and what it made.
function runningBody(task, worker, phase = {}) {
  const nodes = [];
  const section = phase.section || "running";
  if (section === "running") nodes.push(phase.marker === "terminal" ? el("div", { class: "activity", text: compactPaths(worker?.terminal?.activity || "Working in a terminal", worker?.cwd) }) : activityLine(worker));
  else nodes.push(waitLine(task, worker, phase));
  // Its own progress: what it last reported on this task (not its asides while it works);
  // gray once it is old news.
  const stale = section !== "running" && Date.now() - Date.parse(task.updated_at) > 3600000;
  const progress = progressLine(task.progress, task.progress ? task.updated_at : "", task.id, stale);
  if (progress) nodes.push(progress);
  const found = artifacts(task, worker, { active: true });
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
  } else if (["watching", "delivered", "reviewed"].includes(marker) && (task.watching || []).length) {
    const what = followedOnce(task.watching).map((key) => watchedWords(key, task)).join("; ");
    text = `${marker === "watching" ? "Waiting on" : marker === "reviewed" ? "Reviewed; following" : "Delivered; still following"} ${what}`;
    help = marker === "reviewed" ? "A PR you reviewed: its author's next push or reply wakes its worker" : "News there (a review, CI, a reply, the merge) wakes its worker";
  } else if (marker === "queued") {
    const reason = queuedReason(task);
    text = reason.text;
    help = reason.help || "";
  } else if (marker === "paused") {
    help = "Its worker took it up, then went on to another task: it reports this one (done, a question) or goes back to it after";
  }
  return { text, help: help || text, warn: ["idle", "overdue", "stalled", "check-failed"].includes(marker) };
}
function waitLine(task, worker, phase) {
  const { text, help, warn } = waitWords(task, worker, phase);
  return text ? el("div", { class: `reason${warn ? " warn" : ""}`, text, title: help }) : null;
}
// A row's second line: what it waits on (or what it came to) and the main thing it made.
function rowLine(task, worker, phase) {
  const why = rowWhy(task, worker, phase);
  // Not twice: what it waits on may name it already.
  const made = mainArtifact(task);
  const named = phase.section === "monitoring" && made && why.text.includes(made);
  return el(
    "div",
    { class: "row-line" },
    el("span", { class: `why${why.warn ? " warn" : ""}`, text: why.text, "data-tip": why.help && why.help !== why.text ? `${why.text}\n${why.help}` : why.text || undefined }),
    made && !named ? el("span", { class: "made", text: made }) : "",
  );
}
// What a row says on its second line: what it waits on, or what it came to.
const rowWhy = (task, worker, phase) => (phase.section === "monitoring" ? waitWords(task, worker, phase) : { text: finishedWords(task, phase), help: "" });
// What a finished task came to, in its first words ("Cancelled by you." is not said twice).
export function finishedWords(task, phase) {
  if (phase.marker === "still-following") return phase.why;
  const said = gist(task.result || task.progress || "");
  if (task.status === "cancelled") return !said ? "Cancelled" : /^cancel/i.test(said) ? said : `Cancelled: ${said}`;
  return said || phase.why || "";
}
// The main thing a task made, in a word or two: its PR, else the PR it follows, else the first
// thing it made.
function mainArtifact(task) {
  const prs = task.receipts?.prs || [];
  const pr = prs.find((item) => item.own) || prs.find((item) => item.reviewed);
  if (pr) return pr.label.replace(/^[^/]+\//, "");
  const watched = /^github:[^/]+\/(.+)$/.exec((task.watching || [])[0] || "")?.[1];
  if (watched) return watched;
  const made = taskLinks(task).find((link) => ["output", "run"].includes(link.role) && !isHidden(link.key, toneOf(link.said?.status)));
  return made ? shorten(linkName(made), 32) : "";
}
// A thread it follows, in words: "app#594 · Fix the Spark dependency (awaiting approval)"; a
// Slack conversation by its name.
function watchedWords(key, task) {
  const match = /^github:([^/#]+\/[^#]+)#(\d+)$/.exec(key);
  if (!match) return threadWords(key);
  const label = `${match[1]}#${match[2]}`.toLowerCase();
  const pr = (task.receipts?.prs || []).find((item) => item.label.toLowerCase() === label);
  const title = pr?.title || prTitle(label);
  // Where it stands as Artifacts and its chip say it.
  const where = pr ? prStatus(pr)[0] : "";
  return `${match[1].split("/")[1]}#${match[2]}${title ? ` · ${shorten(title, 40)}` : ""}${where ? ` (${where})` : ""}`;
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
// A PR as GitHub has it now, in a few words: "app#7 · ✓ CI · approved".
function prChip(pr) {
  const short = pr.label.replace(/^[^/]+\//, "");
  const named = pr.title || prTitle(pr.label);
  const state = pr.state === "MERGED" ? "merged" : pr.state === "CLOSED" ? "closed" : "";
  const ci = state ? "" : { passing: "✓ CI", failing: "✗ CI", pending: "CI running" }[pr.checks] || "";
  // Where it stands in a word, as Artifacts and Monitoring say it (changes requested wins over
  // failing CI); a word only about CI is not said again beside the CI mark, and "in review"
  // (nothing to say yet) is left out.
  const [word] = prStatus(pr);
  const stands = word === "in review" || (word === "CI failing" && ci) || (word === "CI running" && ci) ? "" : word;
  const review = { APPROVED: "approved", CHANGES_REQUESTED: "changes requested" }[pr.review] || "";
  const failing = pr.checks === "failing" && pr.failing?.length ? `\nFailing: ${pr.failing.join(", ")}` : "";
  const ciWords = { passing: "Checks pass", failing: "Checks fail", pending: "Checks are running", none: "No checks" }[pr.checks] || "";
  const seen = pr.at ? `\nAs of ${new Date(pr.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "";
  const title = [pr.label, named ? `“${named}”` : "", ciWords + failing, !state && pr.awaiting?.length && `Waiting for approval: ${pr.awaiting.join(", ")}`, review && `Review: ${review}`, word === "ready to merge" && "GitHub would merge it now", state && `PR ${state}`, pr.reviewed && !pr.own ? "You reviewed it" : ""].filter(Boolean).join("\n") + seen;
  // Its number and title (shortened to fit), then where it stands (always in sight).
  const where = [state || ci, stands !== state ? stands : ""].filter(Boolean).join(" · ");
  return el(
    "a",
    { class: `receipt pr ${state || pr.checks}`, href: pr.url, target: "_blank", rel: "noreferrer", title },
    el("span", { class: "pr-name", text: named ? `${short} · ${shorten(named, 60)}` : short }),
    where ? el("span", { class: "pr-state", text: ` · ${where}` }) : "",
  );
}
// What was observed while it worked, at a glance: the verdict, tests, changes and PRs.
function chips(task) {
  const receipts = task.receipts;
  const items = [];
  if (receipts?.verdict) {
    const [kind, text, help] = receipts.stale ? ["warn", "Out of date", receipts.stale] : VERDICT[receipts.verdict];
    const done = receipts.criteria.filter((item) => item.state === "verified").length;
    items.push(el("span", { class: `receipt verdict ${kind}`, title: help, text: receipts.verdict === "partial" ? `${text} · ${done}/${receipts.criteria.length}` : text }));
  }
  const result = (label, item) => {
    if (!item) return;
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
  const { shown, own, mentioned } = prChips(task);
  items.push(...own);
  const links = linkGroups(task, shown, mentioned);
  items.push(...links.main);
  const flags = (receipts?.flags || []).map((flag) => el("div", { class: "flag", text: flag }));
  const toggle = el("button", { type: "button", class: "expand link", text: "Show report", onclick: (event) => expandTask(event.currentTarget.closest(".card"), undefined, true) });
  return el("div", { class: "chips" }, el("div", { class: "receipts", title: "Observed while it worked, not just reported" }, ...items, toggle), links.more || "", ...flags);
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
  return el("div", { class: "report" }, text ? el("div", { class: "body markdown", html: markdown(text) }) : null, criteria, more.length ? el("div", { class: "receipts" }, ...more) : null);
}
// Tests it ran outside its own checkout (someone's source, to check what it reports): what they
// found, as evidence; where, and how they ended, on hover.
function testsElsewhere(item) {
  const where = item.cwd ? displayPath(item.cwd).split("/").filter(Boolean).pop() : "";
  const outcome = item.passed === null ? "running" : item.summary || (item.passed ? "passed" : `failed${item.exit_code ? ` (exit ${item.exit_code})` : ""}`);
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
}
