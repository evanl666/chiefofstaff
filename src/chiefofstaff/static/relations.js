// Task relationships (E130): how a team's tasks are related, as its view shows them. Counts
// above (a chip lights the rows it counts), a row per task with its one relation, and a task's
// lineage when asked: where it came from, what came of it, in a line that never crosses.
// Five verbs, each drawn: waits on, continues, branched, parts, vs. Color stays for workers
// and status; the voice is in the words.

import { el } from "./dom.js";
import { byId, closedWorkers } from "./state.js";
import { select } from "./session.js";
import { avatar, labelOf } from "./team.js";
import { agentName, marksShown } from "./agentpick.js";

const SVG = "http://www.w3.org/2000/svg";
function svg(tag, attributes = {}, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attributes)) if (value !== undefined && value !== null) node.setAttribute(key, value);
  node.append(...children.filter(Boolean));
  return node;
}
// Fonts draw ⑂ and ⇉ patchily: each verb is a few strokes in the text's color.
const GLYPHS = {
  waits: ["M1.5 7h10", "M9 4l3 3-3 3"],
  continues: ["M3 1.5v5.5h9", "M9 4l3 3-3 3"],
  branched: ["M4 12.5v-11", "M4 9c0-3.5 6-2.5 6-7", "M8 4l2-2 2 2"],
  parts: ["M1.5 4h10", "M1.5 10h10", "M9.5 2l2 2-2 2", "M9.5 8l2 2-2 2"],
  vs: ["M2 4.5h10", "M9.5 2.5l2.5 2-2.5 2", "M12 9.5H2", "M4.5 7.5L2 9.5l2.5 2"],
  settled: ["M2.5 7.5l3 3 6-6.5"],
};
export function glyph(verb, className = "") {
  const paths = GLYPHS[verb] || GLYPHS.continues;
  return svg("svg", { class: `glyph ${verb}${className ? ` ${className}` : ""}`, viewBox: "0 0 14 14", width: 14, height: 14, "aria-hidden": "true", focusable: "false" }, ...paths.map((d, index) => svg("path", { d, class: index === 0 ? "shaft" : null })));
}
const VERB = { after: "waits", question: "waits", replaced: "continues", superseded: "continues", context: "continues", handoff: "continues", followups: "continues", split: "branched", fork: "branched", parts: "parts", compare: "vs" };
const minutes = (count) => (count < 60 ? `${count} min` : count < 1440 ? `${Math.floor(count / 60)} h` : `${Math.floor(count / 1440)} d`);
// " · 9 min" of a wait (nothing for one just begun).
const waited = (count) => (count >= 1 ? ` · ${minutes(count)}` : "");
const ago = (count) => (count < 1 ? "just now" : `${minutes(count)} ago`);
const shorten = (text, most) => (text.length > most ? `${text.slice(0, most - 1).trimEnd()}…` : text);
const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
const workerOf = (id) => byId(id) || closedWorkers().find((item) => item.id === id);
// The agent a worker runs on, where that tells something (more than one agent on): a side of a
// Compare is "Codex" or "Copilot", work handed on "handed to Claude".
function agentOf(id) {
  const worker = workerOf(id);
  if (!worker || !marksShown()) return "";
  return worker.agent_name || agentName(worker.agent || "copilot");
}

// A task's title where its worker's avatar or name is beside it: without the worker's name it
// starts with ("Checkout UI: the v2 pages" by Checkout UI: "The v2 pages"); the whole title on
// hover. A title that is only the name stays whole.
const AFTER_NAME = /^(?:\s*[:·|]|\s+[-\u2013\u2014]\s)\s*([\s\S]*\S)\s*$/;
export function shownTitle(row) {
  const title = row?.title || "";
  const label = labelOf(row?.session_id || "");
  const rest = label && title.startsWith(label) ? title.slice(label.length).match(AFTER_NAME) : null;
  if (!rest) return title;
  // "the v2 pages" begins as a line does; "v2 pages", "iOS app" as they are.
  return /^[a-z]+(\s|$)/.test(rest[1]) ? rest[1][0].toUpperCase() + rest[1].slice(1) : rest[1];
}
// A team's data as its view read it (/api/teams/<id>/thread): its tasks by id.
function indexed(data) {
  if (!data.byTask) data.byTask = new Map((data.tasks || []).map((row) => [row.id, row]));
  return data.byTask;
}
// The other end of a relation, by its worker's short name when that is another worker, else by
// its task's title (a worker's own earlier task).
function nameOf(data, taskId, from = "") {
  const row = indexed(data).get(taskId);
  if (!row) return "an earlier task";
  return row.session_id !== from ? labelOf(row.session_id) : `“${shorten(shownTitle(row), 32)}”`;
}
const workerName = (data, taskId) => labelOf(indexed(data).get(taskId)?.session_id || "");
// Who it went to: by agent when the work changed agent ("handed to Codex"), else by worker.
function handedTo(data, row, taskId) {
  const other = indexed(data).get(taskId);
  if (!other) return "another task";
  const agent = agentOf(other.session_id);
  if (agent && agent !== agentOf(row.session_id)) return agent;
  return nameOf(data, taskId, row.session_id);
}

