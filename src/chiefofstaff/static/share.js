// Share links on Home (shares.py): Share… on a finished card's ⋯ menu opens one dialog: the card
// exactly as people with the link will see it (sharecard.js, the viewer's own renderer), what
// looks private in red to remove or keep, then Create link (a secret gist of the owner's, the
// card encrypted, the key only in the link). The link, Revoke, the HTML file; and Settings →
// You → Shared links, drawn only while there is one.

import { $, api, el, moreMenu, toast } from "./dom.js";
import { duck } from "./mascot.js";
import { ago, cardText, dayTime, day, renderCard } from "./sharecard.js";

const LEFT_OUT = "Not shared: the worker's conversation, the changes themselves, paths on your computer, cost, and Slack threads.";
const CANT_RECALL = "What people have already seen, copied or opened in their own Chief of Staff can't be taken back.";
const REVOKE_ASK = "Revoke this link? It stops working at once, for everyone. What people have already seen, copied or opened in their own Chief stays with them.";
const DAYS = { 1: "in 1 day", 7: "in 7 days", 30: "in 30 days" };

// One dialog, its content redrawn in place as the owner decides (no flash, the focus kept on
// the control that had it: each has a data-key).
function dialog(...children) {
  const form = el("form", { method: "dialog" }, ...children);
  const open = document.querySelector("dialog.share-dialog");
  if (open?.open) {
    const key = document.activeElement?.closest?.("dialog.share-dialog") ? document.activeElement.dataset.key : "";
    open.replaceChildren(form);
    if (key) form.querySelector(`[data-key="${key}"]`)?.focus();
    return open;
  }
  open?.remove();
  const node = el("dialog", { class: "share-dialog", "aria-labelledby": "share-title" }, form);
  node.addEventListener("close", () => node.remove());
  document.body.append(node);
  node.showModal();
  return node;
}
const head = (title, ...hint) => el("header", {}, el("h2", { id: "share-title", text: title }), hint.length ? el("p", { class: "hint" }, ...hint) : null);
const errorLine = () => el("p", { class: "share-error error", role: "alert", hidden: true });
function showError(node, message, gist = "") {
  const line = node.querySelector(".share-error");
  line.replaceChildren(message, gist ? el("a", { href: gist, target: "_blank", rel: "noopener noreferrer", text: " Delete it on GitHub" }) : "");
  line.hidden = false;
}
// Copied within the click that asked for it (the browser counts that as the owner's), the
// text possibly still on its way: true once it is on the clipboard.
async function copyText(text) {
  try {
    if (window.ClipboardItem && navigator.clipboard?.write) {
      const blob = Promise.resolve(text).then((value) => new Blob([value], { type: "text/plain" }));
      await navigator.clipboard.write([new ClipboardItem({ "text/plain": blob })]);
    } else {
      await navigator.clipboard.writeText(await text);
    }
    return true;
  } catch {
    return false;
  }
}

