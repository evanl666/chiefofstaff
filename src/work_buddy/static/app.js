"use strict";

const $ = (selector) => document.querySelector(selector);
let state = null;
let selectedTask = null;
let settingsLoaded = false;
let refreshing = false;
let authenticated = false;
let desiredProfile = location.hash === "#profile";
const signatures = new Map();
const approvalReasons = new Map();
const statuses = {
  queued: "On it shortly", running: "Working on it", receiving: "Reading the context",
  waiting_workers: "Making progress", waiting_approval: "Waiting for you",
  completed: "Ready", failed: "Needs attention", interrupted: "Paused unexpectedly",
  cancelled: "Stopped", ignored: "No action needed",
};

function node(tag, text = "", className = "") {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function notice(message, error = false) {
  const boxes = [$("#notice"), ...document.querySelectorAll("dialog[open] .dialog-notice")];
  for (const box of boxes) {
    box.textContent = message;
    box.classList.toggle("error", error);
    box.hidden = false;
  }
}

async function api(path, method = "GET", data) {
  const response = await fetch(`/api${path}`, {
    method,
    headers: data === undefined ? {} : { "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
    credentials: "same-origin",
  });
  const payload = await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      authenticated = false;
      $("#login").hidden = false;
      $("#application").hidden = true;
      document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
    }
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg).join("; ") : payload.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return payload;
}

function button(text, callback, className = "secondary") {
  const element = node("button", text, className);
  element.type = "button";
  element.addEventListener("click", async () => {
    element.disabled = true;
    try { await callback(); await refresh(); }
    catch (error) { notice(error.message, true); }
    finally { element.disabled = false; }
  });
  return element;
}

function openDialog(id) {
  if (!authenticated) return;
  const dialog = document.getElementById(id);
  if (!dialog.open) dialog.showModal();
}
document.querySelectorAll("[data-open]").forEach((element) => {
  element.addEventListener("click", () => openDialog(element.dataset.open));
});
document.querySelectorAll("[data-close]").forEach((element) => {
  element.addEventListener("click", () => element.closest("dialog").close());
});

function bindForm(id, handler) {
  $(id).addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const submit = form.querySelector("button:not([type=button])");
    submit.disabled = true;
    try { await handler(form, new FormData(form)); await refresh(); }
    catch (error) { notice(error.message, true); }
    finally { submit.disabled = false; }
  });
}

async function unlock(token) {
  await api("/auth", "POST", { token });
  authenticated = true;
  $("#login").hidden = true;
  $("#application").hidden = false;
}
bindForm("#login-form", async (form, data) => { await unlock(data.get("token")); form.reset(); });

bindForm("#daily-form", async (form, data) => {
  await api("/daily", "POST", { text: data.get("text") });
  form.reset();
  notice("Got it. I'll organize these priorities and keep an eye on the work coming in.");
});

bindForm("#settings-form", async (form, data) => {
  const list = (name) => data.get(name).split(",").map((value) => value.trim()).filter(Boolean);
  const settings = {
    ...Object.fromEntries(data),
    max_workers: Number(data.get("max_workers")),
    timeout_seconds: Number(data.get("timeout_seconds")),
    slack_enabled: data.has("slack_enabled"),
    github_enabled: data.has("github_enabled"),
    paused: state.settings.paused,
    slack_channels: list("slack_channels"),
    github_repos: list("github_repos"),
  };
  const result = await api("/settings", "POST", settings);
  if (!result.slack.error) $("#settings-dialog").close();
  notice(result.slack.error || "Settings saved. You're ready to hand over your day.", Boolean(result.slack.error));
});

bindForm("#credentials-form", async (form, data) => {
  const result = await api("/slack/credentials", "POST", Object.fromEntries(data));
  form.reset();
  notice(result.slack.error || "Credentials saved in the OS keyring.", Boolean(result.slack.error));
});

