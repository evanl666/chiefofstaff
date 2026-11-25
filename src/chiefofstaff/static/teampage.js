// A team's page (team-page-v4.md, phase 1 "Run the team"): the place where the owner runs a
// request several workers carry, with what no single worker's chat gives. The brief (where it
// stands, the one thing that moves it, answered or reviewed right there, what it unblocks);
// Since you looked; the plan (the parts in the order work moves, on a rail, each with its
// state, time, output and one action); So far (what the team made and decided, what to watch
// out for; Home's side panel when wide); Tell the team (the composer, addressed to the parts
// it concerns, who gets it always in sight); Delivered and Copy summary when done; what the
// parts told each other, folded. Everything is drawn from what Chief of Staff keeps (no model
// call); every send is an owner's click through the app's API. Motion only for events, under
// a minute old, never on a reload, none with reduced motion (teamstate.js has the words).

import { $, api, el, moreMenu, once, toast } from "./dom.js";
import { clock, shorten } from "./text.js";
import { applyDraft, byId, closedWorkers, HOME, loadState, ownerQuestions, pendingDrafts, state } from "./state.js";
import { select, setComposer, setLayout, showDraft } from "./session.js";
import { renderSidebar } from "./sidebar.js";
import { closeMentions, setRecipient } from "./mention.js";
import { markOf } from "./agentpick.js";
import { needsYou } from "./inbox.js";
import { deliverablesOf } from "./artifacts.js";
import { duck, setMood } from "./mascot.js";
import * as ts from "./teamstate.js";
import { isTeamView, partAvatar, postSeen, stateDot, TEAM, teamById, teamContext, watchLook } from "./team.js";

const workerOf = (id) => byId(id) || closedWorkers().find((item) => item.id === id);
const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const wide = () => window.matchMedia("(min-width: 1100px)").matches;
// Motion only for what is new and under a minute old, in sight.
const moving = () => !reduced() && document.visibilityState === "visible";
const recent = (at) => !at || Date.now() - Date.parse(at) < 60000;
const newKey = () => Math.random().toString(36).slice(2, 12);
// Appended, leaving out what is not there (null, false, "").
const put = (node, ...kids) => node.append(...kids.flat().filter((kid) => kid !== null && kid !== undefined && kid !== false && kid !== ""));
const TOLD_HOURS = 12;

let page = null;

export async function showTeam(teamId) {
  closeMentions();
  setRecipient(null);
  state.selected = TEAM + teamId;
  setLayout(false);
  renderSidebar();
  $("#back-home").hidden = false;
  $("#session-menu").hidden = true;
  $("#worker-tabs").hidden = true;
  const actions = $("#session-actions");
  delete actions.dataset.key;
  actions.replaceChildren();
  watchRails();
  page = { id: teamId, data: null, items: [], next: "", loadingMore: false, open: "", review: "", answering: "", changes: false, to: null, toOpen: false, prev: null, since: null, sendKey: newKey(), sending: false, asked: false };
  const composer = $("#composer");
  composer.hidden = false;
  composer.classList.add("tp-composer");
  $("#composer-to").hidden = true;
  setComposer({ placeholder: "Tell the team…", attach: false });
  showDraft(TEAM + teamId);
  $("#session-title").textContent = teamById(teamId)?.goal || "Team";
  $("#session-meta").textContent = "";
  $("#timeline").replaceChildren(el("div", { class: "line note", text: "Loading…" }));
  await refreshTeam();
}

// Leaving a team's page (session.js setLayout): Home's panel and the composer as they were.
// Home's panel keeps all it holds (Needs you, Teams, Running, Monitoring, Recently finished,
// Artifacts) while a team's page shows So far in its place: out of sight, then as it was.
function homePanel(panel) {
  if (!panel.classList.contains("tp-panel")) return;
  panel.classList.remove("tp-panel");
  panel.setAttribute("aria-label", "Needs you and tasks");
  panel.querySelector(":scope > .tp-sofar-panel")?.remove();
}
export function leaveTeamPage() {
  homePanel($("#panel"));
  const composer = $("#composer");
  if (composer.classList.contains("tp-composer")) {
    composer.classList.remove("tp-composer");
    composer.querySelector(".tp-to-menu")?.remove();
    const to = $("#composer-to");
    to.replaceChildren();
    to.hidden = true;
  }
}

// The members' tasks Home's state does not hold (finished long ago): their views come with the
// page.
function needed(view) {
  const members = view.data?.team.members || teamById(view.id)?.members || [];
  const listed = new Set((state.snapshot?.tasks || []).map((task) => task.id));
  return members.filter((member) => !member.moved_out && !listed.has(member.task)).map((member) => member.task);
}
export async function refreshTeam() {
  const view = page;
  if (!view || state.selected !== TEAM + view.id) return;
  let data;
  try {
    const need = needed(view);
    data = await api(`/api/teams/${encodeURIComponent(view.id)}${need.length ? `?need=${need.map(encodeURIComponent).join(",")}` : ""}`);
    // A team not in the state (past): its parts known now, their views asked for once.
    if (!view.asked && !need.length && needed({ ...view, data }).length) {
      view.asked = true;
      data = await api(`/api/teams/${encodeURIComponent(view.id)}?need=${needed({ ...view, data }).map(encodeURIComponent).join(",")}`);
    }
  } catch (error) {
    if (state.selected === TEAM + view.id) $("#timeline").replaceChildren(el("div", { class: "line error", text: error.message }));
    return;
  }
  if (page !== view || state.selected !== TEAM + view.id) return;
  // Its id when it grew (a bridge made two groups one).
  if (data.team.id !== view.id) {
    view.id = data.team.id;
    state.selected = TEAM + view.id;
  }
  // Since you looked: from the look before this visit (another tab's, too), kept while here.
  if (view.since === null) view.since = data.team.looked || "";
  view.data = data;
  view.items = data.items;
  view.next = data.next;
  renderTeam();
  considerLook();
}
async function earlier() {
  const view = page;
  if (!view?.next || view.loadingMore) return;
  view.loadingMore = true;
  try {
    const data = await api(`/api/teams/${encodeURIComponent(view.id)}?before=${encodeURIComponent(view.next)}`);
    if (page !== view) return;
    view.items = [...view.items, ...data.items];
    view.next = data.next;
    renderTeam();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    view.loadingMore = false;
  }
}

// The owner's look (Since you looked): after 2 s with the page visible in a focused window,
// and again when something new came while they look; kept on the server for every tab. Back
// after being away, it says what changed since they last had it in sight.
let lookTimer = 0;
let lookedAt = "";
function considerLook() {
  clearTimeout(lookTimer);
  const view = page;
  if (!view?.data || !isTeamView() || document.visibilityState !== "visible" || !document.hasFocus()) return;
  lookTimer = setTimeout(async () => {
    if (page !== view || !isTeamView()) return;
    const newest = newestAt(view);
    if (lookedAt && newest && newest <= lookedAt && view.lookedId === view.id) return;
    try {
      const { looked } = await api(`/api/teams/${encodeURIComponent(view.id)}/looked`, { method: "POST" });
      lookedAt = looked;
      view.lookedId = view.id;
      const team = teamById(view.id);
      if (team) team.looked = looked;
    } catch {
      // Another time: nothing depends on it now.
    }
  }, 2000);
}
function newestAt(view) {
  const rows = view.data?.tasks || [];
  return [...rows.map((row) => row.updated_at), ...(view.items || []).map((item) => item.answered_at || item.at)].filter(Boolean).sort().pop() || "";
}
document.addEventListener("visibilitychange", () => {
  if (!page || !isTeamView()) return;
  if (document.visibilityState === "visible") {
    // Back: what changed since they last had it in sight.
    page.since = lookedAt || page.since;
    page.prev = null;
    refreshTeam();
  } else clearTimeout(lookTimer);
});
window.addEventListener("focus", considerLook);
window.addEventListener("blur", () => clearTimeout(lookTimer));
// The header's time ticks (a minute at a time).
setInterval(() => {
  if (page?.data && isTeamView()) header(page.model);
}, 30000);
window.matchMedia("(min-width: 1100px)").addEventListener("change", () => {
  if (page?.data && isTeamView()) renderTeam();
});