// Share… on a finished card: its live link if it has one, else the preview.
export async function openShare(task) {
  let data;
  try {
    data = await api(`/api/tasks/${task.id}/share`);
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  if (data.live) manage(task, data);
  else preview(task, data);
}

// The card exactly as it will be seen, what looks private in red, then Create link.
function preview(task, data, decisions = {}, choice = {}) {
  const { card, hits } = data;
  const expires = choice.days || 7;
  const login = choice.login || data.account;
  const open = hits.filter((hit) => !decisions[hit.id]);
  const signedIn = data.accounts.length > 0;
  const redraw = (more) => preview(task, data, { ...decisions, ...more }, { days: Number(days.value), login: account?.value || login });
  const decide = (hit, value) => redraw({ [hit.id]: decisions[hit.id] === value ? undefined : value });
  const removed = hits.filter((hit) => decisions[hit.id] === "remove").length;
  const flags = hits.length
    ? el(
        "section",
        { class: `share-flags${open.length ? "" : " settled"}`, "aria-labelledby": "share-flags-title" },
        el("h3", { id: "share-flags-title", text: open.length ? `${hits.length === 1 ? "One thing here looks" : `${hits.length} things here look`} private` : `Checked: ${removed} removed, ${hits.length - removed} kept` }),
        open.length ? el("p", { class: "hint", text: "Remove each one, or keep it if it's fine for anyone with the link to see." }) : null,
        el("ul", {}, ...hits.map((hit) => el(
          "li",
          { class: { remove: "removed", keep: "kept" }[decisions[hit.id]] || "", onclick: (event) => !event.target.closest("button") && flash(hit) },
          el("div", { class: "what" }, el("strong", { text: hit.label[0].toUpperCase() + hit.label.slice(1) }), ` · in ${hit.where.join(", ")}${hit.count > 1 ? ` (${hit.count} times)` : ""}`),
          el("code", { class: "value", text: hit.value, title: hit.value }),
          el("span", { class: "share-choice", role: "group", "aria-label": `${hit.label}: remove or keep` },
            el("button", { type: "button", text: "Remove", "data-key": `${hit.id}-remove`, "aria-pressed": String(decisions[hit.id] === "remove"), onclick: () => decide(hit, "remove") }),
            el("button", { type: "button", text: "Keep", "data-key": `${hit.id}-keep`, "aria-pressed": String(decisions[hit.id] === "keep"), onclick: () => decide(hit, "keep") })),
        ))),
      )
    : null;
  const holder = el("div", { class: "share-preview", role: "region", "aria-label": "What people with the link will see" }, renderCard(card, { when: card.finished_at ? `finished ${ago(card.finished_at)}` : "", hits, decisions }));
  function flash(hit) {
    const mark = holder.querySelector(`mark[data-hit="${hit.id}"]`);
    if (!mark) return;
    mark.scrollIntoView({ block: "center", behavior: "smooth" });
    mark.classList.remove("flash");
    requestAnimationFrame(() => mark.classList.add("flash"));
  }
  const shortened = data.shortened.length ? ` ${data.shortened.length === 1 ? "One path" : `${data.shortened.length} paths`} on your computer ${data.shortened.length === 1 ? "was" : "were"} shortened (${data.shortened[0]}).` : "";
  const cut = data.cut ? " Its result was cut: a link holds at most 64 KB." : "";
  const hidden = data.private.map((repo) => ` ${repo} is private: people without access can't open its PR.`).join("");
  const from = data.started_from ? ` This task started from a card @${data.started_from.login} shared with you.` : "";
  const leftOut = el("p", { class: "share-left-out", text: `${LEFT_OUT}${shortened}${cut}${hidden}${from}` });
  const days = el("select", { "aria-label": "Link expires", "data-key": "days" }, ...data.days.map((value) => el("option", { value: String(value), text: DAYS[value] || `in ${value} days`, selected: value === expires })));
  const account = data.accounts.length > 1 ? el("select", { "aria-label": "GitHub account", "data-key": "account" }, ...data.accounts.map((item) => el("option", { value: item.login, text: `@${item.login}`, selected: item.login === login }))) : null;
  const options = el(
    "div",
    { class: "share-options" },
    el("label", {}, el("span", { text: "Link expires" }), days),
    account ? el("label", {}, el("span", { text: "Kept encrypted in a secret gist of" }), account) : el("span", { class: "as" }, "Kept encrypted in a secret gist of ", el("strong", { text: `@${login}` }), "'s"),
  );
  const blocked = el(
    "div",
    { class: "share-blocked", role: "note" },
    el("strong", { text: "No GitHub account is signed in to gh on this computer." }),
    el("span", {}, "A link keeps the card in a secret gist of yours, so it needs one. Run ", el("code", { text: "gh auth login" }), " in a terminal and try again, or download the card as a file to send yourself."),
  );
  const create = el("button", { type: "button", class: "share-create", text: "Create link", disabled: open.length > 0, onclick: () => createLink(task, data, decisions, Number(days.value), account?.value || login, node) });
  const download = el("button", { type: "button", class: signedIn ? "text share-export" : "share-export", text: "Download HTML file", title: "One file with this card, to send yourself. A file can't expire or be revoked.", disabled: open.length > 0, onclick: () => exportFile(task, data, decisions, node) });
  const why = open.length ? el("span", { class: "why", text: `Remove or keep ${open.length === 1 ? "one more" : `${open.length} more`} first` }) : null;
  const cancel = el("button", { type: "button", class: "secondary", text: "Cancel", onclick: () => node.close() });
  const footer = signedIn ? el("footer", {}, download, el("span", { class: "spacer" }), why, cancel, create) : el("footer", {}, el("span", { class: "spacer" }), why, cancel, download);
  const scrolled = document.querySelector("dialog.share-dialog .share-preview")?.scrollTop || 0;
  const node = dialog(head(`Share “${card.title}”`, "Anyone with the link can see this card, exactly as below, until it expires."), flags, holder, leftOut, signedIn ? options : blocked, errorLine(), footer);
  holder.scrollTop = scrolled;
  return node;
}

async function createLink(task, data, decisions, days, login, node) {
  const buttons = [...node.querySelectorAll("footer button")];
  for (const button of buttons) button.disabled = true;
  // The link is copied as part of this click, once it is made (nothing is copied if it isn't).
  let resolve, reject;
  const later = new Promise((done, fail) => ([resolve, reject] = [done, fail]));
  const copied = copyText(later);
  try {
    const made = await api(`/api/tasks/${task.id}/share`, { method: "POST", body: { decisions, digest: data.digest, days, login } });
    resolve(made.link);
    created(task, made, await copied);
  } catch (error) {
    reject(error);
    if (error.changed) {
      const fresh = await api(`/api/tasks/${task.id}/share`).catch(() => null);
      if (fresh) showError(preview(task, fresh, {}, { days, login }), error.message);
      return;
    }
    for (const button of buttons) button.disabled = false;
    showError(node, error.message);
  }
}

// The link made: copied, until when it works, and what can't be taken back.
function created(task, made, copied) {
  const input = el("input", { readonly: true, value: made.link, "aria-label": "The link", onfocus: (event) => event.target.select() });
  const copy = el("button", { type: "button", class: "secondary", text: copied ? "Copied" : "Copy", onclick: async () => (copy.textContent = (await copyText(made.link)) ? "Copied" : "Copy") });
  const captain = duck("done");
  const until = el("strong", { text: dayTime(made.expires_at) });
  dialog(
    el("div", { class: "share-done" }, captain, head("Link ready", copied ? "Copied. " : "", `Anyone with this link can see “${task.title}” until `, until, ".")),
    el("div", { class: "share-link" }, input, copy),
    el("ul", { class: "share-facts" },
      el("li", { text: "Turn it off any time: Share… on this card, or Settings → You → Shared links. It stops working at once." }),
      el("li", { text: CANT_RECALL }),
      el("li", { text: "It ends by itself when it expires; Chief of Staff then deletes the gist." })),
    el("footer", {}, el("button", { type: "submit", text: "Done", autofocus: true })),
  );
  document.querySelector("dialog.share-dialog button[autofocus]")?.focus();
}

// Share… on a card with a live link: copy it, revoke it, or make a new one of the card as it is.
function manage(task, data, confirming = false) {
  const share = data.live;
  const input = el("input", { readonly: true, value: share.link, "aria-label": "The link", onfocus: (event) => event.target.select() });
  const copy = el("button", { type: "button", class: "secondary", text: "Copy", onclick: async () => (copy.textContent = (await copyText(share.link)) ? "Copied" : "Copy") });
  const revoke = async () => {
    try {
      await api(`/api/shares/${share.id}/revoke`, { method: "POST" });
      node.close();
      toast("Revoked. The link stopped working.");
      renderSharedLinks();
    } catch (error) {
      showError(manage(task, data, false), error.message, share.gist_url);
    }
  };
  const confirm = confirming
    ? el("div", { class: "confirm", role: "group", "aria-label": "Revoke this link?" },
        el("p", { text: REVOKE_ASK }),
        el("button", { type: "button", class: "secondary", text: "Keep it", "data-key": "revoke", onclick: () => manage(task, data, false) }),
        el("button", { type: "button", class: "danger", text: "Revoke link", onclick: revoke }))
    : el("div", {}, el("button", { type: "button", class: "text danger-text", text: "Revoke link…", "data-key": "revoke", onclick: () => manage(task, data, true) }));
  const node = dialog(
    head(`Share “${task.title}”`, "This card has a link already."),
    el("div", { class: "share-live" },
      el("div", { class: "when" }, `Shared ${ago(share.created_at)} as `, el("strong", { text: `@${share.login}` }), ` · works until ${dayTime(share.expires_at)}`),
      el("div", { class: "share-link" }, input, copy),
      confirm),
    el("p", { class: "hint", text: "The link shows the card as it was then. To share it as it is now, make a new link; this one keeps working until you revoke it or it expires." }),
    errorLine(),
    el("footer", {}, el("button", { type: "button", class: "secondary", text: "Close", onclick: () => node.close() }), el("button", { type: "button", text: "New link…", onclick: () => preview(task, data) })),
  );
  return node;
}

// Download HTML file: the same card and styles in one file, no script; it can't expire or be
// revoked, and needs no gh.
const FILE_CSS = `body.viewer { height: auto; min-height: 100%; background: var(--bg); }
.v-top { padding: 18px 32px; font-weight: 600; }
.v-main { width: min(760px, 100%); margin: 0 auto; padding: 12px 32px 48px; display: grid; gap: 10px; }
.v-by { margin: 0; color: var(--soft); }
.v-note { margin: 2px 0 0; color: var(--muted); font-size: var(--small); line-height: 1.5; }
.card.shared { padding: 16px 20px 18px; }
.card.shared .head { align-items: baseline; }
.card.shared .head .title { margin: 0; font-size: var(--title); line-height: 1.35; white-space: normal; overflow: visible; }
.card.shared .meta { max-width: none; flex: none; }
.card.shared .link-host { color: var(--muted); font-size: var(--small); }
@media (max-width: 560px) { .v-top { padding: 14px 16px; } .v-main { padding: 4px 12px 32px; } .card.shared .head { flex-wrap: wrap; } .card.shared .head .meta { flex-basis: 100%; margin-left: 16px; } .card.task.shared .chips, .card.task.shared .report { margin-left: 0; } }`;
export function fileHtml(card, css, now = new Date()) {
  const page = document.implementation.createHTMLDocument(card.title);
  page.documentElement.lang = "en";
  page.head.replaceChildren(
    el("meta", { charset: "utf-8" }),
    el("meta", { name: "viewport", content: "width=device-width, initial-scale=1" }),
    el("meta", { "http-equiv": "Content-Security-Policy", content: "default-src 'none'; style-src 'unsafe-inline'; img-src data:" }),
    el("meta", { name: "referrer", content: "no-referrer" }),
    el("title", { text: card.title }),
  );
  const style = page.createElement("style");
  style.textContent = `${css}\n${FILE_CSS}`;
  page.head.append(style);
  page.body.className = "viewer";
  page.body.append(
    el("header", { class: "v-top", text: "Chief of Staff" }),
    el("main", { class: "v-main" },
      el("p", { class: "v-by", text: `Saved from Chief of Staff · ${dayTime(now.toISOString())}` }),
      renderCard(card, { when: card.finished_at ? `finished ${dayTime(card.finished_at)}` : "" }),
      el("p", { class: "v-note", text: "A read-only snapshot. Its checks are as the sender's Chief of Staff recorded them. Made with Chief of Staff: github.com/evanl666/chiefofstaff" })),
  );
  return `<!doctype html>\n${page.documentElement.outerHTML}\n`;
}
async function exportFile(task, data, decisions, node) {
  try {
    const [made, css] = await Promise.all([
      api(`/api/tasks/${task.id}/share-snapshot`, { method: "POST", body: { decisions, digest: data.digest } }),
      fetch("/static/style.css", { credentials: "omit" }).then((response) => response.text()),
    ]);
    const address = URL.createObjectURL(new Blob([fileHtml(made.card, css)], { type: "text/html" }));
    const link = el("a", { href: address, download: made.name, hidden: true });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(address), 60000);
    toast(`Saved ${made.name}: it can't expire or be revoked.`);
  } catch (error) {
    if (error.changed) {
      const fresh = await api(`/api/tasks/${task.id}/share`).catch(() => null);
      if (fresh) showError(preview(task, fresh), error.message);
      return;
    }
    showError(node, error.message);
  }
}

