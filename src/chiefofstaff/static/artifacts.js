// Home → Artifacts: what the owner's tasks delivered (their PRs, the documents they wrote, the
// flows they ran), in one list, each with where it stands now: a PR as GitHub has it; a doc or
// a flow as its worker last said, else as its task stands. The owner takes one off the list
// (its task keeps it) and can bring it back. Not what a task only mentions, nor its files,
// mockups, logs or single CI runs: those are on its card.

import { api, el, toast } from "./dom.js";
import { ago, linkKey, linkName, prTitle, taskLinks } from "./text.js";
import { state } from "./state.js";
import { prStatus } from "./board.js";

// What a PR's state comes to: one rule with where its task is placed (board.js).
export { prStatus };
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
// As its task stands, for a doc or a flow its worker said nothing about.
const BY_TASK = {
  running: ["in progress", "active"],
  queued: ["queued", "active"],
  waiting: ["waiting on you", "wait"],
  review: ["to accept", "wait"],
  done: ["delivered", "good"],
  failed: ["task failed", "bad"],
  cancelled: ["task cancelled", "muted"],
  "set-aside": ["set aside", "muted"],
};
const byTask = (task, phase) => BY_TASK[phase?.marker === "set-aside" || task.set_aside ? "set-aside" : task.status] || ["", "neutral"];
const RANK = { bad: 0, wait: 1, active: 2, neutral: 3, good: 4, muted: 5 };
// A PR's part in a task, in a word: it opened it, pushed to it, or reviewed it. A PR it only
// mentions ("referenced") is not something it delivered.
export const PR_ROLES = { created: "Created", modified: "Modified", reviewed: "Reviewed" };
const KIND_WORDS = { pr: "PR", doc: "Doc", flow: "Flow" };
// A PR's word (prStatus's), when the server gives it without the PR as GitHub has it.
const PR_TONES = { merged: "good", closed: "muted", draft: "active", "changes requested": "bad", "merge conflicts": "bad", "CI failing": "bad", "awaiting approval": "wait", "CI running": "active", "ready to merge": "good", approved: "good", "in review": "active" };
const prLabelOf = (url) => {
  const found = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/.exec(url || "");
  return found ? `${found[1]}#${found[2]}` : "";
};
// A link's name in the list: what its worker or a report called it, else what it says beside
// its kind ("abc123" of "Flyte · abc123"), else what it is ("Google Doc"); never a piece of a
// document's id.
function nameOf(link) {
  if (link.name) return link.name;
  const found = link.found;
  if (found.kind === "doc" || !found.label.includes(" · ")) return linkName(link);
  return found.label.split(" · ").slice(1).join(" · ");
}
function prItem(task, pr, role, url = pr?.url, title = "") {
  const label = pr?.label || prLabelOf(url);
  const [status, tone] = pr ? prStatus(pr) : ["", "neutral"];
  const named = title || pr?.title || prTitle(label);
  const short = label.replace(/^[^/]+\//, "");
  // The server's title names the PR already ("app#7 · Fix it").
  const name = !named ? short : named.toLowerCase().startsWith(short.toLowerCase()) ? named : `${short} · ${named}`;
  return { key: `pr:${label.toLowerCase()}`, kind: "PR", role, name, url, status, tone, at: pr?.changed_at || pr?.at || task.updated_at, said: Boolean(pr), pr: pr || null, task };
}

// What one task delivered, its PRs first: { key, kind ("PR", "Doc", "Flow"), role (a PR's:
// created, modified, reviewed), name, url, status, tone, at, said, pr (its receipt), task }.
// The server's `deliverables`; from an older server, the same out of its receipts (its own PRs)
// and the documents and flows it made. `phase`: where it is on Home.
export function deliverablesOf(task, phase) {
  const prs = task.receipts?.prs || [];
  const receipt = (url) => prs.find((pr) => pr.url === url || pr.label.toLowerCase() === prLabelOf(url).toLowerCase());
  const [taskStatus, taskTone] = byTask(task, phase);
  if (Array.isArray(task.deliverables)) {
    return task.deliverables
      .filter((item) => KIND_WORDS[item.kind] && item.url && (item.kind !== "pr" || PR_ROLES[item.role || "created"]))
      .map((item) => {
        if (item.kind === "pr") {
          const found = prItem(task, receipt(item.url), item.role || "created", item.url, item.title);
          return found.pr || !item.status ? found : { ...found, status: item.status, tone: PR_TONES[item.status] || toneOf(item.status), said: true };
        }
        const status = item.status || taskStatus;
        // When its worker last said where it stands (what needs a look first is the newest).
        const declared = (task.artifacts || []).find((said) => linkKey(said.url) === linkKey(item.url));
        return { key: linkKey(item.url), kind: KIND_WORDS[item.kind], role: "", name: item.title || item.url, url: item.url, status, tone: item.status ? toneOf(item.status) : taskTone, at: item.at || declared?.at || task.updated_at, said: Boolean(item.status), by: item.said_by || "", pr: null, task };
      });
  }
  const found = [];
  for (const pr of prs) {
    const role = pr.role || (pr.own ? "created" : "");
    if (PR_ROLES[role]) found.push(prItem(task, pr, role));
  }
  for (const link of taskLinks(task)) {
    // A document it wrote, a flow it ran; not a file of its folder, a log or a CI run.
    const kind = link.found.kind === "doc" && link.role === "output" ? "Doc" : link.found.kind === "run" && link.role === "run" ? "Flow" : "";
    if (!kind) continue;
    const status = link.said?.status || (link.said ? "linked" : taskStatus);
    found.push({ key: link.key, kind, role: "", name: nameOf(link), url: link.url, status, tone: link.said ? toneOf(link.said.status) : taskTone, at: link.said?.at || task.updated_at, said: Boolean(link.said), pr: null, task });
  }
  return found;
}
// Every task's deliverables, once each (a PR two tasks share: the one that made it, else the
// newer word; a document linked in two ways: one row), the ones that need a look first.
// `phases`: where each task is on Home.
const ROLE_RANK = { created: 0, modified: 1, reviewed: 2, "": 3 };
export function collectArtifacts(tasks, phases = new Map()) {
  const found = new Map();
  for (const task of tasks) {
    for (const item of deliverablesOf(task, phases.get(task.id))) {
      const known = found.get(item.key);
      if (known && (ROLE_RANK[known.role] < ROLE_RANK[item.role] || (known.role === item.role && (known.said && !item.said)))) continue;
      if (known && known.role === item.role && known.said === item.said && String(known.at) >= String(item.at)) continue;
      found.set(item.key, item);
    }
  }
  return [...found.values()].sort((a, b) => RANK[a.tone] - RANK[b.tone] || String(b.at).localeCompare(String(a.at)));
}

// A task's runs from this many on are folded: one line says how many there are and how they
// stand ("9 runs · 3 running · 1 failed"); under it only the latest and the failed ones, the
// rest behind "+N" (bug E43: one task's nine runs pushed every PR down the list).
const RUNS_FOLDED = 3;
// Tasks whose runs the owner unfolded (this page's).
const openRuns = new Set();
// What runs come to in a few words, and the tone of the one that needs a look first.
export function runsWords(runs) {
  const running = runs.filter((item) => item.tone === "active").length;
  const failed = runs.filter((item) => item.tone === "bad").length;
  const words = [running ? `${running} running` : "", failed ? `${failed} failed` : ""].filter(Boolean).join(" · ");
  const tone = failed ? "bad" : runs.some((item) => item.tone === "wait") ? "wait" : running ? "active" : runs.every((item) => item.tone === "good") ? "good" : "neutral";
  return { words, tone };
}
// The list by task, the task with what needs a look first first: its PRs and docs, then its
// runs, the newest first (from RUNS_FOLDED on: `folded`, as said above; `open`: the tasks whose
// runs are all shown).
export function groupArtifacts(items, open = new Set()) {
  const groups = new Map();
  for (const item of items) {
    const group = groups.get(item.task.id) || { task: item.task, mains: [], runs: [] };
    (item.kind === "Flow" ? group.runs : group.mains).push(item);
    groups.set(item.task.id, group);
  }
  return [...groups.values()].map((group) => {
    const runs = [...group.runs].sort((a, b) => String(b.at).localeCompare(String(a.at)));
    if (runs.length < RUNS_FOLDED) return { ...group, runs, folded: null };
    const unfolded = open.has(group.task.id);
    const shown = unfolded ? runs : runs.filter((item, index) => !index || item.tone === "bad");
    return { ...group, runs: shown, folded: { count: runs.length, hidden: runs.length - shown.length, open: unfolded, at: runs[0].at, ...runsWords(runs) } };
  });
}

// The list's rows: shown ones, a task's together, then (on request) the hidden ones to bring
// back.
export function artifactItems(tasks, focusTask, phases) {
  const all = collectArtifacts(tasks, phases);
  const hidden = new Set(all.filter((item) => isHidden(item.key, item.tone)).map((item) => item.key));
  const shown = all.filter((item) => !hidden.has(item.key));
  const off = all.filter((item) => hidden.has(item.key));
  const rows = [];
  for (const group of groupArtifacts(shown, openRuns)) {
    rows.push(...group.mains.map((item) => artifactRow(item, false, focusTask)));
    if (group.folded) rows.push(runsRow(group, focusTask));
    rows.push(...group.runs.map((item) => artifactRow(item, false, focusTask, Boolean(group.folded))));
  }
  if (state.showHiddenArtifacts) rows.push(...off.map((item) => artifactRow(item, true, focusTask)));
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
// What taking one off said: gone once it is back (said so instead), not left on screen.
let offNote = null;
export async function setHidden(key, hidden, tone = "") {
  const before = state.snapshot.artifacts_hidden || [];
  const same = hiddenList().filter((item) => hiddenKey(item) === key).map((item) => item.key);
  const others = hiddenList().filter((item) => hiddenKey(item) !== key);
  state.snapshot.artifacts_hidden = hidden ? [...others, { key, tone }] : others;
  import("./inbox.js").then((module) => module.renderHome());
  if (!hidden) offNote?.remove();
  try {
    const also = same.filter((old) => old !== key);
    await api("/api/artifacts/hide", { method: "POST", body: { key, hidden, tone, ...(also.length ? { also } : {}) } });
    offNote?.remove();
    offNote = toast(hidden ? "Taken off Artifacts; its task keeps it." : "Back on Artifacts.");
  } catch (error) {
    state.snapshot.artifacts_hidden = before;
    import("./inbox.js").then((module) => module.renderHome());
    toast(error.message, "error");
  }
}
// `folded`: one of a task's folded runs, under the line that says them all (which names the
// task).
function artifactRow(item, hidden, focusTask, folded = false) {
  const when = item.at ? ago(item.at) : "";
  const help = item.said ? (item.kind === "PR" || item.by === "GitHub" ? "As GitHub has it" : item.by === "you" ? "As you marked it" : "As its worker last said") : "As its task stands: its worker has not said where this stands";
  return {
    key: `art:${item.key}:${hidden ? 1 : 0}`,
    version: `${item.name}:${item.role}:${item.status}:${item.tone}:${when}:${item.task.id}:${item.task.title}:${folded}`,
    build: () => {
      const node = el("div", { class: "artifact", "data-key": item.key });
      fill(node, item, hidden, focusTask, when, help, folded);
      return node;
    },
    refresh: (node) => fill(node, item, hidden, focusTask, when, help, folded),
  };
}
// A task's runs in a line: how many, how they stand, and "+N" for those not shown.
function runsRow(group, focusTask) {
  const { task, folded } = group;
  const when = folded.at ? ago(folded.at) : "";
  return {
    key: `art-runs:${task.id}`,
    version: `${folded.count}:${folded.words}:${folded.tone}:${folded.hidden}:${folded.open}:${when}:${task.title}`,
    build: () => {
      const node = el("div", { class: "artifact runs" });
      fillRuns(node, group, focusTask, when);
      return node;
    },
    refresh: (node) => fillRuns(node, group, focusTask, when),
  };
}
// Its runs unfolded or folded again; a keyboard's focus stays on the line (redrawn).
function toggleRuns(taskId, node) {
  if (openRuns.has(taskId)) openRuns.delete(taskId);
  else openRuns.add(taskId);
  const focused = node.contains(document.activeElement);
  import("./inbox.js").then((module) => {
    module.renderHome();
    if (focused) [...document.querySelectorAll("#tasks-artifacts .artifact.runs")].find((line) => line.dataset.task === taskId)?.querySelector(".a-fold")?.focus();
  });
}
function fillRuns(node, group, focusTask, when) {
  const { task, folded } = group;
  node.className = `artifact runs tone-${folded.tone}`;
  node.dataset.task = task.id;
  const toggle = () => toggleRuns(task.id, node);
  node.replaceChildren(
    el("span", { class: "a-kind", text: "Flow" }),
    el("button", { type: "button", class: "a-name a-fold", text: `${folded.count} runs`, "aria-expanded": String(folded.open), "data-tip": folded.open ? "Show only the latest run and the failed ones" : `Show all ${folded.count} runs`, onclick: toggle }),
    folded.words ? el("span", { class: "a-status", text: folded.words, "data-tip": "As their worker last said, or GitHub" }) : el("span"),
    folded.hidden ? el("button", { type: "button", class: "a-more link", text: `+${folded.hidden}`, "aria-label": `Show ${folded.hidden} more runs of ${task.title}`, "data-tip": `${folded.hidden} more, not failed`, onclick: toggle }) : el("span"),
    el(
      "div",
      { class: "a-meta" },
      el("button", { type: "button", class: "a-task link", text: task.title, "data-tip": "Show its task", onclick: () => focusTask(task.id, task.session_id) }),
      when ? el("span", { class: "a-when", text: `· latest ${when === "now" ? "just now" : `${when} ago`}` }) : "",
    ),
  );
}
// Its tooltips are Home's own (data-tip): one that cannot be left pointing at another row.
function fill(node, item, hidden, focusTask, when, help, folded = false) {
  node.className = `artifact tone-${item.tone}${hidden ? " hidden-artifact" : ""}${folded ? " in-runs" : ""}`;
  const role = PR_ROLES[item.role] || "";
  node.replaceChildren(
    el("span", { class: "a-kind", text: item.kind }),
    el("a", { class: "a-name", href: item.url, target: "_blank", rel: "noreferrer", text: item.name, "data-tip": `${item.name}\n${item.url}` }),
    el("span", { class: `a-status${item.said ? "" : " derived"}`, text: item.status, "data-tip": `${help}${item.at ? ` · ${new Date(item.at).toLocaleString()}` : ""}` }),
    hidden
      ? el("button", { type: "button", class: "a-hide link", text: "Bring back", "data-tip": "Back on this list", onclick: () => setHidden(item.key, false) })
      : el("button", { type: "button", class: "a-hide", text: "✕", "aria-label": `Take ${item.name} off this list`, "data-tip": "Take it off this list (its task keeps it; it comes back if it fails)", onclick: () => setHidden(item.key, true, item.tone) }),
    el(
      "div",
      { class: "a-meta" },
      role ? el("span", { class: `a-role role-${item.role}`, text: role, "data-tip": { created: "Its task opened this PR", modified: "Its task pushed to this PR", reviewed: "Its task reviewed this PR" }[item.role] }) : "",
      role ? el("span", { text: "·" }) : "",
      folded ? "" : el("button", { type: "button", class: "a-task link", text: item.task.title, "data-tip": "Show its task", onclick: () => focusTask(item.task.id, item.task.session_id) }),
      when ? el("span", { class: "a-when", text: `${folded ? "" : "· "}${when === "now" ? "just now" : `${when} ago`}` }) : "",
    ),
  );
}
