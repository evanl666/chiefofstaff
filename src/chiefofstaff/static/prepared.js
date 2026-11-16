// Prepared for you (shepherd.py): the owner's PRs kept moving. Three parts, each drawn only while
// there is something to show: Home's section of what was found on their other PRs (at most three
// rows, the rest under All found, each with Hand to a worker); on a worker PR's task row, its line
// and its one action (Fix it, Review fix or Review & post, Resume, Look); and the plan's review,
// where the owner sees exactly what would be published and approves it.

import { api, el, toast } from "./dom.js";
import { parseDiff } from "./diff.js";
import { markdown, shorten, since } from "./text.js";
import { loadState, state } from "./state.js";
import { openSettings } from "./settings.js";

const shepherd = () => state.snapshot?.shepherd || null;

// Home: what was found on the owner's other PRs, between Needs you and Running. Null (and no
// section) when there is nothing to show.
let showingAll = false;
export function preparedView() {
  const found = shepherd()?.prepared;
  if (!shepherd()?.on || !found || !found.rows?.length) return null;
  return found;
}
export function preparedItems() {
  const found = preparedView();
  if (!found) return [];
  if (showingAll) {
    return [
      ...found.all.map((item) => ({ key: `found:${item.target}:${item.why || item.what}`, build: () => foundRow(item) })),
      { key: `found-foot:${found.read}`, build: () => allFoot(found) },
    ];
  }
  const rows = found.rows.map((row) => ({ key: `prepared:${row.target}:${row.what}`, build: () => preparedRow(row) }));
  const others = found.all.length - found.rows.length;
  if (others > 0) rows.push({ key: `prepared-more:${others}`, build: () => moreRow(found.more, others) });
  return rows;
}
function prLink(item) {
  return el("a", { class: "pr", href: item.url, target: "_blank", rel: "noreferrer", text: item.label, title: `${item.repo}#${item.number} on GitHub` });
}
// A PR found: its title, then what it needs (or why it is not one to hand over) and its link;
// Hand to a worker at its end when it is one.
function foundCard(item, rest = "") {
  const line = el("div", { class: "prepared-line" }, el("span", { class: item.why ? "why" : "what", text: item.why || item.what }), el("span", { class: "sep", text: "·", "aria-hidden": "true" }), prLink(item));
  const text = el("div", { class: "prepared-text" }, el("div", { class: "prepared-title", text: item.title, title: item.title }), line);
  if (item.why) return el("div", { class: `card prepared-row ${rest}`.trim(), "data-target": item.target }, text);
  const hand = el("button", { type: "button", class: "text", text: "Hand to a worker", "data-tip": "A worker checks its branch out and keeps it moving: fixes and replies prepared for your approval", onclick: () => handOver(item, hand) });
  return el("div", { class: `card prepared-row ${rest}`.trim(), "data-target": item.target }, text, hand);
}
const preparedRow = (row) => foundCard(row);
const foundRow = (item) => foundCard(item, "found");
function moreRow(more, others) {
  const text = more ? `${more} more · All found…` : "All found…";
  return el("div", { class: "prepared-foot" }, el("button", { type: "button", class: "link quiet", text, "data-tip": `Everything found on your open PRs (${others + (preparedView()?.rows.length || 0)})`, onclick: () => showAll(true) }));
}
function allFoot(found) {
  const looked = found.read ? `Looked ${since(new Date(found.read * 1000).toISOString())}` : "";
  const again = el("button", { type: "button", class: "link quiet", text: "Look again", onclick: () => lookAgain(again) });
  return el("div", { class: "prepared-foot" }, el("span", { class: "muted", text: looked }), again, el("button", { type: "button", class: "link quiet", text: "Settings", onclick: () => openSettings("proactive") }), el("button", { type: "button", class: "link", text: "Done", onclick: () => showAll(false) }));
}
function showAll(on) {
  showingAll = on;
  import("./inbox.js").then(({ renderHome }) => renderHome());
}
async function lookAgain(button) {
  button.disabled = true;
  try {
    state.snapshot.shepherd = await api("/api/shepherd/scan", { method: "POST" });
    import("./inbox.js").then(({ renderHome }) => renderHome());
  } catch (error) {
    toast(error.message, "attention");
  } finally {
    button.disabled = false;
  }
}
async function handOver(row, button) {
  button.disabled = true;
  button.textContent = "Starting…";
  try {
    await shepherdAction(row.target, "hand");
    toast(`Handed ${row.label} to a worker.`);
    await loadState();
  } catch (error) {
    button.disabled = false;
    button.textContent = "Hand to a worker";
    toast(error.message, "attention");
  }
}
export function shepherdAction(target, action, extra = {}) {
  return api("/api/shepherd/pr", { method: "POST", body: { target, action, ...extra } });
}

