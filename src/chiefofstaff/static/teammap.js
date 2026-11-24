// Teams lite's flow map (teams-v2.md §6): a node per part, left to right as the work moves (top
// to bottom when three columns do not fit), one line per pair of parts with one label that says
// what it carries, and the same in words for a screen reader. Static: nothing moves.

import { el } from "./dom.js";

const SVG = "http://www.w3.org/2000/svg";
function svg(tag, attributes = {}, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attributes)) if (value !== undefined && value !== null) node.setAttribute(key, value);
  node.append(...children.filter(Boolean));
  return node;
}
const NODE = 152;
const GAP = 92;
const HEADS = { open: "amber", owner: "amber", failed: "red", msg: "soft", pending: "muted", done: "muted" };
const SHIFT = { right: "translate(-100%, -50%)", left: "translate(0, -50%)", above: "translate(-50%, -100%)", below: "translate(-50%, 0)", center: "translate(-50%, -50%)" };

// Columns by what waits on what: a part with no predecessor in the map is in the first; one
// that comes after another, one to the right of it. Rows in the order parts joined.
export function layout(parts, lines) {
  const before = new Map(parts.map((part) => [part.id, []]));
  for (const line of lines) if (line.kind === "flow" && before.has(line.to) && before.has(line.from)) before.get(line.to).push(line.from);
  const column = new Map();
  const depth = (id, seen = new Set()) => {
    if (column.has(id)) return column.get(id);
    if (seen.has(id)) return 0;
    seen.add(id);
    const found = Math.max(-1, ...before.get(id).map((other) => depth(other, seen))) + 1;
    column.set(id, found);
    return found;
  };
  for (const part of parts) depth(part.id);
  // Many side by side: four a column at most, the rest in the next (a long column reads badly).
  const depths = Math.max(0, ...column.values()) + 1;
  const firstOf = [];
  let columns = 0;
  for (let at = 0; at < depths; at++) {
    firstOf.push(columns);
    columns += Math.max(1, Math.ceil(parts.filter((part) => column.get(part.id) === at).length / COLUMN_MOST));
  }
  const seen = new Array(depths).fill(0);
  for (const part of parts) {
    const at = column.get(part.id);
    column.set(part.id, firstOf[at] + Math.floor(seen[at]++ / COLUMN_MOST));
  }
  const counts = Array.from({ length: columns }, (_, index) => parts.filter((part) => column.get(part.id) === index).length);
  const rows = Math.max(1, ...counts);
  const placed = new Map();
  const next = new Array(columns).fill(0);
  for (const part of parts) {
    const col = column.get(part.id);
    // One alone in its column sits across the rows of the others, centered.
    const span = counts[col] === 1 ? rows : 1;
    placed.set(part.id, { col, row: next[col]++, span });
  }
  // Room between rows for a label only where a line joins two parts of one column.
  const tight = !lines.some((line) => placed.get(line.parts[0])?.col === placed.get(line.parts[1])?.col);
  return { placed, columns, rows, tight };
}
const COLUMN_MOST = 4;

// A label's pieces, the one that needs attention first: in one line (" · ") where there is room,
// else one a line.
function fillLabel(node, line, stacked) {
  node.replaceChildren();
  line.pieces.forEach((piece, index) => {
    if (index) node.append(stacked ? el("br") : el("span", { class: "sep", text: " · " }));
    node.append(el("span", { class: `piece${piece.tone ? ` ${piece.tone}` : ""}`, text: piece.text }));
  });
}

function rectOf(map, id) {
  const box = map.getBoundingClientRect();
  const found = map.querySelector(`.tv2-node[data-part="${CSS.escape(id)}"]`);
  if (!found) return null;
  const rect = found.getBoundingClientRect();
  return { l: rect.left - box.left, r: rect.right - box.left, t: rect.top - box.top, b: rect.bottom - box.top, cx: (rect.left + rect.right) / 2 - box.left, cy: (rect.top + rect.bottom) / 2 - box.top };
}

