"use strict";

const $ = (selector) => document.querySelector(selector);
let state = null;
let selectedTask = null;
let settingsLoaded = false;
let approvalSignature = "";
let memorySignature = "";
let refreshing = false;
let authenticated = false;

function node(tag, text = "", className = "") {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function notice(message, error = false) {
  const box = $("#notice");
  box.textContent = message;
  box.className = error ? "error" : "";
  box.hidden = false;
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
    }
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg).join("; ")
      : payload.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return payload;
}

function button(text, callback, className = "secondary") {
  const element = node("button", text, className);
  element.type = "button";
  element.addEventListener("click", async () => {
    element.disabled = true;
    try {
      await callback();
      await refresh();
    } catch (error) {
      notice(error.message, true);
    } finally {
      element.disabled = false;
    }
  });
  return element;
}

function page(name) {
  document.querySelectorAll(".page").forEach((element) => {
    element.hidden = element.id !== `page-${name}`;
  });
  document.querySelectorAll(".nav").forEach((element) => {
    element.classList.toggle("active", element.dataset.page === name);
  });
}

document.querySelectorAll(".nav").forEach((element) => {
  element.addEventListener("click", () => page(element.dataset.page));
});

function bindForm(id, handler) {
  $(id).addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const submit = form.querySelector("button:not([type=button])");
    submit.disabled = true;
    try {
      await handler(form, new FormData(form));
      await refresh();
    } catch (error) {
      notice(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });
}

async function unlock(token) {
  await api("/auth", "POST", { token });
  authenticated = true;
  $("#login").hidden = true;
  $("#application").hidden = false;
}

bindForm("#login-form", async (form, data) => {
  await unlock(data.get("token"));
  form.reset();
});

bindForm("#task-form", async (form, data) => {
  const result = await api("/tasks", "POST", Object.fromEntries(data));
  selectedTask = result.id;
  form.reset();
  notice("Task queued. Your Lead will pick it up.");
});

bindForm("#settings-form", async (form, data) => {
  const settings = {
    ...Object.fromEntries(data),
    max_workers: Number(data.get("max_workers")),
    timeout_seconds: Number(data.get("timeout_seconds")),
    slack_enabled: data.has("slack_enabled"),
    paused: state.settings.paused,
    slack_channels: data.get("slack_channels").split(",").map((value) => value.trim()).filter(Boolean),
  };
  const result = await api("/settings", "POST", settings);
  notice(result.slack.error || "Settings saved.", Boolean(result.slack.error));
});

bindForm("#credentials-form", async (form, data) => {
  const result = await api("/slack/credentials", "POST", Object.fromEntries(data));
  form.reset();
  notice(result.slack.error || "Credentials saved in the OS keyring.", Boolean(result.slack.error));
});

bindForm("#distill-form", async (form, data) => {
  const result = await api("/distill", "POST", {
    material: data.get("material"),
    source_files: data.get("source_files").split("\n").map((value) => value.trim()).filter(Boolean),
  });
  selectedTask = result.id;
  page("work");
  notice("Lead is building an initial profile. Confirm useful candidates before they become memory.");
});

bindForm("#feedback-form", async (form, data) => {
  if (!selectedTask) throw new Error("Select a task first.");
  await api(`/tasks/${selectedTask}/feedback`, "POST", { text: data.get("text") });
  form.reset();
  notice("Feedback recorded. A learning task will propose evidence-backed memories.");
});

bindForm("#memory-form", async (form, data) => {
  await api("/memories", "POST", Object.fromEntries(data));
  form.reset();
  notice("Confirmed memory saved locally.");
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
    notice(result.version + " " + (result.note || ""));
  } catch (error) { notice(error.message, true); }
});

$("#reconnect-slack").addEventListener("click", async () => {
  try {
    const result = await api("/slack/reconnect", "POST");
    notice(result.error || (result.connected ? "Slack connected." : "Slack is disabled."), Boolean(result.error));
    await refresh();
  } catch (error) { notice(error.message, true); }
});

function renderApprovals(approvals) {
  const signature = JSON.stringify(approvals);
  if (signature === approvalSignature) return;
  approvalSignature = signature;
  const container = $("#approvals");
  container.replaceChildren();
  if (!approvals.length) {
    container.append(node("div", "Nothing waiting on you. Your buddy will ask when it needs a decision.", "empty"));
  }
  for (const approval of approvals) {
    const card = node("div", "", "approval");
    const task = state.tasks.find((item) => item.id === approval.task_id);
    card.append(node("div", `${approval.kind.toUpperCase()} · ${task?.title || approval.task_id}`, "eyebrow"));
    card.append(node("p", approval.question));
    const payload = approval.payload;
    if (approval.kind === "files") {
      card.append(node("div", payload.project_path, "path"));
      for (const file of payload.files) {
        card.append(node("strong", file.path));
        card.append(node("pre", file.diff || "(No text difference)"));
      }
    } else if (approval.kind === "slack") {
      card.append(node("div", `To ${payload.channel} · thread ${payload.thread_ts}`, "path"));
      card.append(node("pre", payload.text));
    } else if (approval.kind === "memory") {
      card.append(node("p", payload.text));
      card.append(node("p", `${payload.category} · ${payload.scope} · ${payload.project_path || "No project"}`, "muted"));
      card.append(node("p", `Evidence: ${payload.evidence}`, "muted"));
    }
    const reason = node("textarea");
    reason.rows = 2;
    reason.maxLength = 5000;
    reason.placeholder = "Optional: explain your choice so Lead can learn.";
    reason.setAttribute("aria-label", `Reason for ${approval.question}`);
    card.append(reason);
    const buttons = node("div", "", "button-row");
    for (const option of approval.options) {
      buttons.append(button(option, async () => {
        await api(`/approvals/${approval.id}`, "POST", { choice: option, reason: reason.value });
        notice("Decision recorded. Any learning will appear as a candidate, not an automatic rule.");
      }, option === "Reject" ? "secondary" : ""));
    }
    card.append(buttons);
    container.append(card);
  }
}