bindForm("#distill-form", async (form, data) => {
  await api("/distill", "POST", {
    material: data.get("material"),
    source_files: data.get("source_files").split("\n").map((value) => value.trim()).filter(Boolean),
  });
  notice("I'll build and save your initial profile. Review the observations here when they're ready.");
});

bindForm("#feedback-form", async (form, data) => {
  if (!selectedTask) throw new Error("Choose an update to reply to.");
  await api(`/tasks/${selectedTask}/feedback`, "POST", { text: data.get("text") });
  form.reset();
  notice("Thanks. I'll learn from this in the background; new observations appear in What Buddy knows.");
});

$("#pause").addEventListener("click", async () => {
  if (!state) return;
  try {
    await api("/settings", "POST", { ...state.settings, paused: !state.settings.paused });
    await refresh();
  } catch (error) { notice(error.message, true); }
});
$("#check-agent").addEventListener("click", async () => {
  try {
    const result = await api("/agent/check", "POST");
    notice(`${result.version} ${result.note || ""}`);
  } catch (error) { notice(error.message, true); }
});
$("#reconnect-slack").addEventListener("click", async () => {
  try {
    const result = await api("/slack/reconnect", "POST");
    notice(result.error || (result.connected ? "Slack connected." : "Slack is disabled."), Boolean(result.error));
    await refresh();
  } catch (error) { notice(error.message, true); }
});

function renderApprovals(selector, approvals) {
  const container = $(selector);
  if (container.contains(document.activeElement) && document.activeElement.matches("textarea")) return;
  const signature = JSON.stringify(approvals);
  if (signatures.get(selector) === signature) return;
  signatures.set(selector, signature);
  container.replaceChildren();
  for (const approval of approvals) {
    const card = node("div", "", "approval");
    const task = state.tasks.find((item) => item.id === approval.task_id);
    card.append(node("div", task?.title || "A note from Buddy", "eyebrow"));
    card.append(node("p", approval.question));
    const payload = approval.payload;
    if (approval.kind === "files") {
      for (const file of payload.files) {
        card.append(node("strong", file.path));
        card.append(node("pre", file.diff || "(No text difference)"));
      }
    } else if (approval.kind === "slack") {
      card.append(node("div", `Reply to ${payload.channel}, thread ${payload.thread_ts}`, "hint"));
      card.append(node("pre", payload.text));
    } else if (approval.kind === "review") {
      card.append(node("div", `${payload.repo}#${payload.number} at ${payload.head_sha.slice(0, 8)}`, "hint"));
      card.append(node("pre", payload.body));
    } else if (approval.kind === "memory") {
      card.append(node("p", payload.text));
      card.append(node("p", `Evidence: ${payload.evidence}`, "hint"));
      card.append(node("p", payload.scope === "general" ? "General working preference" : "This project only", "hint"));
    }
    const reason = node("textarea");
    reason.rows = 2;
    reason.maxLength = 5000;
    reason.value = approvalReasons.get(approval.id) || "";
    reason.placeholder = "Anything I should understand about your choice? Optional.";
    reason.setAttribute("aria-label", `Reason for ${approval.question}`);
    reason.addEventListener("input", () => approvalReasons.set(approval.id, reason.value));
    if (approval.kind !== "memory") card.append(reason);
    const buttons = node("div", "", "button-row");
    for (const option of approval.options) {
      buttons.append(button(option, async () => {
        await api(`/approvals/${approval.id}`, "POST", { choice: option, reason: reason.value });
        approvalReasons.delete(approval.id);
        notice(approval.kind === "memory" ? "Remembered locally for future work." : "Understood. I'll take it from here.");
      }, option === "Reject" ? "secondary" : ""));
    }
    card.append(buttons);
    container.append(card);
  }
}

