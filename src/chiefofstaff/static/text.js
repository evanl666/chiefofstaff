// Text for display: a safe Markdown subset, links and short paths.

import { escapeHtml } from "./dom.js";
import { state } from "./state.js";
// Markdown: a small, safe subset. Text is escaped first; only these tags are produced.

const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// A URL ends at whitespace, brackets, quotes, Markdown marks or punctuation (Western or CJK):
// "…/pull/1068：head", "…/pull/79”" and "…/repo**" are the URL without what follows. One rule
// for links in text and for the chips on cards.
// Parentheses may be part of a URL (a wiki page "Foo_(bar)"): one that closes nothing in it is
// the sentence's.
const URL_CHARS = "[^\\s<>\\[\\]{}`'\"*|，。、；：！？（）「」『』【】“”‘’《》〈〉…]+";
export const URL_RE = new RegExp(`https?:\\/\\/${URL_CHARS}`, "g");
// Sentence punctuation after a URL (not "_" or "~": paths end with them).
const URL_TRAIL = /(?:[.,;:!?'"\]]|&quot;|&#39;)$/;
const count = (text, char) => text.split(char).length - 1;
// A URL and the sentence punctuation after it: ["https://x/y", "."].
export function trimUrl(raw) {
  let url = raw;
  let trail = "";
  for (;;) {
    const mark = url.match(URL_TRAIL)?.[0];
    if (mark) {
      trail = mark + trail;
      url = url.slice(0, -mark.length);
    } else if (url.endsWith(")") && count(url, ")") > count(url, "(")) {
      trail = `)${trail}`;
      url = url.slice(0, -1);
    } else {
      return [url, trail];
    }
  }
}
// A URL's path as words: undecodable escapes ("%zz") stay as they are.
export function decodePath(path) {
  try {
    return decodeURIComponent(path);
  } catch {
    return path;
  }
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
        // A link's address may hold one level of parentheses.
        "\\[([^\\]\\n]+)\\]\\((https?:\\/\\/(?:[^\\s()]|\\([^\\s()]*\\))+)\\)",
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
  const path = decodePath(parsed.pathname);
  const last = path.split("/").filter(Boolean).pop() || "";
  // Its role on a card: "pr"; "output", what the task made (a doc); "run", a top-level
  // workflow run the owner would open (a Flyte execution, an Airflow DAG run); "diagnostic",
  // what is under one (a Spark application, logs, a debug page, a CI run); "reference",
  // something it points to.
  const is = (kind, label, role, extra = {}) => ({ kind, label, role, ...extra });
  // A file its worker made (declared by its path): opened here, not by the browser.
  if (parsed.protocol === "file:") return is("file", last || "File", "output", { path });
  if ((match = /^https:\/\/github\.com\/([^/]+)\/([^/]+)\/pull\/(\d+)/.exec(url))) {
    const key = `${match[1]}/${match[2]}#${match[3]}`.toLowerCase();
    return is("pr", `${match[2]}#${match[3]}`, "pr", { key, title: prTitle(key) });
  }
  if ((match = /^https:\/\/github\.com\/[^/]+\/([^/]+)\/issues\/(\d+)/.exec(url))) return is("issue", `${match[1]}#${match[2]}`, "reference");
  if ((match = /^https:\/\/github\.com\/[^/]+\/([^/]+)\/actions\/runs\//.exec(url))) return is("run", `CI run · ${match[1]}`, "diagnostic");
  if (/^https:\/\/github\.com\/[^/]+\/[^/]+\/(blob|tree)\//.test(url)) return is("file", last || "File", "reference");
  // A Google document is one with an id (docs.google.com alone is not one). Named by what it
  // is: its id tells it apart (linkKey) and is in the tooltip, never its name.
  const docId = /\/d\/(?:e\/)?([\w-]{6,})/.exec(parsed.pathname)?.[1] || "";
  if (docId && /docs\.google\.com\/spreadsheets/.test(url)) return is("doc", "Sheet", "output", { id: docId });
  if (docId && /docs\.google\.com\/presentation/.test(url)) return is("doc", "Slides", "output", { id: docId });
  if (docId && /docs\.google\.com\//.test(url)) return is("doc", "Google Doc", "output", { id: docId });
  if (/\.slack\.com\/archives\//.test(url)) return is("slack", "Slack message", "reference");
  if ((match = /atlassian\.net\/browse\/([A-Z][A-Z0-9]+-\d+)/.exec(url)) || (match = /\/browse\/([A-Z][A-Z0-9]+-\d+)/.exec(url))) return is("ticket", match[1], "reference");
  if (/notion\.so$/.test(host)) return is("doc", "Notion", "output");
  // Logs, pods and task instances are under a run: for diagnosis, whatever runs them.
  const underRun = /\/(logs?|pods?|nodes|taskinstance|task_instance|containers?)(\/|$)|[?&](task_id|try_number|pod)=/i.test(parsed.pathname + parsed.search);
  // Airflow (also on another host, an rdev box's): the DAG or its run; Flyte: the execution.
  const dag = parsed.searchParams.get("dag_id") || /\/dags\/([^/?#]+)/.exec(path)?.[1] || "";
  if (/airflow/i.test(host) || dag) {
    return is("run", `Airflow${dag ? ` · ${shorten(dag, 28)}` : " run"}`, underRun ? "diagnostic" : "run");
  }
  if (/flyte/i.test(host) || /\/console\/projects\/[^/]+\/domains\/[^/]+\/executions?\//.test(path)) {
    const execution = /\/executions?\/([^/?#]+)/.exec(path)?.[1] || /\/(?:executions?|exec)\/[^/]+\/[^/]+\/([^/?#]+)/.exec(path)?.[1] || "";
    return is("run", `Flyte${execution ? ` · ${shorten(execution, 24)}` : " run"}`, underRun ? "diagnostic" : "run");
  }
  // Spark History: its application, for diagnosis (the flow that ran it is what to open).
  if ((match = /(application_\d+_\d+)/.exec(url)) && /spark|shs|history/i.test(host + path)) return is("run", `Spark · ${match[1]}`, "diagnostic", { id: match[1] });
  if (/spark|shs-/i.test(host)) return is("run", `Spark history${last ? ` · ${shorten(last, 28)}` : ""}`, "diagnostic");
  if (/(^|[.-])rdev([.-]|$)/i.test(host)) return is("run", `rdev · ${shorten(host.split(".")[0], 28)}`, "diagnostic", { id: host.split(".")[0] });
  if (/darwin/i.test(host)) return is("doc", `Darwin${last ? ` · ${shorten(last, 28)}` : ""}`, "output");
  return is("link", last && !/^[\w-]{32,}$/.test(last) ? `${shorten(host, 28)} · ${shorten(last, 24)}` : shorten(host, 40), "reference");
}
// One key for one thing linked in different ways: a Google document by its id; else the
// address without what only says how it was shared ("?usp=sharing", "utm_…") or a place in the
// page ("#L10"). What tells runs apart stays: "?dag_id=a", "#job/2/run/3".
const SHARING = /^(usp|utm_\w+|ref|ref_src|fbclid|gclid|si|authuser|ouid|rtpof|sd)$/i;
export function linkKey(url) {
  try {
    const parsed = new URL(url);
    const doc = /docs\.google\.com$/.test(parsed.hostname) && /\/d\/(?:e\/)?([\w-]{6,})/.exec(parsed.pathname);
    if (doc) return `gdoc:${doc[1]}`;
    const query = [...parsed.searchParams].filter(([name]) => !SHARING.test(name)).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    const search = query.length ? `?${new URLSearchParams(query)}` : "";
    const route = parsed.hash.includes("/") ? parsed.hash : "";
    return `${parsed.hostname.replace(/^www\./, "").toLowerCase()}${parsed.pathname.replace(/\/+$/, "")}${search}${route}`;
  } catch {
    return url;
  }
}
// The links of a report: not those in code (a block or `inline`), nor one cut short ("…" or
// "...").
export function reportUrls(text) {
  return findUrls((text || "").replace(/```[\s\S]*?(```|$)/g, " ").replace(/`[^`\n]+`/g, " ").replace(/https?:\/\/\S*(?:…|\.{3})\S*/g, " "));
}
// Words that only point at a link ("here", "this doc", "链接"): not its name.
const GENERIC_ANCHOR = /^(?:(?:click|see|open)\s+)?(?:(?:the|this|that)\s+)?(?:here|link|doc|docs|document|source|url|page)$|^(?:点击)?(?:这里|此处|链接|这个链接|文档|这个文档)$/i;
// The words a task's reports linked each link with ("[qwen3 SFT 11-05](https://…)"), by its key:
// its latest report's, then those the server kept from earlier ones (`link_names`).
export function linkNames(task) {
  const names = new Map();
  const add = (url, words) => {
    const name = plainText(String(words || "")).replace(/\s+/g, " ").trim();
    const bare = name.replace(/^[\s"'“‘(（]+|[\s"'”’)）.,:;!?。，：；！？]+$/g, "");
    if (name && !/^https?:\/\//.test(name) && !GENERIC_ANCHOR.test(bare) && !names.has(linkKey(url))) names.set(linkKey(url), shorten(name, 80));
  };
  const text = [task.result, task.progress].filter(Boolean).join("\n").replace(/```[\s\S]*?(```|$)/g, " ").replace(/`[^`\n]+`/g, " ");
  for (const [, words, url] of text.matchAll(/(?<!!)\[([^\]\n]+)\]\((https?:\/\/(?:[^\s()]|\([^\s()]*\))+)\)/g)) add(url, words);
  for (const [url, words] of Object.entries(task.link_names || {})) add(url, words);
  return names;
}
const fileUrl = (path) => `file://${path.split("/").map(encodeURIComponent).join("/")}`;
// A task's links, each once, by what each is to the owner: { url, key, found (linkKind), role,
// said (what its worker declared about it), file, name, path }. What it made is what its worker
// declared (report(artifacts=…)), what its reports say it made (`made_links`) and the files it
// wrote in its folder (`files`); a top-level run it gives is its run. A document it only
// mentions, and anything it was given, is a reference: a report saying it made a link it was
// given does not make it its output (declared, or a file it wrote, does).
export function taskLinks(task) {
  const given = new Set((task.given_links || []).map(linkKey));
  const made = new Set((task.made_links || []).map(linkKey));
  const declared = new Map((task.artifacts || []).map((item) => [linkKey(item.url), item]));
  const files = new Map((task.files || []).map((file) => [linkKey(fileUrl(file.path)), { ...file, url: fileUrl(file.path) }]));
  const names = linkNames(task);
  const asked = `${task.title}\n${task.goal || ""}`;
  // What its worker declared first, then what its reports link, then the files it wrote.
  const urls = [...declared.values()].map((item) => item.url).concat(task.links || [], reportUrls([task.result, task.progress].filter(Boolean).join("\n")), [...files.values()].map((file) => file.url));
  const seen = new Set();
  const links = [];
  for (const url of urls) {
    const found = linkKind(url);
    const key = linkKey(url);
    if (!found || seen.has(key)) continue;
    seen.add(key);
    const said = declared.get(key);
    const file = files.get(key);
    let role = found.role;
    if (role === "diagnostic" && found.id && asked.includes(found.id)) role = "run";
    if (said || file || (made.has(key) && !given.has(key))) {
      if (!["pr", "run"].includes(role)) role = "output";
    } else if (role === "output" || given.has(key)) {
      role = "reference";
    }
    links.push({ url, key, found, role, said, file, name: said?.name || file?.name || names.get(key) || "", path: said?.path || file?.path || found.path || "" });
  }
  return links;
}
// A link's name on a chip or a row: what its worker or a report called it, else what it is
// ("Google Doc", "Flyte · abc123", a PR with its title).
export const linkName = (link) => link.name || (link.found.title ? `${link.found.label} · ${shorten(link.found.title, 40)}` : link.found.label);
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
  // An image is a link to it (nothing loads from elsewhere by itself).
  const images = escaped.replace(/!\[([^\]\n]*)\]\((https?:\/\/[^\s()]+)\)/g, (_, alt, href) => `[🖼 ${alt || "image"}](${href})`);
  return images.replace(inlinePattern(), (match, code, bold, strike, italic, label, href, angled, angledLabel, bare, path) => {
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
    const cut = value.search(/&(gt|lt|quot|#39);/);
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
  // Code as it is, and underscores as markdown() shows them: `__init__.py`, __init__.py.
  const codes = [];
  return (text || "")
    .replace(/```[\s\S]*?(```|$)/g, " ")
    .replace(/`([^`\n]+)`/g, (_, code) => `\u0000${codes.push(code) - 1}\u0000`)
    .replace(/!\[([^\]\n]*)\]\((https?:[^\s)]+)\)/g, "$1")
    .replace(/\[([^\]\n]+)\]\((https?:[^\s)]+)\)/g, "$1")
    .replace(/(\*\*|~~)(.+?)\1/g, "$2")
    .replace(/(?<![\w*])\*([^*\s][^*\n]*?)\*(?![\w*])/g, "$1")
    .replace(/\u0000(\d+)\u0000/g, (_, index) => codes[Number(index)])
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
  // Links as what they are, short: "app#592", "docs.google.com…"; what follows a link (")", ".")
  // stays the sentence's.
  return plainText(said.join("\n"))
    .replace(/\s*\n\s*/g, " ")
    .replace(URL_RE, (raw) => {
      const [url, trail] = trimUrl(raw);
      const pr = /^https:\/\/github\.com\/[^/\s]+\/([^/\s]+)\/(?:pull|issues)\/(\d+)/.exec(url);
      if (pr) return `${pr[1]}#${pr[2]}${trail}`;
      const site = /^https?:\/\/(?:www\.)?([^/\s]+)\//.exec(url);
      return site ? `${site[1]}…${trail.replace(/^\./, "")}` : raw;
    });
}
// A notice's main part, without its aside: "Started “Benchmark words() · textkit”" of
// "Started “Benchmark words() · textkit” · new because …" (a " · " inside quotes is the title's).
export function headOf(text) {
  let depth = 0;
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] === "“") depth += 1;
    else if (text[i] === "”") depth = Math.max(0, depth - 1);
    else if (!depth && text.startsWith(" · ", i)) return text.slice(0, i);
  }
  return text;
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
  if ((match = /^slack:([A-Z0-9]+):/.exec(key))) return `Slack thread in ${slackName("#", match[1])}`;
  if ((match = /^slack:([A-Z0-9]+)$/.exec(key))) return `Slack ${slackName("#", match[1])}`;
  if ((match = /^github:(.+)$/.exec(key))) return match[1];
  return key;
}
// A Slack id by its name, as Slack shows it: "@Bob", "#eng", "DM with Bob" (`slack_names`, else
// the name Slack wrote beside it); an id with no name known stays as it is.
function slackName(mark, id, said = "") {
  const name = String(state.snapshot?.slack_names?.[id] || said || "").trim();
  if (mark === "@") return `@${name.replace(/^@/, "") || id}`;
  if (id.startsWith("D")) return !name ? "DM" : /\bDM\b|direct/i.test(name) ? name : `DM with ${name.replace(/^@/, "")}`;
  return `#${name.replace(/^#/, "") || id}`;
}
// A Slack id written bare (by a session, from Slack's tools): a user's (U…, W…), a channel's
// (C…, G…) or a DM's (D…), in capitals with a digit; not part of a word, a link or a path.
const SLACK_ID = String.raw`(?<![\w/.:#@-])([UWCGD](?=[A-Z0-9]*\d)[A-Z0-9]{4,11})(?![\w/-])`;
const SLACK_ID_IN_BRACKETS = new RegExp(String.raw`[ \t]*\(${SLACK_ID}\)`, "g");
const SLACK_ID_BARE = new RegExp(String.raw`(\bDM[ \t]+)?${SLACK_ID}`, "g");
// Slack's text as people read it: names for its ids ("<@U0BOB001> (Bob)", "<#C0ENG|eng>", a
// thread's key, "Bob (U0BOB001)", an id with a name known), and without the message stamps
// ("| ts: 1712345678.000100", "(ts 1712345678.000100)").
export function slackText(text) {
  const names = state.snapshot?.slack_names || {};
  // "DM <#D0BOB0001>" is "DM with Bob", not "DM DM with Bob".
  const named = (dm, mark, id, said) => (dm && !id.startsWith("D") ? dm : "") + slackName(mark, id, said);
  const markOf = (id) => (/^[UW]/.test(id) ? "@" : "#");
  return String(text || "")
    .replace(/[ \t]*\([ \t]*ts:?[ \t]*\d{9,}\.\d+[ \t]*\)/g, "")
    .replace(/[ \t]*[|,]?[ \t]*\bts:?[ \t]*\d{9,}\.\d+/g, "")
    .replace(/\b(thread[ \t]+)?slack:([A-Z][A-Z0-9]{2,})(?::\d{9,}\.\d+)?/g, (_, thread, id) => `${thread ? "thread in " : ""}${slackName("#", id)}`)
    .replace(/(\bDM[ \t]+)?<([@#])([A-Z0-9]{2,})(?:\|([^>\n]+))?>(?:[ \t]*\(([^()\n]{1,60})\)(?=[ \t]*(?:\||$)))?/gm, (_, dm, mark, id, label, beside) => named(dm, mark, id, label || beside))
    .replace(SLACK_ID_IN_BRACKETS, (whole, id, offset, all) => {
      const name = names[id];
      // A user's id after their name says nothing more; one with no name known means nothing.
      if (/^[UW]/.test(id) && id.length >= 8 && (!name || all.slice(0, offset).toLowerCase().endsWith(name.toLowerCase()))) return "";
      return name ? ` (${named("", markOf(id), id)})` : whole;
    })
    .replace(SLACK_ID_BARE, (whole, dm, id) => (names[id] ? named(dm, markOf(id), id) : whole));
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
// A task's changes in a chip: "+12 −3 in 2 files"; a new file too large to count lines in is
// said as such, not as "+0 −0" ("1 large new file"; `large` may be missing).
export function changesText(changed) {
  const large = Math.min(Number(changed.large) || 0, changed.files || 0);
  const counted = (changed.files || 0) - large;
  const lines = counted ? `+${changed.insertions || 0} −${changed.deletions || 0} in ${plural(counted, "file")}` : "";
  return [lines, large ? `${large} large new file${large === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ");
}
// In a folder of the owner's, files changed during the task by others (the owner, a pull,
// another task): not its own, so not counted ("not counted: 2 files"; may be missing).
export const notCountedText = (changed) => (Number(changed.not_counted) > 0 ? `not counted: ${plural(Number(changed.not_counted), "file")}` : "");
export function lines(value) {
  return value.split("\n").map((line) => line.trim()).filter(Boolean);
}
// The first line of a text, without its Markdown: what a one-line entry shows.
export function firstLine(text) {
  // Without its list mark or heading ("1. ", "- ", "## "), not its own numbers ("3 tests").
  const lines = text.split("\n").map((line) => line.replace(/^\s*(?:(?:#{1,6}|>|[-*•+]|\d+[.)])\s+)+/, "").replace(/\*\*|`/g, "").trim()).filter(Boolean);
  // Not a lead-in like "Results:" when something follows it.
  return lines.find((line) => !/^[\p{L}\s]{1,24}:$/u.test(line)) || lines[0] || "";
}
