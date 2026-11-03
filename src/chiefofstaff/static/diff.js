// A task's changes as a diff: read them, comment on lines, send the comments to its worker.

import { api, el, submits, toast } from "./dom.js";
import { plural } from "./text.js";

const OPEN_LINES = 400; // Larger files start folded.
const MAX_LINES = 5000; // Shown per file.

export function parseDiff(text) {
  const files = [];
  let file = null;
  let oldLine = 0;
  let newLine = 0;
  for (const line of text.split("\n")) {
    if (line.startsWith("diff --git ")) {
      const names = /^diff --git a\/(.*) b\/(.*)$/.exec(line);
      file = { name: names ? names[2] : line.slice(11), lines: [], added: 0, removed: 0, note: "" };
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
      if (line.startsWith("+++ b/")) file.name = line.slice(6);
      else if (line.startsWith("Binary files")) file.note = "Binary file changed";
      else if (line.startsWith("deleted file")) file.note = "Deleted";
      else if (line.startsWith("rename from ")) file.note = `Renamed from ${line.slice(12)}`;
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
    el("header", {}, el("h2", { text: task.title }), el("span", { class: "muted", text: "Click a line to comment on it." }), el("span", { class: "spacer" }), el("button", { type: "button", class: "text", text: "Close", onclick: close })),
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
    const input = el("textarea", { rows: 2, placeholder: "Comment for the worker" });
    const compose = el("div", { class: "diff-compose" }, input);
    const add = () => {
      const text = input.value.trim();
      if (!text) return;
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
            note.remove();
            counted();
          },
        }),
      );
      compose.replaceWith(note);
      counted();
    };
    input.addEventListener("keydown", (event) => {
      if (submits(event) && (event.metaKey || event.ctrlKey)) add();
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        compose.remove();
      }
    });
    compose.append(el("div", { class: "buttons" }, el("button", { type: "button", class: "text", text: "Cancel", onclick: () => compose.remove() }), el("button", { type: "button", text: "Comment", onclick: add })));
    row.after(compose);
    input.focus();
  }
  function fileLines(file) {
    const lines = el("div", { class: "diff-lines" });
    for (const line of file.lines.slice(0, MAX_LINES)) {
      const sign = { add: "+", del: "−", ctx: "" }[line.kind] ?? "";
      const number = line.kind === "hunk" ? "" : line.kind === "del" ? line.old : line.line;
      const row = el("div", { class: `diff-line ${line.kind}` }, el("span", { class: "num", text: String(number) }), el("span", { class: "sign", text: sign }), el("span", { class: "code", text: line.text }));
      if (line.kind !== "hunk") {
        row.addEventListener("click", () => {
          if (!window.getSelection()?.toString()) comment(row, file, line);  // Not when copying.
        });
      }
      lines.append(row);
    }
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
        if (!files.length) continue;
        if (checkouts.length > 1) parts.push(el("h3", { text: checkout.branch ? `${checkout.name} · ${checkout.branch}` : checkout.name }));
        parts.push(...files.map(fileView));
        if (checkout.truncated) parts.push(el("p", { class: "muted", text: "The diff is too large to show in full." }));
      }
      body.replaceChildren(...(parts.length ? parts : [el("p", { class: "muted", text: "No changes." })]));
    })
    .catch((failure) => body.replaceChildren(el("p", { class: "error", text: failure.message })));
}