// ---------- the model: Home's own data, the page's, and teamstate.js's words ----------

function modelOf(view) {
  const data = view.data;
  const team = data.team;
  const items = needsYou();
  const ctx = teamContext(items, []);
  const views = new Map((data.views || []).map((task) => [task.id, task]));
  for (const row of data.tasks) {
    const known = ctx.tasks.get(row.id) || views.get(row.id);
    ctx.tasks.set(row.id, { ...row, ...(known || {}), started_at: row.started_at, lead: row.lead, rest: row.rest });
  }
  const st = ts.teamState(team, ctx);
  const brief = ts.briefOf(team, st, ctx.now);
  const made = madeOf(st, ctx);
  const decided = data.decided || [];
  const watch = st.done ? [] : ts.watchOf(team, st, decided);
  const since = st.done ? [] : ts.sinceOf(team, st, { looked: view.since, decided, made, brief });
  // New since the owner last looked (never once it is done: Delivered is all of it).
  const unseen = (at) => !st.done && Boolean(view.since) && Boolean(at) && at > view.since;
  return { team, st, ctx, items, brief, made, decided, watch, since, unseen, stages: ts.stagesOf(team, st) };
}
// What the parts made (PRs, docs, flows), each once, in the plan's order: Home's own list.
function madeOf(st, ctx) {
  const found = [];
  const seen = new Set();
  for (const part of st.parts) {
    if (!part.row || part.moved) continue;
    for (const item of deliverablesOf(part.row, ctx.phases.get(part.task))) {
      if (seen.has(item.key)) continue;
      seen.add(item.key);
      const number = /\/pull\/(\d+)/.exec(item.url || "")?.[1];
      found.push({ ...item, part: part.id, short: number ? `PR #${number}` : item.kind, line: `m:${item.key}` });
    }
  }
  return found;
}

// ---------- drawing ----------

const ducks = new Map();
function captain(mood) {
  if (!ducks.has(mood)) ducks.set(mood, duck(mood));
  const node = ducks.get(mood);
  setMood(node, mood);
  return node;
}
function header(model) {
  if (!model) return;
  const { team, st } = model;
  $("#session-title").textContent = team.goal;
  $("#session-title").title = "";
  const meta = $("#session-meta");
  const words = ts.headerWords(team, st, clock);
  const key = `${st.done}|${stateDot(st)}|${words}`;
  if (meta.dataset.key === key) return;
  meta.dataset.key = key;
  meta.replaceChildren(el("span", { class: "tp-meta" }, st.done ? null : el("span", { class: `dot ${stateDot(st)}`, "aria-hidden": "true" }), words));
  meta.title = st.done ? "" : `Since your request at ${clock(team.request.at)}`;
}
function teamMenu(model) {
  const { team, st } = model;
  const actions = $("#session-actions");
  const menuKey = `team:${team.id}:${st.done}`;
  if (actions.dataset.key === menuKey) return;
  actions.dataset.key = menuKey;
  const copy = { text: "Copy summary", onclick: () => copySummary() };
  const link = { text: "Copy link", onclick: () => navigator.clipboard?.writeText(`${location.origin}${location.pathname}#${TEAM}${team.id}`).then(() => toast("Link copied.")) };
  const dismiss = st.done ? { text: "Dismiss", onclick: () => postSeen({ team: team.id, key: st.key }, true).then(() => select(HOME)) } : null;
  actions.replaceChildren(moreMenu([copy, link, dismiss], "Copy summary, copy link", "⋯", "Team actions"));
}

export function renderTeam() {
  const view = page;
  if (!view?.data) return;
  const model = modelOf(view);
  view.model = model;
  header(model);
  teamMenu(model);
  const { st } = model;
  const scroller = $("#scroller");
  const top = scroller.scrollTop;
  const root = el("div", { class: "team-page" });
  root.append(el("p", { class: "tv2-why" }, el("span", { class: "why-label", text: `For your request at ${clock(model.team.request.at)}:` }), el("button", { type: "button", class: "quote", text: `“${model.team.request.text}”`, title: "Show it in your chat", onclick: () => select(HOME) })));
  const brief = briefCard(model, view);
  root.append(brief);
  const since = sinceLine(model, view);
  if (since) root.append(since);
  if (st.done && !wide()) {
    // Done, narrow: what was delivered first; how it went, folded.
    root.append(soFarCard(model));
    const took = st.endedAt ? ts.duration(ts.minutesSince(model.team.request.at, Date.parse(st.endedAt))) : "";
    const fold = el("details", { class: "tp-talk tp-plan-fold", open: view.planOpen || undefined }, el("summary", {}, "How it went", el("span", { class: "count", text: ` · ${st.total} parts${took ? `, ${took}` : ""}` })), planCard(model, view));
    fold.addEventListener("toggle", () => {
      view.planOpen = fold.open;
      drawRails();
    });
    root.append(fold);
  } else {
    root.append(planCard(model, view));
    const told = toldLine(model, view);
    if (told) root.append(told);
    if (!wide()) root.append(soFarCard(model));
  }
  root.append(talkFold(model, view));
  motion(model, view, root);
  root.style.setProperty("--sync", `-${Date.now() % 2400}ms`);
  view.opening = "";
  $("#timeline").replaceChildren(root);
  scroller.scrollTop = top;
  panel(model);
  paint(view, $("#panel > .tp-sofar-panel"));
  composer(model, view);
  drawRails();
  travel(model, view);
  view.prev = snapshotOf(model);
  if (st.done) watchLook(brief, model.team, st);
}