// Each line's shape: a curve between columns; between two parts stacked, one straight line
// under their avatars (side by side when top to bottom), a head at each end something reached.
function geometry(map, line, placed) {
  const vertical = map.classList.contains("vertical");
  let [first, second] = line.parts;
  let A = rectOf(map, first);
  let B = rectOf(map, second);
  if (!A || !B) return null;
  const [pa, pb] = [placed.get(first), placed.get(second)];
  const together = pa.col === pb.col;
  const heads = (start, end) => ({ start: line.reached.has(start), end: line.reached.has(end) });
  if (together) {
    if (vertical ? A.cx > B.cx : A.cy > B.cy) {
      [A, B] = [B, A];
      [first, second] = [second, first];
    }
    const head = heads(first, second);
    if (!vertical) {
      const x = Math.min(A.l, B.l) + 25;
      const y1 = A.b + 2 + (head.start ? 6 : 0);
      const y2 = B.t - 2 - (head.end ? 6 : 0);
      return { d: `M${x} ${y1} L${x} ${y2}`, heads: head, label: { x: x + 10, y: (A.b + B.t) / 2, anchor: "left", room: Math.max(A.r, B.r) - x - 14 } };
    }
    const y = (A.cy + B.cy) / 2;
    const x1 = A.r + 2 + (head.start ? 6 : 0);
    const x2 = B.l - 2 - (head.end ? 6 : 0);
    return { d: `M${x1} ${y} L${x2} ${y}`, heads: head, label: { x: (A.r + B.l) / 2, y: y - 7, anchor: "above", stacked: true } };
  }
  if (pa.col > pb.col) {
    [A, B] = [B, A];
    [first, second] = [second, first];
  }
  const head = heads(first, second);
  if (!vertical) {
    const x1 = A.r + 2 + (head.start ? 7 : 0);
    const y1 = A.cy;
    const x2 = B.l - 2 - (head.end ? 7 : 0);
    const y2 = B.cy;
    const dx = (x2 - x1) / 2;
    const at = { mid: { x: (x1 + x2) / 2, y: (y1 + y2) / 2, anchor: "center" }, "trunk-out": { x: x1 + 30, y: y1, anchor: "center" }, "trunk-in": { x: x2 - 30, y: y2, anchor: "center" }, start: { x: x1 + 2, y: y1, anchor: "left" } }[line.place || "mid"];
    return { d: `M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`, heads: head, label: { ...at, stacked: true } };
  }
  const x1 = A.cx;
  const y1 = A.b + 2 + (head.start ? 7 : 0);
  const x2 = B.cx;
  const y2 = B.t - 2 - (head.end ? 7 : 0);
  const dy = (y2 - y1) / 2;
  const at = { mid: { x: (x1 + x2) / 2, y: (y1 + y2) / 2, anchor: "center" }, "trunk-out": { x: x1, y: y1 + 2, anchor: "below" }, "trunk-in": { x: x2, y: y2 - 6, anchor: "above" }, start: { x: x1, y: y1 + 4, anchor: "below" } }[line.place || "mid"];
  return { d: `M${x1} ${y1} C${x1} ${y1 + dy} ${x2} ${y2 - dy} ${x2} ${y2}`, heads: head, label: { ...at, stacked: false } };
}

// Lines of the same words out of one part (one result to two) are labeled once, at their
// trunk; lines that meet with different words are each labeled at their own start.
function places(lines) {
  const flows = lines.filter((line) => line.kind === "flow");
  const words = (line) => line.pieces.map((piece) => piece.text).join("|");
  for (const line of flows) {
    const out = flows.filter((other) => other.from === line.from && words(other) === words(line));
    const into = flows.filter((other) => other.to === line.to);
    if (out.length > 1) Object.assign(line, { place: "trunk-out", quiet: out[0] !== line, fan: out });
    else if (into.length > 1 && into.every((other) => words(other) === words(line))) Object.assign(line, { place: "trunk-in", quiet: into[0] !== line, fan: into });
    else line.place = into.length > 1 ? "start" : "mid";
  }
}

