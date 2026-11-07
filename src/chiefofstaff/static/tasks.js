// Task cards on Home: what runs and what it does now, what waits, what is ready to review.

import { api, el, foldable, moreMenu, submits, toast } from "./dom.js";
import { ago, compactPaths, gist, markdown, plural, since } from "./text.js";
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
// Workers at work whose task is finished (news on a PR they follow, a question about it): they
// are running too, and say on what.
export function followUpItem(session) {
  const line = () => stepText(session.step || session.activity || "Working…", session.cwd);
  const about = session.task ? `following up on “${session.task.title}”` : "working";
  return {
    key: `followup:${session.id}`,
    version: `${line()}:${about}`,
    build: () =>
      el(
        "div",
        { class: "card task running followup" },
        el(
          "div",
          { class: "head" },
          el("span", { class: "dot working", title: "Working" }),
          el("button", { type: "button", class: "title", text: session.title, title: "Open its worker", onclick: () => select(session.id, { task: session.task?.task_id }) }),
          el("span", { class: "meta", text: about }),
        ),
        el("div", { class: "task-body" }, el("div", { class: "activity", text: line() })),
      ),
    refresh: (node) => {
      node.querySelector(".activity").textContent = line();
      node.querySelector(".head .meta").textContent = about;
    },
  };
}
// The key covers what changes a card's actions and layout; the version, what only its text.
export function taskItem(task) {
  const worker = byId(task.session_id);
  const live = ["running", "queued"].includes(task.status);
  return {
    key: `task:${task.id}:${task.status}:${task.blocked ? 1 : 0}:${createsPr(task) ? 1 : 0}:${task.asking ? 1 : 0}:${task.news?.at || ""}`,
    id: task.id,
    // For running cards the minute too: "6m" and "3m ago" move on by themselves.
    version: `${task.updated_at}:${task.title}:${worker?.status || ""}:${worker?.activity || ""}:${worker?.step || ""}:${worker?.step_since || ""}:${worker?.task?.task_id || ""}:${task.credits}:${task.runs}:${JSON.stringify(task.receipts?.prs || null)}:${task.status === "review" ? JSON.stringify(task.receipts || null) : ""}:${live ? `${Math.floor(Date.now() / 60000)}:${queuedKey()}` : ""}:${waitsOnOwner(task, worker)}:${(worker?.follows || []).join(",")}`,
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
  for (const part of [".task-body", ".news", ".chips", ".report"]) {
    const old = node.querySelector(`:scope > ${part}`);
    const fresh = part === ".task-body" ? taskBody(task) : part === ".news" ? newsBlock(task) : !review ? null : part === ".chips" ? chips(task) : report(task);
    if (old && fresh) old.replaceWith(fresh);
    else if (old) old.remove();
    else if (fresh) (node.querySelector(":scope > .actions") ? node.querySelector(":scope > .actions").before(fresh) : node.append(fresh));
  }
  expandTask(node, review && openReports.has(task.id));
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
// A result to review shows its full report on demand; an open one stays open while the card
// is redrawn (new receipts, a fresh PR state).
const openReports = new Set();
export function expandTask(node, open = !node.classList.contains("expanded")) {
  node.classList.toggle("expanded", open);
  const toggle = node.querySelector(".expand");
  if (toggle) toggle.textContent = open ? "Hide report" : "Show report";
  if (node.dataset.task) openReports[open ? "add" : "delete"](node.dataset.task);
}
function taskCard(task) {
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  const [meta, help] = taskMeta(task);
  const open = task.session_id ? () => select(task.session_id) : null;
  // The title opens its worker at this task's report: it may be on other work since.
  const openAt = task.session_id ? () => select(task.session_id, { task: task.id }) : null;
  const worker = byId(task.session_id);
  const named = worker && worker.title !== task.title ? `\nWorker: ${worker.title}` : "";
  const node = el(
    "div",
    { class: `card task ${task.status}${task.blocked ? " blocked" : ""}`, "data-session": task.session_id || undefined, "data-task": task.id },
    el(
      "div",
      { class: "head" },
      el("span", { class: `dot ${task.blocked ? "waiting" : DOT[task.status]}` }),
      el("button", { type: "button", class: "title", text: task.title, title: openAt ? `Open its worker at this task${named}` : task.goal || "", onclick: openAt, disabled: !openAt }),
      task.asking ? el("span", { class: "chip waiting", text: "Needs you", title: "Its worker waits for you on a card under Needs you" }) : null,
      el("span", { class: "meta", text: meta, title: help }),
    ),
  );
  const body = taskBody(task);
  if (body) node.append(body);
  const news = newsBlock(task);
  if (news) node.append(news);
  if (task.status === "review") node.append(...[chips(task), report(task)].filter(Boolean));
  expandTask(node, task.status === "review" && openReports.has(task.id));
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
    const inline = ["running", "queued", "done", "cancelled"].includes(task.status) && !task.blocked && !task.news && !actions.dataset.editing;
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
    } else if (task.status === "done" && task.news) {
      // News about finished work: seen, it goes back to the finished ones.
      main = el("button", { type: "button", text: "Got it", onclick: () => act(`/api/tasks/${task.id}/news-seen`, {}, "") });
      more = [{ text: "Follow up…", onclick: () => reply("What should it do about it?", "Sent to the worker.") }, copy, pr, worker];
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
  if (task.status === "running") return runningBody(task, worker);
  if (task.status === "done" || task.status === "cancelled") return null;
  if (task.status === "review") {
    // What came out, in its first sentences (past a "## Summary" title); the report on demand.
    const first = gist(task.result || task.progress || "");
    return first ? el("div", { class: "task-body" }, el("div", { class: "outcome", text: first, title: first })) : null;
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
// Its latest report on this task, in its first sentences: two lines, the rest on a click.
function progressLine(text, at) {
  const words = gist(text);
  if (!words) return null;
  const node = el("div", { class: "progress-line", title: "Its latest report on this task · click to read it all" }, el("span", { class: "words", text: words }), at ? el("span", { class: "when", text: ` · ${since(at)}` }) : "");
  node.addEventListener("click", () => node.classList.toggle("open"));
  return node;
}
// What a task has made or points to, as chips: its PRs (with GitHub's state once read), docs,
// runs, tickets and files it linked; the threads its worker watches; what waits for the owner.
const LINK = /https?:\/\/[^\s<>()[\]`'"，。、；！？）]+/g;
function linkKind(url) {
  let match;
  if ((match = /^https:\/\/github\.com\/([^/]+)\/([^/]+)\/pull\/(\d+)/.exec(url))) return { kind: "pr", label: `${match[2]}#${match[3]}`, key: `${match[1]}/${match[2]}#${match[3]}`.toLowerCase() };
  if ((match = /^https:\/\/github\.com\/[^/]+\/([^/]+)\/issues\/(\d+)/.exec(url))) return { kind: "issue", label: `${match[1]}#${match[2]}` };
  if ((match = /^https:\/\/github\.com\/[^/]+\/([^/]+)\/actions\/runs\//.exec(url))) return { kind: "run", label: `CI run · ${match[1]}` };
  if (/^https:\/\/github\.com\/[^/]+\/[^/]+\/(blob|tree)\//.test(url)) return { kind: "file", label: decodeURIComponent(url.split(/[?#]/)[0].split("/").pop()) || "File" };
  if (/docs\.google\.com\/spreadsheets/.test(url)) return { kind: "doc", label: "Sheet" };
  if (/docs\.google\.com\/presentation/.test(url)) return { kind: "doc", label: "Slides" };
  if (/docs\.google\.com\//.test(url)) return { kind: "doc", label: "Google Doc" };
  if (/\.slack\.com\/archives\//.test(url)) return { kind: "slack", label: "Slack message" };
  if ((match = /atlassian\.net\/browse\/([A-Z][A-Z0-9]+-\d+)/.exec(url))) return { kind: "ticket", label: match[1] };
  if (/notion\.so\//.test(url)) return { kind: "doc", label: "Notion" };
  if (/airflow/i.test(url)) return { kind: "run", label: "Airflow run" };
  if (/flyte/i.test(url)) return { kind: "run", label: "Flyte run" };
  try {
    return { kind: "link", label: new URL(url).hostname.replace(/^www\./, "") };
  } catch {
    return null;
  }
}
function linkChips(task, shown) {
  const chipsFound = [];
  const seen = new Set();
  for (const raw of [task.progress, task.result].filter(Boolean).join("\n").match(LINK) || []) {
    const url = raw.replace(/[.,;:!?'"]+$/, "");
    const found = linkKind(url);
    if (!found || seen.has(url) || (found.key && shown.has(found.key))) continue;
    seen.add(url);
    if (found.key) shown.add(found.key);
    chipsFound.push(el("a", { class: `receipt artifact ${found.kind}`, href: url, target: "_blank", rel: "noreferrer", title: url, text: found.label }));
    if (chipsFound.length >= 6) break;
  }
  return chipsFound;
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
export function artifacts(task, worker = byId(task.session_id), { active = true, prs = true } = {}) {
  const items = [];
  const shown = new Set();
  for (const pr of task.receipts?.prs || []) {
    shown.add(pr.label.toLowerCase());
    if (prs) items.push(prChip(pr));
  }
  items.push(...linkChips(task, shown));
  if (active && worker && ["running", "waiting"].includes(task.status)) {
    for (const key of worker.follows || []) {
      const pr = /^github:(.+)$/.exec(key)?.[1];
      if (pr && shown.has(pr.toLowerCase())) continue;
      const label = pr ? pr.replace(/^[^/]+\//, "") : key.startsWith("slack:") ? "a Slack thread" : key;
      items.push(el("span", { class: "receipt watching", title: `It follows ${key}: what happens there comes to it`, text: `Watching ${label}` }));
    }
  }
  const { drafts, questions } = ownerItems(task, worker);
  for (const draft of drafts) items.push(el("button", { type: "button", class: "receipt needs", title: "Waiting for you under Needs you", text: `${WAITS_WORDS[draft.kind] || "Draft"} · Needs you`, onclick: () => focusDraft(draft.id) }));
  for (const question of questions) items.push(el("button", { type: "button", class: "receipt needs", title: question.text, text: "Question · Needs you", onclick: () => focusQuestion(question.session_id) }));
  return items.length ? el("div", { class: "artifacts receipts" }, ...items) : null;
}
// A worker with several open tasks, as one card: the worker and what it does now, then each of
// its tasks a line: where it stands (on it now, waiting its turn, waiting for you, queued), for
// how long, and its own latest progress. The owner sees at once which task moves.
export function workerGroupItem(worker, list) {
  const rows = list.map((task) => `${task.id}:${task.status}:${task.updated_at}:${task.progress?.length || 0}:${JSON.stringify(task.receipts?.prs || null)}:${waitsOnOwner(task, worker)}`).join(",");
  return {
    key: `worker:${worker.id}:${list.map((task) => task.id).join(",")}`,
    id: worker.id,
    version: `${worker.status}:${worker.step || ""}:${worker.activity || ""}:${worker.step_since || ""}:${worker.task?.task_id || ""}:${(worker.follows || []).join(",")}:${rows}:${Math.floor(Date.now() / 60000)}`,
    build: () => workerGroupCard(worker, list),
    refresh: (node) => node.replaceChildren(...workerGroupCard(worker, list).childNodes),
  };
}
// The task a worker with several is on now. One conversation does them in turn: a step it
// began before a newer task came is still about an earlier one, so the latest task it had when
// its step began; between steps, the one it was given last.
function activeTask(worker, list) {
  const running = list.filter((task) => task.status === "running").sort((a, b) => a.created_at.localeCompare(b.created_at));
  if (!running.length) return "";
  if (worker.status === "working" && worker.step_since) {
    const before = running.filter((task) => task.created_at <= worker.step_since);
    if (before.length) return before[before.length - 1].id;
  }
  return running.some((task) => task.id === worker.task?.task_id) ? worker.task.task_id : running[running.length - 1].id;
}
// Where one of a worker's tasks stands, in a few words, with its mark.
function taskState(task, worker, active) {
  const current = active === task.id;
  if (task.status === "waiting") return ["?", "waiting", "waits for you · Needs you"];
  if (task.status === "queued") return ["…", "queued", queuedReason(task).text];
  if (!current) return ["‖", "paused", "waits its turn"];
  const started = ago(task.created_at);
  if (worker.status === "working") return ["●", "now", `on it now${started === "now" ? "" : ` · ${started}`}`];
  if (worker.activity?.startsWith("waiting for")) return ["●", "now", worker.activity];
  return ["○", "now", (worker.follows || []).length ? "waiting on others" : "between turns"];
}
function workerGroupCard(worker, list) {
  const order = { running: 0, waiting: 1, queued: 2 };
  const active = activeTask(worker, list);
  const current = (task) => (active === task.id ? -1 : 0);
  const sorted = [...list].sort((a, b) => current(a) - current(b) || order[a.status] - order[b.status] || a.created_at.localeCompare(b.created_at));
  const working = worker.status === "working";
  const rows = sorted.map((task) => {
    const [mark, kind, words] = taskState(task, worker, active);
    const confirmCancel = async () => {
      if (!confirm(`Cancel “${task.title}”? Its worker stops working on it.`)) return;
      try {
        upsertTask(await api(`/api/tasks/${task.id}/cancel`, { method: "POST", body: { text: "" } }));
        toast("Task cancelled.");
      } catch (error) {
        toast(error.message, "error");
      }
    };
    return el(
      "li",
      { class: `task-row ${kind}`, "data-task": task.id },
      el("span", { class: "mark", text: mark, title: words }),
      el("div", { class: "row-main" },
        el("div", { class: "row-head" },
          el("button", { type: "button", class: "title link", text: task.title, title: `${task.goal || task.title}\n\nOpen its worker at this task`, onclick: () => select(worker.id, { task: task.id }) }),
          el("span", { class: "state", text: words, title: words }),
          moreMenu([{ text: "Open worker", onclick: () => select(worker.id, { task: task.id }) }, { text: "Cancel task…", onclick: confirmCancel, danger: true }])),
        progressLine(task.progress, task.updated_at),
        artifacts(task, worker, { active: kind === "now" })),
    );
  });
  return el(
    "div",
    { class: "card task worker-group running", "data-session": worker.id },
    el("div", { class: "head" },
      el("span", { class: `dot ${working ? "working" : "watching"}`, title: working ? "Working now" : "Between steps" }),
      el("button", { type: "button", class: "title", text: worker.title, title: "Open the worker", onclick: () => select(worker.id) }),
      el("span", { class: "meta", text: plural(list.length, "task") })),
    el("div", { class: "task-body" }, activityLine(worker)),
    el("ol", { class: "task-rows" }, ...rows),
  );
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
// A running task at a glance: which worker has it and whether that worker is on it right now
// (a worker with several tasks works on one at a time); the step it is in and for how long, or
// how long it has been quiet and what it waits for; and this task's own latest progress.
function runningBody(task, worker) {
  const nodes = [];
  // Its newest task done or in review: it is back on this one.
  const current = !worker?.task || worker.task.task_id === task.id || worker.task.status !== "running";
  if (!current) nodes.push(el("div", { class: "reason", text: `Waits its turn: its worker is on “${worker.task.title}” now` }));
  else nodes.push(activityLine(worker));
  // Its own progress: what it last reported on this task (not its asides while it works).
  const progress = progressLine(task.progress, task.updated_at);
  if (progress) nodes.push(progress);
  const found = artifacts(task, worker, { active: current });
  if (found) nodes.push(found);
  return el("div", { class: "task-body" }, ...nodes);
}
// A worker between turns on a running task: how long it has been quiet, and what it follows.
function quiet(worker) {
  if (!worker) return "Starting…";
  const follows = (worker.follows || []).map((key) => key.replace(/^github:[^/]+\//, "").replace(/^slack:.*/, "a Slack thread"));
  return [`Quiet since ${since(worker.updated_at)}`, follows.length ? `follows ${follows.slice(0, 2).join(", ")}` : ""].filter(Boolean).join(" · ");
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
  const ci = { passing: "✓ CI", failing: "✗ CI", pending: "CI running" }[pr.checks] || "";
  const state = pr.state === "MERGED" ? "merged" : pr.state === "CLOSED" ? "closed" : "";
  // An approval gate (an "Owner Approval" check) waits for a person, not for CI.
  const awaiting = !state && pr.awaiting?.length ? "awaiting approval" : "";
  const ready = !state && !awaiting && pr.checks !== "failing" && ["CLEAN", "HAS_HOOKS"].includes(pr.merge_state) && !pr.draft ? "ready to merge" : "";
  const review = { APPROVED: "approved", CHANGES_REQUESTED: "changes requested" }[pr.review] || "";
  const failing = pr.checks === "failing" && pr.failing?.length ? `\nFailing: ${pr.failing.join(", ")}` : "";
  const ciWords = { passing: "Checks pass", failing: "Checks fail", pending: "Checks are running", none: "No checks" }[pr.checks] || "";
  const seen = pr.at ? `\nAs of ${new Date(pr.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "";
  const title = [pr.label, pr.title ? `“${pr.title}”` : "", ciWords + failing, awaiting && `Waiting for approval: ${pr.awaiting.join(", ")}`, review && `Review: ${review}`, ready && "GitHub would merge it now", state && `PR ${state}`].filter(Boolean).join("\n") + seen;
  return el("a", { class: `receipt pr ${state || pr.checks}`, href: pr.url, target: "_blank", rel: "noreferrer", title, text: [short, state || ci, !state && (awaiting || review), ready].filter(Boolean).join(" · ") });
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
  result("tests", receipts?.tests);
  result("lint", receipts?.checks);
  result("build", receipts?.build);
  const changed = receipts?.changed || {};
  if (changed.files) items.push(el("button", { type: "button", class: "receipt changes", title: "See the changes and comment on them", text: `+${changed.insertions} −${changed.deletions} in ${plural(changed.files, "file")}`, onclick: () => showChanges(task, upsertTask) }));
  for (const pr of receipts?.prs || []) items.push(prChip(pr));
  const prs = new Set((receipts?.prs || []).map((pr) => pr.label.toLowerCase()));
  items.push(...linkChips(task, prs));
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
