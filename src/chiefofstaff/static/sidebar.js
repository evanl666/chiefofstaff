// The sidebar: Home, sessions, closed workers and other Copilot sessions.

import { $, api, el, toast, withoutBidi } from "./dom.js";
import { ago, clock, plural, shortPath, since, span } from "./text.js";
import { closedWorkers, DOT_HELP, HOME, loadState, otherItem, othersOn, otherStatus, routerId, sessions, state, workerTitle } from "./state.js";
import { IN_TERMINAL, readOnlyTimeline, scrollToBottom, select, setComposer, setLayout, showDraft } from "./session.js";
import { homeItem, needsCount, WORKER_STATES, workerStates } from "./inbox.js";
import { closeMentions, setRecipient } from "./mention.js";
import { openSettings, watchGaps } from "./settings.js";
import { ownTasks } from "./tasks.js";
import { duck, setMood } from "./mascot.js";
// The captain beside the name says how Home stands, as the dots do: it salutes when something
// needs the owner, steers while the Router or a worker is at work, keeps watch while tasks wait
// on something, and is quiet otherwise; it cheers a moment when a task is done (and nothing
// needs the owner).
const CHEER_MS = 6000;
const cheer = { finished: null, until: 0 };
export function captainMood({ needs, finished, dots, now = Date.now() }) {
  if (cheer.finished !== null && finished > cheer.finished) {
    cheer.until = now + CHEER_MS;
    setTimeout(renderSidebar, CHEER_MS + 50);
  }
  cheer.finished = finished;
  if (needs) return ["needs", `${needs} need${needs === 1 ? "s" : ""} you`];
  if (now < cheer.until) return ["done", "Done"];
  if (dots.includes("working")) return ["working", "At work"];
  if (dots.includes("watching")) return ["watching", "Keeping watch: tasks wait on CI, a review or a reply"];
  return ["idle", "All quiet"];
}
function captain(needs, where) {
  const brand = document.querySelector("#sidebar .brand");
  let node = brand.querySelector(":scope > .duck");
  if (!node) brand.prepend((node = duck()));
  // The Router and the owner's workers: not Chief of Staff's own helpers.
  const dots = sessions().filter((session) => session.id === routerId() || (session.role === "worker" && !session.helper)).map((session) => where.get(session.id)?.dot);
  const [mood, words] = captainMood({ needs, finished: ownTasks().filter((task) => task.status === "done").length, dots });
  setMood(node, mood);
  node.title = words;
}
// One count in the whole sidebar, on Home: what needs the owner. Workers show only their dot,
// which says where each stands as Home does (Needs you, Running, Monitoring).
function sessionItem(session, label, where) {
  const dot = where?.dot || "idle";
  const named = label !== session.title ? `\nStarted as “${session.title}”` : "";
  // Its turn over, a command it left running (a dev server) goes on: not at work, not idle.
  const background = session.background && session.status !== "working";
  return el(
    "button",
    {
      type: "button",
      class: `item ${dot.split(" ")[0]}${state.selected === session.id ? " selected" : ""}`,
      title: [where?.text, background ? "Waiting for background process" : session.activity].filter(Boolean).join(" · ") + named,
      "data-id": session.id,
      onclick: () => select(session.id),
    },
    el("span", { class: `dot ${dot}`, title: where?.text || "" }),
    el("span", { class: "label", text: label }),
    // A marker in words (queued, overdue…), not a color of its own.
    where?.sub || background ? el("span", { class: "sub", text: where?.sub || "background" }) : null,
  );
}
const ORDER = Object.fromEntries([...WORKER_STATES.map(([dot]) => dot), "open"].map((dot, index) => [dot, index]));
// What the dots mean, under "Workers" when asked.
function legend() {
  return el("div", { class: "legend" }, ...WORKER_STATES.map(([dot, text]) => el("div", { class: "legend-row" }, el("span", { class: `dot ${dot}` }), el("span", { text }))), el("div", { class: "legend-row" }, el("span", { class: "dot open" }), el("span", { text: DOT_HELP.open })));
}
export function renderSidebar() {
  const list = $("#session-list");
  const all = sessions();
  const where = workerStates();
  const rank = (session) => ORDER[(where.get(session.id)?.dot || "idle").split(" ")[0]] ?? 9;
  // Chief of Staff's own helpers (learning, checks, the profile) are not the owner's workers.
  const workers = all.filter((session) => session.role === "worker" && !session.helper).sort((a, b) => rank(a) - rank(b) || b.created_at.localeCompare(a.created_at));
  const nodes = [homeItem()];
  if (workers.length) {
    const toggle = el("button", { type: "button", class: "link legend-toggle", text: "?", title: "What the dots mean", "aria-expanded": String(Boolean(state.legend)), onclick: () => {
      state.legend = !state.legend;
      renderSidebar();
    } });
    nodes.push(el("div", { class: "group" }, el("span", { text: "Workers" }), toggle));
    if (state.legend) nodes.push(legend());
    // Workers with nothing open fold away: the ones that need you, work or wait come first.
    const idle = (worker) => (where.get(worker.id)?.dot || "idle") === "idle" && state.selected !== worker.id;
    const busy = workers.filter((worker) => !idle(worker));
    const resting = workers.filter(idle);
    nodes.push(...busy.map((worker) => sessionItem(worker, workerTitle(worker), where.get(worker.id))));
    if (resting.length) {
      const fold = el("details", { class: "idle-workers", open: Boolean(state.idleOpen) }, el("summary", { text: `Nothing open · ${resting.length}`, title: "Workers whose tasks are all finished" }), ...resting.map((worker) => sessionItem(worker, workerTitle(worker), where.get(worker.id))));
      fold.addEventListener("toggle", () => {
        state.idleOpen = fold.open;
      });
      nodes.push(fold);
    }
  }
  list.replaceChildren(...nodes);
  const count = needsCount();
  captain(count, where);
  document.title = count ? `(${count}) Chief of Staff` : "Chief of Staff";
  // Folded away on a narrow window: its button says what needs the owner, as Home does here.
  const badge = document.querySelector("#sidebar-toggle .badge");
  if (badge) {
    badge.textContent = count ? String(count) : "";
    badge.hidden = !count;
  }
  renderClosed();
  renderExternal();
  renderStatusLine();
}
// On a narrow window the sidebar folds away behind a button at the start of the header: it
// opens over the page, and closes once one of its items is picked, on Escape (the focus back on
// the button) or on a click beside it.
export const NARROW = "(max-width: 760px)";
export function bindSidebarToggle() {
  const application = $("#application");
  const sidebar = $("#sidebar");
  const toggle = el("button", { type: "button", id: "sidebar-toggle", class: "icon", title: "Workers, Settings and Usage", "aria-label": "Workers, Settings and Usage", "aria-controls": "sidebar", "aria-expanded": "false" }, el("span", { class: "bars", "aria-hidden": "true", text: "☰" }), el("span", { class: "badge amber", hidden: true }));
  const show = (open, focus = false) => {
    application.classList.toggle("sidebar-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    if (focus) (open ? sidebar.querySelector("button.item") : toggle)?.focus();
  };
  const opened = () => application.classList.contains("sidebar-open");
  toggle.addEventListener("click", () => show(!opened(), true));
  $("#session-header").prepend(toggle);
  sidebar.addEventListener("click", (event) => {
    if (event.target.closest("button.item, #settings-button, #usage-button")) show(false);
  });
  document.addEventListener("click", (event) => {
    if (opened() && !sidebar.contains(event.target) && !toggle.contains(event.target)) show(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && opened() && !event.target.closest?.("dialog")) show(false, true);
  });
  window.matchMedia(NARROW).addEventListener("change", (event) => event.matches || show(false));
  renderSidebar();
}
// Every closed worker of yours, once asked for (the snapshot has the latest 20).
let allClosed = null;
async function showAllClosed() {
  try {
    allClosed = await api("/api/workers/closed");
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  renderClosed();
}
function renderClosed() {
  const latest = closedWorkers().filter((session) => !session.helper);
  // Those shown now first (as they are now); none reopened since.
  const open = new Set(sessions().map((session) => session.id));
  const known = new Map([...(allClosed || []).filter((session) => !open.has(session.id)), ...latest].map((session) => [session.id, session]));
  const closed = [...known.values()].sort((a, b) => b.closed_at.localeCompare(a.closed_at));
  // The snapshot's list stops at 20 (helpers included): how many there are is counted apart.
  const capped = closedWorkers().length >= 20;
  const total = state.snapshot?.closed_count ?? (capped ? null : closed.length);
  const more = allClosed ? 0 : total === null ? 1 : total - closed.length;
  $("#closed").hidden = !closed.length;
  $("#closed-count").textContent = `· ${Math.max(total ?? 0, closed.length)}${total === null && !allClosed ? "+" : ""}`;
  $("#closed-list").replaceChildren(
    ...closed.map((session) =>
      el(
        "button",
        {
          type: "button",
          class: `item${state.selected === session.id ? " selected" : ""}`,
          title: [workerTitle(session) !== session.title ? `Started as “${session.title}”` : "", session.report].filter(Boolean).join("\n\n") || session.title,
          onclick: () => select(session.id),
        },
        el("span", { class: "dot" }),
        el("span", { class: "label", text: workerTitle(session) }),
        el("span", { class: "sub", text: ago(session.closed_at) }),
      ),
    ),
    more > 0 ? el("button", { type: "button", class: "link show-all", text: total === null ? "Show all" : `Show all ${total}`, title: "Older closed workers too", onclick: showAllClosed }) : [],
  );
}
export function renderStatusLine() {
  const line = $("#status-line");
  const snap = state.snapshot;
  const nodes = [];
  if (snap.runtime.state === "starting") nodes.push(el("div", { text: "Connecting to Copilot…" }));
  // On Home the setup card says it, with what to do.
  if (snap.runtime.state === "error" && state.selected !== HOME) {
    nodes.push(
      el("div", { class: "bad", text: `Copilot: ${snap.runtime.error}` }),
      el("button", { type: "button", class: "link", text: "Retry", onclick: reconnect }),
    );
  }
  if (snap.demo) nodes.push(el("div", { class: "demo", text: "Demo mode: scripted sessions, nothing leaves this machine" }));
  const until = snap.focus?.until && new Date(snap.focus.until) > new Date() ? new Date(snap.focus.until) : null;
  if (until) nodes.push(el("div", { class: "demo", text: `Focusing until ${until.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}: notifications wait` }));
  if (snap.runtime.warning) nodes.push(el("div", { class: "bad", text: snap.runtime.warning }));
  if (snap.settings.slack_enabled && snap.slack.error) {
    // What to do about it, here: fix it in Settings, or stop watching Slack.
    nodes.push(
      el(
        "div",
        { class: "bad" },
        `Slack: ${snap.slack.error} `,
        el("button", { type: "button", class: "link", text: "Settings", onclick: () => openSettings("connections") }),
        " · ",
        el("button", { type: "button", class: "link", text: "Turn off", title: "Stop watching Slack (turn it on again in Settings → Connections)", onclick: turnOffSlack }),
      ),
    );
  }
  if (snap.settings.github_enabled && snap.github.error) nodes.push(el("div", { class: "bad", text: `GitHub: ${snap.github.error}` }));
  // A watch the last scan could not search, or saw only part of: "0 found" means nothing there.
  const { missed, partial } = watchGaps(snap.connector?.last);
  if (missed.length || partial.length) {
    const many = (list, words) => (list.length ? `${list.length === 1 ? "one" : list.length} ${words}` : "");
    nodes.push(
      el(
        "div",
        { class: "bad", title: [...missed.map((item) => `Watch ${item.watch}: not searched (${item.reason})`), ...partial.map((item) => `Watch ${item.watch}: partly covered (${item.reason})`)].join("\n") },
        `Watches: ${[many(missed, "not searched"), many(partial, "only partly covered")].filter(Boolean).join(", ")} `,
        el("button", { type: "button", class: "link", text: "Settings", onclick: () => openSettings("connections") }),
      ),
    );
  }
  if (snap.settings.paused) nodes.push(el("div", { text: "Watchers paused" }));
  line.replaceChildren(...nodes);
}
async function turnOffSlack() {
  try {
    state.snapshot = { ...state.snapshot, ...(await api("/api/settings", { method: "POST", body: { slack_enabled: false } })) };
    renderStatusLine();
    toast("Slack watching is off. Turn it on in Settings → Connections.");
  } catch (error) {
    toast(error.message, "error");
  }
}
async function reconnect() {
  try {
    state.snapshot = { ...state.snapshot, ...(await api("/api/runtime/reconnect", { method: "POST" })) };
    await loadState();
  } catch (error) {
    toast(error.message, "error");
  }
}
function renderExternal() {
  if (othersOn()) {
    renderGroup();
    return;
  }
  const open = state.external.filter((item) => item.open).length;
  headingCount(state.external.length, open);
  $("#external-list").replaceChildren(
    ...state.external.map((item) =>
      el(
        "button",
        {
          type: "button",
          class: `item${state.selected === item.id ? " selected" : ""}`,
          title: item.open ? `Open in a terminal now · ${item.cwd}` : item.cwd,
          onclick: () => showExternal(item),
        },
        el("span", { class: `dot${item.open ? " open" : ""}`, title: item.open ? DOT_HELP.open : "Not open anywhere" }),
        el("span", { class: "label", text: item.summary }),
        el("span", { class: "sub", text: item.open ? "open" : ago(item.modified) }),
      ),
    ),
  );
}
// The group's heading on one line: how many are open, else how many there are (nothing until
// its list is read), on the right as the rows' small words; in full in its tooltip and name.
function headingCount(total, open) {
  const count = $("#external-count");
  const shown = !total ? "" : open ? `${open} open` : String(total);
  if (count.textContent !== shown) count.textContent = shown;
  const words = total ? [plural(total, "session"), open ? `${open} open` : ""].filter(Boolean).join(", ") : "";
  const summary = $("#external > summary");
  put(summary, "title", words);
  put(summary, "aria-label", words && `Other Copilot sessions, ${words}`);
}
export async function loadExternal(refresh = false) {
  try {
    state.external = await api(`/api/external${refresh ? "?refresh=true" : ""}`);
    state.externalRead = true;
    renderExternal();
  } catch (error) {
    toast(error.message, "error");
  }
}
// A Copilot session Chief of Staff does not manage: its latest conversation, read from its log.
// A message continues it here (the Router manages it from then on, in its own folder).
export const externalItem = () => (othersOn() ? otherItem(state.selected) : state.external.find((item) => item.id === state.selected));
export function showExternal(item) {
  // A worker chosen with @ is for messages sent from Home, not for this session.
  closeMentions();
  setRecipient(null);
  state.selected = item.id;
  // With the setting on, a reload shows it again (found by its id when it is in neither list).
  if (othersOn()) localStorage.setItem("cos.selected", item.id);
  showDraft(item.id);
  setLayout(false);
  renderSidebar();
  $("#back-home").hidden = false;
  $("#session-menu").hidden = true;
  $("#session-title").textContent = item.summary;
  $("#session-meta").textContent = externalMeta(item);
  $("#session-meta").title = item.cwd;
  const command = `copilot --resume ${item.id}`;
  delete $("#session-actions").dataset.key;
  $("#session-actions").replaceChildren(
    el("button", { type: "button", class: "text", text: "Copy resume command", title: command, onclick: () => navigator.clipboard.writeText(command).then(() => toast("Copied")) }),
  );
  $("#composer").hidden = false;
  setComposer(item.open ? { locked: IN_TERMINAL } : { placeholder: "Message to continue this session here…", attach: false });
  const history = el("div", { class: "external-history" }, el("div", { class: "line note", text: "Loading its conversation…" }));
  $("#timeline").replaceChildren(history);
  loadHistory(item, history, true);
}

// Read-only, from the session's log; refreshed while it is open in a terminal.
async function loadHistory(item, box, first = false) {
  if (state.selected !== item.id || !box.isConnected) return;
  try {
    const { entries, working, open } = await api(`/api/external/${item.id}/history`);
    if (state.selected !== item.id || !box.isConnected) return;
    if (item.open && !open) {
      // Exited in the terminal while the owner watched: it can be continued here now.
      item.open = false;
      $("#session-meta").textContent = $("#session-meta").textContent.replace("Open in a terminal", "Not managed yet");
      setComposer({ placeholder: "Message to continue this session here…", attach: false });
    }
    const nodes = readOnlyTimeline(entries, item.cwd);
    if (!nodes.length) nodes.push(el("div", { class: "line note", text: "No conversation yet." }));
    if (working) nodes.push(el("div", { class: "working-line", text: "Working in a terminal…" }));
    else if (!open) nodes.push(el("div", { class: "line notice" }, el("span", { text: `A message continues it here, still in ${shortPath(item.cwd) || "its folder"}` })));
    box.replaceChildren(...nodes);
    showQuestion(box, item);
    scrollToBottom(first);
    if (open) setTimeout(() => loadHistory(item, box), 4000);
  } catch (error) {
    box.replaceChildren(el("div", { class: "line error", text: error.message }));
  }
}

// With the setting on (others.py), the group says where each of the owner's other Copilot
// sessions stands: in its rows as they are (a dot, the title, a small word), their tooltips and
// names, and in the view's meta line. The dots are the sidebar's own: amber waits for the owner,
// a pulsing ring works in a terminal, dashed made no progress, a ring is open, green finished.
// `counted`: its small word says for how long; `since`: how its time is said in the meta line.
const STATUSES = {
  asks: { name: "Asks you", dot: "waiting", counted: true, since: "since", log: (at) => `asked at ${at}, no answer since` },
  your_turn: { name: "Your turn", dot: "waiting", counted: true, since: "since", log: (at) => `its turn ended at ${at}` },
  working: { name: "Working", dot: "open busy", since: "since", log: (at, before) => `at work since ${before ? "before " : ""}${at}` },
  no_progress: {
    name: "No progress",
    dot: "stalled",
    counted: true,
    since: "for",
    log: (at) => `nothing written since ${at}`,
    // What its log cannot tell apart.
    note: "A slow step, a hang or a prompt in its terminal look the same from its log.",
  },
  idle: { name: "Idle", dot: "open", since: "since", log: (at) => `its turn ended at ${at}` },
  finished: { name: "Finished", dot: "done", since: "at", log: (at) => `last written at ${at}` },
  stopped: { name: "Stopped", dot: "", since: "at", log: (at) => `last written at ${at}` },
};
const known = (status) => typeof status.elapsed === "number";
// How long, in words ("12 minutes") and briefly ("12 min", "3h").
function lasting(seconds) {
  if (seconds < 60) return "less than a minute";
  if (seconds < 3600) return plural(Math.floor(seconds / 60), "minute");
  if (seconds < 86400) return plural(Math.floor(seconds / 3600), "hour");
  return plural(Math.floor(seconds / 86400), "day");
}
const brief = (seconds) => (seconds < 60 ? "less than a minute" : seconds < 3600 ? `${Math.floor(seconds / 60)} min` : span(seconds));
// Where it stands, in a few words: "Asks you, for 12 minutes".
function headline(status) {
  const kind = STATUSES[status.key];
  if (!known(status)) return kind.name;
  const long = lasting(status.elapsed);
  if (kind.since === "for") return `${kind.name} for ${long}`;
  if (kind.since === "at") return `${kind.name}, ${long} ago`;
  return `${kind.name}, for ${status.before ? "at least " : ""}${long}`;
}
// How a row of the group looks: as without the setting when it has no status (its tooltip says
// why), else its dot, its wait in the small word, and its status in words.
export function otherLook(item, status) {
  const kind = status && STATUSES[status.key];
  if (!kind) {
    return {
      dot: `dot${item.open ? " open" : ""}`,
      dotTitle: item.open ? DOT_HELP.open : "Not open anywhere",
      sub: item.open ? "open" : item.modified ? ago(item.modified) : "",
      title: [item.open ? `Open in a terminal now · ${item.cwd}` : item.cwd, item.why].filter(Boolean).join("\n"),
      name: "",
    };
  }
  const words = headline(status);
  return {
    dot: `dot${kind.dot ? ` ${kind.dot}` : ""}`,
    dotTitle: words,
    sub: kind.counted && known(status) ? span(status.elapsed) : item.open ? "open" : item.modified ? ago(item.modified) : "",
    title: [status.since ? `${words} · from its log: ${kind.log(clock(status.since), status.before)}` : words, status.words, kind.note, item.cwd].filter(Boolean).join("\n"),
    name: `${item.summary}, ${words[0].toLowerCase()}${words.slice(1)}`,
  };
}
// The start of the view's meta line: "Asks you since 2:10 PM", "No progress for 40 min".
function metaLead(status) {
  const kind = STATUSES[status.key];
  if (kind.since === "for") return known(status) ? `${kind.name} for ${brief(status.elapsed)}` : kind.name;
  if (status.since) return `${kind.name} ${kind.since === "at" ? "at" : `since${status.before ? " before" : ""}`} ${clock(status.since)}`;
  if (!known(status)) return kind.name;
  return kind.since === "at" ? `${kind.name} ${brief(status.elapsed)} ago` : `${kind.name} for ${brief(status.elapsed)}`;
}
function externalMeta(item) {
  const status = otherStatus(item.id);
  const lead = status && STATUSES[status.key] ? metaLead(status) : item.open ? "Open in a terminal" : "Not managed yet";
  return [lead, shortPath(item.cwd), item.branch, `updated ${since(item.modified)}`].filter(Boolean).join(" · ");
}
// The end of its timeline, by where it stands: while it asks, the question takes the place of
// "Working in a terminal…" (its step shows only "ask_user"); once its turn ended (its turn,
// idle, finished, stopped) that line goes, the meta line says it. Otherwise (at work, no
// progress, no status, its lease over) the line as without the setting.
const ENDED = new Set(["your_turn", "idle", "finished", "stopped"]);
function showQuestion(box, item) {
  const status = otherStatus(item.id);
  const asks = status?.key === "asks";
  const line = box.querySelector(":scope > .working-line");
  if (line) line.hidden = asks || ENDED.has(status?.key);
  const text = asks ? withoutBidi(status.words ? `Asks you: ${status.words}` : "Asks you") : "";
  const note = box.querySelector(":scope > [data-question]");
  if (!text) note?.remove();
  else if (!note) box.append(el("div", { class: "line note", "data-question": "", text }));
  else if (note.textContent !== text) note.textContent = text;
}
// What the index said changed, or a lease ran out: the group, and the view of one of them.
export function renderOthers(item = externalItem()) {
  renderExternal();
  const box = $("#timeline > .external-history");
  if (!item || !box || state.selected !== item.id) return;
  const meta = externalMeta(item);
  if ($("#session-meta").textContent !== meta) $("#session-meta").textContent = meta;
  const asked = box.querySelector(":scope > [data-question]");
  showQuestion(box, item);
  if (box.querySelector(":scope > [data-question]") !== asked) scrollToBottom();
}
// The group's rows: today's list and the sessions the index follows, together (one opened in a
// terminal shows before the list is read again), open ones first, then the latest, as the list
// is; none continued here since.
function otherSessions() {
  const rows = new Map(state.external.map((item) => [item.id, item]));
  for (const row of state.others.values()) rows.set(row.id, row);
  const when = (item) => Date.parse(item.modified) || 0;
  return [...rows.values()].filter((item) => otherItem(item.id)).sort((a, b) => Number(Boolean(b.open)) - Number(Boolean(a.open)) || when(b) - when(a));
}
// At most `most` rows at rest: those waiting for the owner, then those without progress, then
// those at work, each the longest first, then the rest in the list's order (those of `keep`
// always among them: the one shown, the one the keyboard is on); the others fold away, in the
// list's order.
const RANK = { asks: 0, your_turn: 0, no_progress: 1, working: 2 };
export function restingOthers(items, statusOf, keep = [], most = 5) {
  const urgent = items
    .map((item) => [item, statusOf(item.id)])
    .filter(([, status]) => status && RANK[status.key] !== undefined)
    .sort(([, a], [, b]) => RANK[a.key] - RANK[b.key] || (b.elapsed ?? 0) - (a.elapsed ?? 0))
    .map(([item]) => item);
  const chosen = new Set(urgent.slice(0, most));
  for (const mine of items.filter((item) => keep.includes(item.id) && !chosen.has(item))) {
    // Its room is the last one's that waits or works, unless that one is kept too.
    const last = urgent.findLast((item) => chosen.has(item) && !keep.includes(item.id));
    if (chosen.size >= most && last) chosen.delete(last);
    chosen.add(mine);
  }
  for (const item of items) if (chosen.size < most) chosen.add(item);
  const first = [...urgent, ...items.filter((item) => !urgent.includes(item))].filter((item) => chosen.has(item));
  return [first, items.filter((item) => !chosen.has(item))];
}
// The order under the pointer, or under the keyboard's focus, stays (as Home's lists keep
// theirs): rows keep their places, updated in place, and one new joins the fold; sorted again
// once both have left. Not the focus a click leaves on a row (no visible focus): it stays there
// while its view is read, and the list would not move for minutes.
const holding = (list) => list.matches(":hover, :has(:focus-visible)");
let drawn = null;
function steady(list, rest, more, items) {
  if (!drawn || !holding(list)) return [rest, more];
  if (!list.dataset.held) {
    list.dataset.held = "1";
    // After the event: the focus may only move within the list, or come back to a row moved.
    const left = () => setTimeout(() => {
      if (holding(list) || !list.dataset.held) return;
      list.removeEventListener("mouseleave", left);
      list.removeEventListener("focusout", left);
      delete list.dataset.held;
      renderExternal();
    });
    list.addEventListener("mouseleave", left);
    list.addEventListener("focusout", left);
  }
  const byId = new Map(items.map((item) => [item.id, item]));
  const placed = new Set([...drawn.rest, ...drawn.more]);
  const kept = (ids) => ids.map((id) => byId.get(id)).filter(Boolean);
  return [kept(drawn.rest), [...kept(drawn.more), ...items.filter((item) => !placed.has(item.id))]];
}
function renderGroup() {
  const items = otherSessions();
  const open = items.filter((item) => item.open).length;
  // Counted once the list was read (it does not jump as the list comes in).
  headingCount(state.externalRead ? items.length : 0, open);
  const list = $("#external-list");
  const focused = list.contains(document.activeElement) ? document.activeElement : null;
  // The row the keyboard is on stays at rest while the rest are folded: in the fold, it could
  // not keep the focus.
  const keep = [state.selected, ...(focused?.dataset.id && !state.othersMore ? [focused.dataset.id] : [])];
  const [rest, more] = steady(list, ...restingOthers(items, otherStatus, keep), items);
  drawn = { rest: rest.map((item) => item.id), more: more.map((item) => item.id) };
  // Rows are updated in place: a focused one keeps its focus, a hovered one its tooltip.
  const rows = new Map([...list.querySelectorAll("button.item[data-id]")].map((node) => [node.dataset.id, node]));
  const row = (item) => otherRow(item, rows.get(item.id));
  // The rest fold away as workers with nothing open do.
  let fold = list.querySelector(":scope > details.idle-workers");
  if (more.length) {
    if (!fold) {
      fold = el("details", { class: "idle-workers", open: Boolean(state.othersMore) }, el("summary", { title: "Your other Copilot sessions: open ones first, then the latest" }));
      fold.addEventListener("toggle", () => {
        state.othersMore = fold.open;
      });
    }
    const summary = fold.firstElementChild;
    if (summary.textContent !== `${more.length} more`) summary.textContent = `${more.length} more`;
    place(fold, [summary, ...more.map(row)]);
  }
  place(list, [...rest.map(row), ...(more.length ? [fold] : [])]);
  if (focused?.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
}
// Only what moved is moved (what goes first, so the rest need not move).
function place(box, nodes) {
  const wanted = new Set(nodes);
  for (const child of [...box.children]) if (!wanted.has(child)) child.remove();
  nodes.forEach((node, index) => {
    if (box.children[index] !== node) box.insertBefore(node, box.children[index] || null);
  });
}
function otherRow(item, node) {
  const look = otherLook(item, otherStatus(item.id));
  if (!node) {
    const { id } = item;
    const open = () => {
      const found = otherItem(id);
      if (found) showExternal(found);
    };
    node = el("button", { type: "button", "data-id": id, onclick: open }, el("span"), el("span", { class: "label" }), el("span", { class: "sub" }));
  }
  const [dot, label, sub] = node.children;
  put(node, "class", `item${state.selected === item.id ? " selected" : ""}`);
  put(node, "title", look.title);
  put(node, "aria-label", look.name);
  put(dot, "class", look.dot);
  put(dot, "title", look.dotTitle);
  const summary = withoutBidi(item.summary);
  if (label.textContent !== summary) label.textContent = summary;
  if (sub.textContent !== look.sub) sub.textContent = look.sub;
  return node;
}
// An attribute as it should be, written only when it changes (none when empty).
function put(node, name, value) {
  const shown = withoutBidi(value);
  if (!shown) node.removeAttribute(name);
  else if (node.getAttribute(name) !== shown) node.setAttribute(name, shown);
}
