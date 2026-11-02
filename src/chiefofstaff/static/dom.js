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
