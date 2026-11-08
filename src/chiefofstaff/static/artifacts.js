// Home → Tasks → Artifacts: what the owner's tasks made or run, in one list, each with where it
// stands now: a PR as GitHub has it; a doc or a run as its worker last said, else as its task
// stands. The owner takes one off the list (its task keeps it) and can bring it back.

import { api, el, toast } from "./dom.js";
import { ago, linkKey, linkName, prTitle, shorten, taskLinks } from "./text.js";
import { state } from "./state.js";
import { openFile } from "./files.js";

// What a PR's state comes to, in a word or two, worst first.
export function prStatus(pr) {
  if (pr.state === "MERGED") return ["merged", "good"];
  if (pr.state === "CLOSED") return ["closed", "muted"];
  if (pr.draft) return ["draft", "active"];
  if (pr.checks === "failing") return ["CI failing", "bad"];
  if (pr.review === "CHANGES_REQUESTED") return ["changes requested", "bad"];
  if (pr.awaiting?.length) return ["awaiting approval", "wait"];
  if (pr.checks === "pending") return ["CI running", "active"];
  // GitHub would merge it now (approved, or no review required): a Ready-to-merge card says so.
  if (["CLEAN", "HAS_HOOKS"].includes(pr.merge_state)) return ["ready to merge", "good"];
  if (pr.review === "APPROVED") return ["approved", "good"];
  return ["in review", "active"];
}
// A status a worker gave, by its words: what a word means negated ("no errors", "not ready",
// "incomplete") is not what it means alone.
export function toneOf(status) {
  const bad = "fail|fails|failed|failing|failure|failures|error|errors|errored|broken|blocked|stuck|killed|crash|crashes|crashed|crashing|oom|panic|panicked|exception|exceptions|rejected|timeout|timeouts|timed-out|timed out|time out|timedout";
  const said = ` ${String(status || "").toLowerCase().replace(/[^\p{L}\p{N}-]+/gu, " ").trim()} `;
  // "0 errors, 2 failures": only "0 errors" is negated; the failures still count.
  const negated = new RegExp(`\\s(?:no|zero|0|without|not|never|didn t|didnt|did not)\\s(?:${bad})(?=\\s)|\\s(?:${bad})-free(?=\\s)`, "g");
  const words = said.replace(negated, " ");
  const has = (pattern) => new RegExp(`\\s(?:${pattern})\\s`).test(words);
  if (has(bad)) return "bad";
  if (has("cancelled|canceled|aborted|abandoned|skipped|superseded|obsolete")) return "muted";
  if (has("not|incomplete|unfinished|unpublished|partial|partially|pending|waiting|paused|on-hold|yet")) return /review|approval/.test(words) ? "wait" : "active";
  if (has("review|reviewing|approval|for-review")) return "wait";
  if (has("succeeded|success|successful|done|final|live|complete|completed|merged|published|passed|passing|ready|updated|shipped|deployed|green")) return "good";
  if (has("running|in-progress|progress|draft|queued|started|starting|writing|building|drafting")) return "active";
  if (words !== said && !has("but|except")) return "good";  // "no errors", "error-free".
  return "neutral";
}
// As its task stands, for a link its worker said nothing about; a result that only informs
// (a report, Home says) is not one to review.
const BY_TASK = {
  running: ["in progress", "active"],
  queued: ["queued", "active"],
  waiting: ["waiting on you", "wait"],
  review: ["to review", "wait"],
  reported: ["reported", "neutral"],
  done: ["delivered", "good"],
  failed: ["task failed", "bad"],
  cancelled: ["task cancelled", "muted"],
};
const byTask = (task, phase) => BY_TASK[task.status === "review" && phase && phase.section !== "needs" ? "reported" : task.status] || ["", "neutral"];
const RANK = { bad: 0, wait: 1, active: 2, neutral: 3, good: 4, muted: 5 };
// What kind of thing a link is, in a word.
const KINDS = { pr: "PR", issue: "Issue", ticket: "Ticket", file: "File", slack: "Slack", link: "Link" };
function kindOf(found) {
  if (!found) return "Link";
  if (KINDS[found.kind]) return KINDS[found.kind];
  const first = found.label.split(" · ")[0];
  return { "Google Doc": "Doc", "CI run": "CI", "Spark history": "Spark", "Airflow run": "Airflow", "Flyte run": "Flyte" }[first] || first;
}
// Its name: what its worker or a report called it, else what it says beside its kind ("abc123"
// of "Flyte · abc123"), else what it is ("Google Doc"); never a piece of a document's id.
function nameOf(link) {
  if (link.name) return link.name;
  const found = link.found;
  if (["issue", "ticket", "file", "link", "doc"].includes(found.kind) || !found.label.includes(" · ")) return linkName(link);
  return found.label.split(" · ").slice(1).join(" · ");
}

