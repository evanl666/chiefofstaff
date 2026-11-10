// The Storage dialog: what Chief of Staff's workers take on disk, and cleaning it up safely.
// Its report is the server's own (GET /api/storage, from what was measured in the background):
// opening it measures nothing. While it is open, changes come as "storage" events.

import { $, api, el, toast, withKey } from "./dom.js";
import { clock, lines, plural, shortPath, since } from "./text.js";

const GB = 1e9;
let report = null;
let events = null;
// Settings as this dialog showed them: a change made elsewhere meanwhile is not overwritten.
let shown = null;
// Workers whose files are listed (Review files), by session id: their listing.
const reviews = new Map();

export function sizeText(bytes) {
  if (bytes === null || bytes === undefined) return "not measured";
  if (bytes < 1e6) return `${Math.round(Math.max(bytes, 0) / 1000)} KB`;
  if (bytes < GB) return `${Math.round(bytes / 1e6)} MB`;
  return bytes < 10 * GB ? `${(bytes / GB).toFixed(1)} GB` : `${Math.round(bytes / GB)} GB`;
}
const LEVEL_TEXT = { warning: "Low", critical: "Nearly full" };
const STATE_TEXT = { running: "Working", waiting: "Waiting", closed: "Closed" };

export function bindStorage() {
  $("#storage-button").addEventListener("click", openStorage);
  $("#storage-dialog").addEventListener("close", () => {
    events?.close();
    events = null;
  });
  $("#storage-close").addEventListener("click", () => $("#storage-dialog").close());
  $("#storage-measure").addEventListener("click", () => act("/api/storage/measure"));
  $("#storage-uv-measure").addEventListener("click", () => act("/api/storage/uv-measure"));
  $("#storage-settings").addEventListener("submit", saveSettings);
  refreshStorageLink();
  setInterval(refreshStorageLink, 60000);
}
// The footer button says when the disk is low (until there is room again).
export async function refreshStorageLink() {
  try {
    showLevel(await api("/api/storage"));
  } catch {
    // The page shows the server is away elsewhere.
  }
}
function showLevel(data) {
  const level = data.disk?.level || "normal";
  const button = $("#storage-button");
  button.classList.toggle("warning", level === "warning");
  button.classList.toggle("critical", level === "critical");
  const free = data.disk?.free;
  $("#storage-free").textContent = level !== "normal" && free !== null && free !== undefined ? `${sizeText(free)} free` : "";
  button.title = level === "normal" ? "Storage" : `Storage: disk ${LEVEL_TEXT[level].toLowerCase()}, ${sizeText(free)} free`;
}
async function openStorage() {
  shown = null;
  reviews.clear();
  $("#storage-dialog").showModal();
  if (!events) {
    events = new EventSource(withKey("/api/events"));
    events.onmessage = (event) => {
      const { type, data } = JSON.parse(event.data);
      if (type === "storage") render(data);
    };
  }
  try {
    render(await api("/api/storage"));
  } catch (error) {
    toast(error.message, "error");
  }
}
async function act(path, body, okText = "") {
  try {
    const data = await api(path, { method: "POST", body });
    if (data && data.disk) render(data);
    if (okText) toast(okText);
    return data;
  } catch (error) {
    toast(error.message, "error");
    return null;
  }
}

function render(data) {
  report = data;
  showLevel(data);
  if (!$("#storage-dialog").open) return;
  renderSummary(data);
  renderWorkers(data);
  renderOthers(data);
  renderUv(data.uv);
  renderHistory(data.history || []);
  if (!shown) fillSettings(data.settings);
}
function renderSummary(data) {
  const disk = data.disk || {};
  const level = disk.level || "normal";
  const working = data.working === "cleaning" ? "Cleaning…" : data.working || data.measuring_all ? "Measuring…" : "";
  $("#storage-measure").disabled = Boolean(working);
  const parts = [
    el(
      "div",
      { class: `storage-disk ${level}` },
      el("strong", { text: disk.free === null || disk.free === undefined ? "Disk not read yet" : `${sizeText(disk.free)} free` }),
      disk.total ? el("span", { text: `of ${sizeText(disk.total)} · ${disk.used_percent}% used` }) : null,
      level !== "normal" ? el("span", { class: "storage-level", text: LEVEL_TEXT[level] }) : null,
    ),
    level === "critical" ? el("p", { class: "hint", text: "New worker turns wait until there is room; those running go on." }) : null,
    el("p", { class: "hint", text: `Chief of Staff's workspaces: ${sizeText(data.total)} measured in ${shortPath(data.root)}.` }),
    data.recoverable ? el("p", { class: "storage-recoverable", text: `About ${sizeText(data.recoverable)} can be cleaned safely: build output, dependencies and caches that a build or an install makes again (only files git ignores).` }) : null,
    working ? el("p", { class: "hint", text: working }) : null,
  ];
  $("#storage-summary").replaceChildren(...parts.filter(Boolean));
}