function renderTasks(tasks) {
  const container = $("#tasks");
  container.replaceChildren();
  if (!tasks.length) container.append(node("div", "Give your buddy a goal to start.", "empty"));
  for (const task of tasks) {
    const item = button("", async () => {
      selectedTask = task.id;
      await renderDetail();
    }, `task ${task.parent_id ? "worker" : ""} ${selectedTask === task.id ? "selected" : ""}`);
    item.append(node("strong", task.title));
    const meta = node("div", "", "task-meta");
    meta.append(node("span", task.status.replaceAll("_", " "), `badge ${task.status}`));
    meta.append(node("span", `${task.role} · ${task.kind} · ${task.backend}`));
    item.append(meta);
    container.append(item);
  }
}

async function renderDetail() {
  if (!selectedTask) return;
  const requested = selectedTask;
  const task = await api(`/tasks/${requested}`);
  if (selectedTask !== requested) return;
  $("#task-empty").hidden = true;
  $("#task-detail").hidden = false;
  $("#detail-kind").textContent = `${task.role.toUpperCase()} / ${task.kind.toUpperCase()} / ${task.backend.toUpperCase()}`;
  $("#detail-title").textContent = task.title;
  $("#detail-status").textContent = `${task.status.replaceAll("_", " ")} · ${task.rounds}/8 invocations`;
  $("#detail-instructions").textContent = task.instructions;
  $("#detail-result").textContent = task.result || "No result yet.";
  $("#detail-error").textContent = task.error;
  $("#detail-error").hidden = !task.error;
  const actions = $("#detail-actions");
  actions.replaceChildren();
  if (["failed", "interrupted"].includes(task.status)) {
    actions.append(button("Retry explicitly", () => api(`/tasks/${task.id}/retry`, "POST")));
  }
  if (!["completed", "cancelled", "failed", "interrupted"].includes(task.status)) {
    actions.append(button("Cancel task", () => api(`/tasks/${task.id}/cancel`, "POST"), "danger"));
  }
  const outcomes = $("#detail-approvals");
  outcomes.replaceChildren();
  for (const approval of task.approvals) {
    const history = node("details");
    history.append(node("summary", `${approval.kind}: ${approval.status}${approval.choice ? ` · ${approval.choice}` : ""}`));
    history.append(node("pre", JSON.stringify({
      question: approval.question, payload: approval.payload, reason: approval.reason,
    }, null, 2)));
    outcomes.append(history);
  }
}

function renderMemories(memories) {
  const signature = JSON.stringify(memories);
  if (signature === memorySignature) return;
  memorySignature = signature;
  const container = $("#memories");
  container.replaceChildren();
  if (!memories.length) container.append(node("div", "No confirmed memories yet. Start with distillation or add one explicitly.", "empty"));
  for (const memory of memories) {
    const card = node("div", "", "memory");
    card.append(node("p", memory.text));
    card.append(node("div", `${memory.category} · ${memory.scope} · ${memory.project_path || "No project"}`, "muted"));
    card.append(node("p", `Evidence: ${memory.evidence}`, "muted"));
    card.append(button("Remove from future context", async () => {
      await api(`/memories/${memory.id}`, "DELETE");
      notice("Removed from confirmed memory. Historical tasks and feedback remain in the local database.");
    }, "danger"));
    container.append(card);
  }
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state = await api("/state");
    authenticated = true;
    $("#login").hidden = true;
    $("#application").hidden = false;
    const mode = state.settings.backend === "demo" ? "DEMO — simulated outputs" : "Copilot CLI";
    $("#connection-status").textContent = `${mode} · ${state.active_count} active · Slack ${state.slack.connected ? "connected" : "disconnected"}${state.slack.error ? `: ${state.slack.error}` : ""}`;
    $("#pause").textContent = state.settings.paused ? "Resume new work" : "Pause new work";
    $("#metric-running").textContent = state.tasks.filter((task) => task.status === "running").length;
    $("#metric-pending").textContent = state.approvals.length;
    $("#metric-done").textContent = state.tasks.filter((task) => task.status === "completed").length;
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
    renderApprovals(state.approvals);
    renderTasks(state.tasks);
    renderMemories(state.memories);
    const events = $("#events");
    events.replaceChildren();
    for (const event of state.events.slice(0, 30)) {
      const line = node("div", "", "event");
      const timestamp = node("time", new Date(event.created_at).toLocaleTimeString());
      timestamp.dateTime = event.created_at;
      line.append(timestamp, node("span", `${event.kind}: ${event.message}`));
      events.append(line);
    }
    await renderDetail();
  } catch (error) {
    notice(error.message, true);
  } finally {
    refreshing = false;
  }
}

async function start() {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (token) {
    history.replaceState(null, "", location.pathname);
    try { await unlock(token); }
    catch (error) { notice(error.message, true); return; }
  }
  await refresh();
  setInterval(() => { if (authenticated) refresh(); }, 2500);
}
start();