// Every artifact of these tasks, once each (a PR two tasks share: the one that made it; a
// document linked in two ways: one row), the ones that need a look first: { key, kind, name,
// url, path, status, tone, at, said, task }. What a task made: its PRs, what its worker declared
// or its reports say it made, the runs it gives; not what it was given or only mentions.
// `phases`: where each task is on Home (a report is "reported", not "to review").
export function collectArtifacts(tasks, phases = new Map()) {
  const found = new Map();
  const add = (item, own = false) => {
    const known = found.get(item.key);
    // The worker's own word beats a link in a report; between two, the newer.
    if (known && (known.own && !own)) return;
    if (known && known.own === own && String(known.at) >= String(item.at)) return;
    found.set(item.key, { ...item, own });
  };
  for (const task of tasks) {
    for (const pr of task.receipts?.prs || []) {
      if (!pr.own) continue;
      const [status, tone] = prStatus(pr);
      const title = pr.title || prTitle(pr.label);
      const short = pr.label.replace(/^[^/]+\//, "");
      add({ key: `pr:${pr.label.toLowerCase()}`, kind: "PR", name: title ? `${short} · ${title}` : short, url: pr.url, status, tone, at: pr.changed_at || pr.at || task.updated_at, said: true, task }, true);
    }
    const [status, tone] = byTask(task, phases.get(task.id));
    for (const link of taskLinks(task)) {
      if (link.said) {
        add({ key: link.key, kind: kindOf(link.found), name: nameOf(link), url: link.url, path: link.path, status: link.said.status || "linked", tone: toneOf(link.said.status), at: link.said.at, said: true, task }, true);
      } else if (!link.file && ["output", "run"].includes(link.role)) {
        // Named only in its reports: as its task stands.
        add({ key: link.key, kind: kindOf(link.found), name: nameOf(link), url: link.url, path: link.path, status, tone, at: task.updated_at, said: false, task });
      }
    }
  }
  return [...found.values()].sort((a, b) => RANK[a.tone] - RANK[b.tone] || String(b.at).localeCompare(String(a.at)));
}

// The list's rows: shown ones, then (on request) the hidden ones to bring back.
export function artifactItems(tasks, focusTask, phases) {
  const all = collectArtifacts(tasks, phases);
  const hidden = new Set(all.filter((item) => isHidden(item.key, item.tone)).map((item) => item.key));
  const shown = all.filter((item) => !hidden.has(item.key));
  const off = all.filter((item) => hidden.has(item.key));
  const rows = [...shown, ...(state.showHiddenArtifacts ? off : [])].map((item) => artifactRow(item, hidden.has(item.key), focusTask));
  if (off.length) {
    const words = state.showHiddenArtifacts ? "Hide the ones taken off" : `Show ${off.length} taken off`;
    rows.push({ key: `art-hidden:${off.length}:${Boolean(state.showHiddenArtifacts)}`, build: () => el("button", { type: "button", class: "link show-hidden", text: words, onclick: () => toggleHidden() }) });
  }
  return { rows, shown: shown.length };
}
function toggleHidden() {
  state.showHiddenArtifacts = !state.showHiddenArtifacts;
  import("./inbox.js").then((module) => module.renderHome());
}
// Taken off before (by an earlier version, under its address): known by its key now.
const hiddenList = () => (state.snapshot?.artifacts_hidden || []).map((item) => (typeof item === "string" ? { key: item, tone: "" } : item));
const hiddenKey = (item) => (/^https?:\/\//.test(item.key) ? linkKey(item.key) : item.key);
// Taken off the list (here or on its task's card), with how it stood then: one that failed
// since comes back.
export function isHidden(key, tone = "") {
  const off = hiddenList().find((item) => hiddenKey(item) === key);
  return Boolean(off) && !(tone === "bad" && off.tone !== "bad");
}
export async function setHidden(key, hidden, tone = "") {
  const before = state.snapshot.artifacts_hidden || [];
  const same = hiddenList().filter((item) => hiddenKey(item) === key).map((item) => item.key);
  const others = hiddenList().filter((item) => hiddenKey(item) !== key);
  state.snapshot.artifacts_hidden = hidden ? [...others, { key, tone }] : others;
  import("./inbox.js").then((module) => module.renderHome());
  try {
    const also = same.filter((old) => old !== key);
    await api("/api/artifacts/hide", { method: "POST", body: { key, hidden, tone, ...(also.length ? { also } : {}) } });
    if (hidden) toast("Taken off Artifacts; its task keeps it.");
  } catch (error) {
    state.snapshot.artifacts_hidden = before;
    import("./inbox.js").then((module) => module.renderHome());
    toast(error.message, "error");
  }
}
function artifactRow(item, hidden, focusTask) {
  const when = item.at ? ago(item.at) : "";
  const help = item.said ? (item.kind === "PR" ? "As GitHub has it" : "As its worker last said") : "As its task stands: its worker has not said where this stands";
  return {
    key: `art:${item.key}:${hidden ? 1 : 0}`,
    version: `${item.name}:${item.status}:${item.tone}:${when}:${item.task.id}:${item.task.title}`,
    build: () => {
      const node = el("div", { class: `artifact tone-${item.tone}${hidden ? " hidden-artifact" : ""}`, "data-key": item.key });
      fill(node, item, hidden, focusTask, when, help);
      return node;
    },
    refresh: (node) => fill(node, item, hidden, focusTask, when, help),
  };
}
function fill(node, item, hidden, focusTask, when, help) {
  node.className = `artifact tone-${item.tone}${hidden ? " hidden-artifact" : ""}`;
  node.replaceChildren(
    el("span", { class: "a-kind", text: item.kind }),
    item.path
      ? el("button", { type: "button", class: "a-name link", text: shorten(item.name, 90), title: `${item.name}\n${item.path}\nClick to open it`, onclick: () => openFile(item.path, item.task.session_id) })
      : el("a", { class: "a-name", href: item.url, target: "_blank", rel: "noreferrer", text: shorten(item.name, 90), title: `${item.name}\n${item.url}` }),
    el("span", { class: `a-status${item.said ? "" : " derived"}`, text: item.status, title: `${help}${item.at ? ` · ${new Date(item.at).toLocaleString()}` : ""}` }),
    hidden
      ? el("button", { type: "button", class: "a-hide link", text: "Bring back", title: "Back on this list", onclick: () => setHidden(item.key, false) })
      : el("button", { type: "button", class: "a-hide", text: "✕", "aria-label": `Take ${item.name} off this list`, title: "Take it off this list (its task keeps it; it comes back if it fails)", onclick: () => setHidden(item.key, true, item.tone) }),
    el(
      "div",
      { class: "a-meta" },
      el("button", { type: "button", class: "a-task link", text: item.task.title, title: "Open its task", onclick: () => focusTask(item.task.id, item.task.session_id) }),
      when ? el("span", { class: "a-when", text: ` · ${when === "now" ? "just now" : `${when} ago`}` }) : "",
    ),
  );
}
