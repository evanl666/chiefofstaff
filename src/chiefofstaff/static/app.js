// Chief of Staff's web UI: starts the page and wires its controls.

import { $, api, submits, toast } from "./dom.js";
import { byId, connectEvents, current, loadModels, loadState, markRead, routerId, state } from "./state.js";
import { externalItem, loadExternal } from "./sidebar.js";
import { appendMessages, select } from "./session.js";
import { closeMentions, mention, onComposerInput, onMentionKey, routerSide, setRecipient } from "./mention.js";
import { openFile } from "./files.js";
import { INBOX, scheduleInbox } from "./inbox.js";
import { fillSettings, refreshUsageLink, renderLearning, saveSettings, saveToken, settingsError, showUsage, slackStatusText } from "./settings.js";
function bindUi() {
  const input = $("#composer-input");
  const autosize = () => {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  };
  input.addEventListener("input", () => {
    autosize();
    onComposerInput();
  });
  // Closed once focus is elsewhere (a click on the list keeps it: mousedown is prevented there).
  input.addEventListener("blur", () => setTimeout(() => document.activeElement !== input && closeMentions(), 150));
  input.addEventListener("keydown", (event) => {
    if (onMentionKey(event)) return;
    if (submits(event) && !event.shiftKey) {
      event.preventDefault();
      $("#composer").requestSubmit();
    }
  });
  $("#composer").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    const sessionId = state.selected;
    const other = externalItem();
    if (!text || (sessionId !== INBOX && !current() && !other)) return;
    input.value = "";
    autosize();
    const to = routerSide() ? mention.to : null;
    try {
      if (to) {
        // Straight to one worker; the Router hears of it with its next message.
        const sent = await api(`/api/sessions/${routerId()}/messages`, { method: "POST", body: { text, to: to.id } });
        setRecipient(null);
        // A worker deep in a step reads it only when the step ends: said here, where the owner is.
        toast(sent.note ? `Sent to “${to.title}”. ${sent.note.split("; said")[0]}` : `Sent to “${to.title}”`);
        return;
      }
      if (other) {
        // Another Copilot session: the message continues it here, as a worker.
        input.disabled = true;
        input.placeholder = "Continuing it here…";
        await api(`/api/external/${sessionId}/messages`, { method: "POST", body: { text } });
        state.external = state.external.filter((item) => item.id !== sessionId);
        await loadState();
        await select(sessionId);
        return;
      }
      if (sessionId === INBOX) {
        await api(`/api/sessions/${routerId()}/messages`, { method: "POST", body: { text } });
        state.chief = { asked: text, replies: [] };
        scheduleInbox();
        return;
      }
      appendMessages([await api(`/api/sessions/${sessionId}/messages`, { method: "POST", body: { text } })]);
    } catch (error) {
      if (other) input.disabled = false;
      input.value = text;
      autosize();
      toast(error.message, "error");
    }
  });
  // A path in a message or report: a file in a worker's folders opens; anything else is copied.
  document.addEventListener("click", (event) => {
    const chip = event.target.closest(".path");
    if (!chip) return;
    // In an open file: a path relative to the session that wrote it.
    const session = chip.closest("[data-session]")?.dataset.session || (state.selected !== INBOX ? state.selected : "");
    openFile(chip.dataset.path, session);
  });
  $("#settings-button").addEventListener("click", () => {
    fillSettings();
    $("#settings-dialog").showModal();
    // Feedback comes in all the time; what was learned is read fresh.
    api("/api/state")
      .then((data) => {
        state.snapshot.learning = data.learning;
        renderLearning();
      })
      .catch(() => {});
  });
  $("#settings-cancel").addEventListener("click", () => $("#settings-dialog").close());
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
      $("#settings-dialog").close();
      await select(INBOX);
      toast("Building your profile. Its draft comes to your inbox to check before it is used.");
    } catch (error) {
      settingsError(error.message);
    }
  });
  $("#learn-now").addEventListener("click", async () => {
    try {
      await api("/api/lessons/learn", { method: "POST" });
      $("#settings-dialog").close();
      toast("Learning from your feedback. You review the lessons before they are used.");
    } catch (error) {
      settingsError(error.message);
    }
  });
  $("#usage-button").addEventListener("click", () => {
    $("#usage-dialog").showModal();
    showUsage("today");
  });
  for (const button of document.querySelectorAll("#usage-dialog [data-range]")) {
    button.addEventListener("click", () => showUsage(button.dataset.range));
  }
  for (const input of document.querySelectorAll("#usage-dialog input[type=date]")) {
    input.addEventListener("change", () => showUsage("custom"));
  }
  setInterval(refreshUsageLink, 60000);
  $("#settings-form").addEventListener("submit", saveSettings);
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
  $("#external").addEventListener("toggle", (event) => {
    if (event.target.open) loadExternal(true);
  });
  window.addEventListener("focus", () => {
    const session = byId(state.selected);
    if (session?.unread) markRead(session.id);
  });
  setInterval(() => {
    if ($("#external").open) loadExternal();
  }, 60000);
  // Running cards say how long a step has taken: refreshed while the inbox is shown.
  setInterval(() => {
    if (state.selected === INBOX) scheduleInbox();
  }, 30000);
}
async function boot() {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (token) {
    await fetch("/api/auth", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
    history.replaceState(null, "", "/");
  }
  try {
    await loadState();
  } catch {
    return;
  }
  $("#application").hidden = false;
  bindUi();
  connectEvents();
  loadModels();
  refreshUsageLink();
  await select(state.selected || INBOX);
}
boot();
