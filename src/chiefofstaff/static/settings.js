// The settings and usage dialogs.

import { $, api, el, toast } from "./dom.js";
import { ago, lines } from "./text.js";
import { state } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { EFFORTS, renderHeader, select } from "./session.js";
function startOfDay(date) {
  const day = new Date(date);
  day.setHours(0, 0, 0, 0);
  return day.getTime() / 1000;
}
export async function refreshUsageLink() {
  try {
    const usage = await api(`/api/usage?since=${startOfDay(new Date())}`);
    $("#usage-button").textContent = `Today · ${usage.total.credits.toFixed(1)} credits`;
  } catch {
    $("#usage-button").textContent = "Usage";
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
  if (range === "today") since = startOfDay(new Date());
  else if (range === "custom") {
    if (!form.from.value) return;
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
  else if (slack.connected) parts.push(`Watching${slack.last_checked ? `, checked ${ago(slack.last_checked)} ago` : ""}.`);
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
  $("#slack-status").textContent = slackStatusText();
  $("#slack-token-remove").hidden = !state.snapshot.slack.token_saved;
  const github = state.snapshot.github;
  $("#github-status").textContent = github.error || (github.connected ? `Watching, checked ${ago(github.last_checked)} ago.` : "");
  $("#data-directory").textContent = `Data: ${state.snapshot.data_directory}`;
  $("#settings-error").hidden = true;
}
// What sessions learned from the owner's feedback, and how often the owner now agrees.
const SCOPE = (scope) => (!scope ? "everywhere" : scope === "slack" ? "Slack replies" : `in ${scope.replace("project:", "").split("/").pop()}`);
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
        el("span", { text: lesson.text }),
        el("small", { text: SCOPE(lesson.scope) }),
        el("button", {
          type: "button",
          class: "link",
          text: "Remove",
          onclick: async () => {
            try {
              state.snapshot = { ...state.snapshot, ...(await api(`/api/lessons/${lesson.id}/remove`, { method: "POST" })) };
              renderLearning();
            } catch (error) {
              settingsError(error.message);
            }
          },
        }),
      ),
    ),
  );
  if (!learning.lessons.length) $("#lessons").append(el("li", { class: "empty-note", text: "No habits yet." }));
  const habits = state.snapshot.habits;
  if (habits) {
    $("#styles").replaceChildren(
      ...habits.styles.map((style) =>
        el(
          "li",
          {},
          el("div", { class: "style-text" }, el("strong", { text: `${style.title} · v${style.version}` }), el("small", { text: style.text })),
          el("button", { type: "button", class: "link", text: "Undo", title: style.version > 1 ? "Back to the previous version" : "Stop using it", onclick: () => memoryAction(`/api/style/${style.section}/undo`) }),
        ),
      ),
    );
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
          { class: check.status === "disabled" ? "off" : "" },
          el(
            "div",
            { class: "style-text" },
            el("strong", { text: `${check.name} · ${check.repo}` }),
            el("small", { text: `${check.why} Caught ${check.caught} of ${check.runs} run${check.runs === 1 ? "" : "s"}.` }),
            el("code", { text: check.command }),
          ),
          el("button", { type: "button", class: "link", text: check.status === "active" ? "Turn off" : "Turn on", onclick: () => memoryAction(`/api/checks/${check.id}/${check.status === "active" ? "disable" : "enable"}`) }),
        ),
      ),
    );
    if (!checks.sandbox) $("#checks").append(el("li", { class: "empty-note", text: "Needs a sandbox (macOS sandbox-exec, or bwrap on Linux)." }));
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
export async function saveSettings(event) {
  event.preventDefault();
  const form = $("#settings-form");
  const body = {
    projects: lines(form.projects.value),
    code_folder: form.code_folder.value.trim(),
    scratch_directory: form.scratch_directory.value.trim(),
    max_workers: Number(form.max_workers.value),
    worker_yolo: form.worker_yolo.checked,
    worker_sandbox: form.worker_sandbox.checked,
    shared_caches: form.shared_caches.checked,
    sandbox_writable: lines(form.sandbox_writable.value),
    worker_idle_hours: Number(form.worker_idle_hours.value || 24),
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
  };
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
export async function saveToken() {
  const form = $("#settings-form");
  const snapshot = await api("/api/slack/token", { method: "POST", body: { token: form.slack_token.value.trim() } });
  state.snapshot = { ...state.snapshot, ...snapshot };
  form.slack_token.value = "";
  $("#slack-status").textContent = slackStatusText();
  $("#slack-token-remove").hidden = false;
}
