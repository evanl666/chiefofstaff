// Home → Tasks → Artifacts: what the owner's tasks made or run, in one list, each with where it
// stands now: a PR as GitHub has it; a doc or a run as its worker last said, else as its task
// stands. The owner takes one off the list (its task keeps it) and can bring it back.

import { api, el, toast } from "./dom.js";
import { ago, findUrls, linkKey, linkKind, prTitle, shorten } from "./text.js";
import { state } from "./state.js";

// What a PR's state comes to, in a word or two, worst first.
export function prStatus(pr) {
  if (pr.state === "MERGED") return ["merged", "good"];
  if (pr.state === "CLOSED") return ["closed", "muted"];
  if (pr.draft) return ["draft", "active"];
  if (pr.checks === "failing") return ["CI failing", "bad"];
  if (pr.review === "CHANGES_REQUESTED") return ["changes requested", "bad"];
  if (pr.awaiting?.length) return ["awaiting approval", "wait"];
  if (pr.review === "APPROVED" && ["CLEAN", "HAS_HOOKS"].includes(pr.merge_state)) return ["ready to merge", "good"];
  if (pr.review === "APPROVED") return ["approved", "good"];
  if (pr.checks === "pending") return ["CI running", "active"];
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
// As its task stands, for a link its worker said nothing about.
const BY_TASK = {
  running: ["in progress", "active"],
  queued: ["queued", "active"],
  waiting: ["waiting on you", "wait"],
  review: ["to review", "wait"],
  done: ["delivered", "good"],
  failed: ["task failed", "bad"],
  cancelled: ["task cancelled", "muted"],
};
const RANK = { bad: 0, wait: 1, active: 2, neutral: 3, good: 4, muted: 5 };
// What kind of thing a link is, in a word.
const KINDS = { pr: "PR", issue: "Issue", ticket: "Ticket", file: "File", slack: "Slack", link: "Link" };
function kindOf(found) {
  if (!found) return "Link";
  if (KINDS[found.kind]) return KINDS[found.kind];
  const first = found.label.split(" · ")[0];
  return { "Google Doc": "Doc", "CI run": "CI", "Spark history": "Spark", "Airflow run": "Airflow", "Flyte run": "Flyte" }[first] || first;
}
const nameOf = (found, url) => {
  if (!found) return url;
  if (["issue", "ticket", "file", "link"].includes(found.kind)) return found.label;
  return found.label.includes(" · ") ? found.label.split(" · ").slice(1).join(" · ") : found.label;
};
// Links of a text, not those in its code blocks.
const linksIn = (text) => findUrls((text || "").replace(/```[\s\S]*?(```|$)/g, " "));

// Every artifact of these tasks, once each (a PR two tasks share: the one that made it; a
// document linked in two ways: one row), the ones that need a look first: { key, kind, name,
// url, status, tone, at, said, task }. Not what a task was given to work from, nor a PR it only
// looked at.
export function collectArtifacts(tasks) {
  const found = new Map();
  const add = (item, own = false) => {
    const known = found.get(item.key);
    // The worker's own word beats a link in a report; between two, the newer.
    if (known && (known.own && !own)) return;
    if (known && known.own === own && String(known.at) >= String(item.at)) return;
    found.set(item.key, { ...item, own });
  };
  for (const task of tasks) {
    const given = new Set((task.given_links || []).map(linkKey));
    for (const pr of task.receipts?.prs || []) {
      if (!pr.own) continue;
      const [status, tone] = prStatus(pr);
      const title = pr.title || prTitle(pr.label);
      const short = pr.label.replace(/^[^/]+\//, "");
      add({ key: `pr:${pr.label.toLowerCase()}`, kind: "PR", name: title ? `${short} · ${title}` : short, url: pr.url, status, tone, at: pr.changed_at || pr.at || task.updated_at, said: true, task }, true);
    }
    const said = new Set();
    for (const item of task.artifacts || []) {
      const key = linkKey(item.url);
      said.add(key);
      const kind = linkKind(item.url);
      add({ key, kind: kindOf(kind), name: item.name || nameOf(kind, item.url), url: item.url, status: item.status || "linked", tone: toneOf(item.status), at: item.at, said: true, task }, true);
    }
    // What it made that its worker named only in its reports (any of them): as its task stands.
    const [status, tone] = BY_TASK[task.status] || ["", "neutral"];
    for (const url of [...new Set([...(task.links || []), ...linksIn([task.result, task.progress].filter(Boolean).join("\n"))])]) {
      const kind = linkKind(url);
      const key = linkKey(url);
      if (said.has(key) || given.has(key) || !kind || !["output", "run"].includes(kind.role)) continue;
      add({ key, kind: kindOf(kind), name: nameOf(kind, url), url, status, tone, at: task.updated_at, said: false, task });
    }
  }
  return [...found.values()].sort((a, b) => RANK[a.tone] - RANK[b.tone] || String(b.at).localeCompare(String(a.at)));
}

// The list's rows: shown ones, then (on request) the hidden ones to bring back.
export function artifactItems(tasks, focusTask) {
  // Taken off the list, with how each stood then: one that failed since comes back.
  const hiddenTone = new Map(hiddenList().map((item) => [hiddenKey(item), item.tone || ""]));
  const isHidden = (item) => hiddenTone.has(item.key) && !(item.tone === "bad" && hiddenTone.get(item.key) !== "bad");
  const all = collectArtifacts(tasks);
  const hidden = new Set(all.filter(isHidden).map((item) => item.key));
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
async function setHidden(key, hidden, tone = "") {
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
    el("a", { class: "a-name", href: item.url, target: "_blank", rel: "noreferrer", text: shorten(item.name, 90), title: `${item.name}\n${item.url}` }),
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