// A row's one relation, in words: [verb, text, class].
export function chipWords(data, row, chip) {
  const other = chip.other ? nameOf(data, chip.other, row.session_id) : "";
  switch (chip.kind) {
    case "after":
      if (chip.side === "from") return chip.state === "pending" ? ["waits", `${other} waits on it`, ""] : ["settled", `unblocked ${other}`, "settled"];
      if (chip.state === "pending") {
        // What it waits on ended without its result: it will not come by itself.
        const ended = indexed(data).get(chip.other);
        if (["failed", "cancelled"].includes(ended?.status)) return ["waits", `waits on ${other} · it ${ended.status === "failed" ? "failed" : "was cancelled"}`, "waiting"];
        return ["waits", `waits on ${other}${waited(chip.minutes)}`, "waiting"];
      }
      if (chip.just) return ["settled", `${workerName(data, chip.other)} delivered · unblocked`, "cleared"];
      return ["settled", `after ${other}`, "settled"];
    case "question":
      return ["waits", `waits on ${labelOf(chip.other_session)}'s answer${waited(chip.minutes)}`, "waiting"];
    case "replaced":
    case "superseded":
    case "handoff": {
      const elsewhere = indexed(data).get(chip.other)?.session_id !== row.session_id;
      if (chip.side === "from") return ["continues", elsewhere || chip.kind === "handoff" ? `handed to ${handedTo(data, row, chip.other)}` : `replaced by ${other}`, "over"];
      return ["continues", elsewhere || chip.kind === "handoff" ? `picks up from ${other}` : `instead of ${other}`, ""];
    }
    case "context":
      return ["continues", chip.side === "to" ? `picks up from ${other}` : `passed on to ${other}`, ""];
    case "split":
      return ["branched", chip.side === "to" ? `split from ${other}` : `split off ${other}`, ""];
    case "fork":
      return ["branched", chip.side === "to" ? `forked from ${workerName(data, chip.other)}` : `forked as ${workerName(data, chip.other)}`, ""];
    case "parts": {
      // The fan's state, the same on each of its parts: "3 parts · 1 landed".
      const landed = chip.landed === chip.total ? "all landed" : `${chip.landed} landed`;
      return ["parts", `${chip.total} parts · ${landed}${chip.failed ? ` · ${chip.failed} failed` : ""}`, ""];
    }
    case "compare": {
      if (!chip.picked) return ["vs", `vs ${chip.others.map((id) => agentOf(indexed(data).get(id)?.session_id) || workerName(data, id)).join(", ")}`, "waiting"];
      return chip.picked === row.id ? ["vs", "picked", ""] : ["vs", `${agentOf(indexed(data).get(chip.picked)?.session_id) || workerName(data, chip.picked)} picked`, "over"];
    }
    case "followups":
      return ["continues", `+${plural(chip.count, "follow-up")}`, ""];
    default:
      return ["continues", "related", ""];
  }
}
// A fan's parts as dots: one filled for each that landed, hollow for the rest.
export function dots(landed, total) {
  return el("span", { class: "dots", "aria-hidden": "true" }, ...Array.from({ length: Math.min(total, 6) }, (_, index) => el("span", { class: index < landed ? "on" : "" })));
}
// The tasks at either end of a row's relation (both lit on hover, its rows and graph nodes).
function ends(data, row, chip) {
  if (!chip) return [row.id];
  if (chip.others) return [row.id, ...chip.others];
  if (chip.other) return [row.id, chip.other];
  return [row.id];
}
export function light(view, taskIds, on) {
  const data = indexed(view);
  for (const id of taskIds) {
    view.root?.querySelector(`.trow[data-task="${id}"]`)?.classList.toggle("lit", on);
    const session = data.get(id)?.session_id;
    if (session) view.root?.querySelector(`.team-graph .node[data-session="${session}"]`)?.classList.toggle("lit", on);
  }
}
function chipNode(view, row) {
  const chip = row.relation;
  if (!chip) return null;
  const [verb, words, kind] = chipWords(view, row, chip);
  const node = el("span", { class: `rel ${VERB[chip.kind] || "continues"}${kind ? ` ${kind}` : ""}`, "data-kind": chip.kind }, glyph(verb), el("span", { class: "words", text: words }));
  const other = chip.other ? indexed(view).get(chip.other) : null;
  node.title = other ? `${words}\n${other.title} · ${labelOf(other.session_id)} · ${statusWord(other)} · ${ago(ageOf(other))}` : words;
  const lit = ends(view, row, chip);
  node.addEventListener("mouseenter", () => light(view, lit, true));
  node.addEventListener("mouseleave", () => light(view, lit, false));
  return node;
}
const STATUS = { queued: "queued", running: "running", waiting: "waiting", review: "to review", done: "done", failed: "failed", cancelled: "cancelled" };
export const statusWord = (row) => STATUS[row.status] || row.status;
const ageOf = (row) => Math.max(0, Math.round((Date.now() - new Date(row.updated_at || row.created_at)) / 60000));
// Over: done instead by another, the side of a compare not picked, or cancelled.
function isOver(row) {
  const chip = row.relation;
  if (row.status === "cancelled") return true;
  if (chip?.side === "from" && ["replaced", "superseded", "handoff"].includes(chip.kind)) return true;
  return chip?.kind === "compare" && Boolean(chip.picked) && chip.picked !== row.id;
}

