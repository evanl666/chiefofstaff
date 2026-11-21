// Teams (E129, E130): workers that work together. Each worker has a short name and a color,
// the same everywhere; what they tell and ask each other is folded on Home.

import { el } from "./dom.js";
import { firstLine } from "./text.js";
import { byId, closedWorkers, state } from "./state.js";

// A color per worker (and per team), from its id: the same on every page and after a reload.
const COLORS = 8;
export function colorOf(id = "") {
  let hash = 0;
  for (const char of id) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
  return hash % COLORS;
}
// A worker's short name, from its title: the part before ":", "·", "(" or " - ", cut at a word
// to 24 characters. (The owner's own name for it, when they gave one, comes from the server.)
export function shortLabel(title = "") {
  const whole = title.replace(/\s+/g, " ").trim();
  const head = whole.split(/\s*[:·|(]\s*|\s+[-\u2013\u2014]\s+/)[0].trim() || whole;
  if (head.length <= 24) return head;
  const cut = head.slice(0, 25);
  const at = cut.lastIndexOf(" ");
  return (at >= 8 ? cut.slice(0, at) : head.slice(0, 24)).replace(/[\s,;.]+$/, "");
}
const workerOf = (id) => byId(id) || closedWorkers().find((item) => item.id === id);
export function labelOf(id, title = "") {
  const worker = workerOf(id);
  return worker?.label || shortLabel(worker?.title || title) || "a worker";
}
export function initials(label) {
  const words = label.split(/[\s/_-]+/).filter((word) => /[\p{L}\p{N}]/u.test(word));
  if (!words.length) return "?";
  const first = [...words[0]];
  return (words.length > 1 ? first[0] + [...words[1]][0] : first.slice(0, 2).join("")).toUpperCase();
}
export function avatar(id, title = "") {
  const label = labelOf(id, title);
  return el("span", { class: `avatar c${colorOf(id)}`, text: initials(label), title: label });
}
// What a Router "peer" row says: who told, asked or answered whom, of which question, and the
// words. Rows from before these were marked say it in their text only.
const PEER = /^“(.+?)” (told|asked|answered) (?:“(.+?)”|the Router): ([\s\S]*)$/;
export function peerOf(message) {
  const meta = message.meta || {};
  const found = (message.text || "").match(PEER);
  return {
    from: meta.peer_from || "",
    to: meta.peer_to || "",
    kind: meta.peer_kind || found?.[2] || "told",
    question: meta.question_id || "",
    fromTitle: found?.[1] || "",
    toTitle: found?.[3] || "",
    text: found ? found[4] : message.text || "",
  };
}
export const peerLine = (peer) => `${labelOf(peer.from, peer.fromTitle)} ${peer.kind} ${labelOf(peer.to, peer.toTitle)}: ${firstLine(peer.text)}`;
// Questions workers wait on each other for, still open.
export const peerWaiting = () => state.snapshot?.peer_waiting || [];
export function ago(iso, now = Date.now()) {
  const minutes = Math.max(0, Math.round((now - new Date(iso)) / 60000));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours} h` : `${Math.floor(hours / 24)} d`;
}
// "1 waiting 9 min": how many of these questions are still open, and since when the oldest.
export function waitingWords(questions) {
  const open = peerWaiting().filter((item) => questions.has(item.id));
  return open.length ? `${open.length} waiting ${ago(open[0].asked)}` : "";
}
