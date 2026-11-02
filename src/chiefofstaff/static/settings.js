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
  form.scratch_directory.value = settings.scratch_directory;
  form.scratch_directory.placeholder = `${state.snapshot.data_directory}/work/workspaces`;
  form.max_workers.value = settings.max_workers;
  form.worker_yolo.checked = settings.worker_yolo;
  form.worker_sandbox.checked = settings.worker_sandbox;
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
  form.worker_credit_limit.value = settings.worker_credit_limit || "";
  form.digest_time.value = settings.digest_time;
  form.profile.value = state.snapshot.profile || "";
  form.setup_notes.value = notesText();
  form.setup_notes.dataset.initial = form.setup_notes.value;
  $("#profile-draft").hidden = !state.snapshot.profile_draft;
  form.paused.checked = settings.paused;
  $("#slack-status").textContent = slackStatusText();
  $("#slack-token-remove").hidden = !state.snapshot.slack.token_saved;
  const github = state.snapshot.github;
  $("#github-status").textContent = github.error || (github.connected ? `Watching, checked ${ago(github.last_checked)} ago.` : "");
  $("#data-directory").textContent = `Data: ${state.snapshot.data_directory}`;
  $("#settings-error").hidden = true;
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
    scratch_directory: form.scratch_directory.value.trim(),
    max_workers: Number(form.max_workers.value),
    worker_yolo: form.worker_yolo.checked,
    worker_sandbox: form.worker_sandbox.checked,
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