// 1 · The brief: where the request stands, and the one thing that moves it.
function bar(model) {
  const { st, team } = model;
  const took = st.done && st.endedAt ? `took ${ts.duration(ts.minutesSince(team.request.at, Date.parse(st.endedAt)))}` : `${ts.duration(ts.minutesSince(team.request.at, Date.now()))} since your request at ${clock(team.request.at)}`;
  return [
    el("div", { class: "tp-bar", "aria-hidden": "true" }, ...st.segments.map((segment, index) => el("i", { class: segment, "data-part": st.parts[index].id }))),
    el("div", { class: "tp-bar-words" }, el("span", { text: `${st.finished} of ${st.total} parts done` }), el("span", { text: took })),
  ];
}
// The decision card or question a part waits on the owner for (Home's own), when there is one.
function askOf(part) {
  const decision = pendingDrafts().find((draft) => draft.kind === "decision" && (draft.target?.askers || []).some((asker) => asker.task_id === part.task || (!asker.task_id && asker.session_id === part.session)));
  if (decision) return { kind: "decision", draft: decision, text: decision.target.question, why: decision.target.why || "", options: decision.target.options || [], recommendation: decision.target.recommendation || "" };
  const question = ownerQuestions().find((item) => item.task_id === part.task) || (part.row?.status === "waiting" && !part.row?.stopped ? part.row?.question : null) || ownerQuestions().find((item) => !item.task_id && item.session_id === part.session);
  if (question) return { kind: "question", question, text: question.text || "", why: question.why || "", options: (question.options || question.choices || []).map((option) => ({ choice: String(option?.choice ?? option?.text ?? option ?? "") })).filter((option) => option.choice), recommendation: "" };
  return null;
}
const firstSentence = (text = "") => shorten(String(text).replace(/\s+/g, " ").trim(), 160);
function briefCard(model, view) {
  const { brief, st, team } = model;
  const node = el("section", { class: "card tp-brief", id: "tp-brief", "aria-live": "polite" });
  const say = el("p", { class: "say" });
  const then = el("p", { class: "then" });
  const acts = el("div", { class: "acts" });
  if (brief.kind === "done") {
    node.classList.add("done");
    node.setAttribute("aria-label", "Delivered");
    const merged = model.made.filter((item) => item.status === "merged").length;
    say.textContent = ts.doneWords(team, st, { merged, decisions: model.decided.length });
    const last = [...st.parts].filter((part) => part.row?.lead).sort((a, b) => String(a.row.ended_at || "").localeCompare(String(b.row.ended_at || ""))).pop();
    then.textContent = last ? `${last.name}: ${last.row.lead}.`.replace(/\.\.$/, ".") : "";
    const prs = model.made.filter((item) => item.kind === "PR" && item.url);
    put(acts, 
      el("button", { type: "button", text: "Copy summary", title: "Its PRs, decisions and a line per part, as Markdown", onclick: () => copySummary() }),
      prs.length === 1 ? el("a", { class: "button secondary", href: prs[0].url, target: "_blank", rel: "noopener", text: "Open its PR" }) : null,
      el("button", { type: "button", class: "link", text: "Dismiss", title: "It goes to Past teams", onclick: () => postSeen({ team: team.id, key: st.key }, true).then(() => select(HOME)) }),
    );
    put(node, el("span", { class: "captain" }, captain("done")), say, then.textContent ? then : null, acts, ...bar(model));
    return node;
  }
  if (brief.kind === "need") {
    const part = brief.part;
    node.classList.add("attention", "needs");
    node.setAttribute("aria-label", "What needs you");
    const ask = brief.need === "ask" ? askOf(part) : null;
    const who = el("span", { class: "who", text: part.name });
    if (brief.need === "review") put(say, who, `'s result is ready${part.row?.lead ? `: ${part.row.lead}` : "."}`);
    else if (brief.need === "ask") put(say, who, ask?.text ? ` asks you: ${firstSentence(ask.text)}` : " asks you something.");
    else if (part.marker === "failed") put(say, who, " failed.");
    else put(say, who, ` needs you: ${part.phase?.why || ts.needsWords(part.marker, part.name)}.`);
    const waited = brief.need === "review" ? "" : `It has waited ${ts.duration(brief.waited)}. `;
    put(then, waited, ...boldNames(brief.unblocks, st), brief.also ? ` ${brief.also}` : "");
    put(node, el("span", { class: "captain" }, captain("needs")), say, then.textContent.trim() ? then : null);
    if (ask) {
      node.classList.add("decision");
      put(node, answerBlock(part, ask));
    } else if (brief.need === "review") {
      put(acts, el("button", { type: "button", text: `Review ${part.name}`, onclick: () => openReview(part.id) }));
      put(node, acts);
    } else {
      put(acts, ...stuckActions(part), el("button", { type: "button", class: "link", text: "Open its chat", onclick: () => openChat(part) }));
      put(node, acts);
    }
    put(node, ...bar(model));
    return node;
  }
  if (brief.kind === "held") {
    node.classList.add("attention", "needs");
    node.setAttribute("aria-label", "What holds it up");
    put(say, el("span", { class: "who", text: brief.part.name }), ` has waited ${ts.duration(brief.waited)} for ${brief.to}'s answer.`);
    put(then, "Its work waits on it. ", ...boldNames(brief.unblocks, st));
    put(acts, nudgeButton(brief.question, brief.to), el("button", { type: "button", class: "link", text: "Open its chat", onclick: () => openChat(brief.part) }));
    put(node, el("span", { class: "captain" }, captain("needs")), say, then, acts, ...bar(model));
    return node;
  }
  node.classList.add("working");
  node.setAttribute("aria-label", "Where it stands");
  const atWork = brief.working.length ? `${ts.listed(brief.working)} ${brief.working.length > 1 ? "are" : "is"} at work` : "";
  const busy = [atWork, ...brief.waiting].filter(Boolean).join("; ");
  say.textContent = `Nothing needs you.${busy ? ` ${busy.charAt(0).toUpperCase()}${busy.slice(1)}.` : ""}`;
  if (brief.next) put(then, "Next: ", el("b", { text: brief.next.name }), brief.nextWords ? ` ${brief.nextWords}.` : " starts next.");
  put(node, el("span", { class: "captain" }, captain(brief.working.length ? "working" : "watching")), say, brief.next ? then : null, ...bar(model));
  return node;
}
// A sentence with the parts' names in bold (what it unblocks).
function boldNames(text, st) {
  if (!text) return [];
  const names = st.parts.map((part) => part.name).sort((a, b) => b.length - a.length);
  const pieces = [];
  let rest = text;
  while (rest) {
    let at = -1;
    let found = "";
    for (const name of names) {
      const index = rest.indexOf(name);
      if (index >= 0 && (at < 0 || index < at)) {
        at = index;
        found = name;
      }
    }
    if (at < 0) {
      pieces.push(rest);
      break;
    }
    if (at) pieces.push(rest.slice(0, at));
    pieces.push(el("b", { text: found }));
    rest = rest.slice(at + found.length);
  }
  return pieces;
}
// Answered right there: the worker's options (the recommended one marked) and its own words.
function answerBlock(part, ask) {
  const error = el("span", { class: "error" });
  const block = el("div", { class: "tp-answer" });
  const send = (text) =>
    once(`tp-answer:${part.id}`, block, async () => {
      if (!text.trim()) return;
      error.textContent = "";
      try {
        if (ask.kind === "decision") {
          const result = await api(`/api/drafts/${ask.draft.id}`, { method: "POST", body: { action: "answer", text } });
          applyDraft(result?.id === ask.draft.id && result.status ? result : { ...ask.draft, status: "answered" });
        } else {
          await api(`/api/questions/${encodeURIComponent(ask.question.id)}/answer`, { method: "POST", body: { text } });
          await loadState();
        }
        toast(`Sent to ${part.name}: ${shorten(text, 80)}`);
        scheduleRefresh();
      } catch (failure) {
        error.textContent = failure.message;
      }
    });
  if (ask.why) block.append(el("p", { class: "why", text: ask.why }));
  const lowered = ask.recommendation.toLowerCase();
  const best = ask.options.find((option) => lowered.includes(option.choice.toLowerCase()));
  if (ask.options.length) {
    block.append(el("div", { class: "options" }, ...ask.options.map((option) => el("button", { type: "button", class: `option${option === best ? " recommended" : ""}`, onclick: () => send(option.choice) }, el("span", { class: "choice", text: option.choice }), option === best ? el("span", { class: "tag", text: "Recommended" }) : null, option.effect ? el("small", { text: option.effect }) : null))));
  }
  if (ask.recommendation) {
    const reason = best && lowered.startsWith(best.choice.toLowerCase()) ? ask.recommendation.slice(best.choice.length).replace(/^[\s:,.;—–\-：，。；]+/u, "") : "";
    block.append(el("div", { class: "recommendation", text: reason ? `Why ${best.choice}: ${reason}` : `Recommended: ${ask.recommendation}` }));
  }
  const input = el("input", { class: "own", placeholder: ask.options.length ? "Your own answer" : "Your answer…", "aria-label": `Your answer to ${part.name}`, autocomplete: "off" });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      send(input.value);
    }
  });
  const own = el("div", { class: "own-row" }, input, el("button", { type: "button", class: "secondary", text: ask.kind === "decision" ? "Decide" : "Answer", onclick: () => send(input.value) }));
  // With its options, the owner's own words one click away (as Home's decision card).
  if (ask.options.length) {
    own.hidden = true;
    const other = el("button", { type: "button", class: "link other", text: "Something else…" });
    other.addEventListener("click", () => {
      other.remove();
      own.hidden = false;
      input.focus();
    });
    block.append(other);
  }
  block.append(own, error);
  return block;
}
function nudgeButton(question, to) {
  const nudged = question.nudged ? Math.round((Date.now() - new Date(question.nudged)) / 60000) : null;
  const button = el("button", { type: "button", text: nudged === null ? `Nudge ${to}` : `Nudged ${nudged} min ago`, disabled: nudged !== null && nudged < 30, title: `Tell ${to} its teammate waits for the answer` });
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      toast((await api(`/api/questions/${encodeURIComponent(question.id)}/nudge`, { method: "POST" })).said);
      await loadState();
    } catch (error) {
      toast(error.message, "error");
      button.disabled = false;
    }
  });
  return button;
}
function stuckActions(part) {
  const status = part.row?.status || part.status;
  if (status !== "failed" && status !== "cancelled") return [];
  return [el("button", { type: "button", text: "Retry", onclick: (event) => act(event.currentTarget, `/api/tasks/${encodeURIComponent(part.task)}/retry`, { text: "" }, `${part.name} goes again.`) })];
}
async function act(button, path, body, done) {
  if (button) button.disabled = true;
  try {
    await api(path, { method: "POST", body });
    if (done) toast(done);
    await loadState();
    scheduleRefresh();
  } catch (error) {
    toast(error.message, "error");
    if (button) button.disabled = false;
  }
}
const openChat = (part) => select(part.moved_out?.session || part.session, { task: part.moved_out?.task || part.task });

