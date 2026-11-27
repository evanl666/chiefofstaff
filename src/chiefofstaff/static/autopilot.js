// Autopilot (autopilot.md §2): one switch in Settings, one button and one status line on Home,
// Stop, one card when a run ends, and one record. Nothing of it is drawn while its Settings line
// is unticked (the snapshot has no `autopilot` then), nor the record before the first run.

import { $, api, el, moreMenu, toast } from "./dom.js";
import { clock } from "./text.js";
import { HOME, state } from "./state.js";
import { select, setLayout } from "./session.js";
import { renderSidebar } from "./sidebar.js";
import { closeMentions, setRecipient } from "./mention.js";

export const RECORD = "autopilot-record";
export const isRecordView = () => state.selected === RECORD;
// Its part of the snapshot (only while its line is ticked), and the run on now.
export const autopilot = () => state.snapshot?.autopilot || null;
export const autopilotRun = () => (autopilot()?.run ? autopilot() : null);

const time = (date) => date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
function dayWord(date) {
  const days = Math.round((new Date(date.toDateString()) - new Date(new Date().toDateString())) / 86400000);
  if (days === 0) return "";
  if (days === 1) return "tomorrow ";
  return `${date.toLocaleDateString([], { weekday: "short" })} `;
}
// "until 6:00 PM", "until tomorrow 9:00 AM", "until you turn it off".
export function untilWords(run = autopilotRun()) {
  if (!run) return "";
  if (!run.until) return "until you turn it off";
  const until = new Date(run.until);
  return `until ${dayWord(until)}${time(until)}`;
}
// The menu's morning: the daily summary's time (9:00 AM when it is off), the next one.
function morningLabel() {
  const [hour, minute] = (autopilot()?.morning || "09:00").split(":").map(Number);
  const next = new Date();
  next.setHours(hour, minute, 0, 0);
  if (next <= new Date()) next.setDate(next.getDate() + 1);
  return `Until ${dayWord(next)}${time(next)}`;
}

// For a screen reader, the header line as it changes: at once when it is turned on, paused or
// ends; its counts at most once a minute. (Made the first time Autopilot is on.)
let announced = { text: "", phase: "", at: 0 };
function announce(phase, text) {
  if (!text || text === announced.text) return;
  if (phase === announced.phase && Date.now() - announced.at < 60000) return;
  let node = document.getElementById("autopilot-said");
  if (!node) {
    node = el("div", { id: "autopilot-said", class: "autopilot-said-aloud", role: "status", "aria-live": "polite" });
    document.body.append(node);
  }
  announced = { text, phase, at: Date.now() };
  node.textContent = text;
}

