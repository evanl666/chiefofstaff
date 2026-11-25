// Chief of Staff's web UI: starts the page and wires its controls.

import { $, api, closeMenus, closeOnBackdrop, FRESH_START, rememberKey, startFresh, submits, toast } from "./dom.js";
import { byId, connectEvents, current, HOME, loadModels, loadState, markRead, othersOn, routerId, state, viewedId } from "./state.js";
import { externalItem, bindSidebarToggle, loadExternal } from "./sidebar.js";
import { agentModels, answerSending, appendMessages, chatAnchor, dropSending, followAnchor, forgetUnsent, noteUnsent, renderHeader, repeatSending, scrolledByOwner, scrollToBottom, select, showSending, unsentFor } from "./session.js";
import { closeMentions, mention, onComposerInput, onMentionKey, routerSide, setAgent, setRecipient } from "./mention.js";
import { openFile } from "./files.js";
import { scheduleHome, showHomeTab } from "./inbox.js";
import { attachmentBody, bindAttachments, hasAttachments, restoreAttachments, showLength, takeAttachments, tooLong } from "./composer.js";
import { bindSettings, openSettings, refreshUsageLink, showUsage } from "./settings.js";
import { bindSplitter } from "./splitter.js";
import { bindStorage } from "./storage.js";
import { watchFocus } from "./agentsetup.js";
import { leaveOut, linkAlone, noteLink, openSharedLink, withCardBody } from "./share.js";
import { isTeamView } from "./team.js";
import { tellTeam } from "./teampage.js";
// Whether Other Copilot sessions are unfolded (kept with the setting on).
const FOLD = "cos.section.external";
function bindUi() {
  const input = $("#composer-input");
  const autosize = () => {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
    showLength();
  };
  input.addEventListener("input", () => {
    autosize();
    onComposerInput();
    noteLink(state.selected === HOME);
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
    if (isTeamView()) {
      // Tell the team (teampage.js): to the parts in its To line, only on this Send.
      if (!text) return;
      const over = tooLong(text);
      if (over) return toast(over, "error");
      input.value = "";
      autosize();
      if (!(await tellTeam(text))) {
        input.value = text;
        autosize();
      }
      return;
    }
    const other = externalItem();
    if ((!text && !hasAttachments()) || (sessionId !== HOME && !current() && !other)) return;
    if (sessionId === HOME && !hasAttachments() && linkAlone(text)) {
      // A share link alone: Chief of Staff opens its card itself; it goes to no session.
      input.value = "";
      autosize();
      noteLink(true);
      try {
        await openSharedLink(text, (message) => appendMessages([message]));
      } catch (error) {
        input.value = text;
        autosize();
        noteLink(true);
        toast(error.message, "error");
      }
      return;
    }
    if (sessionId === HOME && !routerId()) {
      toast("Your chief of staff starts once Copilot is ready.", "error");
      return;
    }
    // Refused here, with the limit in words: the text stays to be shortened.
    const over = tooLong(text);
    if (over) {
      toast(over, "error");
      return;
    }
    const files = takeAttachments();
    input.value = "";
    autosize();
    const to = routerSide() ? mention.to : null;
    // The agent chosen with @ for what this asks (only from Home, only while agents are on),
    // with its model and effort; or several, to compare.
    const agent = routerSide() && !to && mention.agent ? mention.agent.name : "";
    const compare = routerSide() && !to ? mention.agents : [];
    const chosen = agent
      ? { agent, ...(mention.agent.model ? { agent_model: mention.agent.model } : {}), ...(mention.agent.effort ? { agent_effort: mention.agent.effort } : {}) }
      : compare.length
        ? { agents: compare }
        : {};
    // Shown at once, until it is stored (not to another session continued here: its composer
    // says so). The same again while it is on its way is not sent twice: it stands out instead.
    const chat = other ? "" : viewedId();
    const key = JSON.stringify([chat, to?.id || "", text, chosen, files.map((file) => [file.name, file.url.length])]);
    if (chat && repeatSending(key)) return;
    // Words back from a send this page lost on a reload go again under its id (never twice).
    const clientId = chat ? unsentFor(chat, text, to?.id)?.id || newClientId() : "";
    if (chat) {
      noteUnsent({ id: clientId, chat, text, to: to ? { id: to.id, title: to.title } : null, images: files.length });
      showSending(sendingMessage({ clientId, chat, text, to, chosen, files }), key);
    }
    // Images go along only when there are any: a server without them takes the message as before.
    const body = (extra = {}) => ({ text, ...extra, ...(files.length ? { attachments: attachmentBody(files) } : {}), ...(clientId ? { client_id: clientId } : {}) });
    try {
      if (to) {
        // Straight to one worker; the Router hears of it with its next message.
        const sent = await api(`/api/sessions/${routerId()}/messages`, { method: "POST", body: body({ to: to.id }) });
        forgetUnsent(clientId);
        setRecipient(null);
        // Home shows its copy ("→ worker").
        answerSending(clientId, sent.copy || null);
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
        state.others.delete(sessionId);
        await loadState();
        await select(sessionId);
        return;
      }
      // About the task of the worker's the owner looks at in its chat (several to choose from);
      // and the line on its folder's settings it shows, when it waits for one (their word on
      // it is for that line alone).
      const task = chatAnchor();
      const held = byId(viewedId())?.settings_hold?.digest;
      // On Home, a card someone shared goes along, quoted as information (Start a task from this).
      const card = sessionId === HOME ? withCardBody() : {};
      const extra = { ...chosen, ...card, ...(task ? { task_id: task } : {}), ...(held ? { settings_digest: held } : {}) };
      const sent = await api(`/api/sessions/${chat}/messages`, { method: "POST", body: body(extra) });
      forgetUnsent(clientId);
      answerSending(clientId, sent);
      if (agent || compare.length) setAgent(null);
      if (card.shared_card) leaveOut();
    } catch (error) {
      if (other) input.disabled = false;
      // Cut off by the page going away (a reload): whether it was stored is looked up once the
      // page is back (session.js recoverUnsent); its words are not kept as a draft meanwhile.
      if (unloading && !error.answered) return;
      dropSending(clientId);
      // Answered with an error, it was not taken: as before, its words back to change or send
      // again (a new send). Never answered (the connection broke), it may have been: sent
      // again, it goes under the same id.
      if (error.answered) forgetUnsent(clientId);
      if (error.fresh) {
        // Kept for its worker (its Copilot session can't be reopened): not to be sent twice.
        toast(error.message, "error", null, { text: FRESH_START, onclick: () => startFresh(error.fresh) });
        return;
      }
      input.value = text;
      restoreAttachments(files);
      autosize();
      toast(error.message, "error");
    }
  });
  bindAttachments();
  // In a worker's chat, what is written goes with the task in view: its placeholder says which.
  $("#scroller").addEventListener("scroll", followAnchor, { passive: true });
  for (const type of ["wheel", "touchmove"]) $("#scroller").addEventListener(type, scrolledByOwner, { passive: true });
  // The owner is here: what needs them is not sent elsewhere meanwhile (When you're away).
  let seen = 0;
  const present = () => {
    if (document.visibilityState !== "visible" || Date.now() - seen < 60000) return;
    seen = Date.now();
    api("/api/presence", { method: "POST" }).catch(() => {});
  };
  for (const type of ["keydown", "mousemove", "pointerdown"]) window.addEventListener(type, present, { passive: true });
  document.addEventListener("visibilitychange", present);
  for (const button of document.querySelectorAll("#home-tabs button")) {
    button.addEventListener("click", () => showHomeTab(button.dataset.tab, true));
  }
  // Wider again, the chat comes back beside the panel: its end in view, as when it was left.
  window.matchMedia("(max-width: 1099px)").addEventListener("change", (event) => {
    if (!event.matches && state.selected === HOME && state.homeTab !== "chat") scrollToBottom(true);
  });
  // A path in a message or report: a file in a worker's folders opens; anything else is copied.
  document.addEventListener("click", (event) => {
    const chip = event.target.closest(".path");
    if (!chip) return;
    // In an open file: a path relative to the session that wrote it.
    const session = chip.closest("[data-session]")?.dataset.session || viewedId() || "";
    openFile(chip.dataset.path, session);
  });
  bindMenuKeys();
  $("#settings-button").addEventListener("click", () => openSettings());
  $("#back-home").addEventListener("click", () => select(HOME));
  $("#session-more-button").addEventListener("click", (event) => {
    event.stopPropagation();
    const menu = $("#session-more");
    const open = menu.hidden;
    closeMenus();
    if (open && current()?.agent === "claude") agentModels(current());
    if (open) renderHeader();  // As the session is now.
    menu.hidden = !open;
  });
  bindSettings();
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
  bindStorage();
  $("#external").addEventListener("toggle", (event) => {
    if (event.target.open) loadExternal(true);
    // With the setting on, folded or not is the owner's choice, kept.
    if (othersOn()) localStorage.setItem(FOLD, event.target.open ? "1" : "0");
  });
  window.addEventListener("focus", () => {
    const session = byId(viewedId());
    if (session?.unread) markRead(session.id);
  });
  setInterval(() => {
    if ($("#external").open) loadExternal();
  }, 60000);
  // Running cards say how long a step has taken: refreshed while Home is shown.
  setInterval(scheduleHome, 30000);
}
// The page is going away (a reload, a closed tab): a send cut off by it is not one that failed.
let unloading = false;
window.addEventListener("beforeunload", () => (unloading = true));
window.addEventListener("pagehide", () => (unloading = true));
window.addEventListener("pageshow", () => (unloading = false));
// The page's own id for one send (the server knows it by it: never sent twice).
function newClientId() {
  return [...window.crypto.getRandomValues(new Uint8Array(16))].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
// What a send looks like in its chat until it is stored, as the stored one will.
function sendingMessage({ clientId, chat, text, to, chosen, files }) {
  const meta = {
    client_id: clientId,
    ...(to ? { to: to.id, to_title: to.title } : {}),
    ...chosen,
    ...(files.length ? { attachments: files.map(({ name, type, url }) => ({ name, type, url })) } : {}),
  };
  return { id: 0, session_id: chat, kind: "owner", text, meta, delivery: "sending", created_at: new Date().toISOString() };
}
// The "⋯" menus (dom.js moreMenu) from the keyboard: opened with Enter, Space or an arrow key,
// the focus is on an item; arrow keys, Home and End move among them; Escape closes it with the
// focus back on its button, Tab closes it and goes on.
const MENU_BUTTON = ".menu-wrap > button[aria-haspopup]";
const menuItems = (menu) => (menu && !menu.hidden ? [...menu.querySelectorAll('[role="menuitem"]:not(:disabled)')] : []);
function bindMenuKeys() {
  // Before the button's own click (which stops it there), then once its menu shows.
  document.addEventListener("click", (event) => {
    const button = event.target.closest?.(MENU_BUTTON);
    if (button && event.detail === 0) requestAnimationFrame(() => menuItems(button.nextElementSibling)[0]?.focus());
  }, true);
  document.addEventListener("keydown", (event) => {
    const button = event.target.closest?.(MENU_BUTTON);
    if (button && ["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      if (button.nextElementSibling?.hidden) button.click();
      const items = menuItems(button.nextElementSibling);
      items[event.key === "ArrowUp" ? items.length - 1 : 0]?.focus();
      return;
    }
    const menu = event.target.closest?.('[role="menu"]');
    if (!menu) return;
    const items = menuItems(menu);
    const opener = menu.parentElement?.querySelector(`:scope > button[aria-haspopup]`);
    if (event.key === "Escape") {
      event.preventDefault();
      menu.hidden = true;
      opener?.focus();
      return;
    }
    if (event.key === "Tab") {
      menu.hidden = true;
      return;
    }
    const at = items.indexOf(event.target);
    const next = { ArrowDown: at + 1, ArrowUp: at - 1, Home: 0, End: items.length - 1 }[event.key];
    if (next === undefined || !items.length) return;
    event.preventDefault();
    items[(next + items.length) % items.length].focus();
  });
}
async function boot() {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (token) {
    const response = await fetch("/api/auth", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
    if (response.ok) rememberKey(token);
    history.replaceState(null, "", "/");
  }
  try {
    await loadState();
  } catch {
    return;
  }
  $("#application").hidden = false;
  // With the setting on, Other Copilot sessions start unfolded (read as on unfolding), until
  // the owner folds them.
  if (othersOn()) $("#external").open = localStorage.getItem(FOLD) !== "0";
  bindUi();
  bindSidebarToggle();
  bindSplitter();
  closeOnBackdrop();
  connectEvents();
  watchFocus();
  loadModels();
  refreshUsageLink();
  await select(state.selected || HOME);
}
boot();
