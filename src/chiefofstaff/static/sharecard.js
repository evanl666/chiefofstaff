// One shared card, drawn the same way everywhere it shows: Share…'s preview, the viewer page
// (site/s/, which copies this file), the HTML file and a card opened from a pasted link. DOM
// calls only, never innerHTML: what it draws may be someone else's words, and the viewer runs
// under Trusted Types. Imports nothing, so the viewer can load it alone. Home's own classes
// (style.css: .card.task, .receipts, .criteria, .markdown).

// Controls that reorder the text after them (as dom.js's withoutBidi): never shown.
const BIDI_CONTROLS = /[\u202A-\u202E\u2066-\u2069]/g;
const plain = (text) => String(text ?? "").replace(BIDI_CONTROLS, "");

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = plain(value);
    else if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : key === "title" ? plain(value) : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== "" && child !== false) node.append(typeof child === "string" ? plain(child) : child);
  }
  return node;
}

export const VERDICT = {
  verified: ["pass", "✓ Verified", "Every acceptance criterion is backed by a check that was seen running"],
  partial: ["warn", "Partly verified", "Some criteria are backed by a check that was seen running; others are only reported"],
  unverified: ["warn", "Not verified", "No criterion is backed by a check that was seen running"],
  failed: ["fail", "✗ Not met", "A criterion is not met, or the check behind it failed"],
};
const CRITERION = {
  verified: ["pass", "✓", "Verified: a check was seen running, and it passed"],
  claimed: ["warn", "~", "Reported met; no check was seen running"],
  contradicted: ["fail", "✗", "Reported met, but the check failed"],
  not_met: ["fail", "✗", "Not met"],
  not_checked: ["none", "–", "Not checked"],
};
const DOT = { review: "done", done: "done" };

// Text with the hits the owner is deciding on marked (Share…'s preview only): a removed one
// shows [removed], which is what will be shared. A hit over several lines (a key's block, an AWS
// pair) is marked line by line where the text it's in comes in pieces (a paragraph, a list).
function marked(text, hits = [], decisions = {}) {
  const live = hits.flatMap((hit) => {
    if (!hit.value) return [];
    if (text.includes(hit.value)) return [hit];
    if (!hit.value.includes("\n")) return [];
    return hit.value.split("\n").map((line) => line.trim()).filter((line) => line.length > 3 && text.includes(line)).map((line) => ({ ...hit, value: line }));
  });
  if (!live.length) return [text];
  const out = [];
  let rest = text;
  while (rest) {
    let first = null;
    for (const hit of live) {
      const at = rest.indexOf(hit.value);
      if (at >= 0 && (!first || at < first.at || (at === first.at && hit.value.length > first.hit.value.length))) first = { at, hit };
    }
    if (!first) {
      out.push(rest);
      break;
    }
    if (first.at) out.push(rest.slice(0, first.at));
    const decided = decisions[first.hit.id];
    out.push(
      decided === "remove"
        ? el("mark", { class: "share-hit removed", "data-hit": first.hit.id, title: `Removed: ${first.hit.label}` }, el("s", { text: first.hit.was ?? first.hit.value }), el("span", { class: "instead", text: "[removed]" }))
        : el("mark", { class: `share-hit${decided === "keep" ? " kept" : ""}`, "data-hit": first.hit.id, title: decided === "keep" ? `Kept: ${first.hit.label}` : `Looks like ${first.hit.label}`, text: first.hit.value }),
    );
    rest = rest.slice(first.at + first.hit.value.length);
  }
  return out;
}

function safeUrl(raw) {
  try {
    const url = new URL(raw);
    return ["https:", "http:"].includes(url.protocol) ? url : null;
  } catch {
    return null;
  }
}
// A link written by someone else shows where it goes: its words, then its host if they differ.
function link(words, raw, ctx) {
  const url = safeUrl(raw);
  if (!url) return marked(words === raw ? raw : `${words} (${raw})`, ctx.hits, ctx.decisions);
  const anchor = el("a", { href: url.href, target: "_blank", rel: "noopener noreferrer nofollow ugc", title: url.href }, ...marked(words, ctx.hits, ctx.decisions));
  const host = url.host.replace(/^www\./, "");
  return words === raw || words.includes(host) ? [anchor] : [anchor, el("span", { class: "link-host", text: ` (${host})` })];
}