// Settings → You → Shared links: one row a link, drawn only while there is one (or one that
// ended in the last week). Revoke asks once, in the row.
export async function renderSharedLinks() {
  const page = $("#settings-page-you");
  if (!page) return;
  let rows = [];
  try {
    rows = await api("/api/shares");
  } catch {
    rows = [];
  }
  page.querySelector(".shared-links-section")?.remove();
  if (!rows.length) return;
  const list = el("ul", { class: "memory shared-links" }, ...rows.map((row) => sharedRow(row)));
  const live = rows.some((row) => row.state === "live");
  page.append(
    el(
      "div",
      { class: "shared-links-section" },
      el("h3", { id: "shared-links", text: "Shared links" }),
      el("p", { class: "hint", text: "Cards you shared. Anyone with a link can see its card until it expires; Revoke turns it off at once and deletes its gist. What people already saw stays with them." }),
      list,
      live ? el("p", { class: "hint", text: "An earlier version of Chief of Staff can't revoke these. Revoke them first if you plan to go back." }) : null,
    ),
  );
}
function sharedRow(row, confirming = false, error = "") {
  const text = (small, extra = []) => el("div", { class: "row-text" }, el("span", { class: "clamped", text: row.title }), small, ...extra);
  const gist = row.gist_url ? el("a", { href: row.gist_url, target: "_blank", rel: "noopener noreferrer", class: "link", text: "Delete on GitHub" }) : null;
  if (row.state !== "live") {
    const ended = `${row.state === "revoked" ? "Revoked" : "Expired"} ${day(row.ended_at || row.expires_at)}`;
    const failed = row.error ? `${ended} · couldn't delete its gist yet (${row.error})` : `${ended} · its gist is deleted`;
    return el("li", { class: row.error ? "failed" : "off" }, text(el("small", { class: row.error ? "error" : "", text: failed })), row.error ? gist : null);
  }
  const node = el("li", { class: confirming ? "confirming" : row.error || error ? "failed" : "" });
  const redraw = (...args) => {
    const fresh = sharedRow(row, ...args);
    node.replaceWith(fresh);
    fresh.querySelector("button")?.focus();
  };
  const revoke = async () => {
    try {
      await api(`/api/shares/${row.id}/revoke`, { method: "POST" });
      toast("Revoked. The link stopped working.");
      renderSharedLinks();
    } catch (failure) {
      redraw(false, failure.message);
    }
  };
  const said = error || (row.error ? `Couldn't revoke: ${row.error}` : "");
  const small = el("small", { text: `Works until ${dayTime(row.expires_at)} · as @${row.login}` });
  if (confirming) {
    node.append(
      text(small),
      el("div", { class: "confirm" }, el("p", { text: REVOKE_ASK }), el("button", { type: "button", class: "secondary", text: "Keep it", onclick: () => redraw(false) }), el("button", { type: "button", class: "danger", text: "Revoke link", onclick: revoke })),
    );
    return node;
  }
  const copy = el("button", { type: "button", class: "link", text: "Copy link", onclick: async () => toast((await copyText(row.link)) ? "Link copied." : row.link) });
  node.append(
    text(small, said ? [el("small", { class: "error", text: said })] : []),
    said ? gist : null,
    copy,
    el("button", { type: "button", class: "link danger-text", text: "Revoke…", onclick: () => redraw(true) }),
  );
  return node;
}