// Home's header: the line (where "Focusing until …" shows) and its action.
export function autopilotMeta(waiting) {
  const run = autopilotRun();
  if (!run) {
    if (announced.phase && announced.phase !== "off") announce("off", "Autopilot is off.");
    return null;
  }
  const paused = run.paused === "credits";
  announce(paused ? "paused" : "on", paused ? `Autopilot is paused: today's ${run.credits} credits are used.` : `Autopilot is on ${untilWords(run)}: ${run.done} done${waiting ? `, ${waiting} waiting for you` : ""}.`);
  const parts = [el("span", { text: `Autopilot · ${untilWords(run)}` })];
  if (run.paused === "credits") {
    parts.push(el("span", { text: ` · paused: today's ${run.credits} credits are used` }));
  } else {
    parts.push(
      el("span", { class: "autopilot-counts" }, " · ", el("button", { type: "button", class: "link quiet", text: `${run.done} done`, title: "This run in the Autopilot record", onclick: () => showRecord({ run: run.run }) })),
    );
    if (waiting) parts.push(el("span", { class: "autopilot-counts", text: ` · ${waiting} waiting for you` }));
  }
  return el("span", { class: "autopilot-meta" }, ...parts);
}
export function autopilotControl(rerender) {
  if (!autopilot() || state.snapshot.runtime.state !== "ready") return null;
  const run = autopilotRun();
  if (run) {
    return el("button", { type: "button", class: "autopilot-stop", text: "Stop", "aria-label": `Stop Autopilot (on ${untilWords(run)})`, title: "Stop Autopilot now: nothing it queued goes out", onclick: (event) => stopAutopilot(event.currentTarget, rerender) });
  }
  const start = (duration) =>
    api("/api/autopilot", { method: "POST", body: { duration } })
      .then((found) => {
        state.snapshot.autopilot = { ...state.snapshot.autopilot, ...found };
        rerender();
        toast(`Autopilot on ${untilWords()}.`);
      })
      .catch((error) => toast(error.message, "error"));
  const menu = moreMenu(
    [
      { text: "For 2 hours", onclick: () => start("2h") },
      { text: morningLabel(), onclick: () => start("morning") },
      { text: "Until I turn it off", onclick: () => start("off") },
    ],
    "Chief of Staff acts for you while it's on, instead of waiting",
    "Autopilot",
  );
  menu.classList.add("autopilot");
  return menu;
}
async function stopAutopilot(button, rerender) {
  button.disabled = true;
  try {
    const found = await api("/api/autopilot/stop", { method: "POST" });
    state.snapshot.autopilot = { ...state.snapshot.autopilot, ...found };
    rerender();
    toast(found.said);
  } catch (error) {
    button.disabled = false;
    toast(error.message, "error");
  }
}
// The sidebar's status line.
export const autopilotStatus = () => (autopilotRun() ? `Autopilot ${untilWords()}` : "");
// Needs you: a card it is deciding is not drawn yet (two minutes at most); what it set aside
// says why, under it.
export const deciding = (draftId) => (autopilot()?.deciding || []).includes(draftId);
export function setAsideLine(...ids) {
  const reasons = autopilot()?.set_aside || {};
  const why = ids.map((id) => reasons[id]).find(Boolean);
  if (!why) return null;
  return el("div", { class: "why-line autopilot-why", text: why.startsWith("Not accepted") ? why : `Set aside: ${why}.` });
}
export const emptyWords = () => (autopilotRun() ? "Autopilot is on. Nothing needs you." : "Nothing needs you.");

// A line in a chat that is Autopilot's (an answer it gave, its on line): marked as its own (the
// mark is its name: "Autopilot answered: …" reads [Autopilot] answered: …), with its entry, or
// Stop while it is on.
export function autopilotNotice(meta, text) {
  const mark = el("span", { class: "autopilot-mark", text: "Autopilot" });
  const said = el("span", { class: "autopilot-said" }, mark, ` ${/^Autopilot [a-z]/.test(text) ? text.slice("Autopilot ".length) : text}`);
  const { entry, run } = meta.autopilot || {};
  if (entry) {
    return [said, el("button", { type: "button", class: "link quiet", text: "Why", title: "Its entry in the Autopilot record", onclick: (event) => { event.stopPropagation(); showRecord({ entry, run }); } })];
  }
  if (run && autopilotRun()?.run === run) {
    return [said, el("button", { type: "button", class: "link quiet", text: "Stop", "aria-label": "Stop Autopilot", onclick: (event) => { event.stopPropagation(); stopAutopilot(event.currentTarget, () => {}); } })];
  }
  return [said];
}