// The view's counts, chips above its rows: a fan's parts landing, tasks waiting on another's,
// what branched, a compare to pick, and the PRs or project the tasks share.
export function summaryChips(view, onPick) {
  const { summary = {}, anchors = [], groups = [] } = view;
  const chips = [];
  for (const parts of summary.parts || []) {
    const group = groups.find((item) => item.id === parts.group);
    const words = parts.state === "resolved" ? `All ${parts.total} parts landed${group?.landed_at ? ` · ${span(group.started_at, group.landed_at)}` : ""}` : `${parts.landed} of ${parts.total} landed: ready when ${parts.total} of ${parts.total} land`;
    chips.push({ key: `group:${parts.group}`, verb: "parts", text: `${parts.total} parts`, extra: dots(parts.landed, parts.total), title: words, tasks: group?.tasks || [] });
  }
  if (summary.waiting) chips.push({ key: "waiting", verb: "waits", text: `${summary.waiting} waiting`, title: "Tasks that start once another's result is in", tasks: (view.tasks || []).filter((row) => row.relation?.kind === "after" && row.relation.side === "to" && row.relation.state === "pending").map((row) => row.id) });
  const branched = (summary.split || 0) + (summary.fork || 0);
  if (branched) chips.push({ key: "branched", verb: "branched", text: summary.fork && !summary.split ? plural(summary.fork, "fork") : summary.split && !summary.fork ? plural(summary.split, "split") : `${branched} branched`, title: "Split off or forked from another task", tasks: relatedTo(view, ["split", "fork"]) });
  if (summary.compare) chips.push({ key: "compare", verb: "vs", text: plural(summary.compare, "compare"), title: "The same task on several agents", tasks: groups.filter((group) => group.kind === "compare").flatMap((group) => group.tasks) });
  for (const anchor of anchors) chips.push({ key: `anchor:${anchor.label}`, verb: "", text: anchor.label, title: anchor.kind === "pr" ? `${anchor.title} · ${plural(anchor.tasks.length, "task")}` : anchor.title, tasks: anchor.tasks || (view.tasks || []).filter((row) => (anchor.workers || []).includes(row.session_id)).map((row) => row.id), anchor: true });
  if (!chips.length) return null;
  return el(
    "div",
    { class: "rel-summary", role: "group", "aria-label": "How its tasks are related" },
    ...chips.map((chip) =>
      el("button", { type: "button", class: `rel-chip${chip.anchor ? " anchor" : ""}`, "data-key": chip.key, "aria-pressed": String(view.lit === chip.key), title: chip.title, onclick: () => onPick(view.lit === chip.key ? "" : chip.key, chip.tasks) }, chip.verb ? glyph(chip.verb) : null, el("span", { text: chip.text }), chip.extra || null),
    ),
  );
}
function relatedTo(view, kinds) {
  const found = new Set();
  for (const edge of view.relations || []) {
    if (kinds.includes(edge.kind)) {
      found.add(edge.from_task);
      found.add(edge.to_task);
    }
  }
  return [...found];
}
// "42 min", "2 h": how long from one moment to another.
function span(from, to) {
  return minutes(Math.max(0, Math.round((new Date(to) - new Date(from)) / 60000)));
}

