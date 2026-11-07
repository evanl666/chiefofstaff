// Text for display: a safe Markdown subset, links and short paths.

import { escapeHtml } from "./dom.js";
import { state } from "./state.js";
// Markdown: a small, safe subset. Text is escaped first; only these tags are produced.

const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// A URL ends at whitespace, brackets, quotes, Markdown marks or punctuation (Western or CJK):
// "…/pull/1068：head", "…/pull/79”" and "…/repo**" are the URL without what follows. One rule
// for links in text and for the chips on cards.
const URL_CHARS = "[^\\s<>()\\[\\]{}`'\"*|，。、；：！？（）「」『』【】“”‘’《》〈〉…]+";
export const URL_RE = new RegExp(`https?:\\/\\/${URL_CHARS}`, "g");
const URL_TRAIL = /(?:[.,;:!?'"_~\])]|&quot;|&#39;)+$/;
// A URL and the sentence punctuation after it: ["https://x/y", "."].
export function trimUrl(raw) {
  const trail = raw.match(URL_TRAIL)?.[0] || "";
  return [trail ? raw.slice(0, -trail.length) : raw, trail];
}
// Every URL in a text, in order, each once.
// Slack writes "&" in a link as "&amp;": the link has "&".
export function findUrls(text) {
  return [...new Set(((text || "").match(URL_RE) || []).map((raw) => trimUrl(raw.replace(/&amp;/g, "&"))[0]).filter((url) => /^https?:\/\/[^/\s]+\.[^/\s]/.test(url)))];
}
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
        // <https://…> and Slack's <https://…|label>, escaped.
        "&lt;(https?:\\/\\/[^\\s|]+?)(?:\\|([^\\n]+?))?&gt;",
        `(https?:\\/\\/${URL_CHARS})`,
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
const unescapeHtml = (text) => text.replace(/&(amp|lt|gt|quot|#39);/g, (_, name) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" })[name]);
export const shorten = (text, max) => (text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text);
// A PR's title as GitHub has it ("owner/repo#n", any case), once Chief of Staff read it.
export function prTitle(key) {
  return state.snapshot?.pr_titles?.[String(key).toLowerCase()] || "";
}
// What a link is, for a chip or a link's text: { kind, label, key (PRs), title (PRs) }.
export function linkKind(url) {
  let match;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  const host = parsed.hostname.replace(/^www\./, "");
  const path = decodeURIComponent(parsed.pathname);
  const last = path.split("/").filter(Boolean).pop() || "";
  if ((match = /^https:\/\/github\.com\/([^/]+)\/([^/]+)\/pull\/(\d+)/.exec(url))) {
    const key = `${match[1]}/${match[2]}#${match[3]}`.toLowerCase();
    return { kind: "pr", label: `${match[2]}#${match[3]}`, key, title: prTitle(key) };
  }
  if ((match = /^https:\/\/github\.com\/[^/]+\/([^/]+)\/issues\/(\d+)/.exec(url))) return { kind: "issue", label: `${match[1]}#${match[2]}` };
  if ((match = /^https:\/\/github\.com\/[^/]+\/([^/]+)\/actions\/runs\//.exec(url))) return { kind: "run", label: `CI run · ${match[1]}` };
  if (/^https:\/\/github\.com\/[^/]+\/[^/]+\/(blob|tree)\//.test(url)) return { kind: "file", label: last || "File" };
  if (/docs\.google\.com\/spreadsheets/.test(url)) return { kind: "doc", label: "Sheet" };
  if (/docs\.google\.com\/presentation/.test(url)) return { kind: "doc", label: "Slides" };
  if (/docs\.google\.com\//.test(url)) return { kind: "doc", label: "Google Doc" };
  if (/\.slack\.com\/archives\//.test(url)) return { kind: "slack", label: "Slack message" };
  if ((match = /atlassian\.net\/browse\/([A-Z][A-Z0-9]+-\d+)/.exec(url)) || (match = /\/browse\/([A-Z][A-Z0-9]+-\d+)/.exec(url))) return { kind: "ticket", label: match[1] };
  if (/notion\.so$/.test(host)) return { kind: "doc", label: "Notion" };
  // Spark History: its application; Flyte and Airflow: the execution or DAG; rdev and Darwin:
  // which one. Anything else: the host and the last part of the path.
  if ((match = /(application_\d+_\d+)/.exec(url)) && /spark|shs|history/i.test(host + path)) return { kind: "run", label: `Spark · ${match[1]}` };
  if (/spark|shs-/i.test(host)) return { kind: "run", label: `Spark history${last ? ` · ${shorten(last, 28)}` : ""}` };
  if (/flyte/i.test(host)) {
    const execution = /\/executions?\/([^/?#]+)/.exec(path)?.[1] || /\/(?:executions?|exec)\/[^/]+\/[^/]+\/([^/?#]+)/.exec(path)?.[1] || "";
    return { kind: "run", label: `Flyte${execution ? ` · ${shorten(execution, 24)}` : " run"}` };
  }
  if (/airflow/i.test(host)) {
    const dag = parsed.searchParams.get("dag_id") || /\/dags\/([^/?#]+)/.exec(path)?.[1] || "";
    return { kind: "run", label: `Airflow${dag ? ` · ${shorten(dag, 28)}` : " run"}` };
  }
  if (/(^|[.-])rdev([.-]|$)/i.test(host)) return { kind: "run", label: `rdev · ${shorten(host.split(".")[0], 28)}` };
  if (/darwin/i.test(host)) return { kind: "doc", label: `Darwin${last ? ` · ${shorten(last, 28)}` : ""}` };
  return { kind: "link", label: last && !/^[\w-]{32,}$/.test(last) ? `${shorten(host, 28)} · ${shorten(last, 24)}` : shorten(host, 40) };
}
// A link's text in a message: a PR by its number and title; a Slack message as one.
function linkLabel(url) {
  let match;
  if ((match = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/(?:pull|issues)\/(\d+)(?:[/?#].*)?$/.exec(url))) {
    const title = /\/pull\//.test(url) ? prTitle(`${match[1]}#${match[2]}`) : "";
    return title ? `${match[1].split("/")[1]}#${match[2]} · ${shorten(title, 50)}` : `${match[1]}#${match[2]}`;
  }
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
  return `<span class="path" data-path="${path}" title="${path}\nClick to open (or copy)">${escapeHtml(displayPath(path))}</span>`;
}
function inlineEscaped(escaped) {
  return escaped.replace(inlinePattern(), (match, code, bold, strike, italic, label, href, angled, angledLabel, bare, path) => {
    if (code !== undefined) return inlinePattern.path.test(code) ? pathChip(code) : `<code>${code}</code>`;
    if (bold !== undefined) return `<strong>${inlineEscaped(bold)}</strong>`;
    if (strike !== undefined) return `<s>${inlineEscaped(strike)}</s>`;
    if (italic !== undefined) return `<em>${inlineEscaped(italic)}</em>`;
    if (path !== undefined) {
      const trail = path.match(TRAILING)?.[0] || "";
      return pathChip(trail ? path.slice(0, -trail.length) : path) + trail;
    }
    let [value, trail] = href ? [href, ""] : angled ? [angled, ""] : trimUrl(bare);
    // Not past an escaped "<" or ">" ("https://x>" in a sentence), and Slack's "&amp;" is "&".
    const cut = value.search(/&(gt|lt);/);
    if (cut > 0) [value, trail] = [value.slice(0, cut), value.slice(cut) + trail];
    value = value.replace(/&amp;amp;/g, "&amp;");
    const text = label ?? angledLabel ?? escapeHtml(linkLabel(unescapeHtml(value)));
    return `<a href="${value}" title="${value}" target="_blank" rel="noreferrer">${text}</a>${trail}`;
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
// Any info string ("python", "c#", "objective-c", "shell title=x"), not a backtick in it.
const FENCE = /^\s*(```+|~~~+)[^`]*$/;
const HEADING = /^\s*(#{1,6})\s+(.*)$/;
const QUOTE_DEPTH = 6;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^\s*>/;
const ITEM = /^(\s*)([-*•+]|\d+[.)])\s+(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const isTable = (lines, i) => lines[i].includes("|") && TABLE_RULE.test(lines[i + 1] || "") && /\||--/.test(lines[i + 1] || "");
const startsBlock = (lines, i) => [FENCE, HEADING, RULE, QUOTE, ITEM].some((re) => re.test(lines[i])) || isTable(lines, i);
// A table row's cells: a "|" inside `code` or escaped ("\\|") is part of its cell.
function cells(line) {
  const row = line.trim().replace(/^\|/, "");
  const found = [];
  let cell = "";
  let code = false;
  for (let i = 0; i < row.length; i += 1) {
    const char = row[i];
    if (char === "\\" && row[i + 1] === "|") {
      cell += "|";
      i += 1;
    } else if (char === "`") {
      code = !code;
      cell += char;
    } else if (char === "|" && !code) {
      found.push(cell.trim());
      cell = "";
    } else {
      cell += char;
    }
  }
  if (cell.trim() || !row.endsWith("|")) found.push(cell.trim());
  return found;
}
const ALIGN = (rule) => cells(rule).map((cell) => (cell.startsWith(":") && cell.endsWith(":") ? "center" : cell.endsWith(":") ? "right" : ""));
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
      const align = ALIGN(lines[i + 1]);
      const style = (n) => (align[n] ? ` class="align-${align[n]}"` : "");
      const rows = [];
      for (i += 2; i < lines.length; i += 1) {
        // A blank line inside a table, with more rows after it, does not end it.
        if (!lines[i].trim() && lines[i + 1]?.trim().startsWith("|") && !isTable(lines, i + 1)) continue;
        if (!lines[i].includes("|") || !lines[i].trim()) break;
        const row = cells(lines[i]);
        if (row.some(Boolean)) rows.push(row);  // An empty row shows as nothing, not a gap.
      }
      const width = Math.max(head.length, ...rows.map((row) => row.length));
      const columns = [...Array(width).keys()];
      out.push(
        `<div class="table"><table><thead><tr>${columns.map((n) => `<th${style(n)}>${inline(head[n] || "")}</th>`).join("")}</tr></thead>` +
          `<tbody>${rows.map((row) => `<tr>${columns.map((n) => `<td${style(n)}>${inline(row[n] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`,
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
// Markdown as plain words, for a line on a card: no marks, links as their text, code as is.
export function plainText(text) {
  return (text || "")
    .replace(/```[\s\S]*?(```|$)/g, " ")
    .replace(/\[([^\]\n]+)\]\((https?:[^\s)]+)\)/g, "$1")
    .replace(/(\*\*|__|~~)(.+?)\1/g, "$2")
    .replace(/(?<![\w*])\*([^*\s][^*\n]*?)\*(?![\w*])/g, "$1")
    .replace(/`([^`\n]+)`/g, "$1")
    .replace(/^\s*(#{1,6}|>+|[-*•+]|\d+[.)])\s+/gm, "")
    .replace(/^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/gm, "")
    .replace(/^[ \t]*\|[ \t]*|[ \t]*\|[ \t]*$/gm, "")
    .replace(/[ \t]*\|[ \t]*/g, " · ")
    .replace(/[ \t]+/g, " ")
    .replace(/\n{2,}/g, "\n")
    .trim();
}
// What a report says, in its first sentences: past a title ("## Summary", "Result:"), a rule or
// a lone label, to the first line that says something.
export function gist(text) {
  const lines = (text || "").replace(/```[\s\S]*?(```|$)/g, "").split("\n");
  const said = lines.filter((line) => {
    const bare = line.trim();
    if (!bare || /^#{1,6}\s/.test(bare) || /^([-*_])(\s*\1){2,}$/.test(bare) || /^\|?\s*:?-{2,}/.test(bare)) return false;
    // A label alone: "Summary:", "**结论**", "结论：".
    const plain = plainText(bare).replace(/[:：]$/, "");
    return !(plain.length <= 12 && plain.split(/\s+/).length <= 2 && (/[:：]$/.test(plainText(bare)) || /^\*\*.*\*\*$/.test(bare)));
  });
  // Links as what they are, short: "app#592", "docs.google.com…".
  return plainText(said.join("\n"))
    .replace(/\s*\n\s*/g, " ")
    .replace(/https:\/\/github\.com\/[^/\s]+\/([^/\s]+)\/(?:pull|issues)\/(\d+)[^\s]*/g, "$1#$2")
    .replace(/https?:\/\/(?:www\.)?([^/\s]+)\/[^\s]*/g, "$1…");
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
// The first line of a text, without its Markdown: what a one-line entry shows.
export function firstLine(text) {
  // Without its list mark or heading ("1. ", "- ", "## "), not its own numbers ("3 tests").
  const lines = text.split("\n").map((line) => line.replace(/^\s*(?:(?:#{1,6}|>|[-*•+]|\d+[.)])\s+)+/, "").replace(/\*\*|__|`/g, "").trim()).filter(Boolean);
  // Not a lead-in like "Results:" when something follows it.
  return lines.find((line) => !/^[\p{L}\s]{1,24}:$/u.test(line)) || lines[0] || "";
}