function renderWorkers(data, force = false) {
  const list = $("#storage-workers");
  // Not while the owner types "delete" there: it would lose what they typed.
  const typing = document.activeElement?.matches?.(".storage-delete input") && list.contains(document.activeElement);
  if (typing && !force) return;
  if (!data.workers.length) {
    list.replaceChildren(el("p", { class: "hint", text: "No worker folders measured yet." }));
    return;
  }
  list.replaceChildren(...data.workers.map(workerRow));
}
function stateText(worker) {
  if (worker.state === "idle") return worker.idle_days ? `Idle ${worker.idle_days}d` : "Idle";
  return STATE_TEXT[worker.state] || worker.state;
}
function workerRow(worker) {
  const busy = worker.cleaning || worker.state === "running";
  const keep = el("input", {
    type: "checkbox",
    checked: worker.keep,
    onchange: (event) => act("/api/storage/keep", { session_id: worker.session_id, keep: event.target.checked }),
  });
  const row = el(
    "div",
    { class: "storage-worker", "data-session": worker.session_id },
    el(
      "div",
      { class: "storage-head" },
      el("span", { class: "storage-title", text: worker.title }),
      el("span", { class: `storage-state ${worker.state}`, text: stateText(worker) }),
      el("span", { class: "storage-size", text: sizeText(worker.size), title: worker.measured_at ? `Measured ${clock(worker.measured_at)}` : "Not measured yet" }),
    ),
    el(
      "div",
      { class: "storage-meta" },
      worker.regenerable ? el("span", { text: `${sizeText(worker.regenerable)} build output` }) : null,
      worker.measured_at ? el("span", { text: `measured ${since(worker.measured_at)}` }) : null,
      worker.error ? el("span", { class: "bad", text: `Measuring ${worker.error}` }) : null,
    ),
    worker.kept.length ? el("div", { class: "storage-kept" }, el("span", { text: "Kept: " }), worker.kept.join("; ")) : null,
    worker.note ? el("div", { class: "storage-note", text: worker.note }) : null,
    el(
      "div",
      { class: "storage-actions" },
      el("button", {
        type: "button",
        class: "secondary",
        text: worker.cleaning ? "Cleaning…" : "Clean build artifacts",
        disabled: busy || !worker.regenerable,
        title: worker.state === "running" ? "It is working: try once it is idle" : "Deletes only what git ignores and a build makes again",
        onclick: () => act("/api/storage/clean", { session_id: worker.session_id }),
      }),
      el("button", { type: "button", class: "secondary", text: reviews.has(worker.session_id) ? "Hide files" : "Review files", onclick: () => toggleReview(worker) }),
      el("label", { class: "check", title: "Automatic cleanup leaves its build output alone" }, keep, el("span", { text: "Keep its files" })),
    ),
  );
  if (reviews.has(worker.session_id)) row.append(reviewPanel(worker, reviews.get(worker.session_id)));
  return row;
}
async function toggleReview(worker) {
  if (reviews.has(worker.session_id)) reviews.delete(worker.session_id);
  else {
    try {
      reviews.set(worker.session_id, await api(`/api/storage/files?session_id=${encodeURIComponent(worker.session_id)}`));
    } catch (error) {
      toast(error.message, "error");
      return;
    }
  }
  if (report) renderWorkers(report, true);
}
const itemText = (item) => `${item.name}${item.size === null || item.size === undefined ? "" : ` · ${sizeText(item.size)}`}`;
function reviewPanel(worker, files) {
  if (!files.trees.length) return el("div", { class: "storage-review" }, el("p", { class: "hint", text: "No worktree of its own on disk." }));
  return el(
    "div",
    { class: "storage-review" },
    ...files.trees.map((tree) => {
      const lost = [...tree.kept, ...tree.changes];
      return el(
        "section",
        {},
        el("div", { class: "storage-tree" }, el("code", { text: shortPath(tree.path) }), tree.branch ? el("span", { text: tree.branch }) : null),
        tree.unlisted ? el("p", { class: "bad", text: "Its ignored files could not be listed." }) : null,
        el("h4", { text: tree.kept.length ? "Ignored files no build makes again (kept)" : "No other ignored files" }),
        tree.kept.length ? el("ul", {}, ...tree.kept.map((item) => el("li", { text: itemText(item) }))) : null,
        el("h4", { class: tree.changes_unread ? "bad" : "", text: tree.changes_unread ? "Uncommitted changes could not be read" : tree.changes.length ? "Uncommitted changes" : "No uncommitted changes" }),
        tree.changes.length ? el("ul", {}, ...tree.changes.map((item) => el("li", { text: itemText(item) }))) : null,
        tree.build_count ? el("p", { class: "hint", text: `Build output (cleaned by Clean build artifacts): ${plural(tree.build_count, "folder or file")}, like ${tree.build.slice(0, 4).join(", ")}.` }) : null,
        files.closed && tree.worktree && (tree.unlisted || tree.changes_unread) ? el("p", { class: "storage-unread", text: `git could not read all that is in it, so Chief of Staff does not delete it. Look at ${shortPath(tree.path)} yourself.` }) : null,
        files.closed && tree.worktree && !tree.unlisted && !tree.changes_unread ? deleteForm(worker, tree, lost) : null,
      );
    }),
  );
}
// A closed worker's worktree goes only once the owner typed "delete": what is listed is lost.
function deleteForm(worker, tree, lost) {
  const input = el("input", { type: "text", placeholder: "delete", "aria-label": "Type delete to confirm", autocomplete: "off" });
  const button = el("button", {
    type: "button",
    class: "danger",
    text: "Delete worktree",
    disabled: true,
    onclick: async () => {
      if (input.value.trim().toLowerCase() !== "delete") return;
      if (await act("/api/storage/delete-worktree", { session_id: worker.session_id, path: tree.path, confirm: true, seen: tree.digest }, "Worktree deleted; its branch stays.")) {
        reviews.delete(worker.session_id);
        if (report) renderWorkers(report, true);
      }
    },
  });
  input.addEventListener("input", () => {
    button.disabled = input.value.trim().toLowerCase() !== "delete";
  });
  return el(
    "div",
    { class: "storage-delete" },
    el("p", {
      text: lost.length
        ? `Deleting it loses the ${plural(lost.length, "item")} listed above. Its commits stay on branch ${tree.branch || "(none)"}: reopening the worker brings the worktree back from it.`
        : `Nothing above is lost: its commits stay on branch ${tree.branch || "(none)"}, and reopening the worker brings the worktree back.`,
    }),
    el("div", { class: "row" }, input, button),
  );
}

