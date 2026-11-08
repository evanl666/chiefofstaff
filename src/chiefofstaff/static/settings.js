// The settings and usage dialogs.

import { $, api, el, submits, toast } from "./dom.js";
import { lines, plural, since } from "./text.js";
import { closedWorkers, HOME, loadState, sessions, state } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { EFFORTS, renderHeader, select } from "./session.js";
// Settings opens on the tab last used, or the one asked for ("you" for what was learned).
const TAB = "cos.settingsTab";
export function showSettingsTab(tab) {
  const tabs = [...document.querySelectorAll("#settings-form .tab")];
  const shown = tabs.find((section) => section.dataset.tab === tab) || tabs[0];
  localStorage.setItem(TAB, shown.dataset.tab);
  for (const section of tabs) section.hidden = section !== shown;
  for (const button of document.querySelectorAll("#settings-tabs button")) {
    button.setAttribute("aria-selected", String(button.dataset.tab === shown.dataset.tab));
  }
  $("#settings-pane").scrollTop = 0;
}
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
  for (const button of document.querySelectorAll("#settings-tabs button")) {
    button.addEventListener("click", () => showSettingsTab(button.dataset.tab));
  }
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
      mcpServers();
      $("#mcp-error").hidden = true;
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
      $("#slack-status").textContent = slackStatusText();
      $("#slack-token-remove").hidden = true;
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
          el("td", { class: "num", text: `${row.calls} calls` }),
          el("td", { class: "num", text: `${row.credits.toFixed(1)} credits` }),
        ),
      ),
    ),
  );
}
// What a result the owner accepted cost, by the model that did most of a task's work.
function outcomesTable(rows) {
  if (!rows.length) return null;
  return el(
    "div",
    {},
    el("h3", { text: "Per accepted result" }),
    el(
      "table",
      {},
      ...rows.map((row) =>
        el(
          "tr",
          {},
          el("td", { text: row.model || "unknown" }),
          el("td", { class: "num", text: `${row.accepted} of ${row.tasks} accepted` }),
          el("td", { class: "num", text: row.per_accepted === null ? "–" : `${row.per_accepted.toFixed(1)} credits each` }),
        ),
      ),
    ),
  );
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
    $("#usage-total").replaceChildren(
      el("strong", { text: `${total.credits.toFixed(1)} credits` }),
      el("span", { text: `${total.premium.toFixed(0)} premium requests` }),
      el("span", { text: `${total.calls} model calls` }),
      el("span", { text: `${Math.round((total.input_tokens + total.output_tokens) / 1000)}k tokens` }),
    );
    const roles = { router: "Router", slack: "Slack", worker: "Workers", watchdog: "Watchdog" };
    $("#usage-tables").replaceChildren(
      ...[
        usageTable("By session", usage.by_session, (row) => `${row.title || "Deleted session"}${row.closed_at ? " (closed)" : ""}`, (row) => {
          $("#usage-dialog").close();
          select(row.session_id);
        }),
        outcomesTable(usage.outcomes || []),
        usageTable("By model", usage.by_model, (row) => row.model || "unknown"),
        usageTable("By role", usage.by_role, (row) => roles[row.role] || row.role),
      ].filter(Boolean),
    );
    if (!total.calls) $("#usage-tables").replaceChildren(el("div", { class: "line", text: "No model calls in this range." }));
  } catch (error) {
    toast(error.message, "error");
  }
}
export function slackStatusText() {
  const slack = state.snapshot.slack;
  const parts = [slack.token_saved ? "Token saved." : "No token yet."];
  if (slack.error) parts.push(slack.error);
  else if (slack.connected) parts.push(`Watching${slack.last_checked ? `, checked ${since(slack.last_checked)}` : ""}.`);
  if (slack.token_saved && slack.connected && !slack.can_send) parts.push("This token cannot send messages (chat:write).");
  return parts.join(" ");
}
export function fillSettings() {
  const form = $("#settings-form");
  const settings = state.snapshot.settings;
  form.projects.value = settings.projects.join("\n");
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
  form.watchdog_enabled.checked = settings.watchdog_enabled;
  form.slack_enabled.checked = settings.slack_enabled;
  form.slack_token.value = "";
  form.slack_poll_seconds.value = settings.slack_poll_seconds;
  form.slack_delegate_emoji.value = settings.slack_delegate_emoji ? `:${settings.slack_delegate_emoji}:` : "";
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
  const servers = settings.mcp_servers || {};
  form.mcp_servers.value = Object.keys(servers).length ? JSON.stringify(servers, null, 2) : "";
  $("#mcp-error").hidden = true;
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
  renderConnections();
  $("#slack-status").textContent = slackStatusText();
  $("#slack-token-remove").hidden = !state.snapshot.slack.token_saved;
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
function mcpServers() {
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
    if (!config || typeof config !== "object" || !(typeof config.command === "string" || typeof config.url === "string")) {
      throw new Error(`MCP server ${name}: give a command (local) or a url (remote).`);
    }
    // As Copilot checks them (one wrong field and it never starts on a message): see models.py.
    const texts = (v) => Array.isArray(v) && v.every((item) => typeof item === "string");
    const table = (v) => !!v && typeof v === "object" && !Array.isArray(v) && Object.values(v).every((item) => typeof item === "string");
    const string = (v) => typeof v === "string";
    const checks = {
      type: [(v) => ["local", "stdio", "http", "sse"].includes(v), "local, stdio, http or sse"],
      tools: [texts, 'a list of tool names in quotes (["*"] for all)'],
      args: [texts, "a list of strings"],
      env: [table, "an object of strings"],
      headers: [table, "an object of strings"],
      cwd: [string, "a string"],
      working_directory: [string, "a string"],
      timeout: [(v) => Number.isInteger(v) && v > 0, "a whole number of milliseconds"],
    };
    const bad = Object.entries(checks).find(([key, [fits]]) => key in config && !fits(config[key]));
    if (bad) throw new Error(`MCP server ${name}: ${bad[0]} must be ${bad[1][1]}.`);
  }
  return value;
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
  $("#abilities-summary").textContent = [abilities.skills.length ? plural(abilities.skills.length, "skill") : "", (settings.plugins || []).length ? plural(settings.plugins.length, "plugin") : "", servers ? plural(servers, "MCP server") : ""].filter(Boolean).join(" · ") || "None added";
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
  const scanned = connector.scanning
    ? "Scanning now…"
    : connector.scanned_at
      ? `Scanned ${since(connector.scanned_at)}${last.found !== undefined ? ` · ${last.found} found` : ""}${problems ? ` · ${problems}` : ""}`
      : "";
  watchSummary();
  $("#watch-state").textContent = scanned;
  // After hours (the schedule above): how often it scans now, and why; "not scanning" only when
  // after hours is 0. Nothing to say while it scans, or with no watch on.
  const resting = { weekend: "Weekend", outside_hours: "Outside work hours" }[connector.after_hours_reason];
  const watching = (settings.watches || []).some((watch) => watch.enabled !== false);
  const pace = Number(settings.watch_after_hours_minutes ?? 30);
  const restingText = !resting || connector.scanning || !watching ? "" : pace > 0 ? `${resting}: every ${pace} min (after-hours setting)` : `Not scanning now: ${resting.toLowerCase()}, and after hours is 0`;
  $("#watch-resting").textContent = restingText;
  $("#watch-resting").hidden = !restingText;
  $("#watch-state").classList.toggle("error", Boolean(problems));
  $("#watch-scan").disabled = Boolean(connector.scanning);
  $("#watch-activity").hidden = !connector.session_id;
  const away = connector.away || {};
  const tried = away.last || {};
  $("#away-summary").textContent = settings.away_notify ? `On${away.where ? ` · ${away.where}` : ""}` : "Off";
  $("#away-state").textContent = tried.at ? (tried.ok ? `Reached you${tried.where ? ` via ${tried.where}` : ""} ${since(tried.at)}` : `Could not reach you ${since(tried.at)}: ${tried.error || "unknown error"}`) : "";
  $("#away-state").classList.toggle("error", Boolean(tried.at && !tried.ok));
  // What this way can do, as tried: sending, reading your replies, new requests.
  const can = away.can || {};
  const rows = !settings.away_notify || !tried.at
    ? []
    : [
        [can.sends, can.sends ? `Sends: yes, last ${since(can.sent_at)}` : "Sends: not working now"],
        [
          can.reads_replies,
          !can.reads_replies
            ? "Reads your replies: no — answer in Chief of Staff"
            : can.replies_verified_at
              ? `Reads your replies: yes (a reply came back ${since(can.replies_verified_at)})`
              : "Reads your replies: said so, not tried yet — Send a test and answer it",
        ],
        [false, "New requests by reply: no — a reply answers what was sent, by its code"],
      ];
  $("#away-can").replaceChildren(...rows.map(([ok, text]) => el("li", { class: ok ? "ok" : "no", text })));
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
// A habit the owner writes down themselves: everywhere, in Slack replies, or in one project.
function renderLessonForm() {
  const scope = $("#lesson-scope");
  const projects = state.snapshot.settings.projects || [];
  const chosen = scope.value;
  scope.replaceChildren(el("option", { value: "", text: "Everywhere" }), el("option", { value: "slack", text: "Slack replies" }), ...projects.map((path) => el("option", { value: path, text: `In ${path.split("/").pop()}` })));
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
async function saveSettings(event) {
  event.preventDefault();
  const form = $("#settings-form");
  const settings = state.snapshot.settings;
  const body = {
    // Settings this page does not show are sent back as they are, not reset.
    ...settings,
    projects: lines(form.projects.value),
    code_folder: form.code_folder.value.trim(),
    scratch_directory: form.scratch_directory.value.trim(),
    max_workers: Number(form.max_workers.value),
    worker_yolo: form.worker_yolo.checked,
    worker_sandbox: form.worker_sandbox.checked,
    shared_caches: form.shared_caches.checked,
    sandbox_writable: lines(form.sandbox_writable.value),
    worker_idle_hours: Number(form.worker_idle_hours.value || 24),
    stall_hours: Number(form.stall_hours.value || 4),
    mac_notifications: form.mac_notifications.checked,
    watchdog_enabled: form.watchdog_enabled.checked,
    slack_enabled: form.slack_enabled.checked,
    slack_poll_seconds: Number(form.slack_poll_seconds.value),
    slack_delegate_emoji: form.slack_delegate_emoji.value.trim(),
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
    ...watchFields(),
    away_notify: form.away_notify.checked,
    away_minutes: Number(form.away_minutes.value || 10),
    away_how: form.away_how.value.trim(),
  };
  if ("report_footer" in settings) body.report_footer = form.report_footer.checked;
  try {
    body.mcp_servers = mcpServers();
  } catch (error) {
    $("#conn-abilities").open = true;
    showSettingsTab("connections");
    settingsError(error.message);
    return;
  }
  try {
    if (form.slack_token.value.trim()) await saveToken();
    let snapshot = await api("/api/settings", { method: "POST", body });
    const initialNotes = form.setup_notes.dataset.initial || "";
    if (form.setup_notes.value.trim() !== initialNotes.trim()) {
      snapshot = await api("/api/setup-notes", { method: "POST", body: { text: form.setup_notes.value, initial: initialNotes } });
    }
    if (form.profile.value.trim() !== (state.snapshot.profile || "")) {
      snapshot = await api("/api/profile", { method: "POST", body: { text: form.profile.value } });
    }
    state.snapshot = { ...state.snapshot, ...snapshot };
    $("#settings-dialog").close();
    renderSidebar();
    renderHeader();
  } catch (error) {
    settingsError(error.message);
  }
}
// The watches as written in the form, with when they run.
function watchFields() {
  const form = $("#settings-form");
  return {
    watches: [...document.querySelectorAll("#watch-list li")]
      .map((row) => ({ what: row.querySelector(".what").value.trim(), how: row.querySelector(".how").value.trim(), enabled: row.querySelector(".on").checked }))
      .filter((watch) => watch.what),
    watch_hours: `${$("#watch-from").value || "09:00"}-${$("#watch-to").value || "18:00"}`,
    watch_minutes: Number(form.watch_minutes.value || 5),
    watch_after_hours_minutes: Number(form.watch_after_hours_minutes.value || 0),
  };
}
const sameWatches = (a, b) => JSON.stringify(a.map(({ what, how, enabled }) => [what, how || "", enabled !== false])) === JSON.stringify(b.map(({ what, how, enabled }) => [what, how || "", enabled !== false]));
// Only the watches, saved: the rest of the form stays as the owner left it, unsaved.
async function saveAway() {
  settingsError("");
  const form = $("#settings-form");
  const settings = state.snapshot.settings;
  const fields = { away_notify: form.away_notify.checked, away_minutes: Number(form.away_minutes.value || 10), away_how: form.away_how.value.trim() };
  if (fields.away_notify === Boolean(settings.away_notify) && fields.away_minutes === (settings.away_minutes ?? 10) && fields.away_how === (settings.away_how || "")) return true;
  try {
    state.snapshot = { ...state.snapshot, ...(await api("/api/settings", { method: "POST", body: { ...settings, ...fields } })) };
    renderConnections();
    return true;
  } catch (error) {
    settingsError(error.message);
    return false;
  }
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
    state.snapshot = { ...state.snapshot, ...(await api("/api/settings", { method: "POST", body: { ...settings, ...fields } })) };
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
  $("#slack-status").textContent = slackStatusText();
  $("#slack-token-remove").hidden = false;
}
