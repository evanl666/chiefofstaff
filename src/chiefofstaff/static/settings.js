// The settings and usage dialogs.

import { $, api, el, submits, toast } from "./dom.js";
import { codeHtml, lines, plural, since } from "./text.js";
import { closedWorkers, followOthers, HOME, loadState, sessions, state } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { EFFORTS, renderHeader, select } from "./session.js";
import { agentMark, fixLine, statusText } from "./agentpick.js";
import { openSetup, setupLine, setupWords } from "./agentsetup.js";
// Settings opens on the tab last used, or the one asked for ("you" for what was learned).
const TAB = "cos.settingsTab";
export function showSettingsTab(tab) {
  const tabs = [...document.querySelectorAll("#settings-form .tab")];
  const shown = tabs.find((section) => section.dataset.tab === tab) || tabs[0];
  localStorage.setItem(TAB, shown.dataset.tab);
  for (const section of tabs) section.hidden = section !== shown;
  for (const button of document.querySelectorAll("#settings-tabs button")) button.setAttribute("aria-selected", String(button.dataset.tab === shown.dataset.tab));
  tabInView();
  $("#settings-pane").scrollTop = 0;
  // Coding agents looks for them each time it is shown (only then).
  if (shown.dataset.tab === "agents" && !agentsLooked) lookForAgents();
}
// On a phone the tabs scroll sideways: the one shown is in view, whole, also as the width
// changes (a phone turned, a window narrowed while Settings is open). Only the strip scrolls.
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
window.addEventListener("resize", tabInView);
export function openSettings(tab = "") {
  const dialog = $("#settings-dialog");
  // Opened first: a part that fails to render must not keep Settings shut.
  if (!dialog.open) dialog.showModal();
  try {
    fillSettings();
  } catch (error) {
    console.error(error);
    settingsError(`Part of Settings could not be shown: ${error.message}`);
  }
  showSettingsTab(tab || localStorage.getItem(TAB) || "general");
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
export function bindSettings() {
  const dialog = $("#settings-dialog");
  $("#lesson-save").addEventListener("click", addLesson);
  $("#lesson-text").addEventListener("keydown", (event) => {
    if (!submits(event)) return;
    event.preventDefault();  // Not the settings form's submit.
    addLesson();
  });
  // (Coding agents' button, made only when it is offered, is listened to as it is made.)
  $("#settings-tabs").addEventListener("click", (event) => {
    const button = event.target.closest("#settings-tabs button");
    if (button) showSettingsTab(button.dataset.tab);
  });
  // A field the browser finds invalid on Save may be on another tab: show that tab.
  $("#settings-form").addEventListener("invalid", (event) => showSettingsTab(event.target.closest(".tab")?.dataset.tab), true);
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#settings-cancel").addEventListener("click", () => dialog.close());
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
  $("#skill-file").addEventListener("change", async () => {
    const file = $("#skill-file").files[0];
    $("#skill-file").value = "";
    if (file) await addSkill(await file.text());
  });
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
  $("#settings-form").mcp_servers.addEventListener("input", () => {
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
    watchSummary();
    row.querySelector(".what").focus();
  });
  // Scanning uses the watches as written here: saved first (a watch just added counts).
  $("#watch-scan").addEventListener("click", async () => {
    if (!(await saveWatches())) return;
    await settingsAction("/api/watch/scan", "Scanning now.");
  });
  $("#watch-list").addEventListener("input", watchSummary);
  $("#watch-list").addEventListener("change", watchSummary);
  // The test uses how to reach the owner as written here: saved first, as Scan now does.
  $("#away-test").addEventListener("click", async () => {
    if (!(await saveAway())) return;
    await settingsAction("/api/away/test", "");
  });
  for (const [id, find] of [
    ["#watch-activity", () => state.snapshot.connector?.session_id],
    ["#slack-activity", () => sessions().find((session) => session.role === "slack")?.id],
    ["#watchdog-activity", () => sessions().find((session) => session.role === "watchdog")?.id],
  ]) {
    $(id).addEventListener("click", () => {
      const sessionId = find();
      if (!sessionId) return;
      dialog.close();
      select(sessionId);
    });
  }
  // Send to Chief: turning it on unfolds its consent in place, saved only with Turn on; off is
  // saved at once (its socket closes).
  const sendSwitch = $("#settings-form").send_to_chief;
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
    } catch (error) {
      settingsError(error.message);
    }
  });
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
export function fillSettings() {
  const form = $("#settings-form");
  const settings = state.snapshot.settings;
  shown = JSON.parse(JSON.stringify(settings));
  form.projects.value = settings.projects.join("\n");
  const missing = state.snapshot.missing_projects || [];
  $("#projects-missing").textContent = missing.length ? `Folder not found: ${missing.join(", ")}. Restore it, or remove it here.` : "";
  $("#projects-missing").hidden = !missing.length;
  form.code_folder.value = settings.code_folder;
  form.scratch_directory.value = settings.scratch_directory;
  form.scratch_directory.placeholder = settings.code_folder ? `${settings.code_folder}/.chief` : `${state.snapshot.data_directory}/work/workspaces`;
  form.max_workers.value = settings.max_workers;
  form.worker_yolo.checked = settings.worker_yolo;
  form.worker_sandbox.checked = settings.worker_sandbox;
  form.shared_caches.checked = settings.shared_caches;
  form.sandbox_writable.value = (settings.sandbox_writable || []).join("\n");
  const sandbox = state.snapshot.sandbox || {};
  const hint = !sandbox.supported
    ? "This machine cannot run Copilot's sandbox (macOS needs sandbox-exec; Linux needs bwrap), so workers run unsandboxed."
    : sandbox.gh_trusted === false
      ? "To let workers use gh inside the sandbox, trust Copilot's certificate once: copilot sandbox ca create && copilot sandbox ca trust"
      : "";
  $("#sandbox-hint").textContent = hint;
  $("#sandbox-hint").hidden = !hint;
  form.worker_idle_hours.value = settings.worker_idle_hours;
  form.stall_hours.value = settings.stall_hours ?? 4;
  form.mac_notifications.checked = settings.mac_notifications;
  form.other_sessions.checked = Boolean(settings.other_sessions);
  form.update_notice.checked = settings.update_notice !== false;
  // When none of them can say where it stands, its line says why.
  const why = settings.other_sessions ? state.othersWhy : "";
  if (why) form.other_sessions.parentElement.title = why;
  else form.other_sessions.parentElement.removeAttribute("title");
  form.watchdog_enabled.checked = settings.watchdog_enabled;
  form.slack_enabled.checked = settings.slack_enabled;
  form.slack_token.value = "";
  form.slack_poll_seconds.value = settings.slack_poll_seconds;
  form.slack_delegate_emoji.value = settings.slack_delegate_emoji ? `:${settings.slack_delegate_emoji}:` : "";
  form.slack_delegate_reply.checked = settings.slack_delegate_reply !== false;
  document.getElementById("slack-delegate-reply").textContent = `Reply directly when you react with :${settings.slack_delegate_emoji || "robot_face"}:`;
  form.github_enabled.checked = settings.github_enabled;
  form.github_repos.value = settings.github_repos.join("\n");
  form.copilot_command.value = settings.copilot_command;
  form.model.value = settings.model;
  for (const role of ["router", "worker", "slack", "watchdog"]) {
    form[`model_${role}`].value = settings.role_models[role] || "";
    const select = form[`effort_${role}`];
    select.replaceChildren(
      el("option", { value: "", text: "Default" }),
      ...["low", "medium", "high", "xhigh", "max"].map((level) => el("option", { value: level, text: EFFORTS[level] })),
    );
    select.value = settings.role_efforts[role] || "";
  }
  // An effort applies to a model; Auto (Copilot's model picker) takes none.
  const effortHint = () => {
    const auto = (role) => (form[`model_${role}`].value.trim() || form.model.value.trim()).toLowerCase() === "auto";
    $("#effort-hint").hidden = !["router", "worker", "slack", "watchdog"].some((role) => auto(role) && form[`effort_${role}`].value);
  };
  for (const name of ["model", "model_router", "model_worker", "model_slack", "model_watchdog", "effort_router", "effort_worker", "effort_slack", "effort_watchdog"]) {
    form[name].oninput = effortHint;
    form[name].onchange = effortHint;
  }
  effortHint();
  form.worker_credit_limit.value = settings.worker_credit_limit || "";
  form.digest_time.value = settings.digest_time;
  form.profile.value = state.snapshot.profile || "";
  form.setup_notes.value = notesText();
  form.setup_notes.dataset.initial = form.setup_notes.value;
  $("#profile-draft").hidden = !state.snapshot.profile_draft;
  form.learn_from_work.checked = settings.learn_from_work;
  form.learn_checks.checked = settings.learn_checks;
  renderLearning();
  form.paused.checked = settings.paused;
  form.report_footer.checked = settings.report_footer !== false;
  form.report_footer.closest("label").hidden = !("report_footer" in settings);
  form.skill_folders.value = (settings.skill_folders || []).join("\n");
  form.plugins.value = (settings.plugins || []).join("\n");
  // Those off (saved, but Copilot would refuse them) are shown with the others, to be fixed.
  const servers = { ...(settings.mcp_servers || {}), ...(settings.mcp_servers_off || {}) };
  form.mcp_servers.value = Object.keys(servers).length ? JSON.stringify(servers, null, 2) : "";
  $("#mcp-error").textContent = mcpNote(servers);
  $("#mcp-error").hidden = !$("#mcp-error").textContent;
  $("#watch-list").replaceChildren(...(settings.watches || []).map(watchRow));
  watchSummary();
  const [from, to] = (settings.watch_hours || "09:00-18:00").split("-");
  $("#watch-from").value = from;
  $("#watch-to").value = to;
  form.watch_minutes.value = settings.watch_minutes ?? 5;
  form.watch_after_hours_minutes.value = settings.watch_after_hours_minutes ?? 30;
  form.away_notify.checked = Boolean(settings.away_notify);
  form.away_minutes.value = settings.away_minutes ?? 10;
  form.away_how.value = settings.away_how || "";
  renderSendLine();
  renderAgentsLine();
  renderConnections();
  showSlackToken();
  const github = state.snapshot.github;
  $("#github-status").textContent = github.error || (github.connected ? `Watching${github.last_checked ? `, checked ${since(github.last_checked)}` : ""}.` : "");
  $("#data-directory").textContent = `Data: ${state.snapshot.data_directory}`;
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
      watchSummary();
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
    .map(([name, config]) => `MCP server ${name} is off (workers do not get it) until fixed: ${mcpProblem(config) || "Copilot would refuse it."}`)
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
export function renderConnections() {
  const snap = state.snapshot;
  if (!snap) return;
  const settings = snap.settings;
  const slack = snap.slack || {};
  $("#slack-summary").textContent = !settings.slack_enabled ? "Off" : slack.error ? "Problem" : slack.connected ? "Watching" : "On";
  const repos = settings.github_repos.length;
  $("#github-summary").textContent = settings.github_enabled ? `${repos} ${repos === 1 ? "repository" : "repositories"}` : "Off";
  const abilities = snap.abilities || { skills: [], plugins: {} };
  const servers = Object.keys(settings.mcp_servers || {}).length;
  const off = Object.keys(settings.mcp_servers_off || {}).length;
  // As many as are listed below (one written twice is listed once).
  const pluginCount = abilities.plugins ? Object.keys(abilities.plugins).length : new Set(settings.plugins || []).size;
  $("#abilities-summary").textContent = [abilities.skills.length ? plural(abilities.skills.length, "skill") : "", pluginCount ? plural(pluginCount, "plugin") : "", servers + off ? `${plural(servers + off, "MCP server")}${off ? ` (${off} off)` : ""}` : ""].filter(Boolean).join(" · ") || "None added";
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
  watchSummary();
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
  const tried = away.last || {};
  // What this way can do, as tried (the test, and notifications and reads as they happen).
  const can = away.can || {};
  const coverage = can.coverage || {};
  // Its last notice not delivered (bug E72): said here, in red, until one is.
  const undelivered = Boolean(settings.away_notify && tried.at && !tried.ok);
  $("#away-summary").textContent = !settings.away_notify ? "Off" : undelivered ? `Away notice not delivered · ${since(tried.at)}` : ["On", away.where, can.send_only ? "Send only" : ""].filter(Boolean).join(" · ");
  $("#away-summary").classList.toggle("error", undelivered);
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
  $("#watchdog-activity").hidden = !sessions().some((session) => session.role === "watchdog");
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
      parts.length ? `Read ${parts.join(", ")}.` : "",
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
// A list and its heading, shown only when there is something in it (Habits always: the owner
// adds their own there).
function showList(id, count) {
  const list = $(id);
  list.hidden = !count;
  const heading = list.previousElementSibling;
  heading.hidden = !count && !heading.classList.contains("always");
  heading.dataset.count = count;
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
// What Save sends: the settings as the form has them, every other one as it is, and `base`.
export function settingsBody() {
  const form = $("#settings-form");
  const settings = state.snapshot.settings;
  const number = (name, fallback = settings[name]) => numberField(form[name], fallback);
  const body = {
    // Settings this page does not show are sent back as they are, not reset.
    ...settings,
    projects: lines(form.projects.value),
    code_folder: form.code_folder.value.trim(),
    scratch_directory: form.scratch_directory.value.trim(),
    max_workers: number("max_workers"),
    worker_yolo: form.worker_yolo.checked,
    worker_sandbox: form.worker_sandbox.checked,
    shared_caches: form.shared_caches.checked,
    sandbox_writable: lines(form.sandbox_writable.value),
    worker_idle_hours: number("worker_idle_hours"),
    stall_hours: number("stall_hours", settings.stall_hours ?? 4),
    mac_notifications: form.mac_notifications.checked,
    other_sessions: form.other_sessions.checked,
    update_notice: form.update_notice.checked,
    watchdog_enabled: form.watchdog_enabled.checked,
    slack_enabled: form.slack_enabled.checked,
    slack_poll_seconds: number("slack_poll_seconds"),
    slack_delegate_emoji: form.slack_delegate_emoji.value.trim(),
    slack_delegate_reply: form.slack_delegate_reply.checked,
    github_enabled: form.github_enabled.checked,
    github_repos: lines(form.github_repos.value),
    copilot_command: form.copilot_command.value.trim(),
    model: form.model.value.trim(),
    role_models: Object.fromEntries(
      ["router", "worker", "slack", "watchdog"].map((role) => [role, form[`model_${role}`].value.trim()]).filter(([, value]) => value),
    ),
    role_efforts: Object.fromEntries(
      ["router", "worker", "slack", "watchdog"].map((role) => [role, form[`effort_${role}`].value]).filter(([, value]) => value),
    ),
    worker_credit_limit: Number(form.worker_credit_limit.value || 0),
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
  const body = settingsBody();
  try {
    body.mcp_servers = mcpServers();
  } catch (error) {
    $("#conn-abilities").open = true;
    showSettingsTab("connections");
    settingsError(error.message);
    return;
  }
  let agents;
  try {
    agents = agentsBody();
  } catch (error) {
    showSettingsTab("agents");
    settingsError(error.message);
    $(`#settings-form [aria-invalid="true"]`)?.focus();
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
    $("#settings-dialog").close();
    followOthers();
    renderSidebar();
    renderHeader();
  } catch (error) {
    settingsError(error.message);
    showProblem(error);
  }
}
// The field a server's error is about (named by the server, or by its label at the start of
// the message, "Parallel workers: …"), shown on its tab, its part open.
function showProblem(error) {
  const form = $("#settings-form");
  const named = (error.fields || []).map((name) => form.elements.namedItem(name)).find((field) => field?.closest);
  const labelled = [...form.querySelectorAll(".tab label")].find((label) => {
    const words = (label.querySelector("span") || label).textContent.trim();
    return words && error.message.startsWith(`${words}:`);
  });
  const field = named || labelled?.querySelector("input, textarea, select");
  const tab = (field || labelled)?.closest(".tab");
  if (!tab) return;
  showSettingsTab(tab.dataset.tab);
  for (let part = (field || labelled).closest("details"); part; part = part.parentElement.closest("details")) part.open = true;
  field?.focus();
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
    renderConnections();
    return true;
  } catch (error) {
    settingsError(error.message);
    return false;
  }
}
// Other coding agents (more-agents.md 5.10; the agent picker's Settings): their own tab, after
// Models, made only where one is installed or on (with Copilot alone, Settings is as it always
// was, with nothing of them). The tab looks for them (GET /api/agents) each time it is shown,
// never before.
const AGENTS = { codex: "Codex", claude: "Claude" };
let agentsLooked = null; // What the tab showed when it looked: the agents found, as saved.
function agentsTab() {
  const help = "https://github.com/evanl666/chiefofstaff/blob/main/docs/guide.md#workers-on-other-coding-agents";
  const button = el("button", { type: "button", role: "tab", "data-tab": "agents", id: "agents-tab", text: "Coding agents" });
  $("#settings-tabs [data-tab=models]").after(button);
  // Where new workers start and where Chief of Staff's own sessions run (once another agent is
  // on), a row per agent, how to ask for one for a single task, and what stays on Copilot.
  const select = el("select", { name: "agent_new_workers", "aria-describedby": "agents-choice-pays" });
  // Picking an agent there makes it available (as its row's switch says).
  select.addEventListener("change", () => choicePays(select.value));
  select.addEventListener("change", () => {
    const box = $("#settings-form")[`agent_${select.value}_enabled`];
    if (box && !box.checked) {
      box.checked = true;
      box.dispatchEvent(new Event("change"));
    }
  });
  const section = el(
    "section",
    { class: "tab agents-tab", "data-tab": "agents", hidden: true },
    el(
      "div",
      { id: "agents-places", class: "agents-places", hidden: true },
      el("label", { id: "agents-choice", class: "agents-choice", hidden: true }, el("span", { text: "New workers start on" }), select),
      el("small", { id: "agents-choice-pays", class: "agents-role-line agents-choice-pays", hidden: true }),
      // Where Chief of Staff's own sessions run: folded away (Copilot unless chosen).
      el("details", { id: "agents-system", class: "agents-system", hidden: true }, el("summary", { text: "System agents" }), el("div", { id: "agents-roles", class: "agents-roles" })),
    ),
    el("div", { id: "agents-rows", class: "agents-rows" }),
    el("small", { id: "agents-hint", class: "agents-hint", hidden: true }),
    el("small", { class: "agents-intro" }, "Workers, the Router and the Watchdog can also run on another coding agent you have installed and signed in to. Slack and Watch for you stay on Copilot. ", el("a", { class: "link", href: help, target: "_blank", rel: "noreferrer", text: "What differs" })),
  );
  $("#settings-form .tab[data-tab=models]").after(section);
}
function renderAgentsLine() {
  const shown = Boolean(state.snapshot.agents || state.snapshot.agents_found?.length);
  agentsLooked = null;
  if (!shown) {
    $("#agents-tab")?.remove();
    $("#settings-form .tab[data-tab=agents]")?.remove();
    return;
  }
  if (!$("#agents-tab")) agentsTab();
  $("#agents-rows").replaceChildren();
  $("#agents-roles").replaceChildren();
  $("#settings-form").agent_new_workers.replaceChildren(el("option", { value: "copilot", text: "Copilot" }));
  $("#agents-choice").hidden = true;
  $("#agents-choice-pays").hidden = true;
  $("#agents-places").hidden = true;
  $("#agents-hint").hidden = true;
}
async function lookForAgents() {
  const looking = Symbol("looking");
  agentsLooked = looking;
  // Loading: each row says so until the agents answer.
  const loading = (state.snapshot.agents_found || Object.keys(AGENTS)).map((name) => agentRow({ name, label: AGENTS[name] || name, found: true, state: "Checking…", problem: "", models: [] }, true));
  $("#agents-rows").replaceChildren(...loading);
  $("#agents-roles").replaceChildren();
  $("#agents-choice").hidden = true;
  $("#agents-choice-pays").hidden = true;
  $("#agents-places").hidden = true;
  $("#agents-hint").hidden = true;
  let found;
  try {
    found = await api("/api/agents");
  } catch (error) {
    if (agentsLooked !== looking) return;
    agentsLooked = null;  // Looked for again when shown next.
    $("#agents-rows").replaceChildren(...Object.entries(AGENTS).map(([name, label]) => agentRow({ name, label, found: true, state: "", problem: `Could not check: ${error.message}`, models: [] }, true)));
    return;
  }
  if (agentsLooked !== looking) return;
  agentsLooked = found;
  // Sandboxed, an agent whose workers can't start then says why on its row (its sandbox not
  // shown to hold, or your Claude settings widening it).
  $("#agents-rows").replaceChildren(
    ...(found.agents.length ? found.agents.map((agent) => agentRow(found.sandboxed && agent.sandbox ? { ...agent, problem: agent.sandbox } : agent, false)) : [el("small", { class: "agents-none", role: "status", text: noAgentWords(found.supported) })]),
    // Each one not installed, beside one that is: Install, its command shown before it runs.
    ...(found.missing || []).map((agent) => el("div", { class: "agent-row agent-missing", "data-agent": agent.name }, el("div", { class: "agent-head" }, agentMark(agent.name), el("strong", { text: agent.label }), el("small", { class: "agent-problem", role: "status", text: setupWords(agent.name) || agent.said })), el("small", { class: "agent-note" }, setupLine({ ...agent, status: "missing" }) || fixLine(agent.fix)))),
  );
  $("#agents-roles").replaceChildren(...(found.roles || []).map(roleRow));
  newWorkersChoices(found.new_workers);
}
// Where one of Chief of Staff's own sessions runs ("The Router runs on"): shown only while an
// agent it can run on is on, listing only those; under it, why it runs on Copilot for now.
function roleRow(role) {
  const lineId = `role-${role.role}-line`;
  const select = el("select", { name: `role_${role.role}`, "data-chosen": role.chosen });
  if (role.line) select.setAttribute("aria-describedby", lineId);
  select.addEventListener("change", () => newWorkersChoices($("#settings-form").agent_new_workers.value));
  // If its agent can't run it (signed out, not installed, a limit): Copilot and tell me, or ask.
  const fallback = role.unavailable
    ? el(
        "label",
        { class: "agents-choice agents-unavailable" },
        el("span", { text: "If unavailable" }),
        el("select", { name: `unavailable_${role.role}`, "data-chosen": role.unavailable }, el("option", { value: "copilot", text: "Use Copilot and tell me" }), el("option", { value: "ask", text: "Ask me" })),
      )
    : "";
  if (fallback) fallback.querySelector("select").value = role.unavailable;
  return el(
    "div",
    { class: "agents-role", "data-role": role.role, "data-agents": role.agents.join(" "), hidden: true },
    el("label", { class: "agents-choice" }, el("span", { text: `${role.label} runs on` }), select),
    fallback,
    role.line ? el("small", { id: lineId, class: "agents-role-line", role: "status", text: role.line }) : "",
    el("div", { class: "role-setup" }),
  );
}
// A token count as the page shows it (200,000), and as the owner types it: grouped or not, as
// their locale groups digits; null when it is not a whole number of tokens.
const grouped = (count) => count.toLocaleString();
const GROUP = new Intl.NumberFormat().formatToParts(10000).find((part) => part.type === "group")?.value || ",";
const MOST_TOKENS = 1e12; // As agents.MOST_TOKENS: a typo's extra zeros are refused.
function tokenCount(raw) {
  const digits = raw.trim().split(GROUP).join("").replace(/[\s,]/g, "");
  if (digits === "") return 0;
  return /^\d+$/.test(digits) && Number(digits) <= MOST_TOKENS ? Number(digits) : null;
}
// Its limit field says what is wrong with what was typed, beside it (and only then).
function checkLimit(input) {
  const count = tokenCount(input.value);
  const error = $(`#${input.getAttribute("aria-describedby")}`);
  const wrong = count === null;
  error.textContent = wrong ? "A whole number of tokens, or empty for no limit." : "";
  error.hidden = !wrong;
  if (wrong) input.setAttribute("aria-invalid", "true");
  else input.removeAttribute("aria-invalid");
  return count;
}
// How much of its plan's limits it used, where the agent says it: "42% of your 5-hour limit,
// resets 3:40 pm". At 80% and over, a chip beside its name says so.
const USED_CHIP = 80;
function usageLine(usage) {
  return (usage || []).map((entry) => `${entry.used}% of your ${entry.label}${entry.resets_at ? `, resets ${resetTime(entry.resets_at)}` : ""}`).join(" · ");
}
function resetTime(seconds) {
  const at = new Date(seconds * 1000);
  const today = at.toDateString() === new Date().toDateString();
  return today ? at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : at.toLocaleDateString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
}
// One agent's row: its name and state ("version · signed in with …"), Available for workers,
// how much of its plan it used, and, while it is on, its model and limit per worker.
function agentRow(agent, disabled) {
  const id = `agent-${agent.name}-state`;
  const box = el("input", { type: "checkbox", name: `agent_${agent.name}_enabled`, "aria-describedby": id });
  box.checked = Boolean(agent.enabled);
  // Off, it is turned on only once it is ready (installed, signed in) and can run here; on, it
  // can always be turned off.
  const unready = Boolean(agent.status) && agent.status !== "ready";
  box.disabled = disabled || (!agent.enabled && (Boolean(agent.problem) || unready));
  // Its status as the @ picker says it ("Ready · Uses your ChatGPT sign-in"), its version on
  // hover; why it can't run, else; the command that makes it ready, under it.
  const said = unready && setupWords(agent.name) ? setupWords(agent.name) : unready && agent.fix ? agent.said : agent.status && !agent.problem ? statusText({ ...agent, on: true }) : agent.problem || agent.state || "";
  const line = el("small", { id, role: "status", class: agent.problem || unready ? "agent-problem" : "agent-state", title: agent.state || undefined, html: codeHtml(said) });
  // Sign in (or Install) where it says so, its command to copy beside; signed in, Sign in again.
  const fix = unready && agent.fix ? el("small", { class: "agent-note" }, setupLine(agent) || fixLine(agent.fix)) : null;
  const again =
    !unready && agent.status === "ready" && agent.ways?.length
      ? el("button", { type: "button", class: "link sign-in-again", text: "Sign in again", "aria-label": `Sign in to ${agent.label} again`, onclick: () => openSetup(agent.name, { again: true }) })
      : null;
  const most = Math.max(0, ...(agent.usage || []).map((entry) => entry.used));
  const chip = most >= USED_CHIP ? el("span", { class: `chip ${most >= 100 ? "blocked" : "waiting"}`, text: `${most}% used`, title: usageLine(agent.usage) }) : null;
  const usage = agent.usage?.length ? el("small", { class: "agent-usage", text: usageLine(agent.usage) }) : null;
  // A plan's limit as its workers last heard it, the one line its sign-in calls for, and what
  // its workers do differently sandboxed.
  const notes = [agent.limit_line, ...(agent.problem ? [] : [agent.note, agent.sandbox_note])].filter(Boolean).map((text) => el("small", { class: "agent-note", text }));
  // Its models by name, as the popover lists them (a model named that it does not list, kept);
  // typed where it lists none.
  const listed = agent.models || [];
  const modelField = listed.length
    ? el(
        "select",
        { name: `agent_${agent.name}_model` },
        el("option", { value: "", text: "Agent's default" }),
        ...listed.map((model) => el("option", { value: model.id, text: model.name || model.id })),
        ...(agent.model && !listed.some((model) => model.id === agent.model) ? [el("option", { value: agent.model, text: agent.model })] : []),
      )
    : el("input", { name: `agent_${agent.name}_model`, placeholder: "Agent's default", value: agent.model || "", autocomplete: "off" });
  if (listed.length) modelField.value = agent.model || "";
  const errorId = `agent-${agent.name}-limit-error`;
  const limit = el("input", { name: `agent_${agent.name}_limit`, type: "text", inputmode: "numeric", placeholder: "No limit", value: agent.limit ? grouped(agent.limit) : "", autocomplete: "off", "aria-describedby": errorId });
  limit.addEventListener("blur", () => {
    const count = checkLimit(limit);
    if (count) limit.value = grouped(count);
  });
  limit.addEventListener("input", () => limit.hasAttribute("aria-invalid") && checkLimit(limit));
  const fields = el(
    "div",
    { class: "agent-fields" },
    el("label", {}, el("span", { text: "Model" }), modelField),
    el("div", { class: "agent-limit" }, el("label", {}, el("span", { text: "Limit per worker (tokens)" }), limit), el("small", { id: errorId, class: "field-error", hidden: true })),
  );
  fields.hidden = !box.checked;
  box.addEventListener("change", () => {
    fields.hidden = !box.checked;
    newWorkersChoices($("#settings-form").agent_new_workers.value);
  });
  const head = el("div", { class: "agent-head" }, agentMark(agent.name), el("strong", { text: agent.label }), chip, again, el("label", { class: "check agent-switch" }, box, " Available for workers"));
  // One that cannot be turned on looks it, its reason in its line.
  return el("div", { class: `agent-row${box.disabled ? " disabled" : ""}`, "data-agent": agent.name }, head, line, fix, usage, ...notes, fields);
}
// New workers start on Copilot or on an agent available above that can run now (as the form
// has it, saved or not): the line shows once another agent is on, with how to ask for one for
// a single task. One turned off there goes back to Copilot.
function newWorkersChoices(chosen) {
  const form = $("#settings-form");
  const on = (agentsLooked?.agents || []).filter((agent) => form[`agent_${agent.name}_enabled`]?.checked);
  const ready = on.filter((agent) => !agent.problem && !(agentsLooked.sandboxed && agent.sandbox));
  const select = form.agent_new_workers;
  // Last used: the agent last chosen for a worker (never the default unless picked).
  select.replaceChildren(el("option", { value: "copilot", text: "Copilot" }), ...ready.map((agent) => el("option", { value: agent.name, text: agent.label })), el("option", { value: "last", text: "Last used" }));
  select.value = chosen === "last" || ready.some((agent) => agent.name === chosen) ? chosen : "copilot";
  $("#agents-choice").hidden = !on.length;
  choicePays(select.value);
  for (const row of document.querySelectorAll("#agents-roles .agents-role")) {
    const runs = on.filter((agent) => row.dataset.agents.split(" ").includes(agent.name));
    const choice = row.querySelector("select");
    const kept = choice.value || choice.dataset.chosen;
    choice.replaceChildren(el("option", { value: "copilot", text: "Copilot" }), ...runs.map((agent) => el("option", { value: agent.name, text: agent.label })));
    choice.value = runs.some((agent) => agent.name === kept) ? kept : "copilot";
    row.hidden = !runs.length;
    // One of them not ready: Sign in (or Install) here too, never hidden.
    const unready = runs.filter((agent) => agent.status && agent.status !== "ready");
    row.querySelector(".role-setup")?.replaceChildren(
      ...unready
        .map((agent) => [agent, setupLine(agent)])
        .filter(([, setup]) => setup)
        .map(([agent, setup]) => el("small", { class: "agent-note" }, agentMark(agent.name), ` ${agent.label}: ${agent.said} `, setup)),
    );
    const fallback = row.querySelector(".agents-unavailable");
    if (fallback) fallback.hidden = choice.value === "copilot";
  }
  // Folded, its line saying where any of them runs other than on Copilot.
  const system = $("#agents-system");
  const shownRoles = [...document.querySelectorAll("#agents-roles .agents-role:not([hidden])")];
  system.hidden = !shownRoles.length;
  const elsewhere = shownRoles
    .map((row) => [row.querySelector(".agents-choice span").textContent.replace(/ runs on$/, ""), row.querySelector("select")])
    .filter(([, choice]) => choice.value !== "copilot")
    .map(([label, choice]) => `${label} on ${choice.selectedOptions[0]?.text || choice.value}`);
  system.querySelector("summary").textContent = ["System agents", ...elsewhere].join(" · ");
  $("#agents-places").hidden = !on.length;
  const hint = $("#agents-hint");
  hint.hidden = !on.length;
  if (on.length) hint.textContent = `For a single task, say \u201cdo this with ${on[0].label}\u201d or type ${on.map((agent) => `@${agent.name}`).join(" or ")}.`;
}
// Under where new workers start: whose plan or key the chosen agent uses, as its row says (for
// Last used, the agent it starts them on now).
function choicePays(chosen) {
  const line = $("#agents-choice-pays");
  if (!line) return;
  const looked = agentsLooked?.agents || [];
  const agent = chosen === "last" ? agentsLooked?.last_used || "copilot" : chosen;
  const pays = agent === "copilot" ? "Uses your Copilot plan" : looked.find((found) => found.name === agent)?.pays || "";
  const named = agent === "copilot" ? "Copilot" : looked.find((found) => found.name === agent)?.label || agent;
  line.textContent = chosen === "last" ? [`Now ${named}`, pays].filter(Boolean).join(" · ") : pays;
  line.hidden = !line.textContent || $("#agents-choice").hidden;
}
// The tab as the form has it, for Save: only once it was looked at, and only what changed.
function agentsBody() {
  if (!agentsLooked?.agents) return null;
  const form = $("#settings-form");
  const body = {};
  for (const agent of agentsLooked.agents) {
    const enabled = form[`agent_${agent.name}_enabled`].checked;
    const model = form[`agent_${agent.name}_model`].value.trim();
    const input = form[`agent_${agent.name}_limit`];
    const limit = checkLimit(input);
    if (limit === null) {
      input.closest(".agent-fields").hidden = false;  // Said beside the field; Save goes no further.
      showSettingsTab("agents");
      throw new Error("Limit per worker: enter a whole number of tokens.");
    }
    if (enabled !== Boolean(agent.enabled) || model !== (agent.model || "") || limit !== (agent.limit || 0)) body[agent.name] = { enabled, model, limit };
  }
  if (form.agent_new_workers.value !== agentsLooked.new_workers) body.new_workers = form.agent_new_workers.value;
  const roles = {};
  const unavailable = {};
  for (const row of document.querySelectorAll("#agents-roles .agents-role")) {
    const choice = row.querySelector("select");
    const value = row.hidden ? "copilot" : choice.value;
    if (value !== choice.dataset.chosen) roles[row.dataset.role] = value;
    const fallback = row.querySelector(".agents-unavailable select");
    if (fallback && fallback.value !== fallback.dataset.chosen) unavailable[row.dataset.role] = fallback.value;
  }
  if (Object.keys(roles).length) body.roles = roles;
  if (Object.keys(unavailable).length) body.role_unavailable = unavailable;
  return Object.keys(body).length ? body : null;
}
// None of the coding agents it supports is on this computer (bug E80): which they are, and
// what to do.
function noAgentWords(supported = ["the Codex CLI"]) {
  const listed = supported.length > 1 ? `${supported.slice(0, -1).join(", ")} or ${supported[supported.length - 1]}` : supported[0];
  return `No supported coding agent found. Supported: ${listed} — install ${supported.length > 1 ? "one" : "it"}, then open this again.`;
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
    renderConnections();
    return true;
  } catch (error) {
    settingsError(error.message);
    return false;
  }
}
// How many watches are on, as written now; "not saved" until they are.
function watchSummary() {
  const snap = state.snapshot;
  const connector = snap.connector || {};
  const written = watchFields().watches;
  const on = written.filter((watch) => watch.enabled !== false).length;
  const unsaved = !sameWatches(written, snap.settings.watches || []);
  const scanned = connector.scanning ? "scanning now…" : connector.scanned_at ? `scanned ${since(connector.scanned_at)}` : "not scanned yet";
  $("#watches-summary").textContent = written.length ? [`${on} on`, unsaved ? "not saved yet" : scanned].filter(Boolean).join(" · ") : "None";
}
export async function saveToken() {
  const form = $("#settings-form");
  const snapshot = await api("/api/slack/token", { method: "POST", body: { token: form.slack_token.value.trim() } });
  state.snapshot = { ...state.snapshot, ...snapshot };
  form.slack_token.value = "";
  showSlackToken();
}
// A token set by the environment is the one used, whatever is saved here: it is changed there.
function showSlackToken() {
  const slack = state.snapshot.slack || {};
  const env = slack.token_source === "env";
  $("#slack-status").textContent = slackStatusText();
  $("#slack-token-save").closest(".row").hidden = env;
  $("#slack-token-remove").hidden = env || !slack.token_saved;
}