// A worker PR's row: its line (where the shepherd stands), its one action, its menu.
export function keptLine(task) {
  const kept = task.shepherd;
  if (!kept) return null;
  const words = [kept.line, kept.accept_note].filter(Boolean);
  if (!words.length) return null;
  return el("div", { class: `kept-line${kept.refused ? " refused" : ""}`, text: words.join(" · ") });
}
// The row's one action, or null (refused: today's Ask it to fix… stays the action).
export function keptButton(task, act) {
  const kept = task.shepherd;
  const action = kept?.action;
  if (!action) return null;
  const run = {
    fix: () => act(() => shepherdAction(kept.target, "fix"), "Preparing a fix."),
    resume: () => act(() => shepherdAction(kept.target, "resume"), "It keeps moving again."),
    review: () => openPlan(action.step, task),
    look: () => openJournal(task),
  }[action.kind];
  const tips = {
    fix: "Prepare a fix in its worker, checked, for your approval (charged to today's credits)",
    resume: "Keep it moving from its PR as it is now (merging still needs your Accept of its head)",
    review: "See exactly what would be published, and approve it",
    look: "What was tried, and why it stopped",
  };
  return el("button", { type: "button", text: action.label, "data-tip": tips[action.kind], onclick: run });
}
export function keptMenu(task, act) {
  const kept = task.shepherd;
  if (!kept || !["review", "done"].includes(task.status)) return [];
  const items = [];
  if (kept.keep) items.push({ text: "Stop keeping it moving", onclick: () => act(() => shepherdAction(kept.target, "keep", { on: false }), "It's yours again: its news comes to its worker as before.") });
  else items.push({ text: "Keep it moving", onclick: () => act(() => shepherdAction(kept.target, "keep", { on: true }), "Kept moving.") });
  if (kept.merge_when_ready) items.push({ text: "Don't merge it by itself", onclick: () => act(() => shepherdAction(kept.target, "merge-when-ready", { on: false }), "It won't merge by itself.") });
  else if (kept.keep) items.push({ text: "Merge when ready", onclick: () => act(() => shepherdAction(kept.target, "merge-when-ready", { on: true }), "It merges once approved and green, at the head you accepted.") });
  if (kept.head && kept.head !== kept.accepted) items.push({ text: "Accept this head…", onclick: () => acceptHead(task) });
  if (kept.journal?.length) items.push({ text: "What it did", onclick: () => openJournal(task) });
  return items;
}