const INLINE = /(`+)([^`]+?)\1|\*\*([^*]+)\*\*|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>()[\]{}"'`]+[^\s<>()[\]{}"'`.,;:!?])/g;
function inline(text, ctx) {
  const out = [];
  let last = 0;
  for (const found of text.matchAll(INLINE)) {
    if (found.index > last) out.push(...marked(text.slice(last, found.index), ctx.hits, ctx.decisions));
    if (found[2] !== undefined) out.push(el("code", {}, ...marked(found[2], ctx.hits, ctx.decisions)));
    else if (found[3] !== undefined) out.push(el("strong", { class: /:$/.test(found[3]) ? "key" : undefined }, ...inline(found[3], ctx)));
    else if (found[4] !== undefined) out.push(...link(found[4], found[5], ctx));
    else out.push(...link(found[6], found[6], ctx));
    last = found.index + found[0].length;
  }
  if (last < text.length) out.push(...marked(text.slice(last), ctx.hits, ctx.decisions));
  return out;
}

// Markdown as reports use it: paragraphs, lists, headings, quotes, code. Nothing else is markup;
// no image is ever loaded.
export function markdown(text, ctx = {}) {
  const root = el("div", { class: "body markdown" });
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  let list = null;
  let para = [];
  const flush = () => {
    if (para.length) root.append(el("p", {}, ...inline(para.join(" "), ctx)));
    para = [];
  };
  while (i < lines.length) {
    const line = lines[i];
    const fence = /^\s*(```+|~~~+)/.exec(line);
    if (fence) {
      flush();
      list = null;
      const body = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
      i += 1;
      root.append(el("pre", {}, el("code", {}, ...marked(body.join("\n"), ctx.hits, ctx.decisions))));
      continue;
    }
    const item = /^\s*(?:[-*•+]|(\d+)[.)])\s+(.*)$/.exec(line);
    const heading = /^\s*#{1,6}\s+(.*)$/.exec(line);
    const quote = /^\s*>\s?(.*)$/.exec(line);
    if (!line.trim()) {
      flush();
      list = null;
    } else if (item) {
      flush();
      const kind = item[1] ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== kind) root.append((list = el(kind)));
      list.append(el("li", {}, ...inline(item[2], ctx)));
    } else if (heading) {
      flush();
      list = null;
      root.append(el("h4", {}, ...inline(heading[1], ctx)));
    } else if (quote) {
      flush();
      list = null;
      root.append(el("blockquote", {}, ...inline(quote[1], ctx)));
    } else if (list && /^\s{2,}\S/.test(line)) {
      list.lastElementChild.append(" ", ...inline(line.trim(), ctx));
    } else {
      list = null;
      para.push(line.trim());
    }
    i += 1;
  }
  flush();
  return root;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
function checkChip(item) {
  const label = item.label || "tests";
  const tip = `${item.command || ""}${item.runs ? `\n(${plural(item.runs, "run")})` : ""}${item.failed_before ? "\nFailed first, then passed" : ""}`;
  if (item.outcome === "passed") return el("span", { class: "receipt pass", title: tip, text: `${item.failed_before ? "✗→✓" : "✓"} ${label}` });
  if (item.outcome === "failed") return el("span", { class: "receipt fail", title: tip, text: `✗ ${label}` });
  if (item.outcome === "running") return el("span", { class: "receipt warn", title: tip, text: `… ${label} running` });
  return el("span", { class: "receipt warn", title: tip, text: `no ${label} ran` });
}
function prChip(pr) {
  const state = pr.state === "MERGED" ? "merged" : pr.state === "CLOSED" ? "closed" : "";
  const ci = state ? "" : { passing: "✓ CI", failing: "✗ CI", pending: "CI running" }[pr.checks] || "";
  const review = { APPROVED: "approved", CHANGES_REQUESTED: "changes requested" }[pr.review] || "";
  const where = [state || ci, review].filter(Boolean).join(" · ");
  const url = safeUrl(pr.url);
  const github = url && url.host === "github.com";
  const short = String(pr.label || "").replace(/^[^/]+\//, "");
  const repo = String(pr.label || "").split("#")[0];
  return el(
    github ? "a" : "span",
    { class: `receipt pr ${state || pr.checks || ""}`.trim(), href: github ? url.href : undefined, target: github ? "_blank" : undefined, rel: github ? "noopener noreferrer nofollow" : undefined, title: `${pr.label}${pr.title ? `\n“${pr.title}”` : ""}\nOn GitHub: people without access to ${repo} can't open it` },
    el("span", { class: "pr-name", text: pr.title ? `${short} · ${pr.title.length > 60 ? `${pr.title.slice(0, 59)}…` : pr.title}` : short }),
    where ? el("span", { class: "pr-state", text: ` · ${where}` }) : "",
  );
}

// `when`: words for the head's meta ("finished 2 days ago"). `hits`/`decisions`: Share…'s preview.
export function renderCard(card, { when = "", hits = [], decisions = {}, extraClass = "" } = {}) {
  const ctx = { hits, decisions };
  const items = [];
  const criteria = card.criteria || [];
  if (card.verdict && VERDICT[card.verdict]) {
    const [kind, text, help] = VERDICT[card.verdict];
    const done = criteria.filter((item) => item.state === "verified").length;
    items.push(el("span", { class: `receipt verdict ${kind}`, title: help, text: card.verdict === "partial" ? `${text} · ${done}/${criteria.length}` : text }));
  }
  for (const item of card.checks || []) items.push(checkChip(item));
  const changed = card.changes || {};
  if (changed.files) items.push(el("span", { class: "receipt", title: "What its changes came to (the changes themselves are not shared)", text: `+${changed.insertions || 0} −${changed.deletions || 0} in ${plural(changed.files, "file")}` }));
  for (const pr of card.prs || []) items.push(prChip(pr));
  const list = criteria.length
    ? el("ul", { class: "criteria" }, ...criteria.map((item) => {
        const [kind, mark, help] = CRITERION[item.state] || CRITERION.not_checked;
        return el("li", { class: kind, title: `${help}${item.check ? `\n${item.check}` : ""}` }, el("span", { class: "mark", text: mark }), el("span", {}, ...inline(item.text || "", ctx)));
      }))
    : null;
  const more = [];
  if (changed.commits) more.push(el("span", { class: "receipt", text: plural(changed.commits, "commit") }));
  if (card.at) more.push(el("span", { class: "receipt none", title: "The code these results are about", text: `at ${card.at}` }));
  const flags = (card.flags || []).map((flag) => el("div", { class: "flag" }, ...marked(flag, hits, decisions)));
  return el(
    "article",
    { class: `card task shared expanded ${card.status || ""} ${extraClass}`.trim(), "aria-label": card.title },
    el(
      "div",
      { class: "head" },
      el("span", { class: `dot ${DOT[card.status] || ""}`, title: card.status_words || "" }),
      el("h2", { class: "title" }, ...marked(card.title || "", hits, decisions)),
      el("span", { class: "meta" }, el("span", { class: "marker", text: card.status_words || "" }), when ? el("span", { class: "when", text: ` · ${when}` }) : ""),
    ),
    items.length || flags.length ? el("div", { class: "chips" }, items.length ? el("div", { class: "receipts" }, ...items) : null, ...flags) : null,
    el("div", { class: "report" }, card.result ? markdown(card.result, ctx) : null, list, more.length ? el("div", { class: "receipts" }, ...more) : null),
  );
}

// The card as plain Markdown: Copy as text (as sharing.card_text has it for the Router).
export function cardText(card) {
  const lines = [`## ${card.title}`, "", card.status_words || card.status || ""];
  if (card.result) lines.push("", card.result.trim());
  if ((card.criteria || []).length) lines.push("", "**Done when**", ...card.criteria.map((item) => `- ${(CRITERION[item.state] || CRITERION.not_checked)[1]} ${item.text}`));
  const seen = (card.checks || []).map((item) => `- ${item.label}: \`${item.command}\` ${item.outcome}`);
  seen.push(...(card.prs || []).map((pr) => `- ${pr.label}: ${pr.url}`));
  if (seen.length) lines.push("", "**Observed**", ...seen);
  if ((card.flags || []).length) lines.push("", "**Warnings**", ...card.flags.map((flag) => `- ${flag}`));
  return lines.join("\n").trim();
}

const RTF = typeof Intl !== "undefined" ? new Intl.RelativeTimeFormat("en", { numeric: "auto" }) : null;
export function ago(iso, now = Date.now()) {
  const seconds = (Date.parse(iso) - now) / 1000;
  if (!Number.isFinite(seconds)) return "";
  for (const [unit, size] of [["day", 86400], ["hour", 3600], ["minute", 60]]) if (Math.abs(seconds) >= size) return RTF.format(Math.round(seconds / size), unit);
  return "just now";
}
export const day = (iso) => new Date(iso).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
export const dayTime = (iso) => `${day(iso)}, ${new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`;
