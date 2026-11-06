// The border between Home's chat and its panel (Needs you, Tasks): dragged, or moved with the
// arrow keys, it sets the panel's width, kept for next time. A double click sets it back.

import { $ } from "./dom.js";

const KEY = "cos.panelWidth";
const MIN = 280;
const CHAT_MIN = 360;  // The chat keeps room to read and write.
const STEP = 32;

const limits = () => {
  const body = $("#body").getBoundingClientRect();
  return [MIN, Math.max(MIN, body.width - CHAT_MIN)];
};
const clamp = (width) => {
  const [low, high] = limits();
  return Math.round(Math.min(high, Math.max(low, width)));
};
function apply(width, save = true) {
  const panel = $("#panel");
  const handle = $("#panel-resizer");
  if (width === null) {
    panel.style.removeProperty("width");
    localStorage.removeItem(KEY);
  } else {
    panel.style.width = `${clamp(width)}px`;
    if (save) localStorage.setItem(KEY, String(clamp(width)));
  }
  const [low, high] = limits();
  handle.setAttribute("aria-valuemin", String(low));
  handle.setAttribute("aria-valuemax", String(high));
  handle.setAttribute("aria-valuenow", String(Math.round(panel.getBoundingClientRect().width)));
}

export function bindSplitter() {
  const handle = $("#panel-resizer");
  const panel = $("#panel");
  const saved = Number(localStorage.getItem(KEY));
  if (saved) apply(saved, false);
  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add("resizing");
    const right = $("#body").getBoundingClientRect().right;
    const move = (moved) => apply(right - moved.clientX, false);
    const end = (ended) => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", end);
      handle.removeEventListener("pointercancel", end);
      document.body.classList.remove("resizing");
      apply(right - ended.clientX);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
  });
  handle.addEventListener("dblclick", () => apply(null));
  handle.addEventListener("keydown", (event) => {
    const width = panel.getBoundingClientRect().width;
    const change = { ArrowLeft: STEP, ArrowRight: -STEP }[event.key];
    if (change) apply(width + change);
    else if (event.key === "Home") apply(limits()[1]);
    else if (event.key === "End") apply(MIN);
    else return;
    event.preventDefault();
  });
  // A narrower window keeps the chat its room.
  window.addEventListener("resize", () => {
    if (panel.style.width) panel.style.width = `${clamp(Number.parseFloat(panel.style.width))}px`;
  });
}
