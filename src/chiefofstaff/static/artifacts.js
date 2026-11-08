// Home → Tasks → Artifacts: what the owner's tasks made or run, in one list, each with where it
// stands now: a PR as GitHub has it; a doc or a run as its worker last said, else as its task
// stands. The owner takes one off the list (its task keeps it) and can bring it back.

import { api, el, toast } from "./dom.js";
import { ago, findUrls, linkKind, prTitle, shorten } from "./text.js";
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
// A status a worker gave, by its words.
export function toneOf(status) {
  const words = String(status || "").toLowerCase();
  if (/fail|error|broken|blocked|stuck|killed|timed? ?out/.test(words)) return "bad";
  if (/succe|done|final|live|complete|merged|published|passed|ready|updated|shipped|deployed/.test(words)) return "good";
  if (/wait|review|approval/.test(words)) return "wait";
  if (/run|progress|draft|queued|pending|start|writing|building/.test(words)) return "active";
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
function kindOf(found) {
  if (!found) return "Link";
  if (found.kind === "pr") return "PR";
  const first = found.label.split(" · ")[0];
  return { "Google Doc": "Doc", "CI run": "CI", "Spark history": "Spark", "Airflow run": "Airflow", "Flyte run": "Flyte" }[first] || (found.kind === "link" ? "Link" : first);
}
const nameOf = (found, url) => (found?.label.includes(" · ") ? found.label.split(" · ").slice(1).join(" · ") : found?.label || url);

// Every artifact of these tasks, once each (a PR two tasks share: the one that made it), the
// ones that need a look first: { key, kind, name, url, status, tone, at, said, task }.
export function collectArtifacts(tasks) {
  const found = new Map();
  const add = (item, own = false) => {
    const known = found.get(item.key);
    if (known && (known.own || (!own && known.at >= item.at))) return;
    found.set(item.key, { ...item, own });
  };
  for (const task of tasks) {
    for (const pr of task.receipts?.prs || []) {
      const [status, tone] = prStatus(pr);
      const title = pr.title || prTitle(pr.label);
      const short = pr.label.replace(/^[^/]+\//, "");
      add({ key: `pr:${pr.label.toLowerCase()}`, kind: "PR", name: title ? `${short} · ${title}` : short, url: pr.url, status, tone, at: pr.at || task.updated_at, said: true, task }, Boolean(pr.own));
    }
    const said = new Set();
    for (const item of task.artifacts || []) {
      said.add(item.url);
      const kind = kindOf(linkKind(item.url));
      add({ key: item.url, kind, name: item.name || nameOf(linkKind(item.url), item.url), url: item.url, status: item.status || "linked", tone: toneOf(item.status), at: item.at, said: true, task }, true);
    }
    // What it made that its worker named only in its reports: as its task stands.
    const [status, tone] = BY_TASK[task.status] || ["", "neutral"];
    for (const url of findUrls([task.result, task.progress].filter(Boolean).join("\n"))) {
      const kind = linkKind(url);
      if (said.has(url) || !kind || !["output", "run"].includes(kind.role) || (task.goal || "").includes(url)) continue;
      add({ key: url, kind: kindOf(kind), name: nameOf(kind, url), url, status, tone, at: task.updated_at, said: false, task });
    }
  }
  return [...found.values()].sort((a, b) => RANK[a.tone] - RANK[b.tone] || String(b.at).localeCompare(String(a.at)));
}

// The list's rows: shown ones, then (on request) the hidden ones to bring back.
export function artifactItems(tasks, focusTask) {
  const hidden = new Set(state.snapshot?.artifacts_hidden || []);
  const all = collectArtifacts(tasks);
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
async function setHidden(key, hidden) {
  const before = state.snapshot.artifacts_hidden || [];
  state.snapshot.artifacts_hidden = hidden ? [...before, key] : before.filter((item) => item !== key);
  import("./inbox.js").then((module) => module.renderHome());
  try {
    await api("/api/artifacts/hide", { method: "POST", body: { key, hidden } });
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
      : el("button", { type: "button", class: "a-hide", text: "✕", "aria-label": `Take ${item.name} off this list`, title: "Take it off this list (its task keeps it)", onclick: () => setHidden(item.key, true) }),
    el(
      "div",
      { class: "a-meta" },
      el("button", { type: "button", class: "a-task link", text: item.task.title, title: "Open its task", onclick: () => focusTask(item.task.id, item.task.session_id) }),
      when ? el("span", { class: "a-when", text: ` · ${when === "now" ? "just now" : `${when} ago`}` }) : "",
    ),
  );
}
