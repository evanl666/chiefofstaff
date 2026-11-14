// DOM helpers, the API client and toasts.

import { byId, otherItem } from "./state.js";
import { select } from "./session.js";
export const $ = (selector) => document.querySelector(selector);
// Controls that reorder the text after them (embeddings, overrides and isolates: U+202A–U+202E,
// U+2066–U+2069): "\u202Eoverride" would read "edirrevo". Shown text goes without them;
// right-to-left letters, marks (U+200E, U+200F) and joiners (U+200C, U+200D) stay.
const BIDI_CONTROLS = /[\u202A-\u202E\u2066-\u2069]/g;
export const withoutBidi = (text) => String(text ?? "").replace(BIDI_CONTROLS, "");
const SHOWN = new Set(["title", "data-tip", "placeholder"]);
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = withoutBidi(value);
    else if (key === "html") node.innerHTML = withoutBidi(value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : SHOWN.has(key) ? withoutBidi(value) : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(typeof child === "string" ? withoutBidi(child) : child);
  }
  return node;
}
function errorText(data, status) {
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) {
    return data.detail.map((item) => (item.type === "string_too_long" && item.ctx?.max_length ? tooLongText(item) : problemText(item))).join("; ");
  }
  return `Request failed (${status})`;
}
// One of the server's validation problems, with the field it is about as the page names it
// ("Parallel workers: Input should be greater than or equal to 1"). A problem the server worded
// itself ("Value error, …") says what it is about.
const fieldName = (item) => (Array.isArray(item.loc) ? item.loc : []).find((part, index) => typeof part === "string" && !(index === 0 && ["body", "query", "path"].includes(part)));
function problemText(item) {
  const text = String(item.msg).replace(/^Value error, /, "");
  const name = fieldName(item);
  if (!name || item.type === "value_error") return text;
  return `${fieldLabel(name)}: ${text}`;
}
function fieldLabel(name) {
  const field = /^[\w-]+$/.test(name) ? document.querySelector(`[name="${name}"]`) : null;
  const label = field?.closest?.("label");
  const shown = (label?.querySelector("span") || label)?.textContent?.trim() || field?.getAttribute?.("aria-label") || "";
  return shown || `${name[0].toUpperCase()}${name.slice(1).replace(/_/g, " ")}`;
}
// The server's "String should have at most 20000 characters", in words.
function tooLongText({ input, ctx }) {
  const count = typeof input === "string" ? [...input].length : 0;
  const limit = Number(ctx.max_length).toLocaleString("en-US");
  return count ? `Too long: ${count.toLocaleString("en-US")} characters, the limit is ${limit}` : `Too long: the limit is ${limit} characters`;
}
// The launch token, kept in this origin's storage (a cookie would reach every local server).
const KEY = "cos.key";
export const launchKey = () => localStorage.getItem(KEY) || "";
export const rememberKey = (key) => localStorage.setItem(KEY, key);
// A URL of ours the browser loads itself where it cannot send a header (the event stream): with
// the token. Never for a link or an image: an address with the key would land in the history, a
// copied link or anything the page hands on (see blobUrl).
export function withKey(url) {
  if (!url || !url.startsWith("/api/")) return url;
  return `${url}${url.includes("?") ? "&" : "?"}key=${encodeURIComponent(launchKey())}`;
}
// A file of ours (an attachment) fetched with the token in a header, as a blob: address of this
// page: shown and opened from there, no address carries the key. The BLOBS used last are kept;
// one let go is fetched again when asked for (see blobLive).
const BLOBS = 200;
const blobs = new Map();
const live = new Set();
export function blobUrl(url) {
  const known = blobs.get(url);
  if (known) {
    blobs.delete(url);
    blobs.set(url, known);
    return known;
  }
  const pending = fetch(url, { headers: { "X-Chief-Key": launchKey() }, credentials: "omit" }).then((response) => {
    if (!response.ok) throw new Error(response.status === 404 ? "That file is no longer here." : `Could not load the file (${response.status}).`);
    return response.blob().then((blob) => {
      const address = URL.createObjectURL(blob);
      live.add(address);
      return address;
    });
  });
  blobs.set(url, pending);
  pending.catch(() => blobs.delete(url));
  if (blobs.size > BLOBS) {
    const [oldest, gone] = blobs.entries().next().value;
    blobs.delete(oldest);
    gone.then((address) => {
      live.delete(address);
      URL.revokeObjectURL(address);
    }, () => {});
  }
  return pending;
}
// Whether a blob: address from blobUrl still opens (one let go no longer does).
export const blobLive = (address) => live.has(address);
// Once a node comes near the screen (images load as they would with loading="lazy"). The observer
// is made on first use: importing this module does nothing.
const watching = new WeakMap();
let nearScreen = null;
export function whenNearScreen(node, callback) {
  nearScreen ||= new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      nearScreen.unobserve(entry.target);
      watching.get(entry.target)?.();
      watching.delete(entry.target);
    }
  }, { rootMargin: "600px" });
  watching.set(node, callback);
  nearScreen.observe(node);
}
export async function api(path, { method = "GET", body } = {}) {
  const init = { method, headers: { "X-Chief-Key": launchKey() }, credentials: "omit" };
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
  if (!response.ok) {
    // With the fields it is about, for a form to show them.
    const fields = Array.isArray(data.detail) ? data.detail.map(fieldName).filter(Boolean) : [];
    throw Object.assign(new Error(errorText(data, response.status)), { fields });
  }
  return data;
}
// Secondary actions behind "⋯" (or behind a named button, `text`): items are { text, onclick,
// danger }; falsy ones are left out. `label` says what they are (on hover; a named button is
// called by its own words, the label describes it); `name`, what "⋯" is called (whose actions).
export function moreMenu(items, label = "More actions", text = "⋯", name = label) {
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
    "aria-label": text === "⋯" ? name : undefined,
    "aria-haspopup": "menu",
    onclick: (event) => {
      event.stopPropagation();
      const open = menu.hidden;
      closeMenus();
      menu.hidden = !open;
      // In a list that scrolls (a row of Monitoring, say), all of it in sight: as it opens, and
      // once it is at its full size.
      if (open) {
        const inSight = () => menu.scrollIntoView?.({ block: "nearest" });
        inSight();
        menu.addEventListener("animationend", inSight, { once: true });
      }
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
// One request at a time per card: two quick clicks (or Enter twice) send once. `key` names the
// card; `node`'s buttons are off while the request is out, back on when it is done or failed.
const inFlight = new Set();
export const busy = (key) => inFlight.has(key);
export async function once(key, node, run) {
  if (inFlight.has(key)) return undefined;
  inFlight.add(key);
  const buttons = node ? [...node.querySelectorAll("button:not(:disabled)")] : [];
  for (const button of buttons) button.disabled = true;
  try {
    return await run();
  } finally {
    inFlight.delete(key);
    for (const button of buttons) button.disabled = false;
  }
}
// A short tooltip of our own (`data-tip`) for what moves: the browser's stays where it was
// shown, over whatever the list moved there since. Ours goes as soon as what it is for moves,
// changes or goes.
let tip = null;
function tipBox() {
  return document.getElementById("tip") || document.body.appendChild(el("div", { id: "tip", role: "tooltip", hidden: true }));
}
export function hideTip() {
  clearTimeout(tip?.timer);
  tip = null;
  const box = document.getElementById("tip");
  if (box) box.hidden = true;
}
function showTip(node) {
  // Gone or moved from under the mouse while it waited: nothing to say about it.
  if (!node.isConnected || !node.matches(":hover")) {
    hideTip();
    return;
  }
  const box = tipBox();
  box.textContent = node.dataset.tip;
  box.hidden = false;
  const at = node.getBoundingClientRect();
  const size = box.getBoundingClientRect();
  const below = at.bottom + 6 + size.height <= window.innerHeight;
  box.style.top = `${below ? at.bottom + 6 : Math.max(4, at.top - size.height - 6)}px`;
  box.style.left = `${Math.max(8, Math.min(at.left, window.innerWidth - size.width - 8))}px`;
  tip.at = at;
}
// After the page is redrawn: the tooltip says what its element says now, or goes with it.
export function checkTip() {
  if (!tip?.at) return;
  const at = tip.node.isConnected && tip.node.dataset.tip ? tip.node.getBoundingClientRect() : null;
  if (!at || Math.abs(at.top - tip.at.top) > 1 || Math.abs(at.left - tip.at.left) > 1) hideTip();
  else if (tipBox().textContent !== tip.node.dataset.tip) showTip(tip.node);
}
function armTip(event) {
  const node = event.target.closest?.("[data-tip]");
  if (node && node === tip?.node) return;
  hideTip();
  if (node?.dataset.tip) tip = { node, timer: setTimeout(() => showTip(node), 350) };
}
document.addEventListener("pointerover", armTip);
// Gone because the page changed under it: the next move of the mouse shows what is there now.
document.addEventListener("pointermove", (event) => !tip && armTip(event));
document.addEventListener("pointerout", (event) => {
  if (tip && !tip.node.contains(event.relatedTarget)) hideTip();
});
for (const type of ["pointerdown", "keydown", "scroll", "wheel"]) document.addEventListener(type, hideTip, true);
// Home's panel: its tooltips are all ours (a native one stays where it was shown, over whatever
// moved there since), and any change in it (a redraw, a row patched, one gone) takes the one
// shown away, so it never points at another item than its own.
function ownTips(root) {
  for (const node of [root, ...root.querySelectorAll("[title]")]) {
    if (!node.hasAttribute?.("title")) continue;
    if (!node.dataset.tip) node.dataset.tip = node.getAttribute("title");
    node.removeAttribute("title");
  }
}
export function watchPanel(panel) {
  ownTips(panel);
  new window.MutationObserver((records) => {
    // One shown goes (what it points at may have moved or changed); one about to show checks
    // its element is still there, under the mouse, when it does.
    if (tip?.at || (tip && !tip.node.isConnected)) hideTip();
    for (const record of records) for (const node of record.addedNodes) if (node.nodeType === 1) ownTips(node);
  }).observe(panel, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["class", "hidden", "open", "style"] });
}
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
    // One card that cannot be drawn (a malformed link in it, say) must not stop the rest.
    if (node && refresh && node.dataset.version !== version) {
      try {
        refresh(node);
      } catch (error) {
        node = broken(error);
      }
    }
    if (!node) {
      try {
        node = build();
      } catch (error) {
        node = broken(error);
      }
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
function broken(error) {
  console.error(error);
  return el("div", { class: "card broken", text: `This card could not be shown: ${error?.message || error}` });
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
// `action`: { text, onclick }, a button in it (Undo), and a little longer to reach it.
export function toast(text, kind = "", sessionId = null, action = null) {
  const node = el("div", {
    class: `toast ${kind}`,
    text,
    onclick: () => {
      if (sessionId && (byId(sessionId) || otherItem(sessionId))) select(sessionId);
      node.remove();
    },
  });
  if (action) {
    node.append(
      el("button", {
        type: "button",
        class: "toast-action",
        text: action.text,
        onclick: (event) => {
          event.stopPropagation();
          node.remove();
          action.onclick();
        },
      }),
    );
  }
  $("#toasts").append(node);
  setTimeout(() => node.remove(), kind === "attention" || action ? 12000 : 6000);
  return node;
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