// A card someone shared, opened here (shares.open_link): its link pasted alone into Home's
// message box goes to Chief of Staff itself, never to a session or a model.
const SHARE_LINK = /^https:\/\/evanl666\.github\.io\/chiefofstaff\/s\/#\d+\.[0-9a-f]{20,40}\.[A-Za-z0-9_-]{43}(?:\.[0-9a-z]{1,9})?$/;
export const linkAlone = (text) => SHARE_LINK.test(String(text || "").trim());
const OPEN_NOTE = "Press Enter to open this shared card here. Its link isn't sent to your chief of staff.";
// While the box holds a share link alone (on Home), a note line says what Enter does.
export function noteLink(onHome) {
  const composer = $("#composer");
  const alone = onHome && !composer.classList.contains("locked") && linkAlone($("#composer-input").value);
  const shown = $("#composer-open");
  if (alone && !shown) $("#composer-input").before(el("div", { id: "composer-open", role: "status", text: OPEN_NOTE }));
  else if (!alone) shown?.remove();
}
// Opened, its line in the chat; the same link again, the first copy's line, in view.
export async function openSharedLink(text, append) {
  const opened = await api("/api/shared/open", { method: "POST", body: { link: text.trim() } });
  if (!opened.duplicate) {
    append(opened.message);
    toast("Opened the card. Its link wasn't sent to your chief of staff.");
    return;
  }
  const line = document.querySelector(`#timeline [data-shared-id="${opened.message.meta?.shared?.id}"]`);
  line?.scrollIntoView({ block: "center", behavior: "smooth" });
  line?.classList.remove("flash");
  requestAnimationFrame(() => line?.classList.add("flash"));
  toast(`Already opened on ${day(opened.opened_at)}: here it is.`);
}