// The Tasks section: a row per task, its latest ROWS (the earlier on asking), each with its
// one relation and how many more; a click opens its lineage under it (one at a time).
const ROWS = 8;
export function taskList(view, rerender) {
  const rows = view.tasks || [];
  if (!rows.length) return null;
  const hidden = view.allRows ? 0 : Math.max(0, rows.length - ROWS);
  const shown = rows.slice(hidden);
  const earlier = hidden + (view.earlier || 0);
  const lit = new Set(view.litTasks || []);
  return el(
    "section",
    { class: "team-tasks", "aria-label": "Tasks" },
    el("h3", { class: "title" }, "Tasks", el("span", { class: "count", text: String(rows.length + (view.earlier || 0)) })),
    hidden
      ? el("button", { type: "button", class: "link earlier-rows", text: `+${earlier} earlier`, onclick: () => { view.allRows = true; rerender(); } })
      : view.earlier
        ? el("p", { class: "earlier-rows", text: `+${view.earlier} earlier` })
        : null,
    el(
      "div",
      { class: "rows" },
      ...shown.flatMap((row) => {
        const open = view.open === row.id;
        const node = el(
          "div",
          { class: `trow${isOver(row) ? " over" : ""}${lit.has(row.id) ? " lit" : ""}${view.cleared?.has(row.id) ? " unblocked" : ""}`, "data-task": row.id, "data-session": row.session_id },
          el(
            "button",
            { type: "button", class: "main", "aria-expanded": String(open), title: open ? "Close its lineage" : row.lineage ? "Where it came from and what came of it" : "About this task", onclick: () => { view.open = open ? "" : row.id; view.peek = ""; rerender(); } },
            avatar(row.session_id),
            el("span", { class: `sdot ${row.status}`, title: statusWord(row) }),
            el("span", { class: "title", text: shownTitle(row), title: row.title }),
          ),
          chipNode(view, row),
          row.more ? el("span", { class: "more", text: `+${row.more}`, title: `${plural(row.more, "more relation")}: open its lineage` }) : null,
        );
        return open ? [node, lineageOf(view, row, rerender)] : [node];
      }),
    ),
  );
}

