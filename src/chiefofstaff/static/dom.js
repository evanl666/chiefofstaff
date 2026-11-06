// DOM helpers, the API client and toasts.

import { byId } from "./state.js";
import { select } from "./session.js";
export const $ = (selector) => document.querySelector(selector);
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}
function errorText(data, status) {
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) {
    return data.detail.map((item) => String(item.msg).replace(/^Value error, /, "")).join("; ");
  }
  return `Request failed (${status})`;
}
export async function api(path, { method = "GET", body } = {}) {
  const init = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    $("#application").hidden = true;
    $("#signin").hidden = false;
    throw new Error(errorText(data, 401));
  }
  // Starlette's own "Not Found": this page is newer than the server that is still running.
  if (response.status === 404 && data.detail === "Not Found") throw new Error("Chief of Staff was updated. Restart it to use this.");
  if (!response.ok) throw new Error(errorText(data, response.status));
  return data;
}
// Secondary actions behind "⋯" (or behind a named button, `text`): items are { text, onclick,
// danger }; falsy ones are left out.
export function moreMenu(items, label = "More actions", text = "⋯") {
  const list = items.filter(Boolean);
  if (!list.length) return null;
  const menu = el(
    "div",
    { class: "menu", role: "menu", hidden: true },
    ...list.map((item) =>
      el("button", {
        type: "button",
        role: "menuitem",
        class: item.danger ? "danger" : "",
        text: item.text,
        onclick: (event) => {
          menu.hidden = true;
          item.onclick(event);
        },
      }),
    ),
  );
  const button = el("button", {
    type: "button",
    class: text === "⋯" ? "more-button" : "menu-button",
    text,
    title: label,
    "aria-label": label,
    "aria-haspopup": "menu",
    onclick: (event) => {
      event.stopPropagation();
      const open = menu.hidden;
      closeMenus();
      menu.hidden = !open;
    },
  });
  return el("span", { class: "menu-wrap" }, button, menu);
}
export function closeMenus(except = null) {
  for (const menu of document.querySelectorAll(".menu:not([hidden])")) {
    if (!menu.contains(except)) menu.hidden = true;
  }
}
document.addEventListener("click", (event) => closeMenus(event.target));
document.addEventListener("keydown", (event) => event.key === "Escape" && closeMenus());
// Long text shows its start, with Show more. `wrapper` gets the content and the toggle.
export function foldable(wrapper, content, text, { chars = 700, lines = 10 } = {}) {
  wrapper.append(content);
  if (text.length <= chars && text.split("\n").length <= lines) return;
  content.classList.add("folded");
  const toggle = el("button", { type: "button", class: "more", text: "Show more" });
  toggle.addEventListener("click", () => {
    const folded = content.classList.toggle("folded");
    toggle.textContent = folded ? "Show more" : "Show less";
  });
  wrapper.append(toggle);
}
// Keeps nodes whose key did not change; `refresh` updates their content when only their
// `version` did, so a button under the mouse is never swapped out mid-click. A card whose key
// changed is rebuilt, and a form the owner has open on it (a half-typed note) moves into the
// new card, with its text and focus, even when the card moves to another section.
export function reconcile(container, items, editing = new Map()) {
  const existing = new Map([...container.children].map((node) => [node.dataset.key, node]));
  const nodes = items.map(({ key, id, version, build, refresh }) => {
    let node = existing.get(key);
    if (node && refresh && node.dataset.version !== version) refresh(node);
    if (!node) {
      node = build();
      const form = id && editing.get(id);
      if (form) {
        const fresh = node.querySelector(":scope > .actions");
        if (fresh) fresh.replaceWith(form);
        else node.append(form);
      }
    }
    node.dataset.key = key;
    if (id) node.dataset.id = id;
    if (version !== undefined) node.dataset.version = version;
    return node;
  });
  if (nodes.length !== container.children.length || nodes.some((node, index) => container.children[index] !== node)) {
    container.replaceChildren(...nodes);
  }
}
// Moving a focused field in the page blurs it: put the focus and cursor back afterwards.
export function keepingFocus(change) {
  const active = document.activeElement;
  const selection = active && "selectionStart" in active ? [active.selectionStart, active.selectionEnd] : null;
  change();
  if (active?.isConnected && document.activeElement !== active) {
    active.focus();
    if (selection?.[0] != null) active.setSelectionRange(...selection);
  }
}
// Enter that submits: not one that confirms a word in an input method (Chinese, Japanese, …).
export const submits = (event) => event.key === "Enter" && !event.isComposing && event.keyCode !== 229;
export function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
export function toast(text, kind = "", sessionId = null) {
  const node = el("div", {
    class: `toast ${kind}`,
    text,
    onclick: () => {
      if (sessionId && byId(sessionId)) select(sessionId);
      node.remove();
    },
  });
  $("#toasts").append(node);
  setTimeout(() => node.remove(), kind === "attention" ? 12000 : 6000);
}
// A click on the dim area around an open dialog closes it as Esc does: through its "cancel"
// event, so a dialog that keeps unsaved work (the diff's comments) decides. Only when the press
// started there too: a text selection that ends outside the dialog closes nothing.
export function closeOnBackdrop() {
  let pressed = null;
  const outside = (dialog, event) => {
    const box = dialog.getBoundingClientRect();
    return event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom;
  };
  document.addEventListener("pointerdown", (event) => {
    const dialog = event.target;
    pressed = dialog instanceof HTMLDialogElement && dialog.open && outside(dialog, event) ? dialog : null;
  });
  document.addEventListener("click", (event) => {
    const dialog = event.target;
    if (dialog !== pressed || !(dialog instanceof HTMLDialogElement) || !dialog.open || !outside(dialog, event)) return;
    pressed = null;
    if (dialog.dispatchEvent(new Event("cancel", { cancelable: true }))) dialog.close();
  });
}