function renderUpdates() {
  const updates = state.updates || [];
  const signature = JSON.stringify(updates);
  if (signatures.get("updates") === signature) return;
  signatures.set("updates", signature);
  const container = $("#updates");
  container.replaceChildren();
  if (!updates.length) container.append(node("p", "Your day starts with a little direction. Updates will appear here.", "empty"));
  for (const task of updates.slice(0, 15)) {
    const card = node("article", "", "update");
    const top = node("div", "", "update-top");
    top.append(node("span", task.kind === "inbox" ? "Picked up for you" : "Your priorities"));
    top.append(node("span", statuses[task.status] || task.status, `status ${task.status}`));
    card.append(top, node("h3", task.title));
    const preview = task.error || task.preview;
    if (preview) card.append(node("p", preview.length > 360 ? `${preview.slice(0, 360)}...` : preview));
    card.append(button("Read update / give feedback", async () => {
      if (selectedTask !== task.id) $("#feedback-form").reset();
      selectedTask = task.id;
      await renderDetail();
      openDialog("task-dialog");
    }, "quiet"));
    container.append(card);
  }
}

async function renderDetail() {
  if (!selectedTask) return;
  const requested = selectedTask;
  const task = await api(`/tasks/${requested}`);
  if (selectedTask !== requested) return;
  $("#detail-kind").textContent = task.kind === "inbox" ? "PICKED UP FOR YOU" : "FROM YOUR LEAD";
  $("#detail-title").textContent = task.title;
  $("#detail-status").textContent = statuses[task.status] || task.status;
  $("#detail-instructions").textContent = task.instructions;
  $("#detail-result").textContent = task.result || "I'm working on this. I'll share an update here.";
  $("#detail-error").textContent = task.error;
  $("#detail-error").hidden = !task.error;
  const actions = $("#detail-actions");
  actions.replaceChildren();
  if (["failed", "interrupted"].includes(task.status)) {
    actions.append(button("Try again", () => api(`/tasks/${task.id}/retry`, "POST")));
  }
  if (!["completed", "cancelled", "failed", "interrupted", "ignored"].includes(task.status)) {
    actions.append(button("Stop this task", () => api(`/tasks/${task.id}/cancel`, "POST"), "danger"));
  }
  $("#detail-workers").replaceChildren(...state.tasks.filter((item) => item.parent_id === task.id)
    .map((item) => node("p", `${item.title}: ${statuses[item.status] || item.status}`, "hint")));
  const outcomes = $("#detail-approvals");
  outcomes.replaceChildren();
  for (const approval of task.approvals) {
    const history = node("details");
    history.append(node("summary", `${approval.kind}: ${approval.status}`));
    history.append(node("pre", JSON.stringify({
      question: approval.question, payload: approval.payload, reason: approval.reason,
    }, null, 2)));
    outcomes.append(history);
  }
}

