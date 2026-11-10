// Chief of Staff's web UI: starts the page and wires its controls.

import { $, api, closeMenus, closeOnBackdrop, rememberKey, submits, toast } from "./dom.js";
import { byId, connectEvents, current, HOME, loadModels, loadState, markRead, routerId, state, viewedId } from "./state.js";
import { externalItem, bindSidebarToggle, loadExternal } from "./sidebar.js";
import { appendMessages, renderHeader, scrollToBottom, select } from "./session.js";
import { closeMentions, mention, onComposerInput, onMentionKey, routerSide, setRecipient } from "./mention.js";
import { openFile } from "./files.js";
import { scheduleHome, showHomeTab } from "./inbox.js";
import { attachmentBody, bindAttachments, hasAttachments, restoreAttachments, showLength, takeAttachments, tooLong } from "./composer.js";
import { bindSettings, openSettings, refreshUsageLink, showUsage } from "./settings.js";
import { bindSplitter } from "./splitter.js";
import { bindStorage } from "./storage.js";
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
    if ((!text && !hasAttachments()) || (sessionId !== HOME && !current() && !other)) return;
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
    // Images go along only when there are any: a server without them takes the message as before.
    const body = (extra = {}) => ({ text, ...extra, ...(files.length ? { attachments: attachmentBody(files) } : {}) });
    try {
      if (to) {
        // Straight to one worker; the Router hears of it with its next message.
        const sent = await api(`/api/sessions/${routerId()}/messages`, { method: "POST", body: body({ to: to.id }) });
        setRecipient(null);
        appendMessages([sent]);
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
      appendMessages([await api(`/api/sessions/${viewedId()}/messages`, { method: "POST", body: body() })]);
    } catch (error) {
      if (other) input.disabled = false;
      input.value = text;
      restoreAttachments(files);
      autosize();
      toast(error.message, "error");
    }
  });
  bindAttachments();
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
  bindUi();
  bindSidebarToggle();
  bindSplitter();
  closeOnBackdrop();
  connectEvents();
  loadModels();
  refreshUsageLink();
  await select(state.selected || HOME);
}
boot();
