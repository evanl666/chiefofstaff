// Task cards on Home: what runs and what it does now, what waits, what is ready to review.

import { api, el, foldable, moreMenu, submits, toast } from "./dom.js";
import { ago, compactPaths, markdown, plural, since } from "./text.js";
import { byId, closedWorkers, sessions, state, tasks } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { select } from "./session.js";
import { scheduleHome } from "./inbox.js";
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
          el("button", { type: "button", class: "title", text: session.title, title: "Open its worker", onclick: () => select(session.id) }),
          el("span", { class: "meta", text: "in a terminal" }),
        ),
        el("div", { class: "task-body" }, el("div", { class: "activity", text: line() })),
      ),
    refresh: (node) => {
      node.querySelector(".activity").textContent = line();
    },
  };
}
// The key covers what changes a card's actions and layout; the version, what only its text.
export function taskItem(task) {
  const worker = byId(task.session_id);
  const live = ["running", "queued"].includes(task.status);
  return {
    key: `task:${task.id}:${task.status}:${task.blocked ? 1 : 0}:${createsPr(task) ? 1 : 0}`,
    id: task.id,
    // For running cards the minute too: "6m" and "3m ago" move on by themselves.
    version: `${task.updated_at}:${task.title}:${worker?.status || ""}:${worker?.activity || ""}:${worker?.said_at || ""}:${worker?.step_since || ""}:${task.credits}:${task.runs}:${task.status === "review" ? JSON.stringify(task.receipts || null) : ""}:${live ? `${Math.floor(Date.now() / 60000)}:${queuedKey()}` : ""}`,
    build: () => taskCard(task),
    refresh: (node) => refreshTaskCard(node, task),
  };
}
// A queued card's reason changes as other workers start and stop.
const queuedKey = () => `${sessions().filter(busy).length}/${state.snapshot.settings.max_workers}`;
// What its dot says: working, waiting for a slot, for the owner, done or failed.
const DOT = { running: "working", queued: "queued", waiting: "waiting", review: "done", done: "done", failed: "failed", cancelled: "" };
function refreshTaskCard(node, task) {
  const title = node.querySelector(".head .title");
  title.textContent = task.title;
  const [meta, help] = taskMeta(task);
  node.querySelector(".head .meta").textContent = meta;
  node.querySelector(".head .meta").title = help;
  const review = task.status === "review";
  for (const part of [".task-body", ".chips", ".report"]) {
    const old = node.querySelector(`:scope > ${part}`);
    const fresh = part === ".task-body" ? taskBody(task) : !review ? null : part === ".chips" ? chips(task) : report(task);
    if (old && fresh) old.replaceWith(fresh);
    else if (old) old.remove();
    else if (fresh) (node.querySelector(":scope > .actions") ? node.querySelector(":scope > .actions").before(fresh) : node.append(fresh));
  }
}
// Short on the card: how long it has run, or when it finished; the rest on hover.
function taskMeta(task) {
  const ended = ["review", "done", "failed", "cancelled"].includes(task.status);
  const help = [
    ended ? `Finished ${since(task.ended_at || task.updated_at)}` : `Started ${since(task.created_at)}`,
    task.worker && task.worker !== task.title ? `Worker: ${task.worker}` : "",
    task.credits >= 0.05 ? `${task.credits.toFixed(1)} credits` : "",
    task.runs > 1 ? `${task.runs} runs` : "",
  ];
  const started = ago(task.created_at);
  const when = since(task.ended_at || task.updated_at);
  const meta = {
    queued: task.blocked ? "Blocked" : "",
    running: started === "now" ? "just now" : started,
    waiting: `Waiting · ${when}`,
    failed: `Failed · ${when}`,
    cancelled: `Cancelled · ${when}`,
  }[task.status] ?? when;
  return [meta, help.filter(Boolean).join("\n")];
}
// Work on a branch of Chief of Staff's own (chief/…) that is not in a PR yet. A worker that
// checked out someone else's PR to review it is on their branch: nothing to open.
function createsPr(task) {
  const receipts = task.receipts;
  if (!receipts || receipts.prs?.length) return false;
  return (receipts.checkouts || []).some((checkout) => checkout.branch?.startsWith("chief/") && (checkout.commits || checkout.uncommitted));
}
// A result to review shows its full report on demand.
export function expandTask(node, open = !node.classList.contains("expanded")) {
  node.classList.toggle("expanded", open);
  const toggle = node.querySelector(".expand");
  if (toggle) toggle.textContent = open ? "Hide report" : "Show report";
}
function taskCard(task) {
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  const [meta, help] = taskMeta(task);
  const open = task.session_id ? () => select(task.session_id) : null;
  const node = el(
    "div",
    { class: `card task ${task.status}${task.blocked ? " blocked" : ""}`, "data-session": task.session_id || undefined },
    el(
      "div",
      { class: "head" },
      el("span", { class: `dot ${task.blocked ? "waiting" : DOT[task.status]}` }),
      el("button", { type: "button", class: "title", text: task.title, title: open ? "Open its worker" : task.goal || "", onclick: open, disabled: !open }),
      el("span", { class: "meta", text: meta, title: help }),
    ),
  );
  const body = taskBody(task);
  if (body) node.append(body);
  if (task.status === "review") node.append(...[chips(task), report(task)].filter(Boolean));
  const key = `task:${task.id}`;

  // The card leaves its place at once (`moves`); the task's new state puts it where it goes.
  async function act(path, payload, done, moves = true) {
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
      if (done) toast(done);
    } catch (failure) {
      state.leaving.delete(key);
      node.classList.remove("leaving");
      error.textContent = failure.message;
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
  // Live and finished cards keep their "⋯" in the head, on one line with the title; a form
  // the owner opens takes a line of its own.
  function place() {
    const inline = ["running", "queued", "done", "cancelled"].includes(task.status) && !task.blocked && !actions.dataset.editing;
    actions.classList.toggle("inline", inline);
    const parent = inline ? node.querySelector(":scope > .head") : node;
    if (actions.parentElement !== parent) parent.append(actions);
  }
  function reply(placeholder, done, moves = true) {
    const input = el("textarea", { rows: 2, placeholder });
    form(input, "Send", () => input.value.trim() && act(`/api/tasks/${task.id}/follow-up`, { text: input.value.trim() }, done, moves));
  }
  function confirmCancel() {
    const reason = el("input", { placeholder: "Why? (optional)" });
    form(reason, "Cancel task", () => act(`/api/tasks/${task.id}/cancel`, { text: reason.value.trim() }, "Task cancelled."));
    actions.lastElementChild.className = "danger";
    actions.querySelector(".text").textContent = "Keep it";
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
    let main = null;
    let more = [];
    if (task.status === "review") {
      main = el("button", { type: "button", text: "Accept", onclick: () => act(`/api/tasks/${task.id}/accept`, {}, "Accepted.") });
      more = [{ text: "Follow up…", onclick: () => reply("What else should it do?", "Sent to the worker.") }, copy, pr, worker];
    } else if (task.status === "waiting") {
      main = el("button", { type: "button", text: "Reply", onclick: () => reply("Your answer", "Sent to the worker.") });
      more = [worker, cancel];
    } else if (task.status === "failed") {
      main = el("button", { type: "button", text: "Retry", onclick: () => act(`/api/tasks/${task.id}/retry`, {}, "Retrying.") });
      more = [{ text: "Retry with a note…", onclick: retryWithNote }, worker, cancel];
    } else if (task.status === "running" || task.status === "queued") {
      more = [note, worker, cancel];
    } else if (task.status === "done") {
      more = [copy, pr, worker];
    } else {
      more = [worker];
    }
    actions.replaceChildren(error, moreMenu(more) || "", main || "");
    place();
  }
  showActions();
  return node;
}
function taskBody(task) {
  const worker = byId(task.session_id);
  if (task.status === "queued" && !task.blocked) {
    const reason = queuedReason(task);
    return el("div", { class: "task-body" }, el("div", { class: "reason", text: reason.text, title: reason.help || "" }));
  }
  if (task.status === "running") {
    // At a glance: the step it is in now (and for how long), and what it said last.
    const line = worker?.step || worker?.activity || (worker?.status === "working" ? "Working…" : "Between turns");
    const command = compactPaths(line.replace(/^(bash|shell|powershell) /, "$ "), worker?.cwd);
    const long = worker?.step_since && ago(worker.step_since) !== "now";
    const nodes = [
      el("div", { class: "activity", title: line }, worker?.step ? "Now: " : "", command, long ? el("span", { class: "when", text: ` · ${ago(worker.step_since)}` }) : ""),
    ];
    if (worker?.said) {
      nodes.push(el("div", { class: "said", title: worker.said }, `“${worker.said.replace(/\s+/g, " ").trim()}”`, el("span", { class: "when", text: ` · ${since(worker.said_at)}` })));
    }
    return el("div", { class: "task-body" }, ...nodes);
  }
  if (task.status === "done" || task.status === "cancelled") return null;
  if (task.status === "review") {
    // One line of what came out; the whole report on demand.
    const text = task.result || task.progress || "";
    const first = text.split("\n").map((line) => line.replace(/^[#>*\-\s]+/, "").replace(/[*_`]/g, "").trim()).find(Boolean);
    return first ? el("div", { class: "task-body" }, el("div", { class: "outcome", text: first, title: first })) : null;
  }
  const text = task.progress || (task.blocked ? "It waits for a task that ended without finishing." : "");
  if (!text) return null;
  const wrapper = el("div", { class: "task-body" });
  foldable(wrapper, el("div", { class: "body markdown", html: markdown(text) }), text, { chars: 360, lines: 5 });
  return wrapper;
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
    const mark = item.passed ? (item.failed_before ? "✗→✓" : "✓") : "✗";
    const before = item.failed_before ? "\nFailed first, then passed" : "";
    items.push(el("span", { class: `receipt ${item.passed ? "pass" : "fail"}`, title: `${item.command}\n(${plural(item.runs, "run")})${before}`, text: `${mark} ${label}` }));
  };
  result("tests", receipts?.tests);
  result("checks", receipts?.checks);
  result("build", receipts?.build);
  const changed = receipts?.changed || {};
  if (changed.files) items.push(el("button", { type: "button", class: "receipt changes", title: "See the changes and comment on them", text: `+${changed.insertions} −${changed.deletions} in ${plural(changed.files, "file")}`, onclick: () => showChanges(task, upsertTask) }));
  for (const pr of receipts?.prs || []) {
    const checks = { passing: "✓", failing: "✗", pending: "…" }[pr.checks] || "";
    items.push(el("a", { class: `receipt pr ${pr.checks}`, href: pr.url, target: "_blank", rel: "noreferrer", text: `${pr.label}${pr.state && pr.state !== "OPEN" ? ` (${pr.state.toLowerCase()})` : ""} ${checks}`.trim() }));
  }
  const flags = (receipts?.flags || []).map((flag) => el("div", { class: "flag", text: flag }));
  const toggle = el("button", { type: "button", class: "expand link", text: "Show report", onclick: (event) => expandTask(event.currentTarget.closest(".card")) });
  return el("div", { class: "chips" }, el("div", { class: "receipts", title: "Observed while it worked, not just reported" }, ...items, toggle), ...flags);
}
// The whole report: what the worker wrote, each criterion and how it was checked.
function report(task) {
  const receipts = task.receipts || {};
  const text = task.result || task.progress || "";
  const more = [];
  const changed = receipts.changed || {};
  if (changed.commits) more.push(el("span", { class: "receipt", text: plural(changed.commits, "commit") }));
  if (changed.uncommitted) more.push(el("span", { class: "receipt warn", text: `${changed.uncommitted} uncommitted` }));
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