const overlaps = (a, b) => a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;
// A label never over a node or another label: moved along its line's side until it is free.
function settle(map, label, taken) {
  const box = map.getBoundingClientRect();
  const nodes = [...map.querySelectorAll(".tv2-node")].map((node) => node.getBoundingClientRect());
  const top = parseFloat(label.style.top);
  for (const step of [0, 10, -10, 20, -20, 30, -30, 42, -42, 56, -56]) {
    label.style.top = `${top + step}px`;
    const rect = label.getBoundingClientRect();
    if (rect.top < box.top || rect.bottom > box.bottom) continue;
    if (!nodes.some((node) => overlaps(rect, node)) && !taken.some((other) => overlaps(rect, other))) {
      taken.push(rect);
      return;
    }
  }
  label.style.top = `${top}px`;
  taken.push(label.getBoundingClientRect());
}

function draw(map) {
  const { model } = map;
  const placed = map.placed;
  const area = map.querySelector("svg.edges");
  const labels = map.querySelector(".labels");
  area.replaceChildren(svg("defs", {}, ...["muted", "amber", "red", "soft"].map((tone) => svg("marker", { id: `${map.dataset.id}-head-${tone}`, viewBox: "0 0 8 8", refX: "1", refY: "4", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse", markerUnits: "userSpaceOnUse" }, svg("path", { d: "M0 0.6L7 4L0 7.4z", class: `head-${tone}` })))));
  labels.replaceChildren();
  const taken = [];
  for (const line of model.lines) {
    const shape = geometry(map, line, placed);
    if (!shape) continue;
    const head = `url(#${map.dataset.id}-head-${HEADS[line.style]})`;
    area.append(svg("path", { d: shape.d, class: `e ${line.style}`, "data-line": line.id, "marker-end": shape.heads.end ? head : null, "marker-start": shape.heads.start ? head : null }));
    if (line.quiet) continue;
    const say = line.fan ? line.fan.map((other) => other.say).join("; ") : line.say;
    // Its words on hover; a screen reader reads the map in words (below), every line's sentence.
    const label = el("span", { class: "tv2-label", "data-line": line.id, "aria-hidden": "true", title: say, onmouseenter: () => light(map, line.id), onmouseleave: () => light(map, "") });
    // (Through the CSSOM: the page's policy allows no style attribute.)
    Object.assign(label.style, { left: `${shape.label.x}px`, top: `${shape.label.y}px`, transform: SHIFT[shape.label.anchor] });
    if (shape.label.anchor === "left") label.classList.add("start");
    fillLabel(label, line, false);
    labels.append(label);
    if ((shape.label.room && label.offsetWidth > shape.label.room) || (shape.label.stacked && label.offsetWidth > GAP + 40)) fillLabel(label, line, true);
    settle(map, label, taken);
  }
}
function light(map, lineId) {
  const line = map.model.lines.find((item) => item.id === lineId);
  const parts = new Set(line ? line.parts : []);
  for (const node of map.querySelectorAll(".tv2-node")) node.classList.toggle("lit", parts.has(node.dataset.part));
  for (const node of map.querySelectorAll(".e, .tv2-label")) node.classList.toggle("lit", node.dataset.line === lineId);
}
// Left to right while its columns fit; else top to bottom (measured on the map, not a breakpoint).
function place(map) {
  const { columns, rows, placed } = map.layout;
  const vertical = columns > 1 && map.clientWidth - 32 < columns * NODE + (columns - 1) * GAP;
  map.classList.toggle("vertical", vertical);
  const grid = map.querySelector(".grid");
  grid.style.gridTemplateColumns = vertical ? `repeat(${rows}, minmax(0, 1fr))` : `repeat(${columns}, minmax(${NODE}px, 224px))`;
  grid.style.gridTemplateRows = vertical ? "" : `repeat(${rows}, auto)`;
  for (const node of grid.children) {
    const at = placed.get(node.dataset.part);
    if (!at) continue;
    const spans = at.span > 1;
    node.style.gridColumn = vertical ? (spans ? "1 / -1" : String(at.row + 1)) : String(at.col + 1);
    node.style.gridRow = vertical ? String(at.col + 1) : `${at.row + 1} / span ${at.span}`;
    node.classList.toggle("spans", vertical && spans);
  }
}

// The map: `model.parts` ({ id, name, avatar, color, cls, status, mark, open }) and
// `model.lines` ({ id, kind, from, to, parts, pieces, say, style, reached }).
let made = 0;
export function flowMap(model, { label = "How the work moves: parts left to right, each line says what it carries", more = null } = {}) {
  places(model.lines);
  const map = el("div", { class: "tv2-map", role: "group", "aria-label": label, "data-id": `m${++made}` });
  const grid = el("div", { class: "grid" });
  for (const part of model.parts) {
    grid.append(
      el(
        "button",
        { type: "button", class: `tv2-node ${part.color} ${part.cls}`, "data-part": part.id, "aria-label": `${part.name}: ${part.status}. Open its chat.`, title: "Open its chat", onclick: part.open },
        partAvatar(part, { large: true }),
        el("span", { class: "name" }, el("span", { text: part.name }), part.mark || null),
        el("span", { class: "status", text: part.status }),
      ),
    );
  }
  if (more) grid.append(more);
  map.append(svg("svg", { class: "edges", "aria-hidden": "true" }), grid, el("div", { class: "labels" }), words(model));
  map.model = model;
  map.layout = layout(model.parts, model.lines);
  if (more) {
    // In the last column's next free place.
    more.dataset.part = "more";
    const col = map.layout.columns - 1;
    const row = [...map.layout.placed.values()].filter((at) => at.col === col).reduce((most, at) => Math.max(most, at.row + at.span), 0);
    map.layout.placed.set("more", { col, row, span: 1 });
    map.layout.rows = Math.max(map.layout.rows, row + 1);
  }
  map.placed = map.layout.placed;
  map.classList.toggle("tight", map.layout.tight);
  const redraw = () => {
    if (!map.isConnected) return;
    place(map);
    draw(map);
  };
  requestAnimationFrame(redraw);
  let width = 0;
  new window.ResizeObserver(() => {
    if (map.clientWidth === width) return;
    width = map.clientWidth;
    redraw();
  }).observe(map);
  return map;
}
// The map in words, for a screen reader (what the labels say): each part, then each line.
function words(model) {
  const name = (id) => model.parts.find((part) => part.id === id)?.name || "";
  return el(
    "ul",
    { class: "tv2-words visually-hidden", "aria-label": "How the work moves, in words" },
    ...model.parts.map((part) => el("li", { text: `${part.name}: ${part.status}.` })),
    ...model.lines.map((line) => el("li", { text: line.kind === "pair" ? `${name(line.parts[0])} and ${name(line.parts[1])}: ${line.say}.` : `${name(line.from)} → ${name(line.to)}: ${line.say}.` })),
  );
}

// A part's avatar: its name's first character in its color, its state's ring, a tick when done.
export function partAvatar(part, { large = false, mark = part.cls } = {}) {
  const node = el("span", { class: `pa ${part.color}${large ? " lg" : ""}`, "aria-hidden": "true" }, part.avatar, el("span", { class: "ring" }));
  if (/\bis-done\b/.test(mark || "") && !/\bis-ended\b/.test(mark || "")) {
    const tick = document.createElementNS(SVG, "svg");
    tick.setAttribute("viewBox", "0 0 10 10");
    tick.append(svg("path", { d: "M2.2 5.3l1.9 1.9 3.8-4.2" }));
    node.append(el("span", { class: "tick" }, tick));
  }
  if (/\bis-review\b/.test(mark || "")) node.append(el("span", { class: "bang" }));
  return node;
}