// While you were away: the run's card at the end of the Router's chat.
export function summaryCard(summary, at) {
  // Nothing came up and nothing waits: one quiet line.
  if (summary.quiet && !summary.folded?.length) return el("div", { class: "line notice", title: at ? clock(at) : "" }, el("span", { text: summary.said }));
  const head = el(
    "div",
    { class: "head" },
    el("span", { class: "label", text: "While you were away" }),
    // Its hours say when; the time it came only without them.
    summary.window ? el("span", { class: "autopilot-window", text: summary.window, title: at ? new Date(at).toLocaleString() : "" }) : null,
    el("span", { class: "spacer" }),
    at && !summary.window ? el("time", { text: clock(at), title: new Date(at).toLocaleString() }) : null,
  );
  const node = el("div", { class: "card update autopilot-summary" }, head);
  node.append(el("p", { class: "autopilot-said", text: summary.said }));
  if (summary.settings) node.append(el("p", { class: "autopilot-settings", text: summary.settings }));
  for (const group of summary.groups || []) {
    const waiting = group.name === "Waiting for you";
    const rows = group.rows.slice(0, 3).map((row) =>
      el(
        "li",
        { class: row.undone ? "undone" : "" },
        el("span", { class: "autopilot-tick", "aria-hidden": "true", text: waiting ? "•" : "✓" }),
        el(
          "div",
          { class: "autopilot-row" },
          el("button", { type: "button", class: "autopilot-what", text: row.undone ? `${row.text} (undone)` : row.text, title: "Its entry in the Autopilot record", onclick: () => showRecord({ run: summary.run, entry: row.entry }) }),
          row.then ? el("div", { class: "autopilot-then", text: `then: ${row.then}` }) : null,
        ),
        rowAction(row),
      ),
    );
    node.append(el("div", { class: `autopilot-group${waiting ? " waiting" : ""}` }, el("h4", { text: group.name }), el("ul", {}, ...rows)));
  }
  const total = (summary.groups || []).reduce((sum, group) => sum + group.rows.length, 0);
  if ((summary.groups || []).some((group) => group.more)) {
    node.append(el("button", { type: "button", class: "link autopilot-all", text: `View all ${total} →`, onclick: () => showRecord({ run: summary.run }) }));
  }
  for (const folded of summary.folded || []) {
    node.append(el("details", { class: "autopilot-folded" }, el("summary", { text: folded.split("\n")[0] }), el("p", { text: folded.split("\n").slice(1).join("\n") })));
  }
  return node;
}
function rowAction(row) {
  const act = {
    Undo: () => entryAction(row.entry, "undo"),
    Change: () => entryAction(row.entry, "change"),
    Answer: () => select(HOME),
    Review: () => select(HOME),
  }[row.action];
  if (!act) return null;
  return el("button", { type: "button", class: "link quiet autopilot-action", text: row.action, onclick: (event) => { event.stopPropagation(); act(); } });
}
async function entryAction(entry, action) {
  try {
    const found = await api(`/api/autopilot/entries/${entry}/${action}`, { method: "POST" });
    toast(found.said);
    if (isRecordView()) await refreshRecord();
  } catch (error) {
    toast(error.message, "error");
  }
}

