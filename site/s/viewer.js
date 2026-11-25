// The viewer page: reads the link's fragment (which browsers never send), fetches the gist from
// GitHub's API past its cache, decrypts it in this browser and draws the card read-only, with
// Home's own renderer. No cookies, no storage, no analytics: the one request is for the gist.

import { DamagedError, NewerError, expired, open, parseLink } from "./sharecrypto.js";
import { ago, day, el, renderCard } from "./sharecard.js";

const $ = (selector) => document.querySelector(selector);
const state = $("#state");

function show(kind, ...nodes) {
  document.body.dataset.state = kind;
  state.replaceChildren(...nodes);
  // Only a card that opened is "here", or was decrypted.
  const card = kind === "card";
  $("#made-like").hidden = !card;
  $("#decrypted").hidden = !card;
  for (const node of document.querySelectorAll("[data-card-only]")) node.hidden = !card;
}
// The card is not shown: a short title, what happened, and what to do.
function notice(kind, title, ...words) {
  document.title = `${title} · Chief of Staff`;
  show(kind, el("div", { class: `v-notice ${kind}` }, el("h1", { text: title }), ...words.filter(Boolean).map((text) => (typeof text === "string" ? el("p", { text }) : text))));
}

async function fetchGist(gistId) {
  // A fresh answer from GitHub's API (not its 60-second cache): a revoked link stops at once.
  const nonce = crypto.getRandomValues(new Uint32Array(2)).join("");
  return fetch(`https://api.github.com/gists/${gistId}?_=${nonce}`, {
    cache: "no-store",
    credentials: "omit",
    referrerPolicy: "no-referrer",
    headers: { Accept: "application/vnd.github+json" },
  });
}

async function load() {
  // No header can forbid framing on GitHub Pages: a card is never drawn inside another page.
  if (window.top !== window.self) {
    notice("framed", "Open this card in its own tab", "For your safety it isn't shown inside another page.");
    return;
  }
  const link = parseLink(location.hash);
  if (!link) {
    notice("incomplete", "This link is incomplete", "Part of it is missing or changed. Ask whoever sent it to copy the whole link again.");
    return;
  }
  if (link.newer) {
    notice("newer", "This link needs a newer page", "Reload the page to get the latest viewer.");
    return;
  }
  show("opening", el("p", { class: "v-loading", text: "Opening the card…" }));
  let answer;
  try {
    answer = await fetchGist(link.gistId);
  } catch {
    notice("offline", "Couldn't reach GitHub", "The card is stored on GitHub. Check your connection, then try again.", el("button", { type: "button", class: "secondary", text: "Try again", onclick: load }));
    return;
  }
  if (answer.status === 404) {
    if (link.expiresHint && Date.parse(link.expiresHint) <= Date.now()) {
      notice("expired", "This link has expired", `It was shared until ${day(link.expiresHint)}, and its card has been deleted. Ask whoever sent it for a new link if you still need it.`);
    } else {
      notice("revoked", "This link was revoked", "Whoever shared it turned it off, so its card is gone. If it should still work, ask them to send it again.");
    }
    return;
  }
  if ((answer.status === 403 || answer.status === 429) && answer.headers.get("x-ratelimit-remaining") === "0") {
    const reset = Number(answer.headers.get("x-ratelimit-reset")) * 1000;
    const when = reset ? new Date(reset).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) : "in a little while";
    notice("limited", "GitHub needs a short break", `GitHub lets one network open about 60 of these an hour without signing in, and this one has. Try again ${reset ? "at " : ""}${when}.`);
    return;
  }
  if (!answer.ok) {
    notice("offline", "Couldn't reach GitHub", `GitHub answered ${answer.status}. Try again in a moment.`, el("button", { type: "button", class: "secondary", text: "Try again", onclick: load }));
    return;
  }
  let gist;
  let payload;
  try {
    gist = await answer.json();
    const files = Object.values(gist.files || {});
    const file = gist.files?.["share.txt"];
    if (files.length !== 1 || !file || file.truncated || typeof file.content !== "string" || !Number.isInteger(gist.owner?.id) || typeof gist.owner?.login !== "string") throw new DamagedError("not a share");
    payload = await open(file.content, link.key, gist.owner.id);
  } catch (error) {
    if (error instanceof NewerError) notice("newer", "This link needs a newer page", "Reload the page to get the latest viewer.");
    else notice("damaged", "This link is damaged", "The card couldn't be opened: the link or the stored card changed. Ask whoever sent it to copy the link again.");
    return;
  }
  // Who shared it is GitHub's word (the gist's owner), never the payload's.
  const login = gist.owner.login;
  if (expired(payload)) {
    notice("expired", "This link has expired", `@${login} shared it until ${day(payload.expires_at)}. Ask them for a new link if you still need it.`);
    return;
  }
  document.title = `${payload.card.title} · shared by @${login}`;
  show(
    "card",
    el("p", { class: "v-by" }, "Shared by ", el("a", { href: `https://github.com/${encodeURIComponent(login)}`, rel: "noopener noreferrer nofollow", target: "_blank", text: `@${login}` }), ` · ${ago(payload.created_at)}`),
    renderCard(payload.card, { when: payload.card.finished_at ? `finished ${ago(payload.card.finished_at)}` : "" }),
    el("p", { class: "v-note", text: `A read-only snapshot. Its checks are as @${login}'s Chief of Staff recorded them, not checked again here. The link works until ${day(payload.expires_at)}.` }),
  );
  const report = $("#report-link");
  report.href = `https://github.com/contact/report-abuse?report=${encodeURIComponent(login)}`;
  report.hidden = false;
}

async function copy(text, button, done) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    return false;
  }
  button.textContent = done;
  setTimeout(() => (button.textContent = button.dataset.label), 2000);
  return true;
}

function bind() {
  const install = $("#copy-install");
  install.dataset.label = install.textContent;
  install.addEventListener("click", () => copy("uvx chiefofstaff", install, "Copied"));
  // Open in my Chief: the link copied, to paste into Home's message box (Chief of Staff opens
  // it itself; no page reaches a local Chief of Staff, and its AI never gets the link).
  const openIn = $("#open-in-chief");
  openIn.dataset.label = openIn.textContent;
  openIn.addEventListener("click", async () => {
    const copied = await copy(location.href, openIn, "Link copied");
    $("#open-help").hidden = false;
    $("#open-help-manual").hidden = copied;
  });
  window.addEventListener("hashchange", load);
}

bind();
load();