// 2 · Since you looked: one line, only when something changed; each item jumps to its line.
function sinceLine(model, view) {
  if (!model.since.length) return null;
  return el(
    "div",
    { class: "tp-since", role: "status" },
    el("span", { class: "lead", text: `Since you looked at ${clock(view.since)}` }),
    ...model.since.flatMap((item, index) => {
      const button = el("button", { type: "button", class: "link", "data-key": item.key, text: item.text, onclick: () => jump(item.target) });
      return index ? [el("span", { class: "sep", text: "·", "aria-hidden": "true" }), button] : [button];
    }),
  );
}
function jump(target) {
  let node = null;
  if (target === "brief") node = $("#tp-brief");
  else if (target === "notes") {
    node = document.querySelector(".tp-talk.notes");
    if (node) node.open = true;
  } else if (target.startsWith("part:")) node = document.querySelector(`.tp-part[data-part="${CSS.escape(target.slice(5))}"]`);
  else node = document.querySelector(`[data-line="${CSS.escape(target.slice(5))}"]`);
  if (!node) return;
  node.scrollIntoView({ block: "center", behavior: reduced() ? "auto" : "smooth" });
  node.classList.remove("flash", "jump");
  void node.offsetWidth;
  node.classList.add(node.id === "tp-brief" ? "flash" : "jump");
}

// 3 · So far: made, decided, watch out. The new line rises in, lit a moment, then lets go.
function whoMark(model, who) {
  const part = (id) => model.st.parts.find((one) => one.id === id);
  // A mark as the avatars are (drawn, its line says who: "You decided…").
  if (who === "you") return el("span", { class: "you", "data-av": "you", "aria-hidden": "true" });
  if (Array.isArray(who)) {
    const [a, b] = who.map(part).filter(Boolean);
    if (a && b) return el("span", { class: "pair", "aria-hidden": "true" }, partAvatar(a), partAvatar(b));
    who = a?.id;
  }
  const found = part(who);
  return found ? partAvatar(found) : el("span", { class: "you", "data-av": "·", "aria-hidden": "true" });
}
const TONE_CHIP = { good: "done", bad: "failed", wait: "waiting" };
function line(model, { key, who, main, sub, chip, tone, at, kind, url }) {
  const name = url ? el("a", { href: url, target: "_blank", rel: "noopener", text: main }) : document.createTextNode(main);
  return el(
    "div",
    { class: `tp-line${model.unseen(at) ? " unseen" : ""}`, "data-line": key },
    whoMark(model, who),
    el("span", { class: "what" }, el("span", { class: "main", title: main }, kind ? el("span", { class: "kind", "data-kind": kind }) : null, name), sub ? el("span", { class: "sub", text: sub }) : null),
    el("span", { class: "end" }, chip ? el("span", { class: `chip ${TONE_CHIP[tone] || ""}`.trim(), text: chip }) : null),
  );
}
function soFarGroups(model) {
  const { st } = model;
  const name = (id) => st.parts.find((part) => part.id === id)?.name || "";
  const made = model.made.map((item) => line(model, { key: item.line, who: item.part, main: item.name, sub: [name(item.part), item.status && item.at ? `${item.status} ${clock(item.at)}` : item.status].filter(Boolean).join(" · "), chip: item.status, tone: item.tone, at: item.at, kind: item.kind === "PR" ? "PR" : item.kind === "Doc" ? "DOC" : "RUN", url: item.url }));
  const decided = model.decided.map((entry) => {
    const who = entry.kind === "yours" ? "you" : [entry.from, entry.to];
    const sub = entry.kind === "yours" ? `You decided${entry.parts.length ? ` for ${ts.listed(entry.parts.map(name))}` : ""} at ${clock(entry.at)}` : `${name(entry.from)} answered ${name(entry.to)}'s question`;
    return line(model, { key: entry.key, who, main: entry.text, sub, at: entry.at });
  });
  const watch = model.watch.map((item) => {
    const node = line(model, { key: item.key, who: item.parts.length > 1 ? item.parts.slice(0, 2) : item.parts[0], main: item.text, sub: item.sub });
    if (item.question) node.querySelector(".end").append(nudgeButton(item.question, name(item.question.to)));
    return node;
  });
  return [
    ["made", "Made", made],
    ["decided", "Decided", decided],
    ["watch", "Watch out", watch],
  ].filter(([, , items]) => items.length);
}
function soFarCard(model) {
  const title = model.st.done ? "Delivered" : "So far";
  const groups = soFarGroups(model);
  const card = el("section", { class: "card tp-sofar", "aria-label": title }, el("h2", { class: "tp-h", text: title }));
  if (!groups.length) card.append(el("p", { class: "tp-empty", text: "Nothing made or decided yet: it comes here as parts report." }));
  for (const [id, words, items] of groups) card.append(el("div", { class: `tp-group ${id}` }, el("h3", { text: words }), ...items));
  return card;
}
// Wide: Home's own side panel, a section each.
function panel(model) {
  const node = $("#panel");
  if (!wide()) {
    node.hidden = true;
    homePanel(node);
    return;
  }
  node.hidden = false;
  node.classList.add("tp-panel");
  node.setAttribute("aria-label", model.st.done ? "Delivered" : "So far");
  const prs = model.made.filter((item) => item.kind === "PR").length;
  const head = el("div", { class: "tp-panel-head" }, el("h2", { text: model.st.done ? "Delivered" : "So far" }), el("span", { text: model.st.done ? [prs ? `${prs} PR${prs > 1 ? "s" : ""}` : "", `${model.decided.length} decision${model.decided.length === 1 ? "" : "s"}`].filter(Boolean).join(" · ") : "Kept up to date as parts report" }));
  const groups = soFarGroups(model);
  const box = el("div", { class: "tp-sofar-panel" }, head);
  if (!groups.length) box.append(el("p", { class: "tp-empty", text: "Nothing made or decided yet: it comes here as parts report." }));
  for (const [id, words, items] of groups) {
    const section = el("details", { class: `panel-section area tp-sofar-${id}`, open: true }, el("summary", {}, el("h2", { class: "title", text: words }), el("span", { class: "count", text: String(items.length) })), el("div", { class: "items" }, ...items));
    box.append(section);
  }
  const old = node.querySelector(":scope > .tp-sofar-panel");
  if (old) old.replaceWith(box);
  else node.append(box);
}