// The Autopilot record: a view in the main area, as a team's, with ← Home.
let viewing = null;
export async function showRecord({ run = "", entry = "" } = {}) {
  closeMentions();
  setRecipient(null);
  state.selected = RECORD;
  setLayout(false);
  renderSidebar();
  $("#back-home").hidden = false;
  $("#session-menu").hidden = true;
  $("#worker-tabs").hidden = true;
  const actions = $("#session-actions");
  delete actions.dataset.key;
  actions.replaceChildren();
  $("#composer").hidden = true;
  $("#session-title").textContent = "Autopilot record";
  $("#session-title").title = "";
  $("#session-meta").replaceChildren();
  viewing = { run, entry, filter: "all", open: new Map(), unfolded: new Set(entry ? [entry] : []), data: null };
  $("#timeline").replaceChildren(el("div", { class: "line note", text: "Loading the record…" }));
  await refreshRecord();
}
export async function refreshRecord() {
  const view = viewing;
  if (!view || !isRecordView()) return;
  let data;
  try {
    data = await api("/api/autopilot/record");
  } catch (error) {
    $("#timeline").replaceChildren(el("div", { class: "line error", text: error.message }));
    return;
  }
  view.data = data;
  // An entry linked alone (the Router's answer): its run, open.
  if (!view.run && view.entry) {
    try {
      const found = await api(`/api/autopilot/entries/${view.entry}`);
      details.set(found.id, found);
      view.run = found.run_id;
    } catch {
      view.entry = "";
    }
  }
  if (!view.run && data.runs.length) view.run = data.runs[0].id;
  renderRecord();
  const loads = data.runs.filter((run) => run.id === view.run || view.open.has(run.id)).map((run) => loadRun(run.id));
  await Promise.all(loads);
}
async function loadRun(runId) {
  const view = viewing;
  try {
    const found = await api(`/api/autopilot/runs/${runId}`);
    view.open.set(runId, found.actions);
  } catch (error) {
    view.open.set(runId, { error: error.message });
  }
  if (viewing === view) renderRecord();
}
function runTitle(run) {
  const start = new Date(run.started_at);
  const end = run.ended_at ? new Date(run.ended_at) : null;
  const today = start.toDateString() === new Date().toDateString();
  const day = today ? "Today" : start.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  const span = end ? `${time(start)} – ${time(end)}` : `${time(start)} – now`;
  const parts = [`${day}, ${span}`, `${run.actions} action${run.actions === 1 ? "" : "s"}`];
  if (run.left) parts.push(`${run.left} left for you`);
  const ended = { you: "stopped by you", settings: "turned off in Settings", time: "", downgrade: "stopped before a downgrade" }[run.ended_by];
  if (ended) parts.push(ended);
  return parts.join(" · ");
}
const FILTERS = { all: ["All", () => true], parked: ["Left for you", (row) => row.kind === "parked" || row.kind === "not-done"] };
// All, or what it left for you: a radio group (arrows move the choice; focus stays on it).
function filters(view) {
  const keys = Object.keys(FILTERS);
  const choose = (key) => {
    view.filter = key;
    renderRecord();
    $(`.autopilot-record .filter[data-filter="${key}"]`)?.focus();
  };
  const group = el(
    "div",
    { class: "team-filters autopilot-filters", role: "radiogroup", "aria-label": "Show" },
    ...keys.map((key) => {
      const on = view.filter === key;
      return el("button", { type: "button", class: "filter", role: "radio", "data-filter": key, "aria-checked": String(on), tabindex: on ? "0" : "-1", onclick: () => choose(key) }, FILTERS[key][0]);
    }),
  );
  group.addEventListener("keydown", (event) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (!step) return;
    event.preventDefault();
    choose(keys[(keys.indexOf(view.filter) + step + keys.length) % keys.length]);
  });
  return group;
}
function renderRecord() {
  const view = viewing;
  const { data } = view;
  const scroller = el("div", { class: "autopilot-record" });
  if (!data.chain.ok) {
    const after = data.chain.after ? new Date(data.chain.after) : null;
    scroller.append(el("div", { class: "card update attention autopilot-broken", role: "alert", text: after ? `This record was changed outside Chief of Staff after ${time(after)} (${after.toLocaleDateString()}): what follows shows as unverified.` : "This record was changed outside Chief of Staff: what it holds shows as unverified." }));
  }
  if (data.runs.some((run) => run.left)) scroller.append(filters(view));
  for (const run of data.runs) {
    const open = run.id === view.run || view.open.has(run.id);
    const details = el("details", { class: "autopilot-run", open }, el("summary", {}, el("span", { text: runTitle(run) })));
    details.addEventListener("toggle", () => {
      if (details.open && !view.open.has(run.id)) loadRun(run.id);
      if (!details.open) view.open.delete(run.id);
      if (details.open) view.run = view.run || run.id;
    });
    const rows = view.open.get(run.id);
    if (Array.isArray(rows)) {
      const shown = rows.filter(FILTERS[view.filter][1]);
      details.append(
        el("ul", { class: "autopilot-actions" }, ...shown.map((row) => actionRow(row))),
        el("button", { type: "button", class: "link quiet autopilot-copy", text: "Copy as Markdown", onclick: () => copyRun(run.id) }),
      );
      if (!shown.length) details.append(el("p", { class: "autopilot-none", text: run.actions ? "Nothing left for you in this run." : "Nothing came up." }));
    } else if (rows?.error) {
      details.append(el("p", { class: "line error", text: rows.error }));
    }
    scroller.append(details);
  }
  if (data.ask_first !== undefined) {
    const yolo = state.snapshot.settings.worker_yolo;
    const usual = yolo ? "act without asking" : "ask first";
    scroller.prepend(el("p", { class: "autopilot-settings", text: data.ask_first && yolo ? `Workers ask first while Autopilot is on (your usual setting: ${usual}).` : `Workers keep your usual settings while Autopilot is on: ${usual}.` }));
  }
  $("#timeline").replaceChildren(scroller);
  if (view.entry) {
    const node = scroller.querySelector(`[data-entry="${view.entry}"]`);
    if (node) {
      node.scrollIntoView({ block: "center" });
      view.entry = "";
    }
  }
}
const STATE_WORDS = { withdrawn: "withdrawn", undone: "undone", failed: "failed" };
function actionRow(row) {
  const view = viewing;
  const unfolded = view.unfolded.has(row.id);
  const words = [row.text, row.state ? `(${STATE_WORDS[row.state] || row.state})` : ""].filter(Boolean).join(" ");
  const toggle = el(
    "button",
    { type: "button", class: "autopilot-line", "aria-expanded": String(unfolded), "aria-label": `${clock(row.at)}: ${words}`, onclick: () => { if (unfolded) view.unfolded.delete(row.id); else view.unfolded.add(row.id); renderRecord(); } },
    el("time", { text: clock(row.at) }),
    el("span", { class: "autopilot-text", text: words }),
  );
  const item = el("li", { class: `autopilot-item${row.unverified ? " unverified" : ""}`, "data-entry": row.id }, el("div", { class: "autopilot-head" }, toggle, rowAction({ ...row, entry: row.id })));
  for (const then of row.then) item.append(el("div", { class: "autopilot-then", text: `then: ${then}` }));
  if (row.unverified) item.append(el("div", { class: "autopilot-then", text: "Unverified: the record was changed outside Chief of Staff before this." }));
  if (unfolded) item.append(entryDetail(row.id));
  return item;
}
const details = new Map();
function entryDetail(entryId) {
  const node = el("div", { class: "autopilot-detail" });
  const fill = (entry) => {
    const why = entry.why || {};
    const parts = [];
    if (why.basis?.length) parts.push(el("p", {}, el("strong", { text: "Because you said " }), ...why.basis.flatMap((cite, index) => [index ? " and " : "", el("q", { text: cite.quote })])));
    if (why.reason) parts.push(el("p", {}, el("strong", { text: "Why: " }), why.reason));
    if (why.judge && !why.reason) parts.push(el("p", {}, el("strong", { text: "Its judge: " }), why.judge));
    if (why.verification) parts.push(el("p", {}, el("strong", { text: "Verified: " }), why.verification));
    const steps = entry.evidence?.steps || [];
    for (const step of steps) {
      parts.push(el("details", { class: "autopilot-step" }, el("summary", {}, el("code", { text: step.command }), ` · exit ${step.exit}`), el("pre", { text: String(step.output || "").slice(-20000) })));
    }
    if (entry.payload?.answer) parts.push(el("p", {}, el("strong", { text: "Exactly what: " }), entry.payload.answer));
    if (entry.payload?.choice) parts.push(el("p", {}, el("strong", { text: "Decided: " }), entry.payload.choice));
    for (const later of entry.later || []) parts.push(el("p", { class: "autopilot-later" }, el("time", { text: clock(later.at) }), ` ${later.text}`));
    node.replaceChildren(...(parts.length ? parts : [el("p", { text: "Nothing more was recorded for it." })]));
  };
  if (details.has(entryId)) fill(details.get(entryId));
  else {
    node.append(el("p", { class: "line note", text: "Loading…" }));
    api(`/api/autopilot/entries/${entryId}`)
      .then((entry) => {
        details.set(entryId, entry);
        fill(entry);
      })
      .catch((error) => node.replaceChildren(el("p", { class: "line error", text: error.message })));
  }
  return node;
}
async function copyRun(runId) {
  try {
    const { text } = await api(`/api/autopilot/runs/${runId}/markdown`);
    await navigator.clipboard.writeText(text);
    toast("Copied as Markdown.");
  } catch (error) {
    toast(error.message, "error");
  }
}
// Links to it from the Router's answers and Handled for you: #autopilot/<entry>, #autopilot-run/<run>.
export function openAutopilotLink(href) {
  const found = String(href || "").match(/^#autopilot(-run)?\/([\w-]+)$/);
  if (!found) return false;
  showRecord(found[1] ? { run: found[2] } : { entry: found[2] });
  return true;
}
document.addEventListener("click", (event) => {
  const link = event.target.closest?.('a[href^="#autopilot"]');
  if (link && openAutopilotLink(link.getAttribute("href"))) event.preventDefault();
});
