// A task's changes as a diff: read them, comment on lines, send the comments to its worker.

import { api, el, submits, toast } from "./dom.js";
import { plural } from "./text.js";

const OPEN_LINES = 400; // Larger files start folded.
const MAX_LINES = 5000; // Shown per file.

// A path as git wrote it: one with a `"`, a `\` or a control character comes quoted, with C-style
// escapes (octal bytes for UTF-8: "\303\274" is "ü"), as workspaces.git_path reads it.
const GIT_ESCAPES = { a: 7, b: 8, t: 9, n: 10, v: 11, f: 12, r: 13 };
export function gitPath(name) {
  if (name.length < 2 || !name.startsWith('"') || !name.endsWith('"')) return name;
  const encoder = new window.TextEncoder();
  const body = name.slice(1, -1);
  const bytes = [];
  let at = 0;
  for (const match of body.matchAll(/\\([0-7]{3}|.)/gsu)) {
    bytes.push(...encoder.encode(body.slice(at, match.index)));
    const code = match[1];
    if (code.length === 3) bytes.push(parseInt(code, 8));
    else if (code in GIT_ESCAPES) bytes.push(GIT_ESCAPES[code]);
    else bytes.push(...encoder.encode(code));
    at = match.index + match[0].length;
  }
  bytes.push(...encoder.encode(body.slice(at)));
  return new window.TextDecoder().decode(new Uint8Array(bytes));
}
// The new name on a "diff --git a/… b/…" line, each side quoted or not.
const QUOTED = String.raw`"(?:[^"\\]|\\.)*"`;
const GIT_NAMES = new RegExp(String.raw`^diff --git (${QUOTED}|a/.*) (${QUOTED}|b/.*)$`);
function newName(line) {
  const names = GIT_NAMES.exec(line);
  return names ? gitPath(names[2]).replace(/^b\//, "") : line.slice(11);
}

export function parseDiff(text) {
  const files = [];
  let file = null;
  let oldLine = 0;
  let newLine = 0;
  for (const line of text.split("\n")) {
    if (line.startsWith("diff --git ")) {
      file = { name: newName(line), lines: [], added: 0, removed: 0, note: "" };
      files.push(file);
    } else if (!file) {
      continue;
    } else if (line.startsWith("@@")) {
      const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
      if (!hunk) continue;
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      file.lines.push({ kind: "hunk", text: line });
    } else if (!file.lines.length) {
      // The file's header, before its first hunk.
      if (line.startsWith("+++ ") && gitPath(line.slice(4)).startsWith("b/")) file.name = gitPath(line.slice(4)).slice(2);
      else if (line.startsWith("Binary files")) file.note = "Binary file changed";
      else if (line.startsWith("deleted file")) file.note = "Deleted";
      else if (line.startsWith("rename from ")) file.note = `Renamed from ${gitPath(line.slice(12))}`;
    } else if (line.startsWith("+")) {
      file.lines.push({ kind: "add", text: line.slice(1), line: newLine++ });
      file.added++;
    } else if (line.startsWith("-")) {
      file.lines.push({ kind: "del", text: line.slice(1), old: oldLine++ });
      file.removed++;
    } else if (line.startsWith(" ")) {
      file.lines.push({ kind: "ctx", text: line.slice(1), line: newLine++, old: oldLine++ });
    }
  }
  return files;
}

// Comments become one follow-up for the worker, each naming its file and line.
export function reviewText(comments) {
  const items = comments.map((comment) => {
    const where = comment.kind === "del" ? `${comment.file} (removed line ${comment.old})` : `${comment.file}:${comment.line}`;
    const code = comment.text.trim().replaceAll("`", "'").slice(0, 100);
    return `- ${where}${code ? ` \`${code}\`` : ""}: ${comment.body}`;
  });
  return `Review comments on your changes:\n\n${items.join("\n")}`;
}

// A new file too large to show in the diff: its name, that it is new, and its size.
export const largeWords = (file) => `New file, ${sizeText(file.size)}, not shown`;
function sizeText(bytes) {
  if (!bytes) return "too large";
  const units = ["bytes", "KB", "MB", "GB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit++;
  }
  return `${unit ? Math.round(size * 10) / 10 : size} ${units[unit]}`;
}
function largeFile(file) {
  return el("div", { class: "diff-file large-file" }, el("div", { class: "summary" }, el("span", { class: "name", text: file.name }), el("span", { class: "muted", text: largeWords(file) })));
}

export function showChanges(task, onSent) {
  const comments = [];
  const body = el("div", { class: "diff-body" }, el("p", { class: "muted", text: "Loading the changes…" }));
  const error = el("span", { class: "error" });
  const count = el("span", { class: "muted" });
  const send = el("button", { type: "button", text: "Send to the worker", disabled: true, onclick: sendComments });
  const close = () => {
    if (comments.length && !confirm(`Discard ${plural(comments.length, "comment")}?`)) return;
    comments.length = 0;
    dialog.close();
  };
  const dialog = el(
    "dialog",
    { class: "diff-dialog" },
    el("header", {}, el("h2", { text: task.title }), el("span", { class: "muted", text: "Click a line, or press Enter on it, to comment on it." }), el("span", { class: "spacer" }), el("button", { type: "button", class: "text", text: "Close", onclick: close })),
    body,
    el("footer", {}, error, count, send),
  );
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    close();
  });
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();

  function counted() {
    count.textContent = comments.length ? plural(comments.length, "comment") : "";
    send.disabled = !comments.length;
  }
  async function sendComments() {
    send.disabled = true;
    error.textContent = "";
    try {
      const view = await api(`/api/tasks/${task.id}/follow-up`, { method: "POST", body: { text: reviewText(comments) } });
      comments.length = 0;
      dialog.close();
      onSent(view);
      toast("Comments sent to the worker.");
    } catch (failure) {
      error.textContent = failure.message;
      send.disabled = false;
    }
  }
  function comment(row, file, line) {
    if (row.nextElementSibling?.classList.contains("diff-compose")) return row.nextElementSibling.querySelector("textarea").focus();
    const input = el("textarea", { rows: 2, placeholder: "Comment for the worker", "aria-label": "Comment for the worker" });
    const compose = el("div", { class: "diff-compose" }, input);
    // Closed or done, the keyboard is back on its line.
    const back = (node) => {
      const focused = node.contains(document.activeElement);
      node.remove();
      if (focused) row.focus();
    };
    const add = () => {
      const text = input.value.trim();
      if (!text) return;
      const focused = compose.contains(document.activeElement);
      const item = { ...line, file: file.name, body: text };
      comments.push(item);
      const note = el("div", { class: "diff-comment" }, el("span", { text }));
      note.append(
        el("button", {
          type: "button",
          class: "text",
          text: "Remove",
          onclick: () => {
            comments.splice(comments.indexOf(item), 1);
            back(note);
            counted();
          },
        }),
      );
      compose.replaceWith(note);
      if (focused) row.focus();
      counted();
    };
    input.addEventListener("keydown", (event) => {
      if (submits(event) && (event.metaKey || event.ctrlKey)) add();
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        back(compose);
      }
    });
    compose.append(el("div", { class: "buttons" }, el("button", { type: "button", class: "text", text: "Cancel", onclick: () => back(compose) }), el("button", { type: "button", text: "Comment", onclick: add })));
    row.after(compose);
    input.focus();
  }
  function fileLines(file) {
    const lines = el("div", { class: "diff-lines" });
    file.lines.slice(0, MAX_LINES).forEach((line, index) => {
      const sign = { add: "+", del: "−", ctx: "" }[line.kind] ?? "";
      const number = line.kind === "hunk" ? "" : line.kind === "del" ? line.old : line.line;
      const row = el("div", { class: `diff-line ${line.kind}` }, el("span", { class: "num", text: String(number) }), el("span", { class: "sign", text: sign }), el("span", { class: "code", text: line.text }));
      if (line.kind !== "hunk") {
        // Reached with Tab too: Enter or Space comments on it (the dialog says so).
        const what = { add: "Added line", del: "Removed line" }[line.kind] || "Line";
        row.dataset.index = String(index);
        row.tabIndex = 0;
        row.setAttribute("role", "button");
        row.setAttribute("aria-label", `${what} ${number}: ${line.text.trim() || "blank"}`);
      }
      lines.append(row);
    });
    const target = (event) => {
      const row = event.target.closest?.(".diff-line[data-index]");
      return row && lines.contains(row) ? row : null;
    };
    lines.addEventListener("click", (event) => {
      const row = target(event);
      if (row && !window.getSelection()?.toString()) comment(row, file, file.lines[Number(row.dataset.index)]);  // Not when copying.
    });
    lines.addEventListener("keydown", (event) => {
      const row = target(event);
      if (!row || event.target !== row || !(event.key === "Enter" || event.key === " ")) return;
      event.preventDefault();
      comment(row, file, file.lines[Number(row.dataset.index)]);
    });
    if (file.lines.length > MAX_LINES) lines.append(el("p", { class: "muted note", text: `${file.lines.length - MAX_LINES} more lines not shown.` }));
    return lines;
  }
  function fileView(file) {
    const summary = el("summary", {}, el("span", { class: "name", text: file.name }), el("span", { class: "added", text: file.added ? `+${file.added}` : "" }), el("span", { class: "removed", text: file.removed ? `−${file.removed}` : "" }), file.note ? el("span", { class: "muted", text: file.note }) : null);
    const view = el("details", { class: "diff-file" }, summary);
    const fill = () => {
      if (view.open && !view.dataset.filled) {
        view.dataset.filled = "1";
        view.append(fileLines(file));
      }
    };
    view.addEventListener("toggle", fill);
    if (file.lines.length <= OPEN_LINES) {
      view.open = true;
      fill();
    }
    return view;
  }

  api(`/api/tasks/${task.id}/diff`)
    .then((checkouts) => {
      const parts = [];
      for (const checkout of checkouts) {
        const files = parseDiff(checkout.diff);
        // New files too large to show: said, not left out.
        const large = checkout.large || [];
        if (!files.length && !large.length) continue;
        if (checkouts.length > 1) parts.push(el("h3", { text: checkout.branch ? `${checkout.name} · ${checkout.branch}` : checkout.name }));
        parts.push(...files.map(fileView), ...large.map(largeFile));
        if (checkout.truncated) parts.push(el("p", { class: "muted", text: "The diff is too large to show in full." }));
      }
      body.replaceChildren(...(parts.length ? parts : [el("p", { class: "muted", text: "No changes." })]));
    })
    .catch((failure) => body.replaceChildren(el("p", { class: "error", text: failure.message })));
}
