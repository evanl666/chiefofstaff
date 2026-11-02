// Text for display: a safe Markdown subset, links and short paths.

import { escapeHtml } from "./dom.js";
import { state } from "./state.js";
// Markdown: a small, safe subset. Text is escaped first; only these tags are produced.

const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// A path segment ends at whitespace, punctuation (Western or CJK) or markup.
const SEGMENT = "[^\\s/,，。;；:：()（）\\[\\]{}<>`'\"、|&*!?！？]+";
function inlinePattern() {
  const roots = [state.snapshot?.data_directory, state.snapshot?.home].filter(Boolean);
  const key = roots.join("\n");
  if (inlinePattern.key !== key || !inlinePattern.re) {
    const known = [...roots.map((root) => escapeRegExp(escapeHtml(root))), "~"];
    const path =
      `(?:${known.join("|")})(?:\\/${SEGMENT})+` +
      `|(?<![\\w.~\\/-])\\/(?:Users|home|tmp|private|var|opt|etc|Volumes|workspace)(?:\\/${SEGMENT})+`;
    inlinePattern.re = new RegExp(
      [
        "`([^`\\n]+)`",
        "\\*\\*([^*\\n]+)\\*\\*",
        "~~([^~\\n]+)~~",
        "(?<![\\w*])\\*([^*\\s](?:[^*\\n]*[^*\\s])?)\\*(?![\\w*])",
        "\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)",
        "(https?:\\/\\/[^\\s<)，。、；：！？）」』】]+)",
        `(${path})`,
      ].join("|"),
      "g",
    );
    inlinePattern.path = new RegExp(`^(?:${path})$`);
    inlinePattern.key = key;
  }
  return inlinePattern.re;
}
// Trailing punctuation belongs to the sentence, not to a URL or path.
const TRAILING = /(?:[.,;:!?'"]|&quot;|&#39;)+$/;
function linkLabel(url) {
  let match;
  if ((match = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/(?:pull|issues)\/(\d+)(?:[/?#].*)?$/.exec(url))) return `${match[1]}#${match[2]}`;
  if (/^https:\/\/[^/]+\.slack\.com\/archives\//.test(url)) return "Slack message";
  const plain = url.replace(/^https?:\/\/(www\.)?/, "");
  return plain.length > 64 ? `${plain.slice(0, 61)}…` : plain;
}
export function displayPath(full) {
  const data = state.snapshot?.data_directory;
  const home = state.snapshot?.home;
  const scratch = state.snapshot?.settings?.scratch_directory;
  let shown = full;
  if (scratch && full.startsWith(`${scratch}/`)) shown = full.slice(scratch.length + 1);
  else if (data && full.startsWith(`${data}/work/`)) shown = full.slice(data.length + 6).replace(/^(scratch|worktrees|workspaces)\//, "");
  else if (home && full.startsWith(`${home}/`)) shown = `~${full.slice(home.length)}`;
  const parts = shown.split("/");
  if (shown.length > 52 && parts.length > 3) shown = `…/${parts.slice(-2).join("/")}`;
  return shown;
}
function pathChip(path) {
  // Matched on escaped text without "&", so the path needs no further escaping.
  return `<span class="path" data-path="${path}" title="${path}\nClick to copy">${escapeHtml(displayPath(path))}</span>`;
}
function inlineEscaped(escaped) {
  return escaped.replace(inlinePattern(), (match, code, bold, strike, italic, label, href, bare, path) => {
    if (code !== undefined) return inlinePattern.path.test(code) ? pathChip(code) : `<code>${code}</code>`;
    if (bold !== undefined) return `<strong>${inlineEscaped(bold)}</strong>`;
    if (strike !== undefined) return `<s>${inlineEscaped(strike)}</s>`;
    if (italic !== undefined) return `<em>${inlineEscaped(italic)}</em>`;
    const raw = href ?? bare ?? path;
    const trail = href ? "" : raw.match(TRAILING)?.[0] || "";
    const value = trail ? raw.slice(0, -trail.length) : raw;
    if (path !== undefined) return pathChip(value) + trail;
    return `<a href="${value}" target="_blank" rel="noreferrer">${label ?? escapeHtml(linkLabel(value))}</a>${trail}`;
  });
}
// "Label: rest" at the start of a line: a short key (at most three words) is set in bold.
const KEY = /^([\p{L}\p{N}][\p{L}\p{N}\p{M} _-]{0,15}?)\s*([:：])(?!:|\/\/|\d)\s*/u;
function inline(text, keyed = false) {
  const match = keyed && !text.startsWith("**") ? KEY.exec(text) : null;
  if (match && /\p{L}/u.test(match[1]) && match[1].trim().split(/\s+/).length <= 3) {
    const rest = text.slice(match[0].length);
    return `<strong class="key">${escapeHtml(match[1] + match[2])}</strong>${rest ? ` ${inlineEscaped(escapeHtml(rest))}` : ""}`;
  }
  return inlineEscaped(escapeHtml(text));
}
const FENCE = /^\s*(```+|~~~+)\s*[\w+-]*\s*$/;
const HEADING = /^\s*(#{1,6})\s+(.*)$/;
const QUOTE_DEPTH = 6;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^\s*>/;
const ITEM = /^(\s*)([-*•+]|\d+[.)])\s+(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const isTable = (lines, i) => lines[i].includes("|") && TABLE_RULE.test(lines[i + 1] || "") && (lines[i + 1] || "").includes("-");
const startsBlock = (lines, i) => [FENCE, HEADING, RULE, QUOTE, ITEM].some((re) => re.test(lines[i])) || isTable(lines, i);
const cells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
function renderList(items) {
  let html = "";
  const stack = [];
  for (const item of items) {
    while (stack.length && item.indent < stack.at(-1).indent) html += `</li></${stack.pop().tag}>`;
    const top = stack.at(-1);
    if (!top || item.indent > top.indent) {
      const tag = item.number === null ? "ul" : "ol";
      html += item.number > 1 ? `<ol start="${item.number}">` : `<${tag}>`;
      stack.push({ indent: item.indent, tag });
    } else {
      html += "</li>";
    }
    html += `<li>${item.lines.map((line) => inline(line, true)).join("<br>")}`;
  }
  while (stack.length) html += `</li></${stack.pop().tag}>`;
  return html;
}
export function markdown(text, depth = 0) {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
    } else if (FENCE.test(line)) {
      const fence = FENCE.exec(line)[1];
      const code = [];
      for (i += 1; i < lines.length && !lines[i].trim().startsWith(fence); i += 1) code.push(lines[i]);
      i += 1;
      out.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
    } else if (isTable(lines, i)) {
      const head = cells(line);
      const rows = [];
      for (i += 2; i < lines.length && lines[i].includes("|") && lines[i].trim(); i += 1) rows.push(cells(lines[i]));
      out.push(
        `<div class="table"><table><thead><tr>${head.map((cell) => `<th>${inline(cell)}</th>`).join("")}</tr></thead>` +
          `<tbody>${rows.map((row) => `<tr>${head.map((_, n) => `<td>${inline(row[n] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`,
      );
    } else if (HEADING.test(line)) {
      const title = HEADING.exec(line)[2].trimEnd().replace(/(^|\s)#+$/, "").trimEnd();
      out.push(`<h4>${inline(title)}</h4>`);
      i += 1;
    } else if (RULE.test(line)) {
      out.push("<hr>");
      i += 1;
    } else if (QUOTE.test(line)) {
      const quote = [];
      for (; i < lines.length && QUOTE.test(lines[i]); i += 1) quote.push(lines[i].replace(/^\s*>\s?/, ""));
      out.push(
        depth < QUOTE_DEPTH
          ? `<blockquote>${markdown(quote.join("\n"), depth + 1)}</blockquote>`
          : `<p>${quote.map((part) => inline(part.replace(/^[\s>]+/, ""))).join("<br>")}</p>`,
      );
    } else if (ITEM.test(line)) {
      const items = [];
      for (; i < lines.length && lines[i].trim(); i += 1) {
        const match = ITEM.exec(lines[i]);
        if (match) {
          const number = /\d/.test(match[2]) ? Number.parseInt(match[2], 10) : null;
          items.push({ indent: match[1].replace(/\t/g, "  ").length, number, lines: [match[3]] });
        } else if (/^\s+\S/.test(lines[i]) && !startsBlock(lines, i)) {
          items.at(-1).lines.push(lines[i].trim());
        } else {
          break;
        }
      }
      out.push(renderList(items));
    } else {
      const paragraph = [];
      for (; i < lines.length && lines[i].trim() && (!paragraph.length || !startsBlock(lines, i)); i += 1) paragraph.push(lines[i].trim());
      out.push(`<p>${paragraph.map((part) => inline(part, true)).join("<br>")}</p>`);
    }
  }
  return out.join("");
}
export function clock(iso) {
  const date = new Date(iso);
  const time = date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (date.toDateString() === new Date().toDateString()) return time;
  return `${date.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
}
// Steps show paths relative to the session's directory, and home as "~".
export function compactPaths(text, cwd) {
  let result = text;
  if (cwd) result = result.replaceAll(`${cwd}/`, "./").replaceAll(cwd, ".");
  const home = state.snapshot?.home;
  return home ? result.replaceAll(home, "~") : result;
}
export function ago(iso) {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}
export function threadLabel(key) {
  let match;
  if ((match = /^slack:([A-Z0-9]+):/.exec(key))) return `Slack thread in ${match[1]}`;
  if ((match = /^slack:([A-Z0-9]+)$/.exec(key))) return `Slack DM ${match[1]}`;
  if ((match = /^github:(.+)$/.exec(key))) return match[1];
  return key;
}
export function shortPath(path) {
  const home = state.snapshot?.home;
  return home && path?.startsWith(home) ? `~${path.slice(home.length)}` : path || "";
}
export function since(iso) {
  const passed = ago(iso);
  return passed === "now" ? "just now" : `${passed} ago`;
}
export function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}
export function lines(value) {
  return value.split("\n").map((line) => line.trim()).filter(Boolean);
}