// A task's lineage: its columns left to right (where it came from, it with its parts or
// compare under it, what came of it), "… +N" beyond, one caption in plain words, and the
// peek of the task clicked (title, worker, status, age, Open).
export function lineageOf(view, row, rerender) {
  const data = indexed(view);
  const strip = row.lineage;
  const box = el("div", { class: "lineage", "data-task": row.id });
  if (strip) {
    const nodes = [];
    if (strip.earlier) nodes.push(el("span", { class: "beyond", text: `… +${strip.earlier}`, title: `${plural(strip.earlier, "earlier task")} before these` }));
    strip.columns.forEach((column, index) => {
      if (index > 0) nodes.push(link(view, strip.columns[index - 1][0], column[0]));
      const own = index === strip.at;
      // A second task in a column joins the one nearer it its own way: its verb on it.
      const toward = own ? "" : strip.columns[index < strip.at ? index + 1 : index - 1][0];
      nodes.push(
        el(
          "div",
          { class: `col${own ? " own" : ""}` },
          lineageNode(view, column[0], own, rerender),
          own && column.length > 1 ? fan(view, row, column.slice(1), strip.siblings, rerender) : column.slice(1).map((id) => lineageNode(view, id, false, rerender, verbOf(edgeBetween(view, id, toward)))),
        ),
      );
    });
    if (strip.later) nodes.push(el("span", { class: "beyond", text: `+${strip.later} …`, title: `${plural(strip.later, "later task")} after these` }));
    box.append(el("div", { class: "strip" }, ...nodes));
  }
  const peeked = data.get(view.peek || row.id) || row;
  box.append(
    el("p", { class: "caption" }, el("span", { class: "words", text: caption(view, row) })),
    el(
      "div",
      { class: "peek" },
      avatar(peeked.session_id),
      el("span", { class: "who", text: labelOf(peeked.session_id) }),
      // (A title that is only its worker's name is said once.)
      shownTitle(peeked) !== labelOf(peeked.session_id) ? el("span", { class: "what", text: shownTitle(peeked), title: peeked.title }) : null,
      el("span", { class: "state", text: `${statusWord(peeked)} · ${ago(ageOf(peeked))}` }),
      el("button", { type: "button", class: "link", text: "Open", title: "Its chat, at this task", onclick: () => select(peeked.session_id, { task: peeked.id }) }),
    ),
  );
  return box;
}
function lineageNode(view, taskId, own, rerender, verb = "") {
  const row = indexed(view).get(taskId);
  if (!row) return el("span", { class: "lnode gone", text: "a task" });
  return el(
    "button",
    { type: "button", class: `lnode${own ? " self" : ""}${view.peek === taskId ? " peeked" : ""}${isOver(row) ? " over" : ""}`, "data-task": taskId, title: `${row.title} · ${labelOf(row.session_id)} · ${statusWord(row)}`, onclick: () => { view.peek = view.peek === taskId ? "" : taskId; rerender(); } },
    verb ? glyph(verb, "lead") : null,
    avatar(row.session_id),
    el("span", { class: `sdot ${row.status}` }),
    el("span", { class: "name", text: shorten(shownTitle(row), 48) }),
  );
}
// Its parts' or compare's others, under it: one fan.
function fan(view, row, ids, more, rerender) {
  const group = (view.groups || []).find((item) => item.id === row.lineage.group);
  return el(
    "div",
    { class: `fan ${group?.kind || "parts"}` },
    el("span", { class: "fan-mark", title: group?.kind === "compare" ? "The same task on another agent" : "Other parts of the same request" }, glyph(group?.kind === "compare" ? "vs" : "parts")),
    ...ids.map((id) => lineageNode(view, id, false, rerender)),
    more ? el("span", { class: "beyond", text: `+${more}` }) : null,
  );
}
const edgeBetween = (view, one, two) => (view.relations || []).find((item) => (item.from_task === one && item.to_task === two) || (item.from_task === two && item.to_task === one));
const verbOf = (edge) => (edge ? VERB[edge.kind] || "continues" : "");
// The line between two tasks of a lineage: a wait is a dashed arrow until it clears (it
// breathes while it waits), then drawn solid; any other its verb. Hovering lights both ends.
function link(view, left, right) {
  const edge = edgeBetween(view, left, right);
  const verb = verbOf(edge) || "continues";
  const wait = edge?.kind === "after";
  const state = wait ? (edge.state === "pending" ? "pending" : view.cleared?.has(edge.to_task) ? "drawing" : "solid") : "solid";
  const words = edge ? linkWords(view, edge) : "";
  const node = el(
    "span",
    { class: `llink ${verb} ${state}`, title: words, "data-edge": edge?.id || "" },
    wait ? svg("svg", { class: "line", viewBox: "0 0 28 14", width: 28, height: 14, "aria-hidden": "true" }, svg("path", { class: "base", d: "M1 7h24" }), svg("path", { class: "ink", d: "M1 7h24", pathLength: "1" }), svg("path", { class: "head", d: "M22 4l3 3-3 3" })) : glyph(verb),
  );
  node.addEventListener("mouseenter", () => light(view, [left, right], true));
  node.addEventListener("mouseleave", () => light(view, [left, right], false));
  return node;
}
function linkWords(view, edge) {
  const earlier = workerName(view, edge.from_task);
  const later = workerName(view, edge.to_task);
  return {
    after: edge.state === "pending" ? `${later} waits on ${earlier}` : `${later} started once ${earlier} delivered`,
    replaced: "Done instead by this one",
    superseded: "Done instead by this one",
    context: `Built on what ${earlier} did`,
    handoff: `${earlier} handed this to ${later}`,
    split: "Split off",
    fork: `Forked from ${earlier}`,
  }[edge.kind] || "";
}
// One caption, in a human voice: what is most telling about the task, as its chip says it.
export function caption(view, row) {
  const data = indexed(view);
  const chip = row.relation;
  const group = (view.groups || []).find((item) => item.tasks.includes(row.id));
  const me = labelOf(row.session_id);
  if (chip?.kind === "after" && chip.side === "to") {
    const other = workerName(view, chip.other);
    if (chip.state === "pending") return `${me} waits on ${other} · ${minutes(chip.minutes || 0)}`;
    return chip.just ? `${other} delivered · ${me} is unblocked` : `${me} started once ${other} delivered`;
  }
  if (chip?.kind === "question") return `${me} waits on ${labelOf(chip.other_session)}'s answer · ${minutes(chip.minutes || 0)}`;
  if (group?.kind === "parts" && (!chip || ["parts", "followups"].includes(chip.kind) || chip.kind === "after")) return fanWords(view, group);
  if (group?.kind === "compare") {
    const names = group.tasks.map((id) => agentOf(data.get(id)?.session_id) || workerName(view, id));
    return group.picked ? `The same task on ${names.join(" and ")} · ${agentOf(data.get(group.picked)?.session_id) || workerName(view, group.picked)} picked` : `The same task on ${names.join(" and ")} · yours to pick`;
  }
  if (chip && ["replaced", "superseded", "handoff"].includes(chip.kind)) {
    const [from, to] = chip.side === "from" ? [row, data.get(chip.other)] : [data.get(chip.other), row];
    if (from && to && (from.session_id !== to.session_id || chip.kind === "handoff")) {
      const giver = agentOf(from.session_id) && agentOf(to.session_id) && agentOf(from.session_id) !== agentOf(to.session_id) ? [agentOf(from.session_id), agentOf(to.session_id)] : [labelOf(from.session_id), labelOf(to.session_id)];
      return `${giver[0]} handed this to ${giver[1]}`;
    }
    return chip.side === "from" ? `${me} started it over: “${shorten(shownTitle(to), 40)}”` : `${me} started it over, instead of “${shorten(shownTitle(from), 40)}”`;
  }
  if (chip?.kind === "context") return chip.side === "to" ? `${me} picks up from ${workerName(view, chip.other)}` : `${workerName(view, chip.other)} picks up from here`;
  if (chip?.kind === "split") return chip.side === "to" ? `${me} split this off “${shorten(shownTitle(data.get(chip.other)), 40)}”` : `${me} split “${shorten(shownTitle(data.get(chip.other)), 40)}” off`;
  if (chip?.kind === "fork") return chip.side === "to" ? `Forked from ${workerName(view, chip.other)} while it was busy` : `${workerName(view, chip.other)} forked from ${me} while it was busy`;
  if (chip?.kind === "after") return `${workerName(view, chip.other)} ${chip.state === "pending" ? "waits on" : "started once"} ${me}${chip.state === "pending" ? "" : " delivered"}`;
  if (chip?.kind === "followups") return `${me} followed it up ${chip.count === 1 ? "once" : `${chip.count} times`}`;
  if (group) return fanWords(view, group);
  return `${me} · ${statusWord(row)}`;
}
// "3 workers, one request · ready when 3 of 3 land", then "All 3 parts landed · 42 min".
function fanWords(view, group) {
  if (group.state === "resolved") return `All ${group.total} parts landed${group.landed_at ? ` · ${span(group.started_at, group.landed_at)}` : ""}`;
  const workers = new Set(group.tasks.map((id) => indexed(view).get(id)?.session_id).filter(Boolean)).size;
  const failed = group.failed ? ` · ${group.failed} won't` : "";
  return `${workers} workers, one request · ready when ${group.total} of ${group.total} land${failed}`;
}

// Home's line of a team: one word-sized cue of its tasks (a fan landing, or tasks waiting on
// another's), or nothing.
export function cueNode(team) {
  const cue = team.cue || {};
  if (cue.parts) return el("span", { class: "cue" }, dots(cue.parts[0], cue.parts[1]), `${cue.parts[0]} of ${cue.parts[1]} parts`);
  if (cue.waiting) return el("span", { class: "cue" }, glyph("waits"), `${cue.waiting} waiting`);
  return null;
}