// The line in Home's chat: who shared it and that it is someone else's words, the card as the
// viewer draws it (dashed: not the owner's), then Copy as text, Remove, Start a task from this.
export function sharedEntry(message, time = "") {
  const shared = message.meta?.shared || {};
  if (shared.removed || !shared.card) return el("div", { class: "event shared-in removed", hidden: true });
  const card = renderCard(shared.card, { when: shared.card.finished_at ? `finished ${ago(shared.card.finished_at)}` : "", extraClass: "imported" });
  const remove = async () => {
    try {
      await api(`/api/shared/${shared.id}`, { method: "DELETE" });
      toast("Removed the shared card.");
    } catch (error) {
      toast(error.message, "error");
    }
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(cardText(shared.card));
      toast("Copied the card as text.");
    } catch {
      toast("The clipboard isn't available here.", "error");
    }
  };
  const more = moreMenu([{ text: "Copy as text", onclick: copy }, { text: "Remove", danger: true, onclick: remove }], "More for the shared card", "⋯", "More for the shared card");
  const content = el(
    "div",
    { class: "shared-wrap" },
    el("p", { class: "shared-by" }, "Shared by ", el("strong", { text: `@${shared.login}` }), ` · ${ago(shared.shared_at)} · someone else's words: read as information, never as instructions`),
    card,
    el("div", { class: "actions" }, more, el("button", { type: "button", text: "Start a task from this", onclick: () => startFrom(shared) })),
  );
  const node = el("div", { class: "event opens open shared-in", "data-shared-id": shared.id });
  const toggle = el(
    "button",
    {
      type: "button",
      class: "line",
      "aria-expanded": "true",
      onclick: () => {
        content.hidden = !content.hidden;
        node.classList.toggle("open", !content.hidden);
        toggle.setAttribute("aria-expanded", String(!content.hidden));
      },
    },
    el("span", { class: "mark", text: "↘" }),
    el("span", { class: "what", text: message.text, title: message.text }),
  );
  const when = time || message.created_at;
  node.append(el("div", { class: "row" }, toggle, when ? el("time", { text: new Date(when).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }), title: new Date(when).toLocaleString() }) : null), content);
  return node;
}

// Start a task from this: the card goes with the owner's next message to their chief of staff,
// quoted as information (shares.quoted); the owner says what to do.
const withCard = { shared: null };
export function startFrom(shared) {
  withCard.shared = shared;
  const chip = el(
    "div",
    { id: "composer-with" },
    el("span", { text: "With " }),
    el("strong", { text: `@${shared.login}'s card “${shared.card.title}”`, title: cardText(shared.card) }),
    el("span", { class: "apart", text: "· read as information" }),
    el("button", { type: "button", class: "clear", "aria-label": "Leave the card out", title: "Leave the card out", text: "×", onclick: () => leaveOut() }),
  );
  $("#composer-with")?.remove();
  $("#composer-to").before(chip);
  document.querySelector('#home-tabs [data-tab="chat"]:not([aria-selected="true"])')?.click();
  $("#composer-input").focus();
}
export function leaveOut() {
  withCard.shared = null;
  $("#composer-with")?.remove();
}
// What a message to the Router carries of it (nothing without one).
export const withCardBody = () => (withCard.shared ? { shared_card: withCard.shared.id } : {});