// A plan's review: exactly what would be published, approval-class changes first.
const PRIMARY = { fix: "Push", replies: "Post replies", rerun: "Run again" };
export async function openPlan(stepId, task) {
  const body = el("div", { class: "diff-body plan-body" }, el("p", { class: "muted", text: "Loading what it prepared…" }));
  const error = el("span", { class: "error" });
  const go = el("button", { type: "button", text: "Approve", disabled: true });
  const changes = el("button", { type: "button", class: "text", text: "Request changes…", disabled: true });
  const dismiss = el("button", { type: "button", class: "text", text: "Dismiss", disabled: true });
  const title = el("h2", { text: "Prepared for you" });
  const dialog = el(
    "dialog",
    { class: "diff-dialog plan-dialog", "aria-label": "What it prepared" },
    el("header", {}, title, el("span", { class: "muted", text: task.title }), el("span", { class: "spacer" }), el("button", { type: "button", class: "text", text: "Close", onclick: () => dialog.close() })),
    body,
    el("footer", {}, error, el("span", { class: "spacer" }), dismiss, changes, go),
  );
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  let view;
  try {
    view = await api(`/api/shepherd/steps/${stepId}`);
  } catch (failure) {
    body.replaceChildren(el("p", { class: "error", text: failure.message }));
    return;
  }
  const plan = view.plan || {};
  const label = task.shepherd?.label || "its PR";
  title.textContent = { fix: `A fix for ${label}`, replies: `Replies on ${label}`, rerun: `Run CI again on ${label}` }[plan.kind] || `Prepared for ${label}`;
  const threads = (plan.threads || []).map(threadView);
  const disclosure = view.disclosure === "" && threads.some((thread) => thread.posts()) ? disclosureView(view) : null;
  body.replaceChildren(...[summaryView(plan), evidenceView(plan), findingsView(plan), ...threads.map((thread) => thread.node), disclosure?.node, diffView(plan)].filter(Boolean));
  const posting = () => threads.some((thread) => thread.posts());
  const relabel = () => {
    go.textContent = plan.kind === "fix" ? (posting() ? "Push & post" : "Push") : PRIMARY[plan.kind] || "Approve";
  };
  relabel();
  for (const thread of threads) thread.onchange = relabel;
  for (const button of [go, changes, dismiss]) button.disabled = view.state !== "prepared";
  if (plan.kind === "rerun") changes.hidden = true;
  go.addEventListener("click", async () => {
    error.textContent = "";
    const choices = { replies: {}, resolve: [], choice: {}, disclosure: disclosure?.value() || "" };
    for (const thread of threads) Object.assign(choices.replies, thread.reply()), Object.assign(choices.choice, thread.choice()), choices.resolve.push(...thread.resolve());
    if (disclosure && !choices.disclosure && posting()) {
      error.textContent = "Choose whether replies say they were drafted with an AI assistant.";
      disclosure.node.querySelector("input")?.focus();
      return;
    }
    go.disabled = true;
    try {
      await api(`/api/shepherd/steps/${stepId}`, { method: "POST", body: { action: "approve", ...choices } });
      dialog.close();
      toast(Object.values(choices.choice).includes("their-way") ? "Asked its worker to do it their way." : plan.kind === "rerun" ? "Running CI again." : "Publishing what you approved.");
      await loadState();
    } catch (failure) {
      error.textContent = failure.message;
      go.disabled = false;
    }
  });
  dismiss.addEventListener("click", async () => {
    try {
      await api(`/api/shepherd/steps/${stepId}`, { method: "POST", body: { action: "dismiss" } });
      dialog.close();
      await loadState();
    } catch (failure) {
      error.textContent = failure.message;
    }
  });
  changes.addEventListener("click", () => {
    const input = el("textarea", { rows: 2, placeholder: "What should change?", "aria-label": "What should change" });
    const send = el("button", { type: "button", text: "Send", onclick: async () => {
      if (!input.value.trim()) return input.focus();
      send.disabled = true;
      try {
        await api(`/api/shepherd/steps/${stepId}`, { method: "POST", body: { action: "changes", text: input.value.trim() } });
        dialog.close();
        toast("Sent to its worker: a new plan comes.");
        await loadState();
      } catch (failure) {
        error.textContent = failure.message;
        send.disabled = false;
      }
    } });
    const footer = dialog.querySelector("footer");
    footer.replaceChildren(error, input, el("button", { type: "button", class: "text", text: "Cancel", onclick: () => footer.replaceChildren(error, el("span", { class: "spacer" }), dismiss, changes, go) }), send);
    input.focus();
  });
}
function summaryView(plan) {
  if (!plan.summary && !plan.why) return null;
  return el("div", { class: "plan-summary markdown", html: markdown(plan.summary || plan.why) });
}
function evidenceView(plan) {
  if (plan.kind === "rerun") return el("p", { class: "plan-evidence", text: `Its failed jobs run again on ${shortSha(plan.h0 || "")}: nothing changes in the code.`.replace(" on : ", ": ") });
  const evidence = plan.evidence;
  if (!plan.h1) return null;
  const checked = evidence?.holds && !evidence.void;
  const words = evidence?.words || "not checked on its own commit";
  return el(
    "p",
    { class: `plan-evidence${checked ? " good" : ""}` },
    el("span", { class: "mark", text: checked ? "✓" : "~", "aria-hidden": "true" }),
    ` ${checked ? "Checked" : "Not fully checked"} on ${shortSha(plan.h0)} and ${shortSha(plan.h1)}: `,
    el("span", { html: markdown(words).replace(/^<p>|<\/p>$/g, "") }),
    plan.stat ? el("span", { class: "muted", text: ` · ${plan.stat}` }) : "",
    plan.fresh ? el("span", { class: "muted", text: " · made in a fresh copy of the branch (its worker's checkout had other changes)" }) : "",
  );
}
const shortSha = (sha) => (sha || "").slice(0, 7);
function findingsView(plan) {
  const flagged = (plan.findings || []).filter((item) => item.change === "approval");
  if (!flagged.length) return null;
  return el(
    "div",
    { class: "plan-flagged", role: "note" },
    el("strong", { text: "It changes tests or checks: look at these first." }),
    el("ul", {}, ...flagged.map((item) => el("li", {}, el("code", { text: item.path }), ` ${item.why}${item.hints?.length ? ` (${item.hints.join(", ")})` : ""}`))),
  );
}
const STANCES = { fix: "Fixed", answer: "Answer", "push-back": "Pushes back", leave: "Left to you" };
function threadView(thread) {
  const reply = el("textarea", { rows: 3, "aria-label": `Reply to ${thread.author}`, text: thread.reply || "" });
  reply.value = thread.reply || "";
  const resolve = el("input", { type: "checkbox" });
  const choice = el("select", { "aria-label": "What to do about this push-back" }, el("option", { value: "post", text: "Post my reply" }), el("option", { value: "their-way", text: "Do it their way" }), el("option", { value: "leave", text: "Leave it to me" }));
  const view = { node: null, onchange: () => {} };
  const quote = el("blockquote", { class: "plan-quote" }, el("strong", { text: thread.author }), thread.path ? el("span", { class: "muted", text: ` on ${thread.path}${thread.line ? `:${thread.line}` : ""}` }) : "", el("div", { class: "markdown", html: markdown(shorten(thread.said || "", 1200)) }));
  const parts = [quote, el("div", { class: "plan-stance", text: STANCES[thread.stance] || thread.stance })];
  if (thread.stance === "leave") parts.push(el("p", { class: "muted", text: thread.why || "Yours to answer." }));
  else {
    parts.push(reply);
    if (thread.stance === "push-back") parts.push(el("label", { class: "plan-choice" }, choice));
    if (thread.stance === "fix") parts.push(el("label", { class: "check" }, resolve, " Resolve the conversation once the fix is on the PR"));
    if (!thread.writer) parts.push(el("p", { class: "muted", text: `${thread.author} has no write access here: included because you can see it.` }));
  }
  view.node = el("div", { class: "plan-thread" }, ...parts);
  view.posts = () => thread.stance !== "leave" && choice.value === "post" && reply.value.trim() !== "";
  view.reply = () => (thread.stance !== "leave" ? { [thread.comment]: reply.value } : {});
  view.choice = () => (thread.stance === "push-back" ? { [thread.comment]: choice.value } : {});
  view.resolve = () => (thread.stance === "fix" && resolve.checked ? [String(thread.comment)] : []);
  for (const input of [reply, choice]) input.addEventListener("input", () => {
    reply.disabled = choice.value !== "post";
    view.onchange();
  });
  return view;
}
function disclosureView(view) {
  const repo = view.target.replace(/^github:/, "").replace(/#\d+$/, "");
  const yes = el("input", { type: "radio", name: "disclosure", value: "yes" });
  const no = el("input", { type: "radio", name: "disclosure", value: "no" });
  const node = el("fieldset", { class: "plan-disclosure" }, el("legend", { text: `In ${repo}, add a line saying replies are drafted with an AI assistant?` }), el("label", { class: "check" }, yes, " Yes"), el("label", { class: "check" }, no, " No"), el("small", { text: "Asked once per repository; change it in Settings." }));
  return { node, value: () => (yes.checked ? "yes" : no.checked ? "no" : "") };
}
function diffView(plan) {
  if (!plan.diff) return null;
  const files = parseDiff(plan.diff);
  const flagged = new Set((plan.findings || []).filter((item) => item.change === "approval").map((item) => item.path));
  return el(
    "div",
    { class: "plan-diff" },
    el("h3", { text: "The change" }),
    ...files.map((file) => {
      const lines = el("div", { class: "diff-lines" }, ...file.lines.slice(0, 1500).map((line) => el("div", { class: `diff-line ${line.kind} still` }, el("span", { class: "num", text: line.kind === "hunk" ? "" : String(line.kind === "del" ? line.old : line.line) }), el("span", { class: "sign", text: { add: "+", del: "−" }[line.kind] || "" }), el("span", { class: "code", text: line.text }))));
      return el("details", { class: `diff-file${flagged.has(file.name) ? " flagged" : ""}`, open: file.lines.length <= 80 || flagged.has(file.name) }, el("summary", {}, el("span", { class: "name", text: file.name }), el("span", { class: "added", text: file.added ? `+${file.added}` : "" }), el("span", { class: "removed", text: file.removed ? `−${file.removed}` : "" }), file.note ? el("span", { class: "muted", text: file.note }) : null), lines);
    }),
    plan.diff_cut ? el("p", { class: "muted", text: "The rest of the diff is too large to show here." }) : "",
  );
}

// What was tried on a PR, attempt by attempt (Look, What it did).
const STATES = { published: "Published", partly: "Partly published", prepared: "Waiting for you", applying: "Publishing", running: "Running", queued: "Waiting", diagnosis: "Diagnosis", dismissed: "Dismissed", withdrawn: "Withdrawn", stale: "Out of date", interrupted: "Stopped", failed: "Failed", refused: "Not run", nothing: "Nothing came of it" };
const KINDS = { "fix-ci": "CI", "address-review": "Review", merge: "Merge" };
// Each operation, done or not, in words.
const OPS = { push: ["Pushed the fix", "The push"], reply: ["Posted a reply", "A reply"], resolve: ["Resolved a conversation", "Resolving a conversation"], "request-review": ["Asked for a review again", "Asking for a review again"], rerun: ["Ran CI again", "Running CI again"], merge: ["Merged", "The merge"] };
export function operationWords(op) {
  const [done, doing] = OPS[op.kind] || [op.kind, op.kind];
  if (op.state === "sent") return done;
  if (op.state === "uncertain") return `${doing}: not known whether it went out`;
  if (["planned", "dispatching"].includes(op.state)) return `${doing}: on its way`;
  return `${doing}: not done`;
}
export function openJournal(task) {
  const kept = task.shepherd;
  const rows = (kept?.journal || []).slice().reverse().map((entry) => el(
    "li",
    {},
    el("div", { class: "journal-head" }, el("strong", { text: `${KINDS[entry.kind] || entry.kind} · ${STATES[entry.state] || entry.state}` }), el("span", { class: "muted", text: since(entry.at) })),
    entry.summary ? el("div", { class: "muted", text: entry.summary }) : "",
    entry.evidence ? el("div", { html: markdown(entry.evidence) }) : "",
    entry.why && !entry.summary ? el("div", { class: "muted", text: entry.why }) : "",
    entry.operations.length ? el("ul", { class: "journal-ops" }, ...entry.operations.map((op) => el("li", {}, operationWords(op), op.link ? el("a", { href: op.link, target: "_blank", rel: "noreferrer", text: " ↗" }) : "", op.outcome ? el("span", { class: "muted", text: ` ${op.outcome}` }) : ""))) : "",
  ));
  const dialog = el(
    "dialog",
    { class: "diff-dialog journal-dialog", "aria-label": "What it did" },
    el("header", {}, el("h2", { text: `What it did on ${kept?.label || "its PR"}` }), el("span", { class: "spacer" }), el("button", { type: "button", class: "text", text: "Close", onclick: () => dialog.close() })),
    el("div", { class: "diff-body" }, kept?.line ? el("p", { text: kept.line }) : "", rows.length ? el("ol", { class: "journal" }, ...rows) : el("p", { class: "muted", text: "Nothing tried yet." })),
  );
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}
// Accept this head: its diff first (on GitHub), then the click.
function acceptHead(task) {
  const kept = task.shepherd;
  const compare = kept.accepted ? `${kept.url}/files/${kept.accepted}..${kept.head}` : `${kept.url}/files`;
  const yes = el("button", { type: "button", text: `Accept ${shortSha(kept.head)}` });
  const dialog = el(
    "dialog",
    { class: "confirm-dialog", "aria-label": "Accept this head" },
    el("h2", { text: `Accept ${kept.label} at ${shortSha(kept.head)}?` }),
    el("p", {}, "It merges by itself only at a head you accepted. ", el("a", { href: compare, target: "_blank", rel: "noreferrer", text: "Look at what changed" }), " first."),
    el("div", { class: "buttons" }, el("button", { type: "button", class: "text", text: "Cancel", onclick: () => dialog.close() }), yes),
  );
  yes.addEventListener("click", async () => {
    try {
      await shepherdAction(kept.target, "accept-head", { sha: kept.head });
      dialog.close();
      toast(`Accepted ${shortSha(kept.head)}.`);
      await loadState();
    } catch (failure) {
      toast(failure.message, "attention");
    }
  });
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}