// 4 · The plan: the parts in the order work moves, each with its state, time, output and action.
const CLASS = { needs: "is-needs", blocked: "is-needs", working: "is-working", watching: "is-watching" };
function partClass(part) {
  if (part.finished) return part.accepted ? "is-done" : "is-done is-ended";
  if (part.state === "watching" && part.next) return "is-next";
  return CLASS[part.state] || "";
}
function planCard(model, view) {
  const { st } = model;
  const card = el("section", { class: "card tp-plan", "aria-label": "Plan" }, el("h2", { class: "tp-h" }, "Plan", el("span", { class: "count", text: `${st.finished} of ${st.total} done` })));
  card.append(document.createElementNS("http://www.w3.org/2000/svg", "svg"));
  card.lastChild.setAttribute("class", "rails");
  card.lastChild.setAttribute("aria-hidden", "true");
  const list = el("div", { role: "list", "aria-label": "Parts, in the order work moves" });
  for (const stage of model.stages) {
    const node = el("div", { class: "tp-stage", "data-stage": String(stage.index) });
    if (stage.label) node.append(el("div", { class: "tp-stage-label", text: stage.label }));
    for (const part of stage.parts) node.append(...partRow(model, view, part));
    list.append(node);
  }
  card.append(list);
  return card;
}
function partRow(model, view, part) {
  const { st, team, brief } = model;
  const ask = part.state === "needs" && ts.needKind(part.marker) === "ask" ? askOf(part) : null;
  const words = ts.partWords(team, st, part, { ask: ask ? firstSentence(ask.text) : "", clock });
  const cls = partClass(part);
  const avatar = partAvatar({ ...part, cls: cls + (part.state === "needs" || (part.state === "blocked" && part.held?.minutes >= ts.BLOCKING_MINUTES) ? " is-review" : "") }, { large: true });
  const kind = ts.needKind(part.marker);
  let primary = null;
  if (part.state === "needs" && kind === "review" && !st.done) primary = el("button", { type: "button", class: view.review === part.id ? "secondary" : "", text: "Review", "aria-expanded": String(view.review === part.id), onclick: (event) => { event.stopPropagation(); openReview(part.id); } });
  else if (part.state === "needs" && ask && brief.part?.id !== part.id) primary = el("button", { type: "button", text: "Answer", "aria-expanded": String(view.answering === part.id), onclick: (event) => { event.stopPropagation(); view.answering = view.answering === part.id ? "" : part.id; view.opening = view.answering; renderTeam(); } });
  else if (part.state === "needs" && brief.part?.id !== part.id) [primary] = stuckActions(part);
  else if (part.state === "blocked" && part.held?.minutes >= ts.BLOCKING_MINUTES && brief.part?.id !== part.id) primary = nudgeButton(part.held, st.parts.find((one) => one.id === part.held.to)?.name || "");
  const outs = (model.made || []).filter((item) => item.part === part.id).slice(0, 3);
  const mark = markOf(workerOf(part.moved_out?.session || part.session));
  const row = el(
    "div",
    { class: `tp-part ${part.color} ${cls}${view.open === part.id ? " open" : ""}`, "data-part": part.id },
    el("span", { class: "av" }, avatar),
    el(
      "div",
      { class: "main" },
      el(
        "div",
        { class: "tp-head", role: "button", tabindex: "0", "aria-expanded": String(view.open === part.id), "aria-label": `${part.name}: ${words}`, onclick: () => toggleOpen(part.id), onkeydown: (event) => (event.key === "Enter" || event.key === " ") && (event.preventDefault(), toggleOpen(part.id)) },
        el("div", { class: "name" }, el("span", { text: part.name }), mark || null),
        el("div", { class: "status", text: words, title: words }),
      ),
      outs.length ? el("div", { class: "outs" }, ...outs.map((item) => (item.url ? el("a", { class: `chip ${TONE_CHIP[item.tone] || ""}`.trim(), href: item.url, target: "_blank", rel: "noopener", text: item.kind === "PR" ? `${item.short}${item.status ? ` ${item.status}` : ""}` : item.name }) : el("span", { class: "chip", text: item.name })))) : null,
    ),
    el("span", { class: "time", text: ts.partTime(part) }),
    el("span", { class: "act" }, primary, rowMenu(model, part)),
  );
  const after = [];
  if (view.review === part.id && part.state === "needs" && kind === "review") after.push(reviewDrawer(model, part));
  else if (view.answering === part.id && ask) after.push(el("div", { class: "tp-review tp-ask card decision" }, el("p", { class: "lead", text: ask.text }), answerBlock(part, ask)));
  else if (view.open === part.id) after.push(detail(model, part));
  // Opened just now: it rises in (not at every redraw while open).
  if (view.opening === part.id && moving()) after.forEach((node) => node.classList.add("rise"));
  // A part and what it opens, one item of the list.
  return [el("div", { class: "tp-item", role: "listitem" }, row, ...after)];
}
function toggleOpen(id) {
  page.open = page.open === id ? "" : id;
  page.opening = page.open;
  renderTeam();
}
function openReview(id) {
  page.review = page.review === id ? "" : id;
  page.opening = page.review;
  page.changes = false;
  renderTeam();
  requestAnimationFrame(() => document.querySelector(`.tp-part[data-part="${CSS.escape(id)}"]`)?.scrollIntoView({ block: "center", behavior: reduced() ? "auto" : "smooth" }));
}
function rowMenu(model, part) {
  const status = part.row?.status || part.status;
  const [how] = ts.tellHow(model.team, model.st, part);
  const items = [
    how && !part.finished ? { text: `Tell ${part.name}…`, onclick: () => tellOnly(part.id) } : null,
    { text: "Open its chat", onclick: () => openChat(part) },
    part.state === "working" && byId(part.session)?.status === "working" ? { text: "Stop", onclick: () => act(null, `/api/sessions/${encodeURIComponent(part.session)}/stop`, {}, `${part.name} stops after its current step; it waits for you.`) } : null,
    part.finished && how === "reopen" ? { text: "Follow up…", onclick: () => tellOnly(part.id) } : null,
    !part.finished && !part.moved && status !== "cancelled" ? { text: "Cancel this part…", danger: true, onclick: () => confirmCancel(part) } : null,
  ];
  return moreMenu(items, `${part.name}: more`, "⋯", `${part.name}: more`);
}
function tellOnly(id) {
  page.to = new Set([id]);
  page.toOpen = false;
  renderTeam();
  $("#composer-input").focus();
}
// Irreversible: asked in a modal first.
function confirmCancel(part) {
  const reason = el("input", { placeholder: "Why? (optional)", "aria-label": "Why" });
  const dialog = el("dialog", { class: "tp-confirm", "aria-label": `Cancel ${part.name}` });
  const close = () => {
    dialog.close();
    dialog.remove();
  };
  const go = el("button", { type: "button", class: "danger", text: "Cancel this part" });
  go.addEventListener("click", async () => {
    go.disabled = true;
    await act(null, `/api/tasks/${encodeURIComponent(part.task)}/cancel`, { text: reason.value.trim() }, `${part.name} was cancelled.`);
    close();
  });
  dialog.append(el("form", { method: "dialog" }, el("h2", { text: `Cancel ${part.name}?` }), el("p", { class: "muted", text: "Its worker stops and the part ends; what waits on it won't get its result. This can't be undone (Retry runs it again)." }), reason, el("div", { class: "row" }, el("button", { type: "button", class: "secondary", text: "Keep it", onclick: close }), go)));
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}
// What it is doing now, under a part when opened: its latest updates, its links.
function detail(model, part) {
  const updates = part.row?.progress_updates || [];
  const body = updates.length
    ? el("ol", {}, ...updates.slice(0, 5).map((update) => el("li", {}, el("time", { text: update.at ? clock(update.at) : "" }), el("span", { text: String(update.text || "").split("\n")[0] }))))
    : el("p", { text: part.finished ? part.row?.lead || "Nothing more to show here." : part.state === "watching" && part.next ? `Not started. ${ts.startsWords(model.team, model.st, part)}.` : part.row?.progress || "No update yet." });
  const [how] = ts.tellHow(model.team, model.st, part);
  return el("div", { class: "tp-detail" }, body, el("div", { class: "links" }, el("button", { type: "button", class: "link", text: "Open its chat", onclick: () => openChat(part) }), how ? el("button", { type: "button", class: "link", text: part.finished ? `Follow up on ${part.name}…` : `Tell ${part.name}…`, onclick: () => tellOnly(part.id) }) : null));
}
// Review, in place: what it says it did, its PR, Accept or ask for changes.
function reviewDrawer(model, part) {
  const error = el("span", { class: "error" });
  const box = el("div", { class: "tp-review", role: "region", "aria-label": `${part.name}'s result` });
  if (part.row?.lead) box.append(el("p", { class: "lead", text: part.row.lead }));
  if (part.row?.rest) box.append(el("p", { class: "rest", text: part.row.rest }));
  for (const item of model.made.filter((one) => one.part === part.id).slice(0, 3)) box.append(line(model, { key: `r:${item.key}`, who: part.id, main: item.name, sub: item.status ? `${item.short} · ${item.status}` : item.short, chip: item.status, tone: item.tone, kind: item.kind === "PR" ? "PR" : "DOC", url: item.url }));
  if (page.changes) {
    const field = el("textarea", { placeholder: `What should ${part.name} change?`, "aria-label": "Changes to ask for", rows: "3" });
    const send = el("button", { type: "button", text: `Send to ${part.name}` });
    send.addEventListener("click", async () => {
      if (!field.value.trim()) return field.focus();
      send.disabled = true;
      try {
        await api(`/api/tasks/${encodeURIComponent(part.task)}/request-changes`, { method: "POST", body: { text: field.value.trim() } });
        page.review = "";
        page.changes = false;
        toast(`${part.name} is told what to change; it goes back to work.`);
        await loadState();
        scheduleRefresh();
      } catch (failure) {
        error.textContent = failure.message;
        send.disabled = false;
      }
    });
    box.append(field, el("div", { class: "acts" }, send, el("button", { type: "button", class: "link", text: "Cancel", onclick: () => { page.changes = false; renderTeam(); } }), error));
    requestAnimationFrame(() => field.focus());
  } else {
    const accept = el("button", { type: "button", text: "Accept" });
    accept.addEventListener("click", async () => {
      accept.disabled = true;
      try {
        await api(`/api/tasks/${encodeURIComponent(part.task)}/accept`, { method: "POST" });
        page.review = "";
        toast(`Accepted ${part.name}.`);
        await loadState();
        scheduleRefresh();
      } catch (failure) {
        error.textContent = failure.message;
        accept.disabled = false;
      }
    });
    box.append(el("div", { class: "acts" }, accept, el("button", { type: "button", class: "secondary", text: "Request changes", onclick: () => { page.changes = true; renderTeam(); } }), el("button", { type: "button", class: "link", text: "Open its chat", onclick: () => openChat(part) }), error));
  }
  return box;
}

