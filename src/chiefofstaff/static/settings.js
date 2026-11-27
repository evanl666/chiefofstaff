// The settings and usage dialogs.

import { autopilot, autopilotRun, showRecord, untilWords } from "./autopilot.js";
import { $, api, el, submits, toast } from "./dom.js";
import { lines, plural, since } from "./text.js";
import { closedWorkers, followOthers, HOME, loadState, sessions, state } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { renderHeader, select } from "./session.js";
import { agentsBody, agentsSaved, agentsShown, agentsStateChanged, modelsArrived, renderAgentsPage, setupChanged as agentSetupChanged } from "./agentspage.js";
import { agentsOn, agentsSaid } from "./agentpick.js";
import { openTrust, setupWords } from "./agentsetup.js";
import { renderSharedLinks } from "./share.js";

// Settings' pages (docs/design/settings-v2.md): what each name opens, old names included, so a
// link, a toast, a timeline line's `meta.settings` and the last page kept in localStorage open
// the right place. `connections/<row>` opens that connection's details.
const PLACES = {
  general: { tab: "general" },
  workers: { tab: "general", scroll: "general-workers" },
  advanced: { tab: "general", unfold: "more" },
  proactive: { tab: "general", unfold: "prs" },
  autopilot: { tab: "general", unfold: "autopilot" },
  models: { tab: "agents", unfold: "models" },
  agents: { tab: "agents" },
  connections: { tab: "connections" },
  you: { tab: "you" },
};
const CONNECTIONS = ["slack", "github", "watches", "away", "tools", "send"];
export function settingsPlace(name = "") {
  const [head, row] = String(name || "").split("/");
  if (head === "connections" && CONNECTIONS.includes(row)) return { tab: "connections", connection: row };
  return PLACES[head] || { tab: "general" };
}
// Settings opens on the page last used, or the one asked for ("you" for what was learned).
const TAB = "cos.settingsTab";
const tabButtons = () => [...document.querySelectorAll("#settings-tabs [role=tab]")].filter((button) => !button.hidden);
export function showSettingsTab(name, { focus = false } = {}) {
  const place = settingsPlace(name);
  const tabs = [...document.querySelectorAll("#settings-pane > .tab")];
  const shown = tabs.find((section) => section.dataset.tab === place.tab) || tabs[0];
  localStorage.setItem(TAB, shown.dataset.tab);
  for (const section of tabs) section.hidden = section !== shown;
  for (const button of document.querySelectorAll("#settings-tabs [role=tab]")) {
    const on = button.dataset.tab === shown.dataset.tab;
    button.setAttribute("aria-selected", String(on));
    button.tabIndex = on ? 0 : -1;
  }
  tabInView();
  $("#settings-pane").scrollTop = 0;
  if (place.connection) openConnection(place.connection, { focus });
  // General's More (Advanced's old page), Keep my PRs moving's Options (Prepared for you's old
  // page; while it is on) or its Workers group (Workers' old page), in view.
  if (place.unfold === "more") $("#general-more").open = true;
  if (place.unfold === "prs" && $("#settings-form").proactive.checked) setUnfold("prs-more", true);
  if (place.unfold === "autopilot" && $("#settings-form").autopilot.checked) setUnfold("autopilot-more", true);
  const target = { more: "general-more", prs: "proactive-line", autopilot: "autopilot-line" }[place.unfold] || place.scroll;
  if (target) document.getElementById(target)?.scrollIntoView?.({ block: "start" });
  // The models it named: Who does the work, at the top of its page, nothing folded.
  if (place.unfold === "models") document.querySelector("#agents-page .work-grid")?.scrollIntoView({ block: "nearest" });
  // Agents & models looks at the installed agents when it is shown (at most once an opening).
  if (shown.dataset.tab === "agents") agentsShown();
  if (focus && !place.connection) document.querySelector(`#settings-tabs [role=tab][data-tab="${shown.dataset.tab}"]`)?.focus();
}
// What a line governs (its Options), unfolded or folded, its button saying which.
function setUnfold(id, open) {
  const part = document.getElementById(id);
  if (!part) return;
  part.hidden = !open;
  document.querySelector(`[aria-controls="${id}"]`)?.setAttribute("aria-expanded", String(open));
}
// Narrow (900 px and below) the pages are tabs over the page, scrolling sideways where they
// don't fit: the one shown is in view, whole, also as the width changes. Only the strip scrolls.
function tabInView() {
  const strip = $("#settings-tabs");
  const button = strip?.querySelector('[aria-selected="true"]');
  if (!button || strip.scrollWidth <= strip.clientWidth) return;
  const box = strip.getBoundingClientRect();
  const at = button.getBoundingClientRect();
  const edge = 10;
  if (at.left < box.left + edge) strip.scrollLeft -= box.left + edge - at.left;
  else if (at.right > box.right - edge) strip.scrollLeft += at.right - box.right + edge;
}
const narrow = window.matchMedia?.("(max-width: 900px)");
function tabsOrientation() {
  $("#settings-tabs")?.setAttribute("aria-orientation", narrow?.matches ? "horizontal" : "vertical");
}
window.addEventListener("resize", () => {
  tabInView();
  tabsOrientation();
});
export function openSettings(where = "") {
  const dialog = $("#settings-dialog");
  // Opened first: a part that fails to render must not keep Settings shut.
  if (!dialog.open) dialog.showModal();
  try {
    fillSettings();
  } catch (error) {
    console.error(error);
    settingsError(`Part of Settings could not be shown: ${error.message}`);
  }
  tabsOrientation();
  showSettingsTab(where || localStorage.getItem(TAB) || "general", { focus: true });
  // The owner's share links, at the end of You: nothing is drawn without one.
  renderSharedLinks();
  // Feedback, the owner's words and checks come in all the time; what was learned is read fresh.
  api("/api/state")
    .then((data) => {
      state.snapshot.learning = data.learning;
      state.snapshot.habits = data.habits;
      state.snapshot.checks = data.checks;
      state.snapshot.abilities = data.abilities;
      state.snapshot.connector = data.connector;
      if (data.projects) state.snapshot.projects = data.projects;
      renderLearning();
      renderConnections();
    })
    .catch((error) => console.error(error));
}
// A connection's details, in place of the list (its fields were there all along).
export function openConnection(key, { focus = true } = {}) {
  $("#conn-list").hidden = true;
  for (const detail of document.querySelectorAll("#settings-pane [data-conn-detail]")) detail.hidden = detail.dataset.connDetail !== key;
  $("#settings-pane").scrollTop = 0;
  if (focus) document.querySelector(`[data-conn-detail="${key}"] h3`)?.focus();
}
// Back to the list: focus on the row it came from.
export function closeConnection({ focus = true } = {}) {
  const open = document.querySelector("#settings-pane [data-conn-detail]:not([hidden])");
  for (const detail of document.querySelectorAll("#settings-pane [data-conn-detail]")) detail.hidden = true;
  $("#conn-list").hidden = false;
  if (focus && open) document.querySelector(`#conn-list .conn[data-conn="${open.dataset.connDetail}"] .conn-open`)?.focus();
}
// Where a field is, opened: its page, its connection, every fold above it; scrolled to and
// focused. Every problem goes through it: the browser's, the page's and the server's.
export function revealField(input) {
  const pane = input?.closest?.("#settings-pane > .tab");
  if (!pane) return;
  showSettingsTab(pane.dataset.tab);
  const detail = input.closest("[data-conn-detail]");
  if (detail) openConnection(detail.dataset.connDetail, { focus: false });
  for (let node = input.parentElement; node && node !== pane; node = node.parentElement) {
    if (node.tagName === "DETAILS") node.open = true;
    if (node.hidden && !node.matches("[data-conn-detail]")) {
      node.hidden = false;
      if (node.id) document.querySelector(`[aria-controls="${node.id}"]`)?.setAttribute("aria-expanded", "true");
    }
  }
  input.scrollIntoView?.({ block: "center" });
  input.focus();
}
export function bindSettings() {
  const dialog = $("#settings-dialog");
  const form = $("#settings-form");
  $("#lesson-save").addEventListener("click", addLesson);
  $("#lesson-text").addEventListener("keydown", (event) => {
    if (!submits(event)) return;
    event.preventDefault();  // Not the settings form's submit.
    addLesson();
  });
  const tabs = $("#settings-tabs");
  tabs.addEventListener("click", (event) => {
    const button = event.target.closest("[role=tab]");
    if (button) showSettingsTab(button.dataset.tab);
  });
  // A tablist: arrows (either way), Home and End move between pages.
  tabs.addEventListener("keydown", (event) => {
    const steps = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };
    const buttons = tabButtons();
    const at = buttons.indexOf(document.activeElement);
    if (at < 0 || !(event.key in steps || event.key === "Home" || event.key === "End")) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (at + steps[event.key] + buttons.length) % buttons.length;
    showSettingsTab(buttons[next].dataset.tab, { focus: true });
  });
  form.addEventListener("submit", saveSettings);
  $("#settings-cancel").addEventListener("click", () => dialog.close());
  // A draft: marked changed (a menu then sends its value), and its connection's row says it is
  // not saved yet. Nothing is drawn anew.
  const drafted = (event) => {
    const input = event.target;
    if (!input.matches?.("input, select, textarea") || input.type === "file") return;
    input.dataset.dirty = "1";
    if (input.closest("[data-conn-detail]")) renderConnectionList();
  };
  form.addEventListener("input", drafted);
  form.addEventListener("change", drafted);
  $("#settings-pane").addEventListener("click", (event) => {
    const options = event.target.closest("button.options[aria-controls]");
    if (options) {
      const id = options.getAttribute("aria-controls");
      setUnfold(id, document.getElementById(id).hidden);
      return;
    }
    if (event.target.closest("[data-back]")) {
      closeConnection();
      return;
    }
    const act = event.target.closest(".conn .conn-act");
    if (act) {
      connectionAction(act.closest(".conn").dataset.conn, act.dataset.act);
      return;
    }
    const row = event.target.closest(".conn");
    if (row) openConnection(row.dataset.conn);
    const activity = event.target.closest("#watch-activity, #slack-activity, #watchdog-activity");
    if (activity) {
      const find = {
        "watch-activity": () => state.snapshot.connector?.session_id,
        "slack-activity": () => sessions().find((session) => session.role === "slack")?.id,
        "watchdog-activity": () => sessions().find((session) => session.role === "watchdog")?.id,
      }[activity.id];
      const sessionId = find();
      if (!sessionId) return;
      dialog.close();
      select(sessionId);
    }
  });
  // Copilot's models came after Settings opened: its menus again, their values kept.
  window.addEventListener("models", modelsArrived);
  $("#profile-use").addEventListener("click", () => {
    $("#settings-form").profile.value = state.snapshot.profile_draft;
    $("#profile-draft").hidden = true;
    settingsError("Review the draft, edit it if needed, then Save to use it.");
  });
  $("#profile-discard").addEventListener("click", async () => {
    state.snapshot = { ...state.snapshot, ...(await api("/api/profile/discard", { method: "POST" })) };
    $("#profile-draft").hidden = true;
  });
  $("#profile-distill").addEventListener("click", async () => {
    try {
      await api("/api/profile/distill", { method: "POST" });
      dialog.close();
      await select(HOME);
      toast("Building your profile. Its draft comes to Needs you, to check before it is used.");
    } catch (error) {
      settingsError(error.message);
    }
  });
  $("#learn-now").addEventListener("click", learnNow);
  $("#learn-last-run").addEventListener("click", () => {
    const run = lastLearner();
    if (!run) return;
    dialog.close();
    select(run.id);
  });
  $("#skill-upload").addEventListener("click", () => $("#skill-file").click());
  $("#sandbox-trust").addEventListener("click", openTrust);
  $("#skill-file").addEventListener("change", async () => {
    const file = $("#skill-file").files[0];
    $("#skill-file").value = "";
    if (file && (await addSkill(await file.text()))) $("#skill-paste-box").hidden = true;
  });
  // Add a skill…: paste it, or upload its SKILL.md.
  $("#skill-paste").addEventListener("click", () => {
    $("#skill-paste-box").hidden = false;
    $("#skill-text").focus();
  });
  $("#skill-cancel").addEventListener("click", () => {
    $("#skill-paste-box").hidden = true;
  });
  $("#skill-add").addEventListener("click", async () => {
    if (await addSkill($("#skill-text").value)) {
      $("#skill-text").value = "";
      $("#skill-paste-box").hidden = true;
    }
  });
  $("#plugins-retry").addEventListener("click", () => settingsAction("/api/plugins/retry", "Retrying the plugins that failed."));
  form.mcp_servers.addEventListener("input", () => {
    try {
      const note = mcpNote(mcpServers());
      $("#mcp-error").textContent = note;
      $("#mcp-error").hidden = !note;
    } catch (error) {
      $("#mcp-error").textContent = error.message;
      $("#mcp-error").hidden = false;
    }
  });
  $("#watch-add").addEventListener("click", () => {
    if ($("#watch-list").children.length >= 10) {
      settingsError("At most 10 watches.");
      return;
    }
    const row = watchRow({ what: "", how: "", enabled: true });
    $("#watch-list").append(row);
    renderConnectionList();
    row.querySelector(".what").focus();
  });
  // Scanning uses the watches as written here: saved first (a watch just added counts).
  $("#watch-scan").addEventListener("click", scanNow);
  // The test uses how to reach the owner as written here: saved first, as Scan now does.
  $("#away-test").addEventListener("click", sendTest);
  // Send to Chief: turning it on unfolds its consent in place, saved only with Turn on; off is
  // saved at once (its socket closes).
  const sendSwitch = form.send_to_chief;
  sendSwitch.addEventListener("change", () => {
    if (sendSwitch.checked) {
      $("#send-how").hidden = true;
      $("#send-problem").hidden = true;
      $("#send-consent").hidden = false;
    } else if (!$("#send-consent").hidden) {
      renderSendLine();
    } else {
      saveSend(false);
    }
  });
  $("#send-on").addEventListener("click", () => saveSend(true));
  // Keep my PRs moving: its Options are offered while it is on (before it is saved, to set it
  // up). What a line says follows its draft: Ask first, Sandbox, Keep my PRs moving's levels.
  form.proactive.addEventListener("change", showPrsLine);
  for (const input of [form.ask_first, form.worker_sandbox]) input.addEventListener("change", generalWords);
  $("#prs-more").addEventListener("input", generalWords);
  $("#prs-more").addEventListener("change", generalWords);
  // Autopilot: its Options are offered while its line is ticked (before it is saved too).
  form.autopilot.addEventListener("change", showAutopilotLine);
  $("#autopilot-more").addEventListener("input", generalWords);
  $("#send-cancel").addEventListener("click", () => {
    renderSendLine();
    sendSwitch.focus();
  });
  $("#slack-token-save").addEventListener("click", async () => {
    try {
      settingsError("");
      await saveToken();
    } catch (error) {
      settingsError(error.message);
    }
  });
  $("#slack-token-remove").addEventListener("click", async () => {
    try {
      const snapshot = await api("/api/slack/token", { method: "DELETE" });
      state.snapshot = { ...state.snapshot, ...snapshot };
      showSlackToken();
      renderConnectionList();
    } catch (error) {
      settingsError(error.message);
    }
  });
  form.slack_delegate_emoji.addEventListener("input", () => {
    const emoji = form.slack_delegate_emoji.value.trim().replace(/^:|:$/g, "") || "robot_face";
    $("#slack-delegate-reply").textContent = `Reply directly when you react with :${emoji}:`;
  });
}
// The page's state changed while Settings is open: what it says, not what the owner wrote.
export function settingsStateChanged() {
  agentsStateChanged();
  generalWords();  // (Which agents are on: their names on General's lines.)
  drawSandboxTrust();
}
export function setupChanged() {
  agentSetupChanged();
  drawSandboxTrust();
}
// Copilot's sandbox certificate not trusted (gh can't reach GitHub inside the sandbox): one
// click trusts it (agentsetup.js: what it runs, asked first); "Trusting…" while it runs.
function drawSandboxTrust() {
  const sandbox = state.snapshot?.sandbox || {};
  const button = $("#sandbox-trust");
  if (!button) return;
  button.hidden = !(sandbox.supported && sandbox.gh_trusted === false);
  button.textContent = setupWords("sandbox") || "Trust the sandbox's certificate";
  // Trusted since (from here, or in a terminal): its line goes too.
  const hint = $("#sandbox-hint");
  if (button.hidden && hint?.hasAttribute("data-trust")) hint.hidden = true;
}
function startOfDay(date) {
  const day = new Date(date);
  day.setHours(0, 0, 0, 0);
  return day.getTime() / 1000;
}
export async function refreshUsageLink() {
  try {
    const usage = await api(`/api/usage?since=${startOfDay(new Date())}`);
    $("#usage-today").textContent = `${usage.total.credits.toFixed(1)} credits today`;
    $("#usage-button").title = `Usage: ${usage.total.credits.toFixed(1)} credits today`;
  } catch {
    $("#usage-today").textContent = "";
    $("#usage-button").title = "Usage";
  }
}
function usageTable(title, rows, label, onclick) {
  if (!rows.length) return null;
  return el(
    "div",
    {},
    el("h3", { text: title }),
    el(
      "table",
      {},
      ...rows.map((row) =>
        el(
          "tr",
          { class: onclick ? "link" : "", onclick: onclick ? () => onclick(row) : undefined },
          el("td", { text: label(row) }),
          el("td", { class: "num", text: plural(row.calls, "call") }),
          el("td", { class: "num", text: `${row.credits.toFixed(1)} credits` }),
        ),
      ),
    ),
  );
}
// One other agent's usage in the range: its total on its heading, its models' tokens (a range
// as a range), and apart, muted, each time it stopped mid-turn, what it spent then unknown.
function agentUsage(part) {
  const count = (tokens) => tokens.toLocaleString();
  const amount = (least, most) => (least < most ? `${count(least)}–${count(most)} tokens` : `${count(most)} tokens`);
  const clock = (at) => {
    const when = new Date(at * 1000);
    const today = when.toDateString() === new Date().toDateString();
    return when.toLocaleString([], today ? { hour: "numeric", minute: "2-digit" } : { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };
  const least = part.models.reduce((sum, row) => sum + row.least, 0);
  const most = part.models.reduce((sum, row) => sum + row.tokens, 0);
  return el(
    "div",
    { class: "agent-usage" },
    el("h3", {}, el("span", { text: part.name }), part.models.length ? el("span", { class: "num", text: amount(least, most) }) : ""),
    part.models.length ? el("table", {}, ...part.models.map((row) => el("tr", {}, el("td", { text: row.model || "unknown model" }), el("td", { class: "num", text: amount(row.least, row.tokens) })))) : "",
    ...part.unknown.map((row) => el("small", { class: "unknown", text: `Unknown: ${row.title || "a deleted worker"} — ${part.name} crashed at ${clock(row.at)}` })),
  );
}
// Tokens in a few digits: 812, 45k, 3.2M.
function tokens(count) {
  if (count >= 1e6) return `${(count / 1e6).toFixed(1)}M`;
  return count >= 1e3 ? `${Math.round(count / 1e3)}k` : `${count}`;
}
const credits = (value) => `${value.toFixed(1)} credits`;
// The sessions that spent the most. A worker by its name, with the tasks its spend went to
// under it, folded (its name is its current work's: the whole spend is not that task's).
function sessionTable(rows) {
  if (!rows.length) return null;
  const open = (row) => {
    $("#usage-dialog").close();
    select(row.session_id);
  };
  const table = el("table", {});
  for (const row of rows) {
    const tasks = row.tasks || [];
    const label = el("td", {}, el("span", { text: `${row.title || "Deleted session"}${row.closed_at ? " (closed)" : ""}` }));
    const subrows = tasks.map((task) =>
      el("tr", { class: "sub", hidden: true }, el("td", { text: task.title }), el("td", { class: "num", text: plural(task.calls, "call") }), el("td", { class: "num", text: credits(task.credits) })),
    );
    if (row.worker) {
      const what = tasks.length ? `worker · ${plural(tasks.length, "task")}` : "worker";
      const toggle = el("button", {
        type: "button",
        class: "link quiet",
        "aria-expanded": "false",
        text: what,
        onclick: (event) => {
          event.stopPropagation();
          const shown = toggle.getAttribute("aria-expanded") !== "true";
          toggle.setAttribute("aria-expanded", String(shown));
          for (const sub of subrows) sub.hidden = !shown;
        },
      });
      label.append(tasks.length ? toggle : el("small", { class: "muted", text: what }));
    }
    table.append(el("tr", { class: "link", onclick: () => open(row) }, label, el("td", { class: "num", text: plural(row.calls, "call") }), el("td", { class: "num", text: credits(row.credits) })), ...subrows);
  }
  return el("div", {}, el("h3", { text: "By session" }), table);
}
// The tasks that spent the most; last, what no task spent.
function taskTable(rows, untasked) {
  const tasks = rows.filter((row) => row.task_id).slice(0, 50);
  const none = rows.find((row) => !row.task_id);
  if (!tasks.length) return null;
  const who = untasked.by.map((part) => part.who);
  return el(
    "div",
    {},
    el("h3", { text: "By task" }),
    el(
      "table",
      {},
      ...tasks.map((row) =>
        el(
          "tr",
          {
            class: "link",
            onclick: () => {
              $("#usage-dialog").close();
              select(row.session_id);
            },
          },
          el("td", { text: row.title }),
          el("td", { class: "num", text: plural(row.calls, "call") }),
          el("td", { class: "num", text: credits(row.credits) }),
        ),
      ),
      none ? el("tr", {}, el("td", {}, el("span", { text: "No task" }), el("small", { class: "muted", text: names(who) })), el("td", { class: "num", text: plural(none.calls, "call") }), el("td", { class: "num", text: credits(none.credits) })) : "",
    ),
  );
}
// "Router, Watch for you, and 2 more".
function names(list) {
  return list.length > 3 ? `${list.slice(0, 3).join(", ")} and ${list.length - 3} more` : list.join(", ");
}
// What the range's credits went to: each task with spend in it counted once, with what it
// spent in the range; and what no task spent, by whom.
function spendTable(spend) {
  const rows = [];
  const line = (label, count, amount) => rows.push(el("tr", {}, el("td", { text: label }), el("td", { class: "num", text: count }), el("td", { class: "num", text: amount })));
  const { accepted, in_progress: going, stopped, untasked } = spend;
  if (accepted.tasks) line("Finished and accepted", plural(accepted.tasks, "task"), `${credits(accepted.each)} each`);
  if (going.tasks) line("In progress", plural(going.tasks, "task"), `${credits(going.credits)} so far`);
  if (stopped.tasks) line("Failed or cancelled", plural(stopped.tasks, "task"), credits(stopped.credits));
  if (untasked.credits >= 0.05) line("Not tied to a task", names(untasked.by.map((part) => part.who)), credits(untasked.credits));
  if (!rows.length) return null;
  return el("div", {}, el("h3", { text: "Tasks with spend in this range" }), el("table", { class: "spend" }, ...rows));
}
export async function showUsage(range) {
  const form = $("#usage-dialog form");
  let since;
  let until = Date.now() / 1000 + 1;
  // Dates show only for a custom range.
  form.querySelector(".custom").hidden = range !== "custom";
  if (range === "today") since = startOfDay(new Date());
  else if (range === "custom") {
    for (const button of form.querySelectorAll("[data-range]")) button.classList.toggle("active", button.dataset.range === range);
    if (!form.from.value) {
      form.from.focus();
      return;
    }
    since = startOfDay(new Date(`${form.from.value}T00:00`));
    if (form.to.value) until = startOfDay(new Date(`${form.to.value}T00:00`)) + 86400;
  } else since = Date.now() / 1000 - Number(range) * 86400;
  for (const button of form.querySelectorAll("[data-range]")) button.classList.toggle("active", button.dataset.range === range);
  try {
    const usage = await api(`/api/usage?since=${since}&until=${until}`);
    const total = usage.total;
    // Premium requests as Copilot counts them: a call a user message started, at its model's
    // multiplier (not the agent's next calls); calls kept before that was known, apart.
    const unread = Math.round(total.premium_unread);
    const compacting = total.compactions ? `, ${total.compactions} of them to compact` : "";
    $("#usage-total").replaceChildren(
      el("strong", { text: credits(total.credits) }),
      el("span", { text: `${Math.round(total.premium).toLocaleString()} premium requests (as Copilot counts them)${unread ? `, up to ${unread.toLocaleString()} more from older calls` : ""}` }),
      el("span", { text: `${total.calls.toLocaleString()} model calls${compacting}` }),
      el("span", { class: "tokens", text: `Tokens: ${tokens(total.output_tokens)} output, ${tokens(total.fresh_input_tokens)} fresh input, ${tokens(total.cache_read_tokens)} cache reads, ${tokens(total.cache_write_tokens)} cache writes; ${tokens(total.context_per_call)} context a call on average` }),
    );
    const roles = { router: "Router", slack: "Slack", worker: "Workers", watchdog: "Watchdog" };
    $("#usage-tables").replaceChildren(
      ...[
        sessionTable(usage.by_session),
        taskTable(usage.by_task, usage.spend.untasked),
        spendTable(usage.spend),
        usageTable("By model", usage.by_model, (row) => row.model || "unknown"),
        usageTable("By role", usage.by_role, (row) => roles[row.role] || row.role),
      ].filter(Boolean),
    );
    // Other coding agents', each a part of its own (tokens, never credits): only with rows.
    const agents = (usage.agents || []).map(agentUsage);
    if (!total.calls) $("#usage-tables").replaceChildren(el("div", { class: "line", text: agents.length ? "No Copilot model calls in this range." : "No model calls in this range." }));
    $("#usage-tables").append(...agents);
  } catch (error) {
    toast(error.message, "error");
  }
}
const SLACK_TOKEN_ENV = "CHIEFOFSTAFF_SLACK_USER_TOKEN";
export function slackStatusText() {
  const slack = state.snapshot.slack;
  const env = slack.token_source === "env";
  const parts = [env ? `Set by ${SLACK_TOKEN_ENV}: change it there.` : slack.token_saved ? "Token saved." : "No token yet."];
  if (slack.error) parts.push(slack.error);
  else if (slack.connected) parts.push(`Watching${slack.last_checked ? `, checked ${since(slack.last_checked)}` : ""}.`);
  if ((slack.token_saved || env) && slack.connected && !slack.can_send) parts.push("This token cannot send messages (chat:write).");
  return parts.join(" ");
}
// Every field of every page, connection and fold, from what is saved: Settings is one form, its
// pages only shown and hidden, so a draft stays wherever the owner goes until Save or Cancel
// (Cancel closes; the next opening fills every field again).
export function fillSettings() {
  const form = $("#settings-form");
  const settings = state.snapshot.settings;
  shown = JSON.parse(JSON.stringify(settings));
  for (const input of form.querySelectorAll("[data-dirty]")) delete input.dataset.dirty;
  for (const input of form.querySelectorAll("[aria-invalid]")) input.removeAttribute("aria-invalid");
  closeConnection({ focus: false });
  form.projects.value = settings.projects.join("\n");
  const missing = state.snapshot.missing_projects || [];
  $("#projects-missing").textContent = missing.length ? `Folder not found: ${missing.join(", ")}. Restore it, or remove it here.` : "";
  $("#projects-missing").hidden = !missing.length;
  form.code_folder.value = settings.code_folder;
  form.scratch_directory.value = settings.scratch_directory;
  form.scratch_directory.placeholder = settings.code_folder ? `${settings.code_folder}/.chief` : `${state.snapshot.data_directory}/work/workspaces`;
  form.max_workers.value = settings.max_workers;
  form.fork_busy.checked = Boolean(settings.fork_busy);
  // Ask first: worker_yolo, the other way round (checked: workers ask).
  form.ask_first.checked = !settings.worker_yolo;
  form.worker_sandbox.checked = settings.worker_sandbox;
  form.shared_caches.checked = settings.shared_caches;
  form.sandbox_writable.value = (settings.sandbox_writable || []).join("\n");
  const sandbox = state.snapshot.sandbox || {};
  const hint = !sandbox.supported
    ? "This computer cannot run the sandbox (macOS needs sandbox-exec; Linux needs bwrap), so workers run unsandboxed."
    : sandbox.gh_trusted === false
      ? "To let Copilot workers use gh inside the sandbox, trust Copilot's certificate once: copilot sandbox ca create && copilot sandbox ca trust"
      : "";
  $("#sandbox-hint").textContent = hint;
  $("#sandbox-hint").hidden = !hint;
  $("#sandbox-hint").toggleAttribute("data-trust", Boolean(sandbox.supported && sandbox.gh_trusted === false));
  drawSandboxTrust();
  form.worker_idle_hours.value = settings.worker_idle_hours;
  form.stall_hours.value = settings.stall_hours ?? 4;
  form.mac_notifications.checked = settings.mac_notifications;
  // Only where desktop notifications can show (macOS): the server says when they can't.
  $("#notifications-line").hidden = state.snapshot.desktop_notifications === false;
  form.other_sessions.checked = Boolean(settings.other_sessions);
  form.update_notice.checked = settings.update_notice !== false;
  form.proactive.checked = Boolean(settings.proactive);
  $("#prs-more").replaceChildren();
  showPrsLine();
  // Every Options and More folded again, as each opening starts.
  setUnfold("sandbox-more", false);
  setUnfold("prs-more", false);
  $("#general-more").open = false;
  generalWords();
  form.autopilot.checked = Boolean(settings.autopilot);
  $("#autopilot-more").replaceChildren();
  setUnfold("autopilot-more", false);
  showAutopilotLine();
  // When none of them can say where it stands, its line says why.
  const why = settings.other_sessions ? state.othersWhy : "";
  $("#others-why").textContent = why;
  $("#others-why").hidden = !why;
  form.slack_enabled.checked = settings.slack_enabled;
  form.slack_token.value = "";
  form.slack_poll_seconds.value = settings.slack_poll_seconds;
  form.slack_delegate_emoji.value = settings.slack_delegate_emoji ? `:${settings.slack_delegate_emoji}:` : "";
  form.slack_delegate_reply.checked = settings.slack_delegate_reply !== false;
  document.getElementById("slack-delegate-reply").textContent = `Reply directly when you react with :${settings.slack_delegate_emoji || "robot_face"}:`;
  form.github_enabled.checked = settings.github_enabled;
  form.github_repos.value = settings.github_repos.join("\n");
  // Agents & models: Copilot's row (its workers' model, effort and limit, its command), each
  // other agent's, and Chief of Staff's own sessions.
  renderAgentsPage();
  form.digest_time.value = settings.digest_time;
  form.profile.value = state.snapshot.profile || "";
  form.setup_notes.value = notesText();
  form.setup_notes.dataset.initial = form.setup_notes.value;
  $("#profile-draft").hidden = !state.snapshot.profile_draft;
  youFolds();
  form.learn_from_work.checked = settings.learn_from_work;
  form.learn_checks.checked = settings.learn_checks;
  renderLearning();
  form.paused.checked = settings.paused;
  form.report_footer.checked = settings.report_footer !== false;
  $("#footer-line").hidden = !("report_footer" in settings);
  form.skill_folders.value = (settings.skill_folders || []).join("\n");
  form.plugins.value = (settings.plugins || []).join("\n");
  // Those off (saved, but Copilot would refuse them) are shown with the others, to be fixed.
  const servers = { ...(settings.mcp_servers || {}), ...(settings.mcp_servers_off || {}) };
  form.mcp_servers.value = Object.keys(servers).length ? JSON.stringify(servers, null, 2) : "";
  $("#mcp-error").textContent = mcpNote(servers);
  $("#mcp-error").hidden = !$("#mcp-error").textContent;
  $("#watch-list").replaceChildren(...(settings.watches || []).map(watchRow));
  const [from, to] = (settings.watch_hours || "09:00-18:00").split("-");
  $("#watch-from").value = from;
  $("#watch-to").value = to;
  form.watch_minutes.value = settings.watch_minutes ?? 5;
  form.watch_after_hours_minutes.value = settings.watch_after_hours_minutes ?? 30;
  form.away_notify.checked = Boolean(settings.away_notify);
  form.away_minutes.value = settings.away_minutes ?? 10;
  form.away_how.value = settings.away_how || "";
  $("#skill-paste-box").hidden = true;
  renderSendLine();
  renderConnections();
  showSlackToken();
  const github = state.snapshot.github;
  $("#github-status").textContent = github.error || (github.connected ? `Watching${github.last_checked ? `, checked ${since(github.last_checked)}` : ""}.` : "");
  $("#data-directory").textContent = state.snapshot.data_directory;
  $("#settings-error").hidden = true;
}
// Connections: what each section has, in its summary line; what runs, as it changes.
function watchRow(watch) {
  return el(
    "li",
    {},
    el("input", { class: "what", value: watch.what, placeholder: "What to look out for, e.g. “questions about the release in #eng”", "aria-label": "What to watch for" }),
    el("input", { class: "how", value: watch.how || "", placeholder: "How (optional): e.g. Glean search", "aria-label": "How to look" }),
    el("label", { class: "check", title: "On" }, el("input", { class: "on", type: "checkbox", checked: watch.enabled !== false, "aria-label": "On" })),
    el("button", { type: "button", class: "link remove", text: "×", title: "Remove", "aria-label": "Remove watch", onclick: (event) => {
      event.currentTarget.closest("li").remove();
      renderConnectionList();
    } }),
  );
}
export function mcpServers() {
  const text = $("#settings-form").mcp_servers.value.trim();
  if (!text) return {};
  let value;
  try {
    value = JSON.parse(text);
  } catch (error) {
    throw new Error(`MCP servers: not valid JSON (${error.message}).`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("MCP servers: write an object, {\"name\": {…}}.");
  for (const [name, config] of Object.entries(value)) {
    // One saved before and still off, as it was: kept off (the server says so), not refused.
    if (keptOff(name, config)) continue;
    const problem = mcpProblem(config);
    if (problem) throw new Error(`MCP server ${name}: ${problem}`);
  }
  return value;
}
// A key order of its own: the same configuration however its fields are ordered.
const canonical = (value) => JSON.stringify(value, (_, part) => (part && typeof part === "object" && !Array.isArray(part) ? Object.fromEntries(Object.entries(part).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) : part));
function keptOff(name, config) {
  const off = state.snapshot.settings.mcp_servers_off || {};
  return name in off && canonical(off[name]) === canonical(config);
}
// What is off of the servers written, and why: saved before, they are kept for the owner to
// fix, and workers do not get them until then.
export function mcpNote(servers) {
  return Object.entries(servers)
    .filter(([name, config]) => keptOff(name, config))
    .map(([name, config]) => `MCP server ${name} is off (workers do not get it) until fixed: ${mcpProblem(config) || "it would be refused."}`)
    .join(" ");
}
// What Copilot would refuse in one server's configuration, as models.py's mcp_problem says it
// (one wrong field and it never starts on a message).
function mcpProblem(config) {
  if (!config || typeof config !== "object" || Array.isArray(config)) return 'write an object such as {"command": "…"} or {"url": "…"}.';
  const filled = (key) => typeof config[key] === "string" && Boolean(config[key].trim());
  const lacking = (key) => (key in config ? "empty" : "missing");
  const kind = config.type;
  if ("type" in config && !["local", "stdio", "http", "sse"].includes(kind)) return "type must be local, stdio, http or sse.";
  // A local server runs a command; a remote one (http when no type is given) has a url.
  if (kind === undefined && !filled("command") && !filled("url")) return "give a command (local) or a url (remote).";
  if (["http", "sse"].includes(kind) || (kind === undefined && !filled("command"))) {
    if (!filled("url")) return `url is ${lacking("url")}: type "${kind}" needs one (https://…).`;
    if (!/^https?:\/\/[^\s/?#]+\S*$/i.test(config.url.trim())) return "url must be an http:// or https:// address.";
  } else if (!filled("command")) {
    return `command is ${lacking("command")}: type "${kind}" needs the program to run.`;
  }
  const texts = (v) => Array.isArray(v) && v.every((item) => typeof item === "string");
  const table = (v) => !!v && typeof v === "object" && !Array.isArray(v) && Object.values(v).every((item) => typeof item === "string");
  const string = (v) => typeof v === "string";
  const checks = {
    tools: [texts, 'a list of tool names in quotes (["*"] for all)'],
    args: [texts, "a list of strings"],
    env: [table, "an object of strings"],
    headers: [table, "an object of strings"],
    cwd: [string, "a string"],
    working_directory: [string, "a string"],
    timeout: [(v) => Number.isInteger(v) && v > 0, "a whole number of milliseconds"],
  };
  const bad = Object.entries(checks).find(([key, [fits]]) => key in config && !fits(config[key]));
  return bad ? `${bad[0]} must be ${bad[1][1]}.` : "";
}
async function addSkill(content) {
  settingsError("");
  try {
    const skill = await api("/api/skills", { method: "POST", body: { content } });
    toast(`Skill added: ${skill.name}.`);
    await loadState();
    renderConnections();
    return true;
  } catch (error) {
    settingsError(error.message);
    return false;
  }
}
// A button in Settings that does something now (not on Save): the snapshot it returns.
async function settingsAction(path, done) {
  settingsError("");
  try {
    state.snapshot = { ...state.snapshot, ...(await api(path, { method: "POST" })) };
    renderConnections();
    if (done) toast(done);
  } catch (error) {
    settingsError(error.message);
  }
}
const PLUGIN_STATE = { pending: "waiting", installing: "installing…", installed: "installed", failed: "failed" };
// What the last scan could not cover, watch by watch: those it did not search at all (a problem
// kept it from running them, so the server marked each with that problem) apart from those it
// searched only in part.
export function watchGaps(last = {}) {
  const problems = (Array.isArray(last.problems) ? last.problems.join("; ") : String(last.problems || "")).trim();
  const none = (item) => item.searched === false || (problems && item.reason && problems.startsWith(item.reason.trim()));
  const gaps = last.incomplete || [];
  return { problems, missed: gaps.filter(none), partial: gaps.filter((item) => !none(item)) };
}
// What watches could not check and no scan will (the server gave up on it: older than its
// source can search, or partly covered scan after scan), the newest first: kept in sight, as a
// partial scan is no complete one.
export function coverageGaps(connector = {}) {
  return (connector.gaps || []).map((gap) => gap.text || `Could not check “${gap.watch}”`);
}
// Connections: a row each, its status in one line and at most one action (connectionStatus);
// each one's details as they stand (its lists, its last scan or test), the fields untouched.
export function renderConnections() {
  const snap = state.snapshot;
  if (!snap) return;
  const settings = snap.settings;
  const abilities = snap.abilities || { skills: [], plugins: {} };
  $("#skills").replaceChildren(
    ...abilities.skills.map((skill) =>
      el(
        "li",
        {},
        el("div", { class: "row-text" }, el("strong", { text: skill.name }), el("small", { text: skill.description })),
        el("button", { type: "button", class: "link", text: "Remove", onclick: () => settingsAction(`/api/skills/${encodeURIComponent(skill.name)}/remove`, "Skill removed.") }),
      ),
    ),
  );
  if (!abilities.skills.length) $("#skills").append(el("li", { class: "empty-note", text: "None uploaded." }));
  const plugins = Object.entries(abilities.plugins || {});
  $("#plugin-status").replaceChildren(
    ...plugins.map(([source, status]) =>
      el(
        "li",
        { title: status.error || status.note || "" },
        el("div", { class: "row-text" }, el("span", { class: "mono", text: source }), status.error || status.note ? el("small", { text: status.error || status.note }) : null),
        el("span", { class: `chip ${status.state === "installed" ? "done" : status.state === "failed" ? "failed" : "progress"}`, text: PLUGIN_STATE[status.state] || status.state }),
      ),
    ),
  );
  $("#plugins-retry").hidden = !plugins.some(([, status]) => status.state === "failed");
  const connector = snap.connector || {};
  const last = connector.last || {};
  // What the last scan could not do, in one line: each reason once.
  const gaps = watchGaps(last);
  const reasons = [...new Set(gaps.missed.map((item) => item.reason).filter(Boolean))];
  const watches = (list) => `${list.length === 1 ? "watch" : "watches"} ${list.map((item) => item.watch).join(", ")}`;
  const problems = [
    gaps.problems && !reasons.some((reason) => gaps.problems.startsWith(reason)) ? gaps.problems : "",
    gaps.missed.length ? `${watches(gaps.missed)} not searched${reasons.length ? `: ${reasons.join("; ")}` : ""}` : "",
    ...gaps.partial.map((item) => `watch ${item.watch} only partly covered: ${item.reason}`),
  ].filter(Boolean).join(" · ");
  const uncovered = coverageGaps(connector).join(" · ");
  // Scans that failed since the last that worked (bug E102): that, not what the last one found.
  const failed = connector.failed;
  // An account it could not use, not connected yet (E116): that, not "0 found".
  const connection = connector.connection_needed;
  const connectionText = connection
    ? `Connection needed: ${connection.source ? `${connection.source} (${connection.server})` : connection.server}${connection.retrying ? " · retrying" : ""}`
    : "";
  const scanned = connector.scanning
    ? "Scanning now…"
    : connectionText
      ? `${connectionText}${connector.scanned_at ? ` · last scanned ${since(connector.scanned_at)}` : ""}`
      : failed
      ? `Scan failed ${since(failed.at)}${failed.count > 1 ? ` (${failed.count} in a row)` : ""}: ${failed.reason}${connector.scanned_at ? ` · last scanned ${since(connector.scanned_at)}` : ""}`
      : connector.scanned_at
        ? `Scanned ${since(connector.scanned_at)}${last.found !== undefined ? ` · ${last.found} found` : ""}${problems ? ` · ${problems}` : ""}`
        : "";
  $("#watch-state").textContent = [scanned, uncovered].filter(Boolean).join(" · ");
  // After hours (the schedule above): how often it scans now, and why; "not scanning" only when
  // after hours is 0. Nothing to say while it scans, or with no watch on.
  const resting = { weekend: "Weekend", outside_hours: "Outside work hours" }[connector.after_hours_reason];
  const watching = (settings.watches || []).some((watch) => watch.enabled !== false);
  const pace = Number(settings.watch_after_hours_minutes ?? 30);
  const restingText = !resting || connector.scanning || !watching ? "" : pace > 0 ? `${resting}: every ${pace} min (after-hours setting)` : `Not scanning now: ${resting.toLowerCase()}, and after hours is 0`;
  $("#watch-resting").textContent = restingText;
  $("#watch-resting").hidden = !restingText;
  $("#watch-state").classList.toggle("error", Boolean(connection || problems || uncovered || (failed && !connector.scanning)));
  $("#watch-scan").disabled = Boolean(connector.scanning);
  $("#watch-activity").hidden = !connector.session_id;
  const away = connector.away || {};
  // What away is read from on this system: keyboard and mouse (macOS, Windows), else this page.
  $("#away-after").textContent = away.idle === "app" ? "Away after (minutes without using Chief of Staff)" : "Away after (minutes without keyboard or mouse)";
  const tried = away.last || {};
  // What this way can do, as tried (the test, and notifications and reads as they happen).
  const can = away.can || {};
  const coverage = can.coverage || {};
  const reached = tried.at ? (tried.ok ? `Reached you${tried.where ? ` via ${tried.where}` : ""} ${since(tried.at)}` : `Could not reach you ${since(tried.at)}: ${tried.error || "unknown error"}`) : "";
  const limited = settings.away_notify && coverage.limited ? `Coverage limited: ${coverage.detail}` : "";
  $("#away-state").textContent = [reached, limited].filter(Boolean).join(" · ");
  $("#away-state").classList.toggle("error", Boolean((tried.at && !tried.ok) || limited));
  const STATES = { yes: "Yes", no: "No", untested: "Not tested" };
  const row = (label, entry = {}) => [
    entry.state || "untested",
    `${label}: ${STATES[entry.state] || STATES.untested}${entry.detail ? ` (${entry.detail})` : ""}${entry.at ? `, tested ${since(entry.at)}` : ""}`,
  ];
  const rows = !settings.away_notify
    ? []
    : [
        ...(can.send_only ? [["no", "Send only: your replies there are not read. Answer in Chief of Staff."]] : []),
        row("Send", can.send),
        row("Delivery", can.delivery),
        row("Read replies", can.replies),
        row("New commands", can.commands || { state: "no" }),
      ];
  $("#away-can").replaceChildren(...rows.map(([status, text]) => el("li", { class: status === "yes" ? "ok" : status, text })));
  $("#away-can").hidden = !rows.length;
  $("#slack-activity").hidden = !sessions().some((session) => session.role === "slack");
  const watchdog = $("#watchdog-activity");
  if (watchdog) watchdog.hidden = !sessions().some((session) => session.role === "watchdog");
  renderConnectionList();
}
// A connection's status in one line, from what its details say (shortened): its tone (ok: it
// runs, warn: something needs a look, bad: it fails) and its one action, only when there is
// one next step. `written`: the watches as the form has them now.
export function connectionStatus(key, snap, { written, unsaved: drafts = false } = {}) {
  const settings = snap.settings || {};
  const connector = snap.connector || {};
  if (key === "slack") {
    const slack = snap.slack || {};
    if (!settings.slack_enabled) return { text: "Off" };
    if (slack.error) return { text: `Problem: ${slack.error}`, tone: "bad" };
    if (!slack.token_saved && slack.token_source !== "env") return { text: "Token needed", tone: "warn", action: ["setup", "Set up"] };
    if (slack.connected) return { text: `Watching${slack.last_checked ? ` · checked ${since(slack.last_checked)}` : ""}`, tone: "ok" };
    return { text: "On" };
  }
  if (key === "github") {
    const github = snap.github || {};
    const repos = (settings.github_repos || []).length;
    const listed = repos ? `${repos} ${repos === 1 ? "repository" : "repositories"}` : "";
    if (!settings.github_enabled) return { text: ["Off", listed ? `${listed} listed` : ""].filter(Boolean).join(" · ") };
    if (github.error) return { text: `Problem: ${github.error}`, tone: "bad" };
    if (github.connected) return { text: `Watching ${listed || "no repositories"}${github.last_checked ? ` · checked ${since(github.last_checked)}` : ""}`, tone: "ok" };
    return { text: ["On", listed].filter(Boolean).join(" · ") };
  }
  if (key === "watches") {
    const watches = written || settings.watches || [];
    if (!watches.length) return { text: "None" };
    const on = watches.filter((watch) => watch.enabled !== false).length;
    const scan = on ? ["scan", "Scan now"] : null;
    // Written, not saved yet: how many are on (its last scan was of others).
    if (drafts) return { text: `${on} on`, action: scan };
    const connection = connector.connection_needed;
    if (connector.scanning) return { text: `${on} on · scanning now…`, tone: "ok" };
    if (connection) return { text: `Connection needed: ${connection.source || connection.server}`, tone: "warn", action: ["card", "Open card"] };
    const failed = connector.failed;
    if (failed) return { text: `Scan failed ${since(failed.at)}: ${failed.reason}`, tone: "bad", action: scan };
    const { missed, partial } = watchGaps(connector.last);
    const gaps = [missed.length ? `${missed.length} not searched` : "", partial.length ? `${partial.length} only partly covered` : ""].filter(Boolean).join(" · ");
    const found = connector.last?.found;
    const scanned = connector.scanned_at ? `scanned ${since(connector.scanned_at)}${found !== undefined ? ` · ${found} found` : ""}` : "not scanned yet";
    return { text: [`${on} on`, scanned, gaps].filter(Boolean).join(" · "), tone: gaps ? "warn" : on && connector.scanned_at ? "ok" : "", action: scan };
  }
  if (key === "away") {
    const away = connector.away || {};
    const tried = away.last || {};
    if (!settings.away_notify) return { text: "Off" };
    // Its last notice not delivered (bug E72): said, in red, until one is.
    if (tried.at && !tried.ok) return { text: `Away notice not delivered · ${since(tried.at)}`, tone: "bad", action: ["test", "Send a test"] };
    return { text: ["On", away.where, away.can?.send_only ? "Send only" : ""].filter(Boolean).join(" · "), tone: "ok" };
  }
  if (key === "tools") {
    const abilities = snap.abilities || { skills: [], plugins: {} };
    const servers = Object.keys(settings.mcp_servers || {}).length;
    const off = Object.keys(settings.mcp_servers_off || {});
    // As many as are listed (one written twice is listed once).
    const plugins = abilities.plugins ? Object.values(abilities.plugins) : [...new Set(settings.plugins || [])];
    const failing = plugins.filter((status) => status?.state === "failed").length;
    const counts = [
      (abilities.skills || []).length ? plural(abilities.skills.length, "skill") : "",
      plugins.length && !failing ? plural(plugins.length, "plugin") : "",
      servers + off.length ? plural(servers + off.length, "MCP server") : "",
    ].filter(Boolean);
    if (failing) return { text: [`${plural(failing, "plugin")} could not be installed`, ...counts].join(" · "), tone: "warn", action: ["retry", "Retry"] };
    if (off.length) return { text: [`MCP server ${off[0]} is off until fixed`, ...counts.filter((part) => !part.includes("MCP"))].join(" · "), tone: "warn" };
    return { text: counts.join(" · ") || "None added" };
  }
  if (key === "send") {
    if (!settings.send_to_chief) return { text: "Off" };
    if (snap.send_problem) return { text: `Problem: ${snap.send_problem}`, tone: "bad" };
    return { text: "On", tone: "ok" };
  }
  return { text: "" };
}
// What the owner changed in a connection's details and has not saved: said on its row (a field
// that saves at once, Send to Chief's switch, aside).
function unsaved(key, written) {
  if (key === "watches" && !sameWatches(written, state.snapshot.settings.watches || [])) return true;
  return [...document.querySelectorAll(`[data-conn-detail="${key}"] [data-dirty]`)].some((input) => input.name !== "send_to_chief");
}
// Saved at once (Scan now, Send a test, Save token): those fields are no longer drafts.
function savedNow(selector) {
  for (const input of document.querySelectorAll(selector)) delete input.dataset.dirty;
}
const CONNECTION_NAMES = { slack: "Slack", github: "GitHub reviews", watches: "Watches", away: "When you're away", tools: "Skills, plugins and MCP servers", send: "Send to Chief from the command line" };
export function renderConnectionList() {
  const snap = state.snapshot;
  if (!snap || !$("#conn-list")) return;
  const written = watchFields().watches;
  for (const row of document.querySelectorAll("#conn-list .conn")) {
    const key = row.dataset.conn;
    const drafts = unsaved(key, written);
    const status = connectionStatus(key, snap, { written: key === "watches" ? written : undefined, unsaved: drafts });
    const text = `${status.text}${drafts ? " · not saved yet" : ""}`;
    const said = row.querySelector(".state");
    said.textContent = text;
    said.className = `state${status.tone ? ` ${status.tone}` : ""}`;
    row.querySelector(".conn-open").setAttribute("aria-label", `${CONNECTION_NAMES[key]}: ${text}`);
    const act = row.querySelector(".conn-act");
    act.hidden = !status.action;
    if (status.action) {
      act.dataset.act = status.action[0];
      act.textContent = status.action[1];
      act.setAttribute("aria-label", `${status.action[1]}: ${CONNECTION_NAMES[key]}`);
    }
    const head = document.querySelector(`[data-conn-state="${key}"]`);
    if (head) {
      head.textContent = text;
      head.className = `state${status.tone ? ` ${status.tone}` : ""}`;
    }
  }
}
// A row's one action, as its details would do it.
async function connectionAction(key, act) {
  if (act === "setup") {
    openConnection(key, { focus: false });
    $("#settings-form").slack_token.focus();
  } else if (act === "scan") {
    await scanNow();
  } else if (act === "card") {
    $("#settings-dialog").close();
    select(HOME);
  } else if (act === "test") {
    await sendTest();
  } else if (act === "retry") {
    await settingsAction("/api/plugins/retry", "Retrying the plugins that failed.");
  }
}
async function scanNow() {
  if (!(await saveWatches())) return;
  await settingsAction("/api/watch/scan", "Scanning now.");
}
async function sendTest() {
  if (!(await saveAway())) return;
  await settingsAction("/api/away/test", "");
}
// What sessions learned from the owner's feedback, and how often the owner now agrees.
const SCOPE = (scope) => (!scope ? "everywhere" : scope === "slack" ? "Slack replies" : `in ${scope.replace("project:", "").split("/").pop()}`);
// A long text shows one line, with "more" to read it all.
function clamped(text) {
  const node = el("span", { class: "clamp", text });
  if (text.length <= 90 && !text.includes("\n")) return node;
  const toggle = el("button", { type: "button", class: "link more", text: "more" });
  toggle.addEventListener("click", () => {
    const open = node.classList.toggle("open");
    toggle.textContent = open ? "less" : "more";
  });
  return el("span", { class: "clamped" }, node, toggle);
}
// Chief of Staff's own learning worker: the latest one, and whether one works now.
function lastLearner() {
  return [...sessions(), ...closedWorkers()].filter((session) => session.helper === "learner").sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
}
let pressed = false;  // Learn now was clicked and its answer has not come yet.
function learningNow() {
  const run = lastLearner();
  return pressed || Boolean(state.snapshot.habits?.running) || Boolean(run && !run.closed_at && ["working", "starting", "queued"].includes(run.status));
}
async function learnNow() {
  settingsError("");
  pressed = true;
  renderLearnRow();
  try {
    const snapshot = await api("/api/lessons/learn", { method: "POST" });
    if (snapshot?.habits && "running" in snapshot.habits) {
      // It goes on in the background: its progress and outcome show here.
      state.snapshot = { ...state.snapshot, ...snapshot };
    } else {
      // An older server answers once it is done reading.
      $("#settings-dialog").close();
      toast("Learning from your work. How you write applies at once; you check new habits first.");
    }
  } catch (error) {
    settingsError(error.message);
  } finally {
    pressed = false;
    renderLearning();
  }
}
function renderLearnRow() {
  const busy = learningNow();
  const button = $("#learn-now");
  button.disabled = busy;
  button.classList.toggle("busy", busy);
  button.textContent = busy ? "Learning…" : "Learn now";
  const habits = state.snapshot.habits || {};
  const last = habits.last || {};
  const status = $("#learn-state");
  status.classList.toggle("error", Boolean(!busy && last.error));
  status.textContent = busy ? "Learning now…" : last.error ? `Last run failed: ${last.error}` : last.text ? `${last.text}${last.at ? ` · ${since(last.at)}` : ""}` : "";
  $("#learn-last-run").hidden = !lastLearner();
  status.closest(".learn-run").hidden = !status.textContent && $("#learn-last-run").hidden;
}
export function renderLearning() {
  const learning = state.snapshot.learning;
  if (!learning) return;
  const parts = [];
  const labels = { recommendations: "recommendations you took", drafts: "drafts sent as written", results: "results accepted as they were" };
  for (const [key, label] of Object.entries(labels)) {
    const [yes, total] = learning.agreement.now[key];
    if (!total) continue;
    const [before, earlier] = learning.agreement.before[key];
    const trend = earlier ? ` (was ${Math.round((100 * before) / earlier)}%)` : "";
    parts.push(`${Math.round((100 * yes) / total)}% ${label}${trend}`);
  }
  $("#agreement").textContent = parts.length ? `Last ${learning.agreement.days} days: ${parts.join(" · ")}.` : "";
  $("#lessons").replaceChildren(
    ...learning.lessons.map((lesson) =>
      el(
        "li",
        {},
        el("div", { class: "row-text inline" }, clamped(lesson.text), el("small", { text: SCOPE(lesson.scope) })),
        el("button", { type: "button", class: "link", text: "Remove", onclick: () => memoryAction(`/api/lessons/${lesson.id}/remove`) }),
      ),
    ),
  );
  showList("#lessons", learning.lessons.length);
  const habits = state.snapshot.habits;
  if (habits) {
    $("#styles").replaceChildren(
      ...habits.styles.map((style) =>
        el(
          "li",
          {},
          el("div", { class: "row-text" }, el("span", {}, el("strong", { text: style.title }), el("small", { text: ` · v${style.version}` })), clamped(style.text)),
          el("button", { type: "button", class: "link", text: "Undo", title: style.version > 1 ? "Back to the previous version" : "Stop using it", onclick: () => memoryAction(`/api/style/${style.section}/undo`) }),
        ),
      ),
    );
    showList("#styles", habits.styles.length);
    const parts = Object.entries(OBSERVED)
      .filter(([kind]) => habits.observed[kind])
      .map(([kind, [one, many]]) => `${habits.observed[kind]} ${habits.observed[kind] === 1 ? one : many}`);
    const fresh = habits.new + learning.new_feedback;
    const errors = Object.values(habits.errors || {});
    $("#learn-status").textContent = [
      parts.length ? `Read ${parts.join(", ")}.` : "From Slack (or a tool here that reaches it, such as Glean), PR reviews, commits and Copilot sessions.",
      fresh ? `${fresh} new to learn from.` : "Nothing new to learn from.",
      errors.length ? `Could not read: ${errors[0]}` : "",
    ]
      .filter(Boolean)
      .join(" ");
  }
  const checks = state.snapshot.checks;
  if (checks) {
    $("#checks").replaceChildren(
      ...checks.checks.map((check) =>
        el(
          "li",
          { class: check.status === "disabled" ? "off" : "", title: `${check.why}\n${check.command}` },
          el("div", { class: "row-text" }, el("strong", { text: check.name }), el("small", { text: `${check.repo.split("/").pop()} · caught ${check.caught} of ${check.runs} run${check.runs === 1 ? "" : "s"}` })),
          el("button", { type: "button", class: "link", text: check.status === "active" ? "Turn off" : "Turn on", onclick: () => memoryAction(`/api/checks/${check.id}/${check.status === "active" ? "disable" : "enable"}`) }),
        ),
      ),
    );
    if (!checks.sandbox) $("#checks").append(el("li", { class: "empty-note", text: "Needs a sandbox (macOS sandbox-exec, or bwrap on Linux)." }));
    showList("#checks", $("#checks").children.length);
  }
  renderLessonForm();
  // Nothing learned yet: one line says so, not three empty lists.
  const empty = ["#styles", "#lessons", "#checks"].every((id) => !$(id).children.length);
  $("#learned-nothing").hidden = !empty;
  renderLearnRow();
}
// A list, folded with its count, shown only when there is something in it (Habits always: the
// owner adds their own there).
function showList(id, count) {
  const list = $(id);
  list.hidden = !count;
  const fold = list.closest("details");
  fold.hidden = !count && fold.id !== "you-habits";
  $(`${id}-count`).textContent = count ? String(count) : "";
}
// A habit the owner writes down themselves: everywhere, in Slack replies, or in one project (one
// in Settings or a repository in the code folder, as the server lists them: `projects`).
function renderLessonForm() {
  const scope = $("#lesson-scope");
  const projects = [...new Set([...(state.snapshot.settings.projects || []), ...(state.snapshot.projects || [])])];
  const chosen = scope.value;
  scope.replaceChildren(el("option", { value: "", text: "Everywhere" }), el("option", { value: "slack", text: "Slack replies" }), ...projects.map((path) => el("option", { value: path, text: `In ${path.split("/").pop()}`, title: path })));
  scope.value = [...scope.options].some((option) => option.value === chosen) ? chosen : "";
}
async function addLesson() {
  const input = $("#lesson-text");
  const text = input.value.trim();
  if (!text) {
    input.focus();
    return;
  }
  try {
    state.snapshot = { ...state.snapshot, ...(await api("/api/lessons", { method: "POST", body: { text, scope: $("#lesson-scope").value } })) };
    input.value = "";
    renderLearning();
    toast("Habit added: your sessions use it from their next message.");
  } catch (error) {
    settingsError(error.message);
  }
}
// What the owner's words were read from, as counted.
const OBSERVED = {
  slack_dm: ["Slack DM", "Slack DMs"],
  slack_channel: ["channel message", "channel messages"],
  pr_review: ["review comment", "review comments"],
  commit: ["commit", "commits"],
  instruction: ["request to sessions", "requests to sessions"],
};
async function memoryAction(path) {
  try {
    state.snapshot = { ...state.snapshot, ...(await api(path, { method: "POST" })) };
    renderLearning();
  } catch (error) {
    settingsError(error.message);
  }
}
function notesText() {
  return Object.entries(state.snapshot.setup_notes || {})
    .map(([topic, entry]) => `${topic}: ${entry.note}`)
    .join("\n");
}
export function settingsError(message) {
  const node = $("#settings-error");
  node.textContent = message;
  node.hidden = !message;
}
// A menu's value only once the owner changed it; else what is saved (a model it does not list,
// an effort the model does not offer, a list not loaded yet: never replaced).
function kept(name, saved) {
  const input = $("#settings-form")[name];
  return input?.dataset?.dirty ? input.value.trim() : saved;
}
const ROLES = ["router", "worker", "slack", "watchdog"];
// What Save sends: the settings as the form has them, wherever each field is, every other one
// as it is, and `base`.
export function settingsBody() {
  const form = $("#settings-form");
  const settings = state.snapshot.settings;
  const number = (name, fallback = settings[name]) => numberField(form[name], fallback);
  const named = (name) => form[name];
  const body = {
    // Settings this page does not show are sent back as they are, not reset.
    ...settings,
    projects: lines(form.projects.value),
    code_folder: form.code_folder.value.trim(),
    scratch_directory: form.scratch_directory.value.trim(),
    max_workers: number("max_workers"),
    fork_busy: form.fork_busy.checked,
    worker_yolo: !form.ask_first.checked,
    worker_sandbox: form.worker_sandbox.checked,
    shared_caches: form.shared_caches.checked,
    sandbox_writable: lines(form.sandbox_writable.value),
    worker_idle_hours: number("worker_idle_hours"),
    stall_hours: number("stall_hours", settings.stall_hours ?? 4),
    mac_notifications: form.mac_notifications.checked,
    other_sessions: form.other_sessions.checked,
    update_notice: form.update_notice.checked,
    proactive: form.proactive.checked,
    ...proactiveFields(),
    watchdog_enabled: named("watchdog_enabled") ? named("watchdog_enabled").checked : settings.watchdog_enabled,
    autopilot: form.autopilot.checked,
    ...autopilotFields(),
    slack_enabled: form.slack_enabled.checked,
    slack_poll_seconds: number("slack_poll_seconds"),
    slack_delegate_emoji: form.slack_delegate_emoji.value.trim(),
    slack_delegate_reply: form.slack_delegate_reply.checked,
    github_enabled: form.github_enabled.checked,
    github_repos: lines(form.github_repos.value),
    copilot_command: named("copilot_command") ? named("copilot_command").value.trim() : settings.copilot_command,
    model: kept("model", settings.model),
    role_models: Object.fromEntries(ROLES.map((role) => [role, kept(`model_${role}`, settings.role_models[role] || "")]).filter(([, value]) => value)),
    role_efforts: Object.fromEntries(ROLES.map((role) => [role, kept(`effort_${role}`, settings.role_efforts[role] || "")]).filter(([, value]) => value)),
    worker_credit_limit: named("worker_credit_limit") ? Number(named("worker_credit_limit").value || 0) : settings.worker_credit_limit,
    digest_time: form.digest_time.value,
    learn_from_work: form.learn_from_work.checked,
    learn_checks: form.learn_checks.checked,
    paused: form.paused.checked,
    skill_folders: lines(form.skill_folders.value),
    plugins: lines(form.plugins.value),
    mcp_servers: {},
    // Those off are in the MCP servers box with the others: what it has is what is kept.
    mcp_servers_off: {},
    ...watchFields(),
    away_notify: form.away_notify.checked,
    away_minutes: number("away_minutes", settings.away_minutes ?? 10),
    away_how: form.away_how.value.trim(),
    // As this dialog showed them: a setting another tab changed since stays as that tab left it.
    base: shown,
  };
  if ("report_footer" in settings) body.report_footer = form.report_footer.checked;
  return body;
}
async function saveSettings(event) {
  event.preventDefault();
  const form = $("#settings-form");
  settingsError("");
  // The browser's own checks, on any field, shown where the field is (folded or on another
  // page: the form has novalidate, so none is "not focusable").
  const invalid = [...form.querySelectorAll(":invalid")].find((input) => !input.closest("[data-off]"));
  if (invalid) {
    revealField(invalid);
    invalid.reportValidity();
    return;
  }
  const body = settingsBody();
  try {
    body.mcp_servers = mcpServers();
  } catch (error) {
    settingsError(error.message);
    revealField(form.mcp_servers);
    return;
  }
  let agents;
  try {
    agents = agentsBody();
  } catch (error) {
    settingsError(error.message);
    if (error.field) revealField(error.field);
    return;
  }
  try {
    if (form.slack_token.value.trim()) await saveToken();
    if (agents) body.agents = agents;
    let snapshot = await api("/api/settings", { method: "POST", body });
    // The agents on are in the answer only while one is; a save that stopped workers says so.
    if (agents) snapshot = { agents: undefined, ...snapshot };
    if (snapshot.stopped) toast(snapshot.stopped);
    const initialNotes = form.setup_notes.dataset.initial || "";
    if (form.setup_notes.value.trim() !== initialNotes.trim()) {
      snapshot = await api("/api/setup-notes", { method: "POST", body: { text: form.setup_notes.value, initial: initialNotes } });
    }
    if (form.profile.value.trim() !== (state.snapshot.profile || "")) {
      snapshot = await api("/api/profile", { method: "POST", body: { text: form.profile.value } });
    }
    state.snapshot = { ...state.snapshot, ...snapshot };
    if (agents) agentsSaved();
    $("#settings-dialog").close();
    followOthers();
    renderSidebar();
    renderHeader();
  } catch (error) {
    settingsError(error.message);
    showProblem(error);
  }
}
// The field a server's error is about: named by the server, or by its label at the start of
// the message ("Workers at once: …", "Workers model: …"), shown where it is (revealField).
const MODEL_LABELS = { "Default model:": "model", "Router model:": "model_router", "Workers model:": "model_worker", "Slack model:": "model_slack", "Watchdog model:": "model_watchdog", "The code folder ": "code_folder" };
export function problemField(error) {
  const form = $("#settings-form");
  const byLabel = Object.entries(MODEL_LABELS).find(([words]) => error.message.startsWith(words));
  if (byLabel) return form.elements.namedItem(byLabel[1]);
  const named = (error.fields || []).map((name) => form.elements.namedItem(name)).find((field) => field?.closest);
  if (named) return named;
  const labelled = [...form.querySelectorAll(".tab label")].find((label) => {
    const words = (label.querySelector("span") || label).textContent.trim();
    return words && error.message.startsWith(`${words}:`);
  });
  // (A line's name labels its field from beside it; a wrapping label holds it.)
  return labelled?.control || labelled?.querySelector("input, textarea, select") || null;
}
function showProblem(error) {
  const field = problemField(error);
  if (!field) return;
  field.setAttribute("aria-invalid", "true");
  revealField(field);
}
// The watches as written in the form, with when they run.
function watchFields() {
  const form = $("#settings-form");
  const settings = state.snapshot.settings;
  return {
    watches: [...document.querySelectorAll("#watch-list li")]
      .map((row) => ({ what: row.querySelector(".what").value.trim(), how: row.querySelector(".how").value.trim(), enabled: row.querySelector(".on").checked }))
      .filter((watch) => watch.what),
    watch_hours: `${$("#watch-from").value || "09:00"}-${$("#watch-to").value || "18:00"}`,
    watch_minutes: numberField(form.watch_minutes, settings.watch_minutes ?? 5),
    watch_after_hours_minutes: numberField(form.watch_after_hours_minutes, settings.watch_after_hours_minutes ?? 30),
  };
}
// A number field left empty keeps its saved value (Number("") is 0, which no field means).
function numberField(input, saved) {
  return input.value.trim() === "" ? saved : Number(input.value);
}
// The settings as this dialog showed them when it opened (and as its own saves left them): what
// a save goes from (`base`), so it does not undo what another tab changed since.
let shown = {};
// After a save from inside the dialog: those fields are as saved now.
function savedHere(fields) {
  shown = { ...shown, ...Object.fromEntries(Object.keys(fields).map((key) => [key, state.snapshot.settings[key]])) };
}
const sameWatches = (a, b) => JSON.stringify(a.map(({ what, how, enabled }) => [what, how || "", enabled !== false])) === JSON.stringify(b.map(({ what, how, enabled }) => [what, how || "", enabled !== false]));
// Only the watches, saved: the rest of the form stays as the owner left it, unsaved.
async function saveAway() {
  settingsError("");
  const form = $("#settings-form");
  const settings = state.snapshot.settings;
  const fields = { away_notify: form.away_notify.checked, away_minutes: numberField(form.away_minutes, settings.away_minutes ?? 10), away_how: form.away_how.value.trim() };
  if (fields.away_notify === Boolean(settings.away_notify) && fields.away_minutes === (settings.away_minutes ?? 10) && fields.away_how === (settings.away_how || "")) return true;
  try {
    state.snapshot = { ...state.snapshot, ...(await api("/api/settings", { method: "POST", body: { ...fields, base: shown } })) };
    savedHere(fields);
    savedNow("[data-conn-detail=away] [data-dirty]");
    renderConnections();
    return true;
  } catch (error) {
    settingsError(error.message);
    return false;
  }
}
// Send to Chief's line as saved: off, the switch alone; on, the command and the recipes, or why
// its socket could not open.
function renderSendLine() {
  const on = Boolean(state.snapshot.settings.send_to_chief);
  const problem = on ? state.snapshot.send_problem || "" : "";
  $("#settings-form").send_to_chief.checked = on;
  $("#send-consent").hidden = true;
  // Its command as it runs in the owner's terminal (bug E63): started with uvx, there is no
  // `chiefofstaff` there until it is installed.
  const how = state.snapshot.send_command || { command: "chiefofstaff", install: "" };
  const command = /[/\\]/.test(how.command) && /\s/.test(how.command) ? `"${how.command}"` : how.command;
  const recipes = $("#send-how a");
  $("#send-how").replaceChildren(
    ...(how.install ? [el("code", { text: how.install }), " once puts the command on your PATH", el("br")] : []),
    el("code", { text: `${command} send "…"` }),
    " from a terminal, or a hotkey: ",
    recipes,
  );
  $("#send-how").hidden = !on || Boolean(problem);
  $("#send-problem").textContent = problem;
  $("#send-problem").hidden = !problem;
}
async function saveSend(on) {
  settingsError("");
  try {
    const snapshot = await api("/api/settings", { method: "POST", body: { send_to_chief: on, base: shown } });
    // Its problem is in the answer only while there is one.
    state.snapshot = { ...state.snapshot, send_problem: "", ...snapshot };
    savedHere({ send_to_chief: on });
  } catch (error) {
    settingsError(error.message);
  }
  renderSendLine();
  $("#settings-form").send_to_chief.focus();
}
async function saveWatches() {
  settingsError("");
  const fields = watchFields();
  const settings = state.snapshot.settings;
  if (!fields.watches.length) {
    settingsError("Write what to watch for first (+ Add watch).");
    return false;
  }
  const saved = { watches: settings.watches || [], watch_hours: settings.watch_hours, watch_minutes: settings.watch_minutes, watch_after_hours_minutes: settings.watch_after_hours_minutes };
  if (sameWatches(fields.watches, saved.watches) && fields.watch_hours === saved.watch_hours && fields.watch_minutes === saved.watch_minutes && fields.watch_after_hours_minutes === saved.watch_after_hours_minutes) return true;
  try {
    state.snapshot = { ...state.snapshot, ...(await api("/api/settings", { method: "POST", body: { ...fields, base: shown } })) };
    savedHere(fields);
    savedNow("[data-conn-detail=watches] [data-dirty]");
    renderConnections();
    return true;
  } catch (error) {
    settingsError(error.message);
    return false;
  }
}
export async function saveToken() {
  const form = $("#settings-form");
  const snapshot = await api("/api/slack/token", { method: "POST", body: { token: form.slack_token.value.trim() } });
  state.snapshot = { ...state.snapshot, ...snapshot };
  form.slack_token.value = "";
  savedNow("[name=slack_token]");
  showSlackToken();
  renderConnectionList();
}
// A token set by the environment is the one used, whatever is saved here: it is changed there.
function showSlackToken() {
  const slack = state.snapshot.slack || {};
  const env = slack.token_source === "env";
  $("#slack-status").textContent = slackStatusText();
  $("#slack-token-save").closest(".row").hidden = env;
  $("#slack-token-remove").hidden = env || !slack.token_saved;
}

// General's lines say what they do as their draft has them (docs/design/settings-v2.md §4, §7.2).
// Ask first is worker_yolo the other way round, said as a worker's ⋯ says it; under it, always,
// what holds either way (#ask-first-floor: 0.1.57's floor).
// Each says the agents on by name where they differ (Codex has no sandbox Chief of Staff checked).
const ASK_FIRST = {
  off: () => `Workers act without asking, as in your own ${agentsSaid()} sessions.`,
  on: () => "Workers ask before risky commands.",
};
const SANDBOX = {
  off: () => "Workers can change any of your files, as your own sessions can.",
  on: () => `Workers can change files only in their own folders.${agentsOn().includes("codex") ? " Codex can't run workers while it's on." : ""}`,
};
// Workers that read Slack: sandboxed on the agents whose sandbox Chief of Staff checked; on
// Codex, without one, asking first.
function slackWords() {
  const on = agentsOn();
  const boxed = on.filter((name) => name !== "codex");
  if (!on.includes("codex")) return "Workers that read Slack are always sandboxed.";
  if (!boxed.length) return "Workers that read Slack run without a sandbox on Codex, asking first.";
  return `Workers that read Slack are always sandboxed on ${agentsSaid(boxed)}; on Codex they run without one, asking first.`;
}
function generalWords() {
  const form = $("#settings-form");
  $("#ask-first-help").textContent = form.ask_first.checked ? ASK_FIRST.on() : ASK_FIRST.off();
  $("#sandbox-help").textContent = form.worker_sandbox.checked ? SANDBOX.on() : SANDBOX.off();
  $("#sandbox-slack").textContent = slackWords();
  // Off, its Options still apply: to the workers that read Slack, sandboxed where they run
  // (not on Codex).
  $("#sandbox-when-off").hidden = form.worker_sandbox.checked || !agentsOn().some((name) => name !== "codex");
  $("#proactive-help").textContent = prsWords();
  $("#autopilot-help").textContent = autopilotWords();
}

// Keep my PRs moving (shepherd.py): its Options, under its line while it is on: whose PRs it
// looks after, each kind's level, the day's credits for work nobody clicked; folded, the hosts
// its steps may reach and per repository whether replies say they were drafted with an AI
// assistant. Made when Settings opens with it on, or once it is ticked (from what is saved);
// then only shown and hidden as the switch goes, so what was written there stays.
const LEVELS = [["off", "Off"], ["suggest", "Suggest"], ["prepare", "Prepare"]];
const KIND_NAMES = [["fix-ci", "CI failures"], ["address-review", "Review comments"]];
const round = (value) => Math.round((value || 0) * 10) / 10;
function renderPrsOptions() {
  const form = $("#settings-form");
  const settings = state.snapshot.settings;
  const kept = state.snapshot.shepherd || {};
  const levels = settings.proactive_levels || {};
  const level = (kind, name) =>
    el("label", {}, el("span", { text: name }), el("select", { name: `level_${kind}` }, ...LEVELS.map(([value, text]) => el("option", { value, text, selected: (levels[kind] || "suggest") === value }))));
  const repos = (kept.repositories || []).map((repo) => {
    const choice = (settings.proactive_repos || {})[repo] || {};
    return el(
      "li",
      { class: "repo-row", "data-repo": repo },
      el("span", { class: "name", text: repo }),
      el("label", { class: "inline" }, el("span", { text: "Replies say they were drafted with AI" }), el("select", { class: "disclosure", "aria-label": `${repo}: replies say they were drafted with an AI assistant` }, ...[["", "Ask me"], ["yes", "Yes"], ["no", "No"]].map(([value, text]) => el("option", { value, text, selected: (choice.disclosure || "") === value })))),
      el("label", { class: "check" }, el("input", { type: "checkbox", class: "localhost", checked: Boolean(choice.localhost) }), " Its tests may use this computer's servers"),
    );
  });
  const folded = repos.length ? `Other hosts fixes may download from · Repositories (${repos.length})` : "Other hosts fixes may download from";
  $("#prs-more").replaceChildren(
    el(
      "div",
      { class: "row" },
      el("label", { class: "check" }, el("input", { name: "proactive_worker_prs", type: "checkbox", checked: settings.proactive_worker_prs !== false }), " PRs my workers made"),
      el("label", { class: "check" }, el("input", { name: "proactive_my_prs", type: "checkbox", checked: settings.proactive_my_prs !== false }), " My other open PRs in my repositories"),
    ),
    el(
      "div",
      { class: "row" },
      ...KIND_NAMES.map(([kind, name]) => level(kind, name)),
      el("label", { class: "budget" }, el("span", { text: "Credits a day" }), el("input", { name: "proactive_budget", type: "number", min: "0", max: "10000", step: "1", placeholder: "0", "aria-label": "Credits it may spend a day without asking", value: settings.proactive_budget ? String(settings.proactive_budget) : "" })),
    ),
    el("small", { text: "Suggest offers Fix it; Prepare does it at once, within the credits it may spend a day without asking (0: only when you ask). Nothing is pushed or posted until you approve it." }),
    el(
      "details",
      { class: "more-fold", id: "prs-hosts" },
      el("summary", { text: folded }),
      el("label", {}, el("span", { text: "Other hosts fixes may download from" }), el("textarea", { name: "step_hosts", rows: 2, placeholder: "registry.example.com — one per line" }), el("small", { text: "PyPI, npm, crates.io and Go's proxy are allowed already. Never GitHub." })),
      repos.length ? el("ul", { class: "repo-list" }, ...repos) : null,
    ),
  );
  form.step_hosts.value = (settings.step_hosts || []).join("\n");
}
// Its line as its switch is (saved or not): its Options offered while it is on (made the first
// time); off, folded and left out of the save, what was written in them kept.
function showPrsLine() {
  const form = $("#settings-form");
  const on = form.proactive.checked;
  if (on && !$("#prs-more").childElementCount) renderPrsOptions();
  $("#prs-options").hidden = !on;
  if (on) delete $("#prs-more").dataset.off;
  else {
    $("#prs-more").dataset.off = "";
    setUnfold("prs-more", false);
  }
  const steps = on ? state.snapshot.shepherd?.steps_here || "" : "";
  $("#proactive-steps").textContent = steps;
  $("#proactive-steps").hidden = !steps;
  generalWords();
}
// What it does now, in its helper: each kind's level and the day's credits, as the draft has them.
function prsWords() {
  const form = $("#settings-form");
  if (!form.proactive.checked) return "Prepares fixes for failing CI and replies to reviews, for your approval.";
  const settings = state.snapshot.settings;
  const level = (kind) => form[`level_${kind}`]?.value || (settings.proactive_levels || {})[kind] || "suggest";
  const verb = { suggest: "suggests", prepare: "prepares" };
  const ci = level("fix-ci");
  const review = level("address-review");
  const parts = [];
  if (ci !== "off") parts.push(`${verb[ci]} fixes for failing CI`);
  if (review !== "off") parts.push(ci === review ? "replies to reviews" : `${verb[review]} replies to reviews`);
  let what = parts.length ? parts.join(ci === review ? " and " : ", ") : "looks at your PRs; CI failures and reviews are both off";
  what = `${what[0].toUpperCase()}${what.slice(1)}`;
  const budget = form.proactive_budget ? Number(form.proactive_budget.value || 0) : settings.proactive_budget || 0;
  const spent = state.snapshot.shepherd?.spent;
  const used = spent ? round((spent.settled || 0) + (spent.reserved || 0)) : 0;
  const today = used ? (budget ? `${used} of ${budget} used today` : `${used} used today`) : "";
  return [what, budget ? `up to ${budget} credits a day without asking` : "", today].filter(Boolean).join(" · ");
}
// Autopilot (autopilot.md §8): its line says what it does, and while it is ticked its Options
// hold its credits a day, where it acts, whether workers ask first, and its record. Made the
// first time it is ticked (from what is saved); then only shown and hidden as the line goes.
function showAutopilotLine() {
  const on = $("#settings-form").autopilot.checked;
  if (on && !$("#autopilot-more").childElementCount) renderAutopilotOptions();
  $("#autopilot-options").hidden = !on;
  if (!on) setUnfold("autopilot-more", false);
  generalWords();
}
function autopilotWords() {
  const form = $("#settings-form");
  if (!form.autopilot.checked) return "Answers your workers from what you said, and accepts results it checked itself, while it's on.";
  const settings = state.snapshot.settings;
  const credits = form.autopilot_credits ? Number(form.autopilot_credits.value || 0) : settings.autopilot_credits ?? 100;
  const run = autopilotRun();
  const head = run ? `On ${untilWords(run)}` : "Home's Autopilot button turns it on, for a while";
  return `${head} · up to ${credits} credits a day`;
}
function renderAutopilotOptions() {
  const settings = state.snapshot.settings;
  const repos = settings.autopilot_repos || [];
  const only = el("textarea", { name: "autopilot_repos", rows: 2, placeholder: "owner/name, one per line", "aria-label": "Only these repositories", hidden: !repos.length });
  only.value = repos.join("\n");
  const scope = el(
    "select",
    { name: "autopilot_scope", "aria-label": "Where it acts", onchange: () => { only.hidden = scope.value !== "only"; if (!only.hidden) only.focus(); } },
    el("option", { value: "all", text: "All your projects", selected: !repos.length }),
    el("option", { value: "only", text: "Only these…", selected: Boolean(repos.length) }),
  );
  const record = autopilot()?.record
    ? el("button", { type: "button", class: "link quiet autopilot-record-link", text: "Autopilot record", onclick: () => { $("#settings-dialog").close(); showRecord(); } })
    : null;
  const sandbox = state.snapshot.sandbox || {};
  const parts = [
    el(
      "div",
      { class: "row" },
      el("label", { class: "budget" }, el("span", { text: "Credits a day" }), el("input", { name: "autopilot_credits", type: "number", min: "0", max: "10000", step: "1", "aria-label": "Credits Autopilot may spend a day", value: String(settings.autopilot_credits ?? 100) })),
      el("label", { class: "acts-in" }, el("span", { text: "Acts in" }), scope),
      record,
    ),
    only,
    el(
      "div",
      { class: "ask-first" },
      el("label", { class: "check" }, el("input", { name: "autopilot_ask_first", type: "checkbox", checked: settings.autopilot_ask_first !== false, "aria-describedby": "autopilot-ask-help" }), " While Autopilot is on, workers ask first"),
      el("small", { id: "autopilot-ask-help", text: "The turns it continues get Ask first's checks, whatever your setting for workers; your own turns don't." }),
    ),
    sandbox.supported === false ? el("small", { class: "note", text: "Here it can't verify results offline: this computer has no sandbox for it. It answers and sets questions aside; results wait for you." }) : null,
  ];
  $("#autopilot-more").replaceChildren(...parts.filter(Boolean));
}
function autopilotFields() {
  const form = $("#settings-form");
  const settings = state.snapshot.settings;
  // Unticked, what its Options hold stays as saved.
  if (!form.autopilot.checked || !$("#autopilot-more").childElementCount) return {};
  return {
    autopilot_credits: numberField(form.autopilot_credits, settings.autopilot_credits ?? 100),
    autopilot_repos: form.autopilot_scope.value === "only" ? lines(form.autopilot_repos.value) : [],
    autopilot_ask_first: form.autopilot_ask_first.checked,
  };
}
function proactiveFields() {
  const form = $("#settings-form");
  const settings = state.snapshot.settings;
  if (!form.proactive.checked || !$("#prs-more").childElementCount) return {};
  // A repository's choices and a kind's level, only once changed here (a Save of nothing
  // changed leaves them as saved: settings-v2.md §8.7).
  const repos = { ...(settings.proactive_repos || {}) };
  for (const row of document.querySelectorAll("#prs-more .repo-row")) {
    if (!row.querySelector("[data-dirty]")) continue;
    repos[row.dataset.repo] = { disclosure: row.querySelector(".disclosure").value, localhost: row.querySelector(".localhost").checked };
  }
  const levels = settings.proactive_levels || {};
  return {
    proactive_worker_prs: form.proactive_worker_prs.checked,
    proactive_my_prs: form.proactive_my_prs.checked,
    proactive_levels: Object.fromEntries(KIND_NAMES.map(([kind]) => [kind, kept(`level_${kind}`, levels[kind] || "")]).filter(([, level]) => level)),
    proactive_budget: numberField(form.proactive_budget, settings.proactive_budget || 0),
    step_hosts: lines(form.step_hosts.value),
    proactive_repos: repos,
  };
}

// You: each list folded with its count (Habits always there, to add one); the profile with its
// first line (or that a draft waits), the environment notes with how many.
function youFolds() {
  const profile = (state.snapshot.profile || "").trim();
  $("#profile-preview").textContent = state.snapshot.profile_draft ? "Draft ready to review" : profile ? profile.split("\n")[0] : "Empty";
  $("#profile-preview").classList.toggle("warn", Boolean(state.snapshot.profile_draft));
  const notes = Object.keys(state.snapshot.setup_notes || {}).length;
  $("#notes-count").textContent = notes ? plural(notes, "note") : "None";
}