function renderProfile() {
  const pending = state.approvals.filter((item) => item.kind === "memory");
  $("#memory-badge").textContent = pending.length || "";
  $("#memory-review-section").hidden = !pending.length;
  renderApprovals("#memory-approvals", pending);
  const profile = state.initial_profile;
  $("#onboarding").hidden = Boolean(profile && !["failed", "interrupted", "cancelled"].includes(profile.status));
  $("#profile-status").textContent = profile
    ? profile.error || (profile.status === "running" || profile.status === "queued"
      ? "Lead is reading the context and building your initial profile."
      : profile.result || "Your profile is saved locally.")
    : "Lead builds an initial profile from your connected project. Add background if you like; you don't need to write the profile.";
  const active = state.profile_tasks.filter((task) => ["running", "queued"].includes(task.status));
  $("#learning-status").textContent = active.length
    ? "Learning from your context and feedback in the background."
    : pending.length ? `${pending.length} observations to review in What Buddy knows.` : "";
  const signature = JSON.stringify(state.memories);
  if (signatures.get("memories") !== signature) {
    signatures.set("memories", signature);
    const container = $("#memories");
    container.replaceChildren();
    if (!state.memories.length) container.append(node("p", "Nothing confirmed yet. Your profile will grow as we work together.", "hint"));
    for (const memory of state.memories) {
      const card = node("div", "", "memory");
      card.append(node("p", memory.text));
      const evidence = node("details");
      evidence.append(node("summary", memory.scope === "general" ? "Why I remember this" : "For this project"));
      evidence.append(node("p", memory.evidence, "hint"));
      evidence.append(button("Forget this observation", async () => {
        await api(`/memories/${memory.id}`, "DELETE");
        notice("Removed from future memory. Historical tasks and feedback remain on this computer.");
      }, "quiet"));
      card.append(evidence);
      container.append(card);
    }
  }
  $("#profile-history").replaceChildren(...state.profile_tasks.map((task) => {
    const row = node("p", `${task.kind === "distill" ? "Initial profile" : "Feedback learning"}: ${statuses[task.status] || task.status}`, "hint");
    if (task.error) row.append(node("span", ` - ${task.error}`, "error-text"));
    return row;
  }));
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state = await api("/state");
    authenticated = true;
    $("#login").hidden = true;
    $("#application").hidden = false;
    $("#day-label").textContent = new Date(`${state.today}T12:00:00`).toLocaleDateString(undefined, {
      weekday: "long", month: "long", day: "numeric",
    }).toUpperCase();
    const prefix = state.settings.backend === "demo" ? "Demo mode. " : "";
    $("#lead-status").textContent = prefix + (state.settings.paused
      ? "New work is paused." : state.active_count ? "I'm moving things forward." : "Ready when you are.");
    const slack = state.settings.slack_enabled ? (state.slack.connected ? "Watching Slack" : "Slack needs attention") : "Slack not connected";
    const github = state.settings.github_enabled ? (state.github.error ? "GitHub needs attention" : state.github.connected ? "Watching review requests" : "Connecting GitHub reviews") : "PR requests not connected";
    $("#watch-status").textContent = `${slack} / ${github}`;
    $("#connection-status").textContent = `${state.settings.backend} / Slack: ${state.slack.error || (state.slack.connected ? "connected" : "off")} / GitHub: ${state.github.error || (state.github.connected ? "connected" : "off")}`;
    $("#pause").textContent = state.settings.paused ? "Resume new work" : "Pause new work";
    $("#data-location").textContent = state.data_directory;
    $("#memory-location").textContent = `${state.data_directory}/memory/confirmed.json`;
    if (!settingsLoaded) {
      const form = $("#settings-form");
      for (const [key, value] of Object.entries(state.settings)) {
        const input = form.elements.namedItem(key);
        if (!input) continue;
        if (input.type === "checkbox") input.checked = value;
        else input.value = Array.isArray(value) ? value.join(", ") : value;
      }
      settingsLoaded = true;
    }
    $("#brief-history").hidden = !state.daily_briefs.length;
    $("#briefs").replaceChildren(...state.daily_briefs.map((brief) => node("pre", brief.text)));
    const decisions = state.approvals.filter((item) => item.kind !== "memory");
    $("#decision-panel").hidden = !decisions.length;
    renderApprovals("#approvals", decisions);
    renderUpdates();
    renderProfile();
    $("#events").replaceChildren(...state.events.slice(0, 20).map((event) => {
      const line = node("div", "", "event");
      line.append(node("time", new Date(event.created_at).toLocaleTimeString()), node("span", `${event.kind}: ${event.message}`));
      return line;
    }));
    if ($("#task-dialog").open) await renderDetail();
    if (desiredProfile) { desiredProfile = false; openDialog("profile-dialog"); }
  } catch (error) { notice(error.message, true); }
  finally { refreshing = false; }
}

async function start() {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (token) {
    history.replaceState(null, "", location.pathname);
    try { await unlock(token); } catch (error) { notice(error.message, true); }
  }
  await refresh();
  setInterval(() => { if (authenticated) refresh(); }, 2500);
}
start();