// The rail, once laid out: stage to stage, solid once every result into the next stage came,
// dashed while one is to come; between parts side by side, a quiet line.
const SVG = "http://www.w3.org/2000/svg";
function centreOf(card, id) {
  const avatar = card.querySelector(`.tp-part[data-part="${CSS.escape(id)}"] .pa`);
  if (!avatar) return null;
  const box = card.getBoundingClientRect();
  const rect = avatar.getBoundingClientRect();
  return { x: rect.left + rect.width / 2 - box.left, y: rect.top + rect.height / 2 - box.top, top: rect.top - box.top, bottom: rect.bottom - box.top };
}
function drawRails() {
  const card = document.querySelector("#timeline .tp-plan");
  const model = page?.model;
  if (!card || !model || !card.offsetParent) return;
  const rails = card.querySelector(":scope > svg.rails");
  rails.replaceChildren();
  const segments = [];
  model.stages.forEach((stage, index) => {
    for (let at = 1; at < stage.parts.length; at++) segments.push([stage.parts[at - 1].id, stage.parts[at].id, "together"]);
    const next = model.stages[index + 1];
    if (next) segments.push([stage.parts.at(-1).id, next.parts[0].id, ts.railState(model.team, model.st, stage, next)]);
  });
  for (const [from, to, cls] of segments) {
    const a = centreOf(card, from);
    const b = centreOf(card, to);
    if (!a || !b) continue;
    const path = document.createElementNS(SVG, "path");
    path.setAttribute("d", `M${a.x} ${a.bottom + 5} L${b.x} ${b.top - 5}`);
    path.setAttribute("class", cls);
    rails.append(path);
  }
}
// Drawn again as the column's width changes (the window, the panel's width).
let railsWatch = null;
function watchRails() {
  if (railsWatch || !window.ResizeObserver) return;
  railsWatch = new window.ResizeObserver(() => drawRails());
  railsWatch.observe($("#scroller"));
}