function renderOthers(data) {
  const others = data.others || [];
  $("#storage-others").hidden = !others.length;
  $("#storage-others-list").replaceChildren(...others.map((other) => el("li", {}, el("code", { text: shortPath(other.path) }), ` · ${sizeText(other.size)}`)));
  const stuck = data.stuck || [];
  $("#storage-stuck").hidden = !stuck.length;
  $("#storage-stuck-list").replaceChildren(...stuck.map((item) => el("li", {}, el("code", { text: shortPath(item.path) }), ` · ${sizeText(item.size)} · since ${clock(item.at)}`)));
}
function renderUv(uv) {
  $("#storage-uv-path").textContent = shortPath(uv.path);
  $("#storage-uv-size").textContent = uv.measuring ? "Measuring… (it can take minutes)" : uv.size !== null && uv.size !== undefined ? `${sizeText(uv.size)}, measured ${since(uv.at)}` : uv.error || "Not measured";
  $("#storage-uv-measure").disabled = Boolean(uv.measuring);
}
function renderHistory(history) {
  $("#storage-history").replaceChildren(
    ...(history.length
      ? history.map((entry) =>
          el(
            "li",
            {},
            el("span", { class: "when", text: clock(entry.at) }),
            el("span", { text: `${entry.what} · “${entry.title}” · ${sizeText(entry.freed)} freed${entry.by === "automatic" ? " (automatic)" : ""}`, title: entry.items.join("\n") }),
          ),
        )
      : [el("li", { class: "hint", text: "No cleanups yet." })]),
  );
}

function fillSettings(settings) {
  const form = $("#storage-settings");
  shown = { ...settings };
  form.auto_cleanup.checked = settings.auto_cleanup;
  form.cleanup_idle_days.value = settings.cleanup_idle_days;
  form.regenerable_patterns.value = settings.regenerable_patterns.join("\n");
  form.never_clean_patterns.value = settings.never_clean_patterns.join("\n");
  for (const name of ["disk_warning_gb", "disk_warning_percent", "disk_critical_gb", "disk_critical_percent"]) form[name].value = settings[name];
}
async function saveSettings(event) {
  event.preventDefault();
  const form = $("#storage-settings");
  const body = {
    auto_cleanup: form.auto_cleanup.checked,
    cleanup_idle_days: Number(form.cleanup_idle_days.value || 0),
    regenerable_patterns: lines(form.regenerable_patterns.value),
    never_clean_patterns: lines(form.never_clean_patterns.value),
    ...Object.fromEntries(["disk_warning_gb", "disk_warning_percent", "disk_critical_gb", "disk_critical_percent"].map((name) => [name, Number(form[name].value)])),
    base: shown,
  };
  try {
    await api("/api/settings", { method: "POST", body });
    shown = null;
    render(await api("/api/storage"));
    toast("Storage settings saved");
  } catch (error) {
    toast(error.message, "error");
  }
}