// 5 · What the parts told each other: folded; So far holds what it settled.
function talkFold(model, view) {
  const items = view.items.filter((item) => item.kind === "tell" || item.kind === "ask");
  const part = (id) => model.st.parts.find((one) => one.id === id) || { name: "", avatar: "?", color: "" };
  const node = el("details", { class: "tp-talk notes", open: view.notesOpen || undefined }, el("summary", {}, "What the parts told each other", el("span", { class: "count", text: ` · ${items.length}` })));
  node.addEventListener("toggle", () => (view.notesOpen = node.open));
  const list = el("div", { class: "items" });
  for (const item of items) {
    const from = part(item.from);
    const to = part(item.to);
    const open = item.kind === "ask" && item.status === "open";
    list.append(
      el(
        "div",
        { class: "t-item", "data-id": item.id },
        partAvatar(from),
        el(
          "div",
          {},
          el("div", { class: "who", text: `${from.name} → ${to.name} · ${item.kind === "ask" ? "question" : "note"}` }),
          el("div", { class: "about", text: item.title }),
          item.rest ? el("div", { class: "rest", text: item.rest }) : null,
          item.kind === "ask" && item.status === "answered" ? el("div", { class: "answer", text: `${to.name}: ${item.answer}${item.minutes !== null && item.minutes !== undefined ? ` (in ${ts.duration(item.minutes)})` : ""}` }) : null,
          open ? el("span", { class: "pill waiting", text: `waiting ${ts.duration(item.minutes || 0)}${item.blocks ? ` · holds ${from.name}` : ""}` }) : null,
          item.kind === "ask" && !["open", "answered"].includes(item.status) ? el("span", { class: "pill ended", text: "no answer" }) : null,
        ),
        el("time", { text: clock(item.at), datetime: item.at, title: new Date(item.at).toLocaleString() }),
      ),
    );
  }
  if (!items.length) list.append(el("p", { class: "tp-empty", text: "Nothing yet." }));
  node.append(list);
  if (view.next) node.append(el("button", { type: "button", class: "link show-more", text: "Show earlier", onclick: earlier }));
  return node;
}

// 6 · Tell the team: the product's composer, addressed. Who gets it is always in sight.
function recipients(model, view) {
  const can = new Set(model.st.parts.filter((part) => ts.tellHow(model.team, model.st, part)[0]).map((part) => part.id));
  const chosen = view.to ? [...view.to].filter((id) => can.has(id)) : ts.defaultRecipients(model.team, model.st);
  return model.st.parts.filter((part) => chosen.includes(part.id));
}
function composer(model, view) {
  const form = $("#composer");
  const to = $("#composer-to");
  const shown = !model.st.done || Boolean(view.to?.size);
  form.hidden = !shown;
  if (!shown) return;
  const parts = recipients(model, view);
  const input = $("#composer-input");
  const placeholder = model.st.done ? `Follow up on ${ts.listed(parts.map((part) => part.name))}…` : parts.length === 1 && view.to ? `Tell ${parts[0].name}…` : "Tell the team…";
  if (input.placeholder !== placeholder) input.placeholder = placeholder;
  const names = [];
  parts.forEach((part, index) => {
    if (index) names.push(el("span", { class: "to-word", text: "·", "aria-hidden": "true" }));
    const [how] = ts.tellHow(model.team, model.st, part);
    names.push(el("span", { class: "rcpt" }, partAvatar(part), el("span", { class: "name", text: part.name }), how === "start" ? el("span", { class: "later", text: "(when it starts)" }) : how === "reopen" ? el("span", { class: "later", text: "(reopens it)" }) : null));
  });
  const change = el("button", { type: "button", class: "change", "aria-haspopup": "dialog", "aria-expanded": String(view.toOpen), text: "Change ▾" });
  change.addEventListener("click", (event) => {
    event.stopPropagation();
    view.toOpen = !view.toOpen;
    composer(model, view);
  });
  to.replaceChildren(el("span", { class: "to-word", text: "To" }), ...(parts.length ? names : [el("span", { class: "later", text: "nobody yet" })]), change);
  to.hidden = false;
  form.querySelector(".tp-to-menu")?.remove();
  if (view.toOpen) form.append(toMenu(model, view, parts));
}
function toMenu(model, view, chosen) {
  const ids = new Set(chosen.map((part) => part.id));
  const rows = model.st.parts.map((part) => {
    const [how, words] = ts.tellHow(model.team, model.st, part);
    const box = el("input", { type: "checkbox", checked: ids.has(part.id) || undefined, disabled: !how || undefined });
    box.addEventListener("change", () => {
      const next = new Set(recipients(model, view).map((one) => one.id));
      if (box.checked) next.add(part.id);
      else next.delete(part.id);
      view.to = next;
      composer(model, view);
    });
    return el("label", { class: how ? "" : "off" }, box, partAvatar(part), el("span", {}, part.name, el("span", { class: "how", text: words })));
  });
  const menu = el("div", { class: "menu tp-to-menu", role: "dialog", "aria-label": "Who gets it" }, ...rows, el("p", { class: "note", text: "Each part at work spends one turn reading it. Nothing is sent until you press Send." }));
  menu.addEventListener("click", (event) => event.stopPropagation());
  menu.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      view.toOpen = false;
      composer(model, view);
      $("#composer-to .change")?.focus();
    }
  });
  return menu;
}
document.addEventListener("click", (event) => {
  if (!page?.toOpen || event.target.closest?.(".tp-to-menu, #composer-to .change")) return;
  page.toOpen = false;
  if (page.model) composer(page.model, page);
});
// Sent from the composer (app.js): to the parts in its To line. True once sent.
export async function tellTeam(text) {
  const view = page;
  if (!view?.model || view.sending) return false;
  const parts = recipients(view.model, view);
  if (!parts.length) {
    toast("Choose who gets it: Change ▾.", "error");
    return false;
  }
  view.sending = true;
  try {
    const told = await api(`/api/teams/${encodeURIComponent(view.id)}/tell`, { method: "POST", body: { text, to: parts.map((part) => part.id), key: view.sendKey } });
    view.sendKey = newKey();
    view.to = null;
    view.toOpen = false;
    const name = (id) => parts.find((part) => part.id === id)?.name || "";
    const now = told.to.filter((item) => item.how === "now" || item.how === "reopen").map((item) => name(item.part));
    const later = told.to.filter((item) => item.how === "start").map((item) => name(item.part));
    const failed = told.to.filter((item) => item.how === "failed").map((item) => name(item.part));
    toast([now.length ? `Sent to ${ts.listed(now)}` : "", later.length ? `${ts.listed(later)} ${later.length > 1 ? "get" : "gets"} it when ${later.length > 1 ? "they start" : "it starts"}` : "", failed.length ? `Not sent to ${ts.listed(failed)}` : ""].filter(Boolean).join("; ") + ".", failed.length ? "error" : "");
    await refreshTeam();
    return true;
  } catch (error) {
    toast(error.message, "error");
    return false;
  } finally {
    view.sending = false;
  }
}
// After Send, one quiet line under the plan: who has read it, who gets it when it starts.
const READ = new Set(["received", "processed"]);
function toldLine(model) {
  const told = (page.data.tells || []).at(-1);
  if (!told || Date.now() - Date.parse(told.at) > TOLD_HOURS * 3600000) return null;
  const name = (id) => model.st.parts.find((part) => part.id === id)?.name || "";
  return el(
    "div",
    { class: "tp-sent", role: "status" },
    el("span", { text: `You told the team at ${clock(told.at)}: “${shorten(told.text, 48)}”` }),
    ...told.to.map((item) => {
      const who = name(item.part);
      if (READ.has(item.delivery)) return el("span", { class: "got", text: `${who} ✓ read` });
      const words = { start: ": when it starts", dropped: ": it never started", failed: ": not sent", undelivered: ": not delivered" }[item.delivery];
      return el("span", { class: words && item.delivery !== "start" ? "bad" : "", text: words ? `${who}${words}` : `${who} …` });
    }),
  );
}

// Copy summary: the PRs, the decisions, a line per part, as Markdown.
function copySummary() {
  const model = page?.model;
  if (!model) return;
  const text = ts.summaryMarkdown(model.team, model.st, { made: model.made, decided: model.decided });
  navigator.clipboard?.writeText(text).then(
    () => toast(`Copied: ${model.made.length} made, ${model.decided.length} decided, a line per part.`),
    () => toast("Couldn't copy: the browser refused.", "error"),
  );
}

// ---------- motion: only for what changed since it was drawn, under a minute old ----------

function snapshotOf(model) {
  return {
    states: Object.fromEntries(model.st.parts.map((part) => [part.id, `${part.state}|${part.finished}|${part.accepted}`])),
    words: Object.fromEntries(model.st.parts.map((part) => [part.id, ts.partWords(model.team, model.st, part, { clock })])),
    edges: Object.fromEntries((model.team.edges || []).map((edge) => [`${edge.from}>${edge.to}`, edge.state])),
    segments: model.st.segments,
    lines: [...model.made.map((item) => item.line), ...model.decided.map((entry) => entry.key)],
    since: model.since.map((item) => item.key),
    brief: briefKey(model),
  };
}
const briefKey = (model) => `${model.brief.kind}|${model.brief.part?.id || ""}|${model.brief.need || ""}`;
// What moves, once found (a redraw diffs what it draws with the last one), carries on through
// the redraws that follow (the page is drawn again on every event): each by when it began.
const MOVES = { fresh: 3200, "fresh-in": 450, swap: 500, just: 1200, "tp-lit": 1600 };
function began(view, key, kind) {
  view.moves ||= new Map();
  if (!view.moves.has(key)) view.moves.set(key, { kind, at: performance.now() });
}
function moving_(view, key) {
  const found = view.moves?.get(key);
  if (!found) return null;
  const elapsed = performance.now() - found.at;
  if (elapsed > MOVES[found.kind]) {
    view.moves.delete(key);
    return null;
  }
  return { ...found, elapsed };
}
function lit(node, view, key) {
  const found = node && moving_(view, key);
  if (!found) return;
  node.classList.add(found.kind);
  // Where it was in its animation (both of a fresh line's, rising in and lit).
  const delay = `-${Math.round(found.elapsed)}ms`;
  node.style.animationDelay = found.kind === "fresh" ? `${delay}, ${delay}` : delay;
}
function motion(model, view, root) {
  const prev = view.prev;
  if (prev && moving()) {
    // What is new since the last drawing, and under a minute old.
    for (const item of model.made) if (!prev.lines.includes(item.line) && recent(item.at)) began(view, `line:${item.line}`, "fresh");
    for (const entry of model.decided) if (!prev.lines.includes(entry.key) && recent(entry.at)) began(view, `line:${entry.key}`, "fresh");
    for (const item of model.since) if (!prev.since.includes(item.key)) began(view, `since:${item.key}`, "fresh-in");
    for (const part of model.st.parts) {
      const was = prev.states[part.id];
      if (was && was !== `${part.state}|${part.finished}|${part.accepted}` && part.finished && part.accepted) began(view, `just:${part.id}`, "just");
      if (prev.words[part.id] && prev.words[part.id] !== ts.partWords(model.team, model.st, part, { clock })) began(view, `swap:${part.id}`, "swap");
    }
    if (prev.brief !== briefKey(model)) began(view, "brief", "tp-lit");
    // A segment fills as its part finishes.
    root.querySelectorAll(".tp-bar i").forEach((node, index) => {
      if (prev.segments[index] && prev.segments[index] !== "done" && model.st.segments[index] === "done") node.classList.add(`was-${prev.segments[index]}`);
    });
    requestAnimationFrame(() => requestAnimationFrame(() => document.querySelectorAll(".tp-bar i[class*='was-']").forEach((node) => node.classList.forEach((name) => name.startsWith("was-") && node.classList.remove(name)))));
  }
  paint(view, root);
}
function paint(view, root) {
  if (!root) return;
  if (!moving()) return view.moves?.clear();
  for (const node of root.querySelectorAll("[data-line]")) lit(node, view, `line:${node.dataset.line}`);
  for (const node of root.querySelectorAll(".tp-since button[data-key]")) lit(node, view, `since:${node.dataset.key}`);
  for (const row of root.querySelectorAll(".tp-part")) {
    lit(row, view, `just:${row.dataset.part}`);
    lit(row.querySelector(".status"), view, `swap:${row.dataset.part}`);
  }
  lit(root.querySelector("#tp-brief"), view, "brief");
}
// Dots in the sender's colour along the rail: a result handed on, an answer between parts.
// Each goes on where it was when the page is drawn again before it arrives.
function travel(model, view) {
  const prev = view.prev;
  const card = document.querySelector("#timeline .tp-plan");
  if (!card || !moving() || model.st.done) return (view.trips = []);
  view.trips ||= [];
  if (prev) {
    for (const edge of model.team.edges || []) {
      const was = prev.edges[`${edge.from}>${edge.to}`];
      const source = model.st.parts.find((part) => part.id === edge.from);
      if (was && was !== "met" && edge.state === "met" && recent(source?.row?.ended_at || source?.row?.updated_at)) view.trips.push({ from: edge.from, to: edge.to, time: 1000, at: performance.now() });
    }
    for (const entry of model.decided) if (entry.kind === "between" && !prev.lines.includes(entry.key) && recent(entry.at)) view.trips.push({ from: entry.from, to: entry.to, time: 900, at: performance.now() });
  }
  view.trips = view.trips.filter((trip) => performance.now() - trip.at < trip.time);
  for (const trip of view.trips) {
    const a = centreOf(card, trip.from);
    const b = centreOf(card, trip.to);
    const part = model.st.parts.find((one) => one.id === trip.from);
    if (!a || !b || !part) continue;
    const dot = el("span", { class: `travel ${part.color}` });
    dot.style.left = `${a.x}px`;
    dot.style.top = `${a.y}px`;
    card.append(dot);
    const path = dot.animate([{ transform: "translate(0, 0)" }, { transform: `translate(${b.x - a.x}px, ${b.y - a.y}px)` }], { duration: trip.time, easing: "cubic-bezier(0.45, 0, 0.55, 1)" });
    path.currentTime = performance.now() - trip.at;
    path.onfinish = () => dot.remove();
    path.oncancel = () => dot.remove();
  }
}

// What the page hears (Home's task and peer events) redraws it, a moment later.
let refreshing = 0;
function scheduleRefresh() {
  clearTimeout(refreshing);
  refreshing = setTimeout(() => {
    refreshing = 0;
    refreshTeam();
  }, 300);
}
