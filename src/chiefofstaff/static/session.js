// The main pane: Home or one session, with its header, model settings and timeline.

import { $, api, blobLive, blobUrl, el, foldable, freshButton, toast, whenNearScreen } from "./dom.js";
import { clock, compactPaths, firstLine, gist, headOf, linkLabel, markdown, shorten, slackText, threadLabel } from "./text.js";
import { byId, current, DELIVERY_WORDS, HOME, markRead, otherItem, pendingDrafts, RECONNECTING, routerId, state, STATUS_TEXT, tasks, uncertainDrafts, upsertSession, viewedId, workerTitle } from "./state.js";
import { MARKERS } from "./board.js";
import { slotReason } from "./tasks.js";
import { renderSidebar, showExternal } from "./sidebar.js";
import { focusDraft, focusTask, renderHome, renderHomeHeader, showHomeTab, welcomeCard, workerStates } from "./inbox.js";
import { closeMentions, setRecipient } from "./mention.js";
import { agentMark, agentName, agentPopover, agentsOn, closeAgentPopover, fixLine, markOf, marksShown, modelLabel, modelOptions, ready, statuses, statusOf } from "./agentpick.js";
import { setupAction } from "./agentsetup.js";
import { openSettings } from "./settings.js";
import { showLength } from "./composer.js";
import { duck } from "./mascot.js";
import { sharedEntry } from "./share.js";
import { avatar, labelOf, peerOf, peerLine, showTeam, TEAM, teamOfPeers, teamOfWorker, waitingWords, workerLineage } from "./team.js";
// A chat loads a page of its messages at a time: its latest, then earlier ones when asked.
export const PAGE = 400;
// The chat shown: its first message loaded, and how many come before it (null: not known).
const earlier = { session: "", first: null, more: false, count: null, busy: false };
// A worker's state in the header, as its sidebar dot and Home have it.
// The four places, as the sidebar dot and Home say them.
const SHORT_STATE = { waiting: "Needs you", working: "Running", watching: "Monitoring", idle: "Nothing open" };
// With another coding agent installed, @ picks an agent too (its status in the @ list).
const routerPlaceholder = () => (statuses().length ? "Ask anything… type @ to pick an agent or a worker" : "Ask for anything… (@ to message one worker)");
// Every view but Home has "← Home" at the top left.
function backHome(shown) {
  $("#back-home").hidden = !shown;
}
// The "⋯" of a session: its model settings, and what is done with it less often.
function sessionMenu(session, items = []) {
  // Not rebuilt while open: a choice the owner is making stays where it is.
  if (!$("#session-more").hidden && $("#session-menu").dataset.for === session?.id) return;
  $("#session-menu").dataset.for = session?.id || "";
  $("#session-more").hidden = true;
  if (session) renderTuning(session);
  else $("#session-tuning").hidden = true;
  $("#session-menu-items").replaceChildren(...items.filter(Boolean));
  $("#session-menu").hidden = $("#session-tuning").hidden && !$("#session-menu-items").children.length;
}
const menuButton = (text, onclick, className = "") => el("button", { type: "button", class: className, text, onclick });
export function renderHeader() {
  // A shared card to start a task from goes with a message on Home only.
  const withCard = $("#composer-with");
  if (withCard) withCard.hidden = state.selected !== HOME;
  if (state.selected === HOME) {
    backHome(false);
    $("#worker-tabs").hidden = true;
    renderHomeHeader();
    const router = byId(routerId());
    sessionMenu(router);
    $("#composer").hidden = false;
    const waiting = !router && state.snapshot.runtime.state !== "ready";
    setComposer(router?.elsewhere ? { locked: IN_TERMINAL } : waiting ? { locked: "You can ask once Copilot is connected." } : { placeholder: routerPlaceholder() });
    return;
  }
  const session = current();
  const actions = $("#session-actions");
  if (!session) return;
  backHome(true);
  renderWorkerTabs(session);
  delete actions.dataset.key;
  $("#composer").hidden = Boolean(session.closed_at) && session.role !== "worker";
  let meta = [];
  if (session.closed_at) {
    meta = ["Closed", session.branch];
  } else if (session.role === "router") {
    meta = ["Coordinates your sessions", session.status === "working" ? "routing…" : ""];
  } else if (session.role === "slack") {
    meta = ["Drafts your Slack replies", STATUS_TEXT[session.status]];
  } else if (session.role === "watchdog") {
    meta = ["Checks workers for stalls and drift", STATUS_TEXT[session.status]];
  } else {
    // Its state and branch; the folders it works in are in the tooltip and its ⋯ menu.
    const repos = session.checkouts?.length || 0;
    // Where it stands, in the words of its sidebar dot and of Home.
    const found = workerStates().get(session.id);
    const where = (found?.dot || "idle").split(" ")[0];
    // Its turn over while a command it left running goes on: said, not "Running" alone.
    const background = session.background && session.status !== "working" ? "waiting for background process" : "";
    meta = [SHORT_STATE[where] || STATUS_TEXT[session.status], found?.sub || background, session.status === "queued" || session.activity === RECONNECTING ? session.activity : "", session.branch, repos > 1 ? `${repos} repos` : ""];
    if (session.elsewhere) {
      // What it does in the terminal, read from its log; Chief of Staff does not drive it now.
      const terminal = session.terminal || {};
      meta = [terminal.working ? "Working in a terminal" : "Open in a terminal", terminal.working ? terminal.activity : "", session.branch];
    }
  }
  if (session.credits >= 0.05) meta.push(`${session.credits.toFixed(1)} credits`);
  // On another coding agent: its name and model there after its state, and its tokens.
  if (session.agent) meta.splice(1, 0, AGENT, modelLabel(session.agent, session.model));
  if (session.agent && session.tokens) meta.push(session.token_limit ? `${tokenCount(session.tokens)} of ${tokenCount(session.token_limit)} tokens` : `${tokenCount(session.tokens)} tokens`);
  // While other agents are on, a Copilot worker says so too (why, on hover, when it was to run
  // on another).
  if (session.role === "worker" && !session.agent && state.snapshot.agents) meta.splice(1, 0, FALLBACK);
  const follows = session.follows || [];
  if (follows.length && !session.closed_at) meta.push(`following ${follows.length}`);
  const titles = { router: "Chat with your chief of staff", slack: "Slack", watchdog: "Watchdog" };
  $("#session-title").textContent = titles[session.role] || workerTitle(session);
  $("#session-title").title = session.role === "worker" && session.title !== workerTitle(session) ? `Started as “${session.title}”` : "";
  if (session.agent || meta.includes(FALLBACK)) agentMeta(session, meta.filter(Boolean));
  else $("#session-meta").textContent = meta.filter(Boolean).join(" · ");
  const where = session.role === "worker" ? (session.checkouts?.length ? session.checkouts.map((checkout) => `${checkout.path}${checkout.branch ? ` (${checkout.branch})` : ""}`) : [session.cwd]) : [];
  $("#session-meta").title = [...where, ...follows.map(threadLabel)].filter(Boolean).join("\n");
  // Stop is on the line that shows it working, at the end of its timeline.
  const buttons = [];
  if (session.role === "worker" && session.closed_at) {
    buttons.push(el("button", { type: "button", class: "secondary", text: "Reopen", title: "Continue with its conversation and checkout", onclick: () => reopen(session.id) }));
  }
  actions.replaceChildren(...buttons);
  const items = [];
  if (session.role === "worker" && !session.closed_at) {
    const globalYolo = state.snapshot.settings.worker_yolo;
    const checks = el(
      "select",
      { class: session.yolo ? "yolo" : "", title: "Permission checks for this worker" },
      el("option", { value: "", text: `Default: ${globalYolo && !session.slack ? "yolo" : "ask first"}` }),
      el("option", { value: "on", text: "Yolo (acts without asking)" }),
      el("option", { value: "off", text: "Ask first (asks before risky commands)" }),
    );
    checks.value = session.yolo_choice || "";
    // Ask first only where its agent's checks can run: else disabled, with why.
    const why = session.agent ? session.ask_first_refused : "";
    const reason = why ? el("small", { id: "ask-first-why", hidden: true, text: why }) : null;
    if (why) {
      checks.querySelector('option[value="off"]').disabled = true;
      checks.title = why;
      checks.setAttribute("aria-describedby", reason.id);
    }
    checks.onchange = async () => {
      try {
        const view = await api(`/api/sessions/${session.id}/yolo`, { method: "POST", body: { mode: checks.value } });
        upsertSession(view);
        toast(view.yolo
          ? "Yolo: this worker acts without asking."
          : view.switch_stopped
            ? `Ask first: this worker asks before risky commands. It stopped to switch: ${view.switch_card ? "Go on under Needs you continues." : "send it your message again."}`
            : "Ask first: this worker asks before risky commands.");
      } catch (error) {
        toast(error.message, "error");
      }
    };
    items.push(el("label", {}, el("span", { text: "Permission checks" }), checks, reason));
    // Its short name in the lines the Router and the chats name it by (E129): the owner's, or
    // its own back ("").
    if (session.team) {
      const name = el("input", { type: "text", value: session.label || "", maxlength: "24", "aria-label": "Short name", title: "How its team's lines and thread name it (empty: from its first task)" });
      name.addEventListener("change", async () => {
        try {
          const { label } = await api(`/api/sessions/${session.id}/label`, { method: "POST", body: { text: name.value } });
          name.value = label;
          toast(`Its short name: ${label}.`);
        } catch (error) {
          toast(error.message, "error");
        }
      });
      items.push(el("label", {}, el("span", { text: "Short name" }), name));
    }
  }
  if (session.role === "worker" && session.cwd) {
    const where = { finder: /Windows/.test(navigator.userAgent) ? "File Explorer" : "Finder", vscode: "VS Code" };
    const open = (app) => api(`/api/sessions/${session.id}/open`, { method: "POST", body: { app } }).then(() => toast(`Opened in ${where[app]}.`), (error) => toast(error.message, "error"));
    items.push(
      el("hr"),
      menuButton(/Windows/.test(navigator.userAgent) ? "Open in File Explorer" : "Open in Finder", () => open("finder")),
      menuButton("Open in VS Code", () => open("vscode")),
      menuButton("Copy folder path", () => navigator.clipboard.writeText(session.cwd).then(() => toast("Path copied."), (error) => toast(error.message, "error"))),
    );
  }
  if (session.role === "worker" && session.agent && !session.closed_at && session.status !== "working") {
    // Another agent's worker: its agent's own command (a Copilot worker's is `copilot --resume`,
    // as the guide says); Chief of Staff lets go of it first, and leaves it alone there.
    const terminal = async () => {
      try {
        const { command } = await api(`/api/sessions/${session.id}/terminal`, { method: "POST" });
        await navigator.clipboard.writeText(command);
        toast("Command copied: run it in a terminal. Chief of Staff leaves this worker alone while it is open there.");
      } catch (error) {
        toast(error.message, "error");
      }
    };
    items.push(menuButton("Continue in a terminal", terminal));
  }
  // Continue with another agent: a new worker there goes on from a summary, in the same
  // folders; this one closes. Only once it is idle, only while other agents are on.
  const idle = !["working", "queued", "starting"].includes(session.status) && !session.background && !session.elsewhere;
  if (session.role === "worker" && !session.closed_at && !session.helper && idle && state.snapshot.agents) {
    const names = { copilot: "Copilot", ...state.snapshot.agents.names };
    const others = agentsOn().filter((name) => name !== (session.agent || "copilot"));
    items.push(el("hr"), ...others.map((name) => menuButton(`Continue with ${names[name]}…`, () => continueWith(session, name))));
  }
  if (session.role === "worker" && !session.closed_at) {
    items.push(
      el("hr"),
      session.status === "working" ? menuButton("Stop what it is doing", () => post(`/api/sessions/${session.id}/stop`, "Stopped.")) : "",
      menuButton(
        "Close worker",
        () => {
          // Undone within the toast's time: reopened with what closing cancelled.
          if (session.status !== "working" || confirm("This worker is still working. Close it?")) post(`/api/sessions/${session.id}/close`, "Worker closed.", { text: "Undo", onclick: () => reopen(session.id) });
        },
        "danger",
      ),
    );
  }
  sessionMenu(session, items);
  const placeholders = {
    router: routerPlaceholder(),
    slack: "Message the Slack session…",
    watchdog: "Message the Watchdog…",
  };
  setComposer(
    session.elsewhere
      ? { locked: IN_TERMINAL }
      : { placeholder: session.closed_at ? "Message to reopen this worker…" : placeholders[session.role] || chatPlaceholder(session) },
  );
}
// Continue with <agent>…: what goes to the new worker, in one line (its summary's length and
// the changes it brings), then Continue; the worker stays open (a copy of its worktree goes).
export async function continueWith(session, agent) {
  $("#session-more").hidden = true;  // (Its menu, chosen from, closed.)
  const label = agentName(agent);
  const line = el("p", { class: "continue-line", role: "status", text: "Checking what it sends…" });
  const go = el("button", { type: "button", text: "Continue", disabled: true });
  const cancel = el("button", { type: "button", class: "text", text: "Cancel", onclick: () => dialog.close() });
  const dialog = el(
    "dialog",
    { class: "continue-dialog", "aria-label": `Continue with ${label}` },
    el("h2", {}, agentMark(agent), ` Continue “${shorten(workerTitle(session), 60)}” with ${label}`),
    line,
    el("footer", {}, el("span", { class: "spacer" }), cancel, go),
  );
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  try {
    const preview = await api(`/api/sessions/${session.id}/continue?agent=${encodeURIComponent(agent)}`);
    line.textContent = preview.line;
    if (preview.already) {
      // Continued already (a stale card, another tab): its new worker, not another one.
      go.textContent = "Open it";
      go.onclick = () => {
        dialog.close();
        select(preview.already);
      };
      go.disabled = false;
      go.focus();
      return;
    }
    go.disabled = false;
    go.focus();
  } catch (error) {
    line.textContent = error.message;
    line.classList.add("error");
    return;
  }
  go.onclick = async () => {
    go.disabled = true;
    try {
      const made = await api(`/api/sessions/${session.id}/continue`, { method: "POST", body: { agent } });
      dialog.close();
      toast(made.already ? `Already continued on ${agentName(made.agent || agent)}.` : `Continuing on ${label}.`);
      select(made.session_id);
    } catch (error) {
      go.disabled = false;
      line.textContent = error.message;
      line.classList.add("error");
    }
  };
}
// Restart on <agent>: before a worker's first step, its task on another agent at once (nothing
// was done to lose: no step to confirm).
export async function restartOn(session, picked) {
  try {
    const made = await api(`/api/sessions/${session.id}/restart`, { method: "POST", body: { agent: picked.agent, model: picked.model || "", effort: picked.effort || "" } });
    toast(`Restarted on ${agentName(picked.agent)}.`);
    if (state.selected === session.id) select(made.session_id);
  } catch (error) {
    toast(error.message, "error");
  }
}
// Agents another worker can start on now (on, and ready as last known).
const runnable = (name) => name === "copilot" || ready(statusOf(name) || { status: "ready" });
// A worker's agent as a chip (its start line in the Router chat), while more than one agent is
// on: before its first step, Restart on each other agent; after it, idle, Continue with….
export function agentChip(sessionId) {
  const first = byId(sessionId);
  if (!first || first.role !== "worker" || !marksShown()) return null;
  const agent = first.agent || "copilot";
  const menu = el("div", { class: "menu", role: "menu", hidden: true });
  const button = el(
    "button",
    {
      type: "button",
      class: "agent-chip",
      title: [agentName(agent), modelLabel(agent, first.model)].filter(Boolean).join(" · "),
      "aria-haspopup": "menu",
      onclick: (event) => {
        event.stopPropagation();
        const session = byId(sessionId);
        const open = menu.hidden;
        menu.hidden = true;
        if (!open || !session || session.closed_at) return;
        const idle = !["working", "queued", "starting"].includes(session.status) && !session.background && !session.elsewhere;
        const others = agentsOn().filter((name) => name !== (session.agent || "copilot") && runnable(name));
        const items = session.untouched
          ? others.map((name) => ({ text: `Restart on ${agentName(name)}`, onclick: () => restartOn(session, { agent: name }) }))
          : idle
            ? others.map((name) => ({ text: `Continue with ${agentName(name)}…`, onclick: () => continueWith(session, name) }))
            : [];
        if (!items.length) {
          toast(session.untouched || idle ? "No other agent can run it now." : "It is at work: Continue with… is offered once it is done.");
          return;
        }
        menu.replaceChildren(...items.map((item) => el("button", { type: "button", role: "menuitem", text: item.text, onclick: (inner) => { inner.stopPropagation(); menu.hidden = true; item.onclick(); } })));
        menu.hidden = false;
      },
    },
    agentMark(agent),
    el("span", { text: agentName(agent) }),
  );
  return el("span", { class: "menu-wrap agent-chip-wrap" }, button, menu);
}
// A worker with several tasks: the one the owner chose in its chat, which what they write is
// about (bug QA6-3: it was filed under the one given last, whatever they looked at): the task
// the chat was opened at, else, scrolled up from its end, the latest of its messages in view
// that is about one. "" at the chat's end (the server decides: a question it asks, the task it
// is on, the one their own last message was about, else a task of its own: Runtime.chat_task),
// and with one task or none to choose from.
export function chatAnchor() {
  const session = current();
  if (state.selected === HOME || session?.role !== "worker" || !session.chat_task) return "";
  if (openedAt.session === session.id && openedAt.task in session.chat_task.titles) return openedAt.task;
  const scroller = $("#scroller");
  if (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 160) return "";
  const bottom = scroller.getBoundingClientRect().bottom;
  let found = "";
  for (const node of $("#timeline").querySelectorAll(":scope > [data-task]")) {
    if (node.getBoundingClientRect().top >= bottom) break;
    if (node.dataset.task in session.chat_task.titles) found = node.dataset.task;
  }
  return found;
}
// What the composer of a worker's chat says: where what is written there goes, when it has
// several tasks (the one in view, else the server's choice: maybe a task of its own).
function chatPlaceholder(session) {
  const known = session.chat_task;
  if (!known) return "Message this worker directly…";
  const anchor = chatAnchor();
  const title = anchor ? known.titles[anchor] : known.title;
  return title ? `Message about “${shorten(title, 60)}”…` : "A new task for this worker…";
}
// The task a worker's chat was opened at (a task row's Worker link): what is written there is
// about it until the owner scrolls elsewhere themself.
let openedAt = { session: "", task: "" };
export function scrolledByOwner() {
  if (!openedAt.task) return;
  openedAt = { session: "", task: "" };
  followAnchor();
}
// The placeholder follows what the owner scrolls to (once a frame at most).
let anchoring = 0;
export function followAnchor() {
  if (anchoring) return;
  anchoring = requestAnimationFrame(() => {
    anchoring = 0;
    const session = current();
    if (state.selected === HOME || session?.role !== "worker" || session.closed_at || session.elsewhere) return;
    const input = $("#composer-input");
    const placeholder = chatPlaceholder(session);
    if (input.placeholder !== placeholder) input.placeholder = placeholder;
  });
}
// Two programs must not write to one Copilot session: one open in a terminal is used there.
export const IN_TERMINAL = "Open in a terminal right now. Continue there, or exit it there (/exit) to message it here.";
// The composer, or (locked) a note saying why there is none. `attach`: images can go along.
export function setComposer({ placeholder = "", locked = "", attach = true } = {}) {
  const input = $("#composer-input");
  $("#composer").classList.toggle("locked", Boolean(locked));
  $("#composer").classList.toggle("no-attach", !attach);
  $("#composer-note").textContent = locked;
  input.disabled = Boolean(locked);
  input.placeholder = placeholder;
}
// The agent's name in the meta line of a worker on another coding agent: where it works
// differently from a Copilot worker, its tooltip (and description) says how, a line each.
const AGENT = Symbol("agent");
const FALLBACK = Symbol("fallback");
function agentMeta(session, parts) {
  const notes = session.agent_notes || [];
  const name = agentBadge(session, session.agent_name, notes.join("\n"));
  const fallback = agentBadge(session, "Copilot", session.agent_fallback || "");
  const nodes = parts.flatMap((part, index) => [index ? " · " : "", part === AGENT ? name : part === FALLBACK ? fallback : part]);
  if (notes.length) {
    name.setAttribute("aria-describedby", "session-agent-notes");
    nodes.push(el("span", { id: "session-agent-notes", hidden: true, text: notes.join(". ") }));
  }
  $("#session-meta").replaceChildren(...nodes);
}
// Its agent in its header: with its mark while more than one agent is on; before its first
// step, a button whose popover changes its agent (Restart on…), model and effort.
function agentBadge(session, text, title) {
  const agent = session.agent || "copilot";
  const mark = marksShown() ? agentMark(agent, modelLabel(agent, session.model)) : "";
  if (!session.untouched || session.closed_at || !marksShown()) return el("span", { class: "agent-name", title: title || undefined }, mark, text);
  const button = el("button", { type: "button", class: "agent-chip", "aria-haspopup": "dialog", title: title || "Agent, model and effort, before it begins" }, mark, el("span", { text }), el("span", { class: "caret", "aria-hidden": "true", text: "▾" }));
  button.onclick = () =>
    agentPopover(button, { agent, model: session.model_choice || "", effort: session.effort_choice || "" }, async (picked) => {
      if (picked.agent !== agent) {
        closeAgentPopover();
        await restartOn(session, picked);
        return;
      }
      try {
        upsertSession(await api(`/api/sessions/${session.id}/model`, { method: "POST", body: { model: picked.model, effort: picked.effort } }));
      } catch (error) {
        toast(error.message, "error");
      }
    }, { title: "Runs on" });
  return button;
}
const tokenCount = (count) => (count >= 1e6 ? `${+(count / 1e6).toFixed(1)}M` : count >= 1000 ? `${Math.round(count / 1000)}k` : String(count));
export const EFFORTS = { none: "none", low: "low", medium: "medium", high: "high", xhigh: "extra high", max: "max" };
// A worker's models: its agent's (listed once asked), or Copilot's.
const modelsOf = (session) => (session?.agent ? state.agentModels?.[session.agent] || [] : state.models);
let shownModels = [];
const modelInfo = (id) => shownModels.find((model) => model.id === id);
const tokens = (count) => (count >= 1e6 ? `${+(count / 1e6).toFixed(1)}M` : `${Math.round(count / 1000)}k`);
// Model, thinking effort and context window of the selected session. Each applies from its next message.
function renderTuning(session) {
  const box = $("#session-tuning");
  if (session.agent && !state.agentModels?.[session.agent]) agentModels(session);
  shownModels = modelsOf(session);
  box.hidden = Boolean(session.closed_at || session.elsewhere) || !shownModels.length;
  if (box.hidden) return;
  const model = $("#session-model");
  const effort = $("#session-effort");
  const context = $("#session-context");
  const fallback = session.model_choice ? "" : modelInfo(session.model)?.name || session.model;
  const show = (select, shown) => {
    select.closest("label").hidden = !shown;
  };
  model.replaceChildren(...modelOptions(session.agent, shownModels, { selected: session.model_choice, label: `Default${fallback ? ` · ${fallback}` : ""}` }));
  model.value = session.model_choice || "";
  const info = modelInfo(session.model);
  const efforts = info?.efforts || [];
  show(effort, efforts.length);
  effort.replaceChildren(
    el("option", { value: "", text: `Default${session.effort && !session.effort_choice ? ` (${EFFORTS[session.effort] || session.effort})` : ""}` }),
    ...efforts.map((level) => el("option", { value: level, text: EFFORTS[level] || level })),
  );
  effort.value = efforts.includes(session.effort_choice) ? session.effort_choice : "";
  show(context, info?.long_context);
  if (info?.long_context) {
    context.replaceChildren(
      el("option", { value: "", text: `${tokens(info.context)} tokens` }),
      el("option", { value: "long_context", text: `${tokens(info.long_context)} tokens` }),
    );
    context.value = session.context || "";
  }
  const save = async (body, what) => {
    try {
      const view = await api(`/api/sessions/${session.id}/model`, { method: "POST", body });
      upsertSession(view);
      renderTuning(view);  // Its efforts and context windows are the new model's, menu open or not.
      toast(`${what}. Applies from the next message.`);
    } catch (error) {
      toast(error.message, "error");
      renderTuning(session);
    }
  };
  // A new model starts from its own default effort and context window.
  model.onchange = () => save({ model: model.value }, `Model: ${model.value ? modelInfo(model.value)?.name || model.value : "default"}`);
  // Resend only what the current model supports: a choice left from another model is dropped.
  const keptEffort = efforts.includes(session.effort_choice) ? session.effort_choice : "";
  const keptContext = info?.long_context ? session.context : "";
  effort.onchange = () =>
    save({ model: session.model_choice, effort: effort.value, context: keptContext }, `Thinking effort: ${EFFORTS[effort.value] || "default"}`);
  context.onchange = () =>
    save({ model: session.model_choice, effort: keptEffort, context: context.value }, `Context window: ${context.selectedOptions[0].text.replace(" tokens", "")}`);
}
// Another agent's models, listed once a worker on it shows its ⋯ menu (Claude's, each time it
// opens: those listed meanwhile, its exact models once a worker reported them, as they are now).
export async function agentModels(session) {
  state.agentModels = { ...state.agentModels, [session.agent]: state.agentModels?.[session.agent] || [] };
  try {
    state.agentModels[session.agent] = await api(`/api/models?agent=${encodeURIComponent(session.agent)}`);
  } catch {
    delete state.agentModels[session.agent];  // Asked again next time.
    return;
  }
  if (current()?.id === session.id) renderTuning(current());
}
async function reopen(sessionId) {
  try {
    upsertSession(await api(`/api/sessions/${sessionId}/reopen`, { method: "POST" }));
    toast("Worker reopened.");
  } catch (error) {
    toast(error.message, "error");
  }
}
// An action on a worker, said when done (bug E91: Close and Stop said nothing); `undo`: a
// button in that toast.
async function post(path, done = "", undo = null) {
  try {
    const view = await api(path, { method: "POST" });
    if (view?.id) upsertSession(view);
    if (done) toast(done, "", null, undo);
  } catch (error) {
    toast(error.message, "error");
  }
}
// Home has the Router's conversation and a panel beside it; a session, its timeline alone.
export function setLayout(home) {
  $("#main").classList.toggle("home", home);
  // A worker's tab is its own: any other view shows all it holds.
  delete $("#timeline").dataset.tab;
  $("#panel").hidden = !home;
  $("#home-tabs").hidden = !home;
  $("#notices").hidden = !home || !$("#notices").children.length;
  $("#timeline").hidden = false;
  if (home) showHomeTab(state.homeTab);
}
// `task`: the conversation opens at that task's latest report (or what it was given), not at
// its end: the worker may have gone on to other work since.
export async function select(sessionId, { task = "" } = {}) {
  if (!sessionId) return;
  // A team's view (E130): what its workers told and asked each other.
  if (String(sessionId).startsWith(TEAM)) {
    await showTeam(sessionId.slice(TEAM.length));
    return;
  }
  openedAt = { session: task ? sessionId : "", task };
  // The Router's chat and the inbox of earlier versions are Home.
  if (sessionId === "inbox" || sessionId === routerId()) sessionId = HOME;
  closeMentions();
  // A worker chosen with @ is for messages sent from Home.
  if (sessionId !== HOME) setRecipient(null);
  if (sessionId !== HOME && !byId(sessionId)) {
    // One of the owner's other Copilot sessions (with the setting on): its view.
    const other = otherItem(sessionId);
    if (other) {
      showExternal(other);
      return;
    }
    try {
      state.viewing = await api(`/api/sessions/${sessionId}`);
    } catch {
      await select(HOME);
      return;
    }
  }
  state.selected = sessionId;
  localStorage.setItem("cos.selected", sessionId);
  showDraft(sessionId);
  setLayout(sessionId === HOME);
  renderSidebar();
  renderHeader();
  renderHome();
  const timeline = $("#timeline");
  timeline.replaceChildren();
  state.lastId = 0;
  // Opened, a chat shows its work.
  setWorkerTab(sessionId, "work");
  const viewed = viewedId();
  let messages = [];
  // Messages that come by event while the history loads are added after it.
  const loading = { id: viewed, held: [] };
  if (viewed) {
    state.loading = loading;
    try {
      messages = await api(`/api/sessions/${viewed}/messages?limit=${PAGE}`);
    } catch (error) {
      toast(error.message, "error");
    }
    if (state.loading === loading) state.loading = null;
    if (state.selected !== sessionId) return;
  }
  timeline.replaceChildren();
  state.lastId = 0;
  Object.assign(earlier, { session: viewed, first: messages[0] || null, more: false, count: null, busy: false });
  appendMessages(messages);
  appendMessages(loading.held.filter((message) => !message.amended));
  // What changed meanwhile in messages it shows (where one stands): as they are now. Applied only
  // once they are on the page (bug QA6-1: a message sent while Home's chat loaded showed no
  // word of where it stood).
  for (const message of loading.held.filter((message) => message.amended)) replaceMessage(message);
  scrollToBottom(true);
  if (messages.length >= PAGE) await countEarlier(viewed);
  if (state.selected !== sessionId) return;
  if (task) await showTaskInTimeline(task);
  followAnchor();
  if (viewed && byId(viewed)?.unread) markRead(viewed);
  if (!$("#composer").hidden) $("#composer-input").focus();
}
// How many messages come before the first one shown: a way to load them, if any.
async function countEarlier(sessionId) {
  let count = null;
  try {
    ({ count } = await api(`/api/sessions/${sessionId}/messages/count?before=${earlier.first.id}`));
  } catch {
    // Not known: they can be loaded all the same.
  }
  if (earlier.session !== sessionId || viewedId() !== sessionId) return;
  Object.assign(earlier, { count, more: count !== 0 });
  renderEarlier();
}
// Content added above what is read: the view stays where it was.
function keepScroll(change) {
  const scroller = $("#scroller");
  const fromBottom = scroller.scrollHeight - scroller.scrollTop;
  change();
  scroller.scrollTop = scroller.scrollHeight - fromBottom;
}
// "Load earlier messages" at the top of a chat that has more; the fold of steps at the top
// says when more of its steps come before it.
function renderEarlier() {
  const timeline = $("#timeline");
  let control = timeline.querySelector(":scope > .earlier");
  const top = control ? control.nextElementSibling : timeline.firstElementChild;
  for (const group of timeline.querySelectorAll(":scope > details.steps[data-partial]")) {
    delete group.dataset.partial;
    summarize(group);
  }
  if (earlier.more && top?.matches("details.steps")) {
    top.dataset.partial = "1";
    summarize(top);
  }
  if (!earlier.more) {
    control?.remove();
    return;
  }
  if (!control) {
    control = el("div", { class: "line notice earlier" }, el("button", { type: "button", class: "link", onclick: () => loadEarlier() }));
    keepScroll(() => timeline.prepend(control));
  }
  const button = control.querySelector("button");
  const count = earlier.count ? ` (${earlier.count.toLocaleString("en-US")} more)` : "";
  button.textContent = earlier.busy ? "Loading earlier messages…" : `Load earlier messages${count}`;
  button.disabled = earlier.busy;
}
// The page before the first message shown, above it (the view stays put). Whether it loaded any.
async function loadEarlier() {
  const sessionId = earlier.session;
  if (earlier.busy || !earlier.more || !earlier.first || viewedId() !== sessionId) return false;
  earlier.busy = true;
  renderEarlier();
  let page = null;
  try {
    page = await api(`/api/sessions/${sessionId}/messages?before=${earlier.first.id}&limit=${PAGE}`);
  } catch (error) {
    toast(error.message, "error");
  }
  earlier.busy = false;
  if (earlier.session !== sessionId || viewedId() !== sessionId) return false;
  keepScroll(() => {
    if (page) {
      if (page.length) prependMessages(page);
      earlier.first = page[0] || earlier.first;
      if (earlier.count !== null) earlier.count = Math.max(0, earlier.count - page.length);
      earlier.more = page.length >= PAGE && earlier.count !== 0;
    }
    renderEarlier();
  });
  return Boolean(page?.length);
}
// Earlier messages, above those shown: steps that go on into the fold at the top join it.
function prependMessages(messages) {
  const timeline = $("#timeline");
  const shown = state.selected === HOME ? byId(routerId()) : current();
  const box = el("div");
  const next = earlier.first;
  messages.forEach((message, index) => {
    if (message.id >= next.id || message.session_id !== earlier.session) return;
    place(box, message, messages[index + 1] || next, shown);
  });
  const control = timeline.querySelector(":scope > .earlier");
  const top = control ? control.nextElementSibling : timeline.firstElementChild;
  const last = box.lastElementChild;
  if (last?.matches("details.steps") && top?.matches("details.steps")) {
    top.querySelector("summary").after(...[...last.children].filter((child) => child.tagName !== "SUMMARY"));
    if (!top.dataset.said && last.dataset.said) top.dataset.said = last.dataset.said;
    last.remove();
    summarize(top);
  }
  // A run folded on Home goes on across the page boundary: one fold.
  const kind = foldKind(last);
  if (kind && foldKind(top) === kind) {
    const group = top.dataset.fold ? top : wrapFold(top, kind);
    group.lastElementChild.prepend(...(last.dataset.fold ? last.lastElementChild.children : [last]));
    if (last.dataset.fold) last.remove();
    summarizeFold(group);
  }
  if (control) control.after(...box.children);
  else timeline.prepend(...box.children);
  updateDraftRecords();
  updateWorkerTabs();
}
// What you typed and did not send, per chat: back after a reload, or after signing in again.
const DRAFT = "cos.draft.";
let drafted = null;  // The chat whose text the composer holds.
function keepDraft() {
  if (drafted === null) return;
  const text = $("#composer-input").value;
  if (text.trim()) localStorage.setItem(DRAFT + drafted, text);
  else localStorage.removeItem(DRAFT + drafted);
}
export function showDraft(chat) {
  const input = $("#composer-input");
  if (drafted === null) {
    input.addEventListener("input", keepDraft);
    // Sent: the composer is emptied first (and the text put back if sending fails).
    $("#composer").addEventListener("submit", () => setTimeout(keepDraft));
    window.addEventListener("pagehide", keepDraft);
  } else if (drafted === chat) {
    return;
  } else {
    keepDraft();
  }
  drafted = chat;
  input.value = localStorage.getItem(DRAFT + chat) || "";
  input.style.height = "auto";
  if (input.value && input.scrollHeight) input.style.height = `${input.scrollHeight}px`;
  showLength();
}
async function showTaskInTimeline(taskId) {
  const find = () => {
    const nodes = [...$("#timeline").querySelectorAll(`[data-task="${taskId}"]`)];
    return nodes.filter((item) => item.dataset.kind === "report").pop() || nodes.pop();
  };
  let node = find();
  // Not among the latest messages: earlier ones, a page at a time, until it is there.
  for (let pages = 0; !node && pages < 25 && (await loadEarlier()); pages += 1) node = find();
  if (!node) return;
  // In a fold on Home (a routing receipt): the fold opens to it.
  const group = node.closest(".fold");
  if (group) toggleFold(group, true);
  node.scrollIntoView({ block: "start" });
  node.classList.remove("flash");
  void node.offsetWidth;  // Restarts the highlight.
  node.classList.add("flash");
}
export function scrollToBottom(force = false) {
  const scroller = $("#scroller");
  if (force || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 160) {
    scroller.scrollTop = scroller.scrollHeight;
  }
}
// An attached file, from a blob: address of this page (dom.js blobUrl), never one with the key:
// an image shows once near the screen and opens full size; another file downloads on a click.
function attachmentLink(file) {
  const image = file.type?.startsWith("image/");
  const link = el(
    "a",
    { href: "#", target: "_blank", rel: "noreferrer", title: file.name, download: image ? undefined : file.name },
    image ? el("img", { alt: file.name, onload: imageLoaded }) : el("span", { class: "file", text: file.name }),
  );
  const load = () =>
    blobUrl(file.url).then((url) => {
      link.href = url;
      const picture = link.querySelector("img");
      if (picture) picture.src = url;
    });
  if (image) {
    whenNearScreen(link, () => load().catch((error) => link.replaceChildren(el("span", { class: "file", text: file.name, title: error.message }))));
  }
  link.addEventListener("click", (event) => {
    if (blobLive(link.getAttribute("href"))) return;
    // Not loaded yet (a file, or an image off screen) or let go since: load it, then open or
    // download it.
    event.preventDefault();
    load()
      .then(() => link.click())
      .catch((error) => toast(error.message, "error"));
  });
  return link;
}
// An image has no height until it loads: one that loads with the chat at its end keeps it there.
function imageLoaded(event) {
  const scroller = $("#scroller");
  if (scroller.contains(event.target) && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight - event.target.offsetHeight < 160) {
    scroller.scrollTop = scroller.scrollHeight;
  }
}
const STATUS_CHIP = { answer: "Answer/Update", done: "Done", progress: "Progress", question: "Question", blocked: "Blocked", failed: "Failed" };
// A done report whose result waits for the owner's Accept says so, as Home does.
const toReview = (taskId) => tasks().find((item) => item.id === taskId)?.status === "review";
export function chip(status, taskId = "") {
  if (!STATUS_CHIP[status]) return null;
  const node = el("span", { class: `chip ${status}`, "data-task": status === "done" && taskId ? taskId : undefined });
  showChip(node, status);
  return node;
}
function showChip(node, status = "done") {
  const review = Boolean(node.dataset.task) && toReview(node.dataset.task);
  node.textContent = review ? MARKERS.review : STATUS_CHIP[status];
  node.classList.toggle("waiting", review);
}
// As its task stands now: accepted since, it is done.
export function refreshChips() {
  for (const node of document.querySelectorAll("#timeline .chip[data-task]")) showChip(node);
  refreshFolds();
}
// A card: a header (what, status, time, optional "Open"), then the markdown body. Long bodies fold.
function card({ label, body, className = "", status, task = "", time, open }) {
  const content = el("div", { class: "body markdown", html: markdown(body) });
  const node = el(
    "div",
    { class: `card ${className}` },
    el(
      "div",
      { class: "head" },
      el("span", { class: "label", text: label }),
      chip(status, task),
      el("span", { class: "spacer" }),
      time ? el("time", { text: clock(time), title: new Date(time).toLocaleString() }) : null,
      open ? el("button", { type: "button", class: "open", text: "Open", onclick: open }) : null,
    ),
    content,
  );
  if (body.length > 900 || body.split("\n").length > 14) {
    content.classList.add("folded");
    const toggle = el("button", { type: "button", class: "more", text: "Show more" });
    toggle.addEventListener("click", () => {
      const folded = content.classList.toggle("folded");
      toggle.textContent = folded ? "Show more" : "Show less";
    });
    node.append(toggle);
  }
  return node;
}
// One line in the Home chat: a mark, what happened, a link to where it is; with `body` (or
// `content`, drawn already), it opens in place to read it all.
function eventLine({ mark = "·", kind = "", text, time, view = null, viewText = "View", body = "", content = null }) {
  const parts = [
    el("span", { class: "mark", text: mark }),
    el("span", { class: "what", text, title: text }),
    view ? el("button", { type: "button", class: "link", text: viewText, onclick: (event) => { event.preventDefault(); event.stopPropagation(); view(); } }) : null,
    time ? el("time", { text: clock(time), title: new Date(time).toLocaleString() }) : null,
  ];
  if (!body && !content) return el("div", { class: `event ${kind}`.trim() }, ...parts);
  // Its line opens it; its View is beside that toggle, not in it (no button in a button).
  if (content) content.hidden = true;
  else content = el("div", { class: "markdown", html: markdown(body), hidden: true });
  const node = el("div", { class: `event opens ${kind}`.trim() });
  const toggle = el("button", { type: "button", class: "line", "aria-expanded": "false", onclick: () => {
    content.hidden = !content.hidden;
    node.classList.toggle("open", !content.hidden);
    toggle.setAttribute("aria-expanded", String(!content.hidden));
  } }, parts[0], parts[1]);
  node.append(el("div", { class: "row" }, toggle, ...parts.slice(2)), content);
  return node;
}
// What a worker's report says, in a word: the report itself is on the task's card. Blocked, it
// could not go on (not "Slack is blocked", which reads as if the connection broke).
const REPORTED = {
  done: ["✓", "done", "finished"],
  question: ["?", "waiting", "has a question"],
  blocked: ["!", "waiting", "couldn't go on"],
  failed: ["✗", "failed", "failed"],
  progress: ["…", "", "reported progress"],
};
// Reports whose line says what they came to (or why not), and opens to the whole report.
const SAYS_WHY = new Set(["done", "blocked", "failed"]);
// An answer the owner asked for (or the Router, for them) stands apart from routine lines: a
// small Answer label, its worker's short name (not its task's title, cut short), its words.
function answerLine(id, title = "", said, line) {
  const node = eventLine({ mark: "↩", kind: "answer", text: `${labelOf(id, title.replace(/^“|”$/g, ""))} · ${said}`, ...line });
  node.querySelector(".what").before(el("span", { class: "chip answer", text: "Answer" }));
  return node;
}
// Where something that reached the Router came from: a watch's finding is the watch's, whatever
// source it found it in.
const SOURCES = { slack: "Slack", github: "GitHub", watch: "a watch" };
// Slack's text with names for its ids (from Slack, a thread followed, a watch finding there, the
// Slack session or what was sent to it).
const SENDERS = ["from", "told_by", "asked_by", "answered_by", "target", "session_id"];
const slackish = (meta) => meta.source === "slack" || meta.source === "watch" || meta.thread?.startsWith("slack:") || SENDERS.some((key) => meta[key] && byId(meta[key])?.role === "slack");
const readable = (meta, text) => (slackish(meta) ? slackText(text) : text);
// The Slack session's own timeline reads as Slack does, whoever wrote it (the Router, the session
// itself): names for ids, no message stamps. Its steps stay as they ran.
const asShown = (message) => (message.kind !== "tool" && byId(message.session_id)?.role === "slack" ? { ...message, text: slackText(message.text) } : message);
// Home's conversation is the owner and the Router. Everything else that reaches the Router is
// one line: a task's news points at its card beside the chat; the rest opens in place.
function homeEntry(message) {
  const meta = message.meta || {};
  const time = message.created_at;
  const text = ["inbound", "ask", "answer"].includes(message.kind) ? readable(meta, message.text || "") : message.text || "";
  switch (message.kind) {
    case "inbound": {
      if (meta.from && meta.status?.startsWith("watchdog")) {
        return eventLine({ mark: "⚠", kind: "waiting", text: `Watchdog: “${meta.title}” looks ${meta.status.replace("watchdog: ", "").replace("_", " ")}`, time, body: text });
      }
      if (meta.from && meta.status) {
        if (meta.status === "answer") return answerLine(meta.from, meta.title, firstLine(text), { time, body: text, view: () => focusTask(meta.task_id, meta.from) });
        const [mark, kind, what] = REPORTED[meta.status] || ["·", "", meta.status];
        // What it found, or why it could not go on, in a line (the whole report opens here;
        // its card has the receipts).
        const said = SAYS_WHY.has(meta.status) ? gist(text) : "";
        return eventLine({ mark, kind, text: `${meta.title} ${what}${said ? `: ${said}` : ""}`, time, body: said ? text : "", view: () => focusTask(meta.task_id, meta.from) });
      }
      if (meta.asked_by) return eventLine({ mark: "?", text: `Question from “${meta.title}”: ${firstLine(text)}`, time, body: text, view: () => select(meta.asked_by), viewText: "Open" });
      if (meta.answered_by) return answerLine(meta.answered_by, meta.title, firstLine(text), { time, body: text, view: () => select(meta.answered_by), viewText: "Open" });
      if (meta.review) return eventLine({ mark: "·", text: `Review · ${meta.title}: ${firstLine(text)}`, time, body: text, view: () => select(meta.review), viewText: "Open" });
      if (meta.thread) return eventLine({ mark: "↘", text: `${threadLabel(meta.thread)}: ${firstLine(text)}`, time, body: text });
      if (meta.source === "capture") return sentEntry(meta, text, time);
      const source = SOURCES[meta.source] || "Event";
      const more = meta.count > 1 ? ` (+${meta.count - 1} more)` : "";
      return eventLine({ mark: "↘", text: `${meta.delegated ? `Handed over from ${source}` : `From ${source}`}: ${firstLine(text)}${more}`, time, body: text });
    }
    case "ask":
      return eventLine({ mark: "→", text: `Asked ${meta.title}: ${firstLine(text)}`, time, body: text, view: () => select(meta.target), viewText: "Open" });
    case "peer": {
      // What workers that work together told each other: the Router's copy, in a line by their
      // short names (a run of them folds: see foldInto).
      const peer = peerOf(message);
      return eventLine({ mark: "↔", text: peerLine(peer), time, body: peer.text, view: () => openThread(peer.to), viewText: "Open" });
    }
    case "watchdog": {
      const verdict = { stuck: "looks stuck", looping: "looks like it is looping", off_track: "looks off track" }[meta.verdict];
      return eventLine({ mark: "⚠", kind: "waiting", text: `Watchdog${verdict ? ` · ${verdict}` : ""}: ${firstLine(text)}`, time, body: `${text}${meta.reason ? `\n\nWhy it stepped in: ${meta.reason}` : ""}` });
    }
    case "answer":
      // A worker's answer to what the owner wrote it on Home (@): there too, in a line.
      return answerLine(meta.session_id, meta.from_title, gist(text) || firstLine(text), { time, body: text, view: () => select(meta.session_id, meta.task_id ? { task: meta.task_id } : {}), viewText: "Open" });
    case "update":
      if (meta.digest) return null;
      return eventLine({ mark: meta.needs_action || meta.urgent ? "!" : "·", kind: meta.needs_action || meta.urgent ? "waiting" : "", text: firstLine(text), time, body: text.includes("\n") || text.length > 140 ? text : "" });
    case "draft": {
      const node = eventLine({ mark: "✎", text: `${meta.label || "Draft"}: ${firstLine(text)}`, time, body: text });
      node.classList.add("draft-record");
      node.dataset.draftId = meta.draft_id;
      node.dataset.label = meta.label || "Draft";
      return node;
    }
    case "shared":
      // A card someone shared, opened here from its link (share.js): never the Router's.
      return sharedEntry(message, time);
    case "notice": {
      if (meta.settings) return eventLine({ mark: "✦", text, time, view: () => openSettings(meta.settings), viewText: "Open Settings" });
      if (meta.away_reply) return eventLine({ mark: "↩", text, time });
      if (meta.agent_refused) return refusalCard(text, meta, time);
      if (meta.role_wait) return roleWaitCard(text, meta, time);
      // "Started “X” · new because …": the why is on hover (a " · " in the title is the title's).
      // A second worker started from a busy one is a line of its own, marked ⑂ (E148).
      const started = /^(Started|New task|Continued|Restarted)/.test(text);
      const node = eventLine({ mark: meta.fork ? "⑂" : started ? "" : "·", kind: meta.fork ? "fork" : started ? "started" : "", text: headOf(text), time, view: meta.session_id ? () => focusTask(meta.task_id || "", meta.session_id) : null });
      node.querySelector(".what").title = text;
      // Its agent (while more than one is on): Restart on another before its first step.
      const chip = /^(Started|Queued|Restarted)/.test(text) && meta.session_id ? agentChip(meta.session_id) : null;
      if (chip) node.querySelector(".what").after(chip);
      return node;
    }
    default:
      return null;
  }
}
// Sent with Send to Chief: one line, cut as the others are (in full in its tooltip): the note's
// first line, else what it passed along. It opens to the note only when the line does not show
// all of it, then the link, the page's title and the text passed along, each drawn as words
// (the text as it was, in a code block): nothing passed along shows as formatting or a link. A
// note alone opens only while its line cuts it short.
function sentEntry(meta, text, time) {
  const line = `Sent from the ${meta.client || "command line"}: ${meta.line || firstLine(text)}`;
  if (!meta.sent) return eventLine({ mark: "↘", kind: "sent", text: line, time, body: text });
  const { note = "", link = "", title = "", text: passed = "" } = meta.sent;
  const whole = !note.includes("\n") && note === meta.line;
  const alone = !link && !title && !passed;
  const field = (label, ...value) => el("p", {}, el("strong", { class: "key", text: `${label}:` }), " ", ...value);
  const said = note ? el("p", { class: "sent-note", text: note }) : null;
  const content = el(
    "div",
    { class: "markdown sent" },
    said,
    link ? field("Link", /^https?:\/\//i.test(link) ? el("a", { href: link, title: link, target: "_blank", rel: "noreferrer", text: linkLabel(link) }) : link) : null,
    title ? field("Title", title) : null,
    passed ? el("pre", {}, el("code", { text: passed })) : null,
  );
  const node = eventLine({ mark: "↘", kind: "sent", text: line, time, content });
  if (!said || !whole) return node;
  // Its note shows only while the line cuts it short: measured as it opens, and as its width
  // changes while open. A note alone is all it opens to, so it opens only then: measured from
  // the start, and folded again once the line shows it whole.
  const what = node.querySelector(".what");
  const toggle = node.querySelector("button.line");
  let watching = null;
  const fit = () => {
    const cut = what.scrollWidth > what.clientWidth;
    said.hidden = !content.hidden && !cut;
    if (!alone) return;
    if (!cut && !content.hidden) toggle.click();
    toggle.disabled = !cut;
  };
  const watch = () => {
    watching ||= new window.ResizeObserver(() => requestAnimationFrame(fit));
    watching.observe(what);
  };
  toggle.addEventListener("click", () => {
    fit();
    watch();
  });
  if (alone) {
    toggle.disabled = true;
    watch();
  }
  return node;
}
// A worker's chat has tabs (E130): Work (all of it, what it and the workers it works with tell
// each other in a line each), Team when its task is a part of a team (Notes otherwise: those in
// full, with a way to its team), Tools (its steps) and Reports. They show parts of the same chat.
const WORKER_TABS = { work: "Work", team: "Team", tools: "Tools", reports: "Reports" };
const TAB_PARTS = { team: ".peer-msg", tools: "details.steps", reports: '[data-kind="report"]' };
const TAB_EMPTY = { team: "Nothing told or asked between it and the workers it works with yet.", tools: "No steps yet.", reports: "No reports yet." };
let workerTab = { session: "", tab: "work", opened: [] };
function setWorkerTab(sessionId, tab) {
  const timeline = $("#timeline");
  // Its steps open on Tools; as they were on leaving it.
  if (workerTab.tab === "tools" && tab !== "tools") for (const group of workerTab.opened) group.open = false;
  workerTab = { session: sessionId, tab, opened: [] };
  if (tab === "work") delete timeline.dataset.tab;
  else timeline.dataset.tab = tab;
  if (tab === "tools") {
    workerTab.opened = [...timeline.querySelectorAll(":scope > details.steps:not([open])")];
    for (const group of workerTab.opened) group.open = true;
  }
  // Team: its current task's lineage at the top (E130).
  if (tab === "team") workerLineage(sessionId);
  updateWorkerTabs();
}
function renderWorkerTabs(session) {
  const nav = $("#worker-tabs");
  if (session?.role !== "worker") {
    nav.hidden = true;
    return;
  }
  nav.hidden = false;
  updateWorkerTabs();
}
// Its tab bar as the chat is now: Team (N) with N what is there, its team beside it.
function updateWorkerTabs() {
  const nav = $("#worker-tabs");
  const session = current();
  if (nav.hidden || session?.role !== "worker") return;
  const timeline = $("#timeline");
  const told = timeline.querySelectorAll(":scope > .peer-msg").length;
  const team = teamOfWorker(session.id)?.team;
  // Its exchanges, and its task's lineage (when its task has relations), as before.
  const tabs = Object.entries(WORKER_TABS).filter(([tab]) => tab !== "team" || team || told || session.team).map(([tab, label]) => [tab, tab === "team" && !team ? "Notes" : label]);
  const key = JSON.stringify([session.id, workerTab.tab, told, team?.id || "", tabs]);
  if (nav.dataset.key !== key) {
    nav.dataset.key = key;
    nav.replaceChildren(
      el(
        "div",
        { class: "tabs", role: "tablist", "aria-label": "Parts of its chat" },
        ...tabs.map(([tab, label]) =>
          el("button", { type: "button", role: "tab", "data-tab": tab, "aria-selected": String(workerTab.tab === tab), onclick: () => setWorkerTab(session.id, tab) }, label, tab === "team" && told ? el("span", { class: "count", text: String(told) }) : null),
        ),
      ),
      team ? el("button", { type: "button", class: "link team-link", text: "Open team", title: `${team.goal}: its parts and what they handed each other`, onclick: () => showTeam(team.id) }) : "",
    );
  }
  // What a tab with nothing there says.
  timeline.querySelector(":scope > .tab-empty")?.remove();
  const part = TAB_PARTS[workerTab.tab];
  if (part && !timeline.querySelector(`:scope > ${part}`)) timeline.append(el("p", { class: "tab-empty empty-hint", text: TAB_EMPTY[workerTab.tab] }));
}
// What it and a worker it works with told or asked each other: a line in its work (it opens to
// the words), the card in full on its Team tab.
function peerLineOf(message) {
  const meta = message.meta || {};
  const text = message.text || "";
  const worker = (id) => byId(id)?.role === "worker" || closedWorker(id);
  if (message.kind === "tell") return { id: meta.target, mark: "↔", words: `Told ${labelOf(meta.target)}`, card: `Told ${labelOf(meta.target)}`, text };
  if (message.kind === "ask" && worker(meta.target)) return { id: meta.target, mark: "?", words: `Asked ${labelOf(meta.target)}`, card: `Asked ${labelOf(meta.target)}`, text };
  if (message.kind !== "inbound") return null;
  if (meta.told_by) {
    // A result it finished: its first line names the task, the rest is the result.
    const body = meta.finished ? text.split("\n").slice(1).join("\n") || text : text;
    const name = labelOf(meta.told_by);
    return { id: meta.told_by, mark: meta.finished ? "✓" : "↔", words: `${name}${meta.finished ? " finished" : ""}`, card: meta.finished ? `${name} finished` : `From ${name}`, text: body };
  }
  if (meta.asked_by && worker(meta.asked_by)) return { id: meta.asked_by, mark: "?", words: `${labelOf(meta.asked_by)} asks`, card: `Question from ${labelOf(meta.asked_by)}`, text };
  if (meta.answered_by && worker(meta.answered_by)) return { id: meta.answered_by, mark: "↩", words: `${labelOf(meta.answered_by)} answered`, card: `Answer from ${labelOf(meta.answered_by)}`, text };
  return null;
}
const closedWorker = (id) => (state.snapshot?.closed || []).some((item) => item.id === id);
function peerMessage(message, full) {
  const found = peerLineOf(message);
  if (!found) return full;
  const line = eventLine({ mark: found.mark, text: `${found.words}: ${firstLine(found.text)}`, time: message.created_at, body: found.text });
  line.querySelector(".what").before(avatar(found.id));
  // Its card by the same short name, with the worker's avatar (its whole title on hover).
  const label = full.querySelector(":scope > .head .label");
  if (label) {
    label.title = label.textContent;
    label.textContent = found.card;
    label.before(avatar(found.id));
  }
  return el("div", { class: "peer-msg" }, line, full);
}
function renderMessage(original) {
  const node = renderOne(original);
  return state.selected !== HOME && current()?.role === "worker" ? peerMessage(asShown(original), node) : node;
}
function renderOne(original) {
  const message = asShown(original);
  const meta = message.meta || {};
  const time = message.created_at;
  if (state.selected === HOME && message.session_id === routerId()) {
    const entry = homeEntry(message);
    if (entry) return entry;
  }
  switch (message.kind) {
    case "owner":
      return ownerMessage(message.text, meta, time, message.delivery, message.id);
    case "assistant":
      if (meta.internal) return el("div", { class: "line note", text: message.text, title: clock(time) });
      return el("div", { class: "msg agent markdown", html: markdown(message.text), title: clock(time) });
    case "router":
      return card({ label: "From the Router", body: message.text, className: "router", time });
    case "ask":
      return card({ label: `Asked ${meta.title}`, body: message.text, className: "ask", time, open: () => select(meta.target) });
    case "tell":
      return card({ label: `Told “${meta.title}”`, body: message.text, className: "ask", time, open: () => select(meta.target) });
    case "peer":
      return el("div", { class: "line notice link", title: clock(time), onclick: () => select(meta.peer_to) }, el("span", { text: message.text }));
    case "inbound": {
      if (meta.told_by) {
        return card({ label: meta.finished ? `“${meta.title}” finished` : `From “${meta.title}”`, body: message.text, className: "inbound", time, open: () => select(meta.told_by) });
      }
      if (meta.asked_by) {
        return card({ label: `Question from “${meta.title}”`, body: message.text, className: "inbound", time, open: () => select(meta.asked_by) });
      }
      if (meta.answered_by) {
        return card({ label: `Answer from ${meta.title}`, body: message.text, className: "inbound answer", time, open: () => select(meta.answered_by) });
      }
      if (meta.review) {
        return card({ label: `Review · ${meta.title}`, body: message.text, className: "inbound", time, open: () => select(meta.review) });
      }
      if (meta.thread) return card({ label: threadLabel(meta.thread), body: readable(meta, message.text), className: "inbound", time });
      // Another Claude session's message to a Claude worker: its sender's, never the owner's.
      if (meta.claude_session) return card({ label: `From Claude session “${meta.claude_session}”`, body: message.text, className: "inbound", time });
      if (meta.from) {
        return card({ label: meta.title, body: message.text, className: `report ${meta.status}`, status: meta.status, task: meta.task_id, time, open: () => select(meta.from) });
      }
      if (meta.source === "capture") return card({ label: `You sent this from the ${meta.client || "command line"}`, body: message.text, className: "inbound", time });
      const source = SOURCES[meta.source] || "Event";
      return card({ label: meta.delegated ? `You handed this over from ${source}` : `From ${source}`, body: readable(meta, message.text), className: "inbound", time });
    }
    case "watchdog": {
      const verdict = { stuck: "looks stuck", looping: "looks like it is looping", off_track: "looks off track" }[meta.verdict];
      const node = card({ label: verdict ? `Watchdog · ${verdict}` : "Watchdog", body: message.text, className: "update attention", time });
      if (meta.reason) node.append(el("div", { class: "why-line", text: `Why it stepped in: ${meta.reason}` }));
      return node;
    }
    case "update": {
      if (meta.digest) {
        return card({ label: "Daily summary", body: message.text.replace(/^Daily summary\n/, ""), className: "update", time });
      }
      const label = meta.needs_action ? "Needs you" : "Update";
      return card({ label, body: message.text, className: meta.needs_action ? "update attention" : "update", time });
    }
    case "report":
      return card({ label: "Report", body: message.text, className: `report ${meta.status}`, status: meta.status, task: meta.task_id, time });
    case "answer":
      return card({ label: `Answer from “${meta.from_title}”`, body: message.text, className: "inbound answer", time, open: () => select(meta.session_id) });
    case "draft": {
      const node = card({ label: meta.label || "Draft", body: message.text, className: "draft-record", time });
      node.dataset.draftId = meta.draft_id;
      node.dataset.label = meta.label || "Draft";
      return node;
    }
    case "notice": {
      const open = meta.session_id ? () => select(meta.session_id) : undefined;
      // What Chief of Staff learned or changed on its own: where to see it in Settings.
      // Agent settings in a worker's folder that changed: the owner's to use (theirs, say).
      const settings = meta.settings
        ? el("button", { type: "button", class: "link", text: "Open Settings", onclick: (event) => { event.stopPropagation(); openSettings(meta.settings); } })
        : meta.agent_settings || meta.agent_servers
          ? useSettingsButton(message.session_id, meta.agent_settings || meta.agent_servers)
          : meta.settings_undo
            ? undoSettingsButton(message.session_id, meta.settings_undo.digest)
          : meta.git_quarantine
            ? quarantineButtons(message.session_id, meta.git_quarantine.removable)
            : meta.git_replaced
              ? useGitButton(message.session_id, meta.git_replaced.digest)
              : null;
      // A stop its task's card says more of (another agent's spend): that, in full, on hover.
      const title = meta.full ? `${meta.full}\n${clock(time)}` : clock(time);
      // Handled for the owner (E123, a folder picked): Change, while it can still be.
      const handled = meta.handled && (state.snapshot?.handled || []).some((entry) => entry.id === meta.handled.id && entry.status === "done") ? handledButton(meta.handled) : null;
      return el("div", { class: `line notice${open ? " link" : ""}${settings || handled ? " wide" : ""}`, title, onclick: open }, el("span", { text: message.text }), settings, handled);
    }
    case "error":
      // Sent after all (on a retry, or once Copilot was back): no longer an error.
      if (meta.fresh && !meta.resolved) return el("div", { class: "line error", title: clock(time) }, el("span", { text: message.text }), " ", freshButton(message.session_id));
      return el("div", { class: meta.resolved ? "line note" : "line error", text: message.text, title: clock(time) });
    default:
      return el("div", { class: "line", text: message.text, title: clock(time) });
  }
}
// A worker the owner named an agent for that could not start on it: why, and what they can do
// (nothing starts elsewhere unasked). Held for a sign-in or an install, it says the message is
// saved: Sign in (or Install) and it goes on by itself ("Resuming…", with Cancel), or Continue
// with Copilot, or Don't run it. Its actions go once answered; how it ended is said under it.
const SIGN_IN_HELP = "https://github.com/evanl666/chiefofstaff/blob/main/docs/guide.md#workers-on-other-coding-agents";
function refusalCard(text, meta, time) {
  const open = (state.snapshot.agent_refusals || []).includes(meta.agent_refused);
  const node = el("div", { class: "card agent-refused", title: clock(time) }, el("p", {}, meta.agent ? agentMark(meta.agent) : "", ` ${text}`));
  if (meta.resumed) {
    node.append(el("p", { class: "card-outcome", role: "status", text: meta.resumed }));
    return node;
  }
  if (!open) return node;
  // One answer per card: every action waits while one runs.
  const answer = async (action) => {
    const buttons = [...node.querySelectorAll(".actions button")];
    buttons.forEach((each) => (each.disabled = true));
    try {
      const fresh = await api(`/api/agent-refusals/${meta.agent_refused}`, { method: "POST", body: { action } });
      // (Its key is absent once none is open: not kept from before.)
      state.snapshot = { ...state.snapshot, ...fresh, agent_refusals: fresh.agent_refusals };
      if (!fresh.agent_refusals?.includes(meta.agent_refused)) node.querySelector(".actions")?.remove();
    } catch (error) {
      buttons.forEach((each) => (each.disabled = false));
      toast(error.message, "error");
    }
  };
  const name = agentName(meta.agent);
  if (meta.resuming) {
    // Its agent can run it now: it goes on in a moment, unless Cancel keeps it for the owner.
    const keep = el("button", { type: "button", class: "text", text: "Cancel", title: "Keep it here: start it yourself when you like" });
    keep.onclick = () => answer("keep");
    node.append(el("div", { class: "actions" }, el("span", { class: "resuming", role: "status" }, el("span", { class: "spinner", "aria-hidden": "true" }), `Resuming on ${name}…`), el("span", { class: "spacer" }), keep));
    return node;
  }
  // The agent it is offered on instead: the one new workers start on, else Copilot. For a worker
  // of theirs whose agent could not run, its work goes on there, with what it was given.
  const instead = agentName(meta.instead || "copilot");
  // Held for a sign-in, an install, a limit's reset or Chief of Staff's check of the agent (tried
  // again by itself): it goes on by itself after.
  const held = ["signin", "missing", "limited", "unchecked"].includes(meta.status);
  const copilot = el("button", {
    type: "button",
    class: "secondary",
    text: meta.worker || held ? `Continue with ${instead}` : `Run on ${instead} instead`,
    title: meta.worker ? `A new worker on ${instead} goes on from a summary of this one's work, with what it was given; this one stays open` : undefined,
  });
  copilot.onclick = () => answer("copilot");
  if (held) {
    const drop = el("button", { type: "button", class: "text", text: "Don't run it" });
    drop.onclick = () => answer("wait");
    const status = statusOf(meta.agent);
    let first = null;
    if (meta.kept || ready(status)) {
      first = el("button", { type: "button", text: meta.worker ? `Send it on ${name}` : `Start on ${name}` });
      first.onclick = () => answer("now");
    } else {
      first = setupAction(status || { name: meta.agent, status: meta.status });
    }
    node.append(el("div", { class: "actions" }, first || (meta.fix ? fixLine(meta.fix) : ""), el("span", { class: "spacer" }), drop, copilot));
    return node;
  }
  const wait = el("button", { type: "button", class: "text", text: "Wait" });
  wait.onclick = () => answer("wait");
  // The command that makes it ready, the owner's to run (else a link to the guide).
  const fix = meta.fix ? fixLine(meta.fix) : null;
  node.append(el("div", { class: "actions" }, fix || el("a", { class: "link", href: SIGN_IN_HELP, target: "_blank", rel: "noreferrer", text: "Sign-in help" }), el("span", { class: "spacer" }), wait, copilot));
  return node;
}
// A role waiting for its agent (Settings: If unavailable, Ask me): Sign in (or Install), or Use
// Copilot until then; what it was sent meanwhile goes to it after.
function roleWaitCard(text, meta, time) {
  const node = el("div", { class: "card agent-refused role-wait", title: clock(time) }, el("p", {}, agentMark(meta.agent), ` ${text}`));
  if (meta.resumed) {
    node.append(el("p", { class: "card-outcome", role: "status", text: meta.resumed }));
    return node;
  }
  if (!(state.snapshot.role_waits || []).includes(meta.role_wait)) return node;
  const copilot = el("button", { type: "button", class: "secondary", text: "Use Copilot until then" });
  copilot.onclick = async () => {
    copilot.disabled = true;
    try {
      state.snapshot = { ...state.snapshot, ...(await api(`/api/role-waits/${meta.role_wait}`, { method: "POST" })) };
      node.querySelector(".actions")?.remove();
    } catch (error) {
      copilot.disabled = false;
      toast(error.message, "error");
    }
  };
  const first = setupAction(statusOf(meta.agent) || { name: meta.agent, status: meta.status });
  node.append(el("div", { class: "actions" }, first || (meta.fix ? fixLine(meta.fix) : ""), el("span", { class: "spacer" }), copilot));
  return node;
}
// "Use these settings": what changed is in its line; the worker goes on with them.
function useSettingsButton(sessionId, { digest, changes }, className = "link") {
  const button = el("button", { type: "button", class: className, text: "Use these settings", title: changes, onclick: async (event) => {
    event.stopPropagation();
    button.disabled = true;
    try {
      const { said } = await api(`/api/sessions/${sessionId}/agent-settings`, { method: "POST", body: { digest } });
      toast(said);
    } catch (error) {
      toast(error.message, "error");
      button.disabled = false;
    }
  } });
  return button;
}
function undoSettingsButton(sessionId, digest, className = "link") {
  const button = el("button", { type: "button", class: className, text: "Undo", title: "Trust the earlier settings again only if these bytes have not changed", onclick: async (event) => {
    event.stopPropagation();
    button.disabled = true;
    try {
      const { said } = await api(`/api/sessions/${sessionId}/agent-settings/undo`, { method: "POST", body: { digest } });
      toast(said);
    } catch (error) {
      toast(error.message, "error");
      button.disabled = false;
    }
  } });
  return button;
}
// A .git a worker's folder had replaced: the owner says to use it as it is; the worker goes on.
function useGitButton(sessionId, digest) {
  const button = el("button", { type: "button", class: "link", text: "Use this .git", onclick: async (event) => {
    event.stopPropagation();
    button.disabled = true;
    try {
      const { said } = await api(`/api/sessions/${sessionId}/git-identity`, { method: "POST", body: { digest } });
      toast(said);
    } catch (error) {
      toast(error.message, "error");
      button.disabled = false;
    }
  } });
  return button;
}
// Undo (Change, for a folder picked) of what was handled for the owner: as under Needs you.
function handledButton({ id, undo }) {
  const button = el("button", { type: "button", class: "link", text: undo || "Undo", onclick: async (event) => {
    event.stopPropagation();
    button.disabled = true;
    try {
      const { said } = await api(`/api/handled/${id}/undo`, { method: "POST" });
      toast(said);
      button.remove();
    } catch (error) {
      toast(error.message, "error");
      button.disabled = false;
    }
  } });
  return button;
}
// A git file that appeared in a .git a worker can write: used, or (in Chief of Staff's own
// worktree folders) kept removed; the worker goes on.
function quarantineButtons(sessionId, removable) {
  const answer = (use) => async (event) => {
    event.stopPropagation();
    buttons.forEach((button) => (button.disabled = true));
    try {
      const { said } = await api(`/api/sessions/${sessionId}/git-quarantine`, { method: "POST", body: { use } });
      toast(said);
    } catch (error) {
      toast(error.message, "error");
      buttons.forEach((button) => (button.disabled = false));
    }
  };
  const buttons = [el("button", { type: "button", class: "link", text: "Use it", onclick: answer(true) })];
  if (removable) buttons.push(el("button", { type: "button", class: "link", text: "Keep it removed", onclick: answer(false) }));
  return buttons;
}
// The owner's message: long ones fold, images show small and open full size; under it, whether
// it reached its session (`delivery`), in a word.
function ownerMessage(text, meta = {}, time = "", delivery = "", messageId = 0) {
  const node = el("div", { class: "msg owner", title: time ? clock(time) : undefined });
  // The worker it went to (Home's copy says which): why it waits, if it does, is that worker's.
  const worker = meta.to || (state.selected !== HOME ? viewedId() : "");
  if (worker) node.dataset.worker = worker;
  // Sent from Home straight to one worker.
  if (meta.to) node.append(el("button", { type: "button", class: "to", text: `→ ${meta.to_title}`, title: "Open this worker", onclick: () => select(meta.to) }));
  // The agent chosen for it with @ (its work runs there).
  if (meta.agents?.length) node.append(el("span", { class: "to", text: `Compare on ${meta.agents.map(agentName).join(" and ")}` }));
  else if (meta.agent) node.append(el("span", { class: "to", text: `On ${[agentName(meta.agent), modelLabel(meta.agent, meta.agent_model)].filter(Boolean).join(" · ")}` }));
  // With a card someone shared, quoted as information.
  if (meta.shared_card?.title) node.append(el("span", { class: "to", text: `With @${meta.shared_card.login}'s card “${meta.shared_card.title}”` }));
  const files = meta.attachments || [];
  if (files.length) {
    node.append(
      el(
        "div",
        { class: "attachments" },
        ...files.map(attachmentLink),
      ),
    );
  }
  if (text) foldable(node, el("div", { class: "text", text }), text);
  if (meta.settings_hold && messageId && WAITING.has(delivery)) node.append(settingsHoldCard(messageId, meta.settings_hold));
  else if (meta.settings_handled && meta.settings_handled.status !== "released") node.append(settingsHandledCard(meta.settings_handled));
  const [said, tone] = DELIVERY_WORDS[delivery] || [];
  if (said) {
    node.dataset.delivery = delivery;
    node.append(el("div", { class: `delivery ${tone || ""}`.trim(), text: said }));
  }
  return node;
}
// Under a message waiting with its worker for the owner's word on its folder's settings: what
// changed and the three ways on, each for the settings shown (their digest). Words such as "ok"
// never use them: a line says to use the button (bug E121).
function settingsHoldCard(messageId, hold) {
  const actions = el("div", { class: "hold-actions" });
  const run = (path, done) => async (event) => {
    event.stopPropagation();
    for (const button of actions.querySelectorAll("button")) button.disabled = true;
    try {
      const result = await api(path, { method: "POST", body: { digest: hold.digest } });
      toast(done);
      if (result.session_id && result.session_id !== hold.session_id) select(result.session_id);
    } catch (error) {
      toast(error.message, "error");
      for (const button of actions.querySelectorAll("button")) button.disabled = false;
    }
  };
  const sep = () => el("span", { class: "sep", text: " · " });
  actions.append(
    useSettingsButton(hold.session_id, hold),
    sep(),
    el("button", { type: "button", class: "link", text: "Start a new worker", title: "Its task goes on in a new worker, in a fresh checkout without these settings", onclick: run(`/api/messages/${messageId}/settings-hold/new-worker`, "Started a new worker for it.") }),
    sep(),
    el("button", { type: "button", class: "link", text: "Cancel message", title: "Don't send this message", onclick: run(`/api/messages/${messageId}/settings-hold/cancel`, "Not sent.") }),
  );
  return el(
    "div",
    { class: "settings-hold" },
    el("div", { class: "hold-change", text: `Not sent yet: its folder's settings changed${hold.changes ? ` (${hold.changes})` : ""}`, title: hold.reason }),
    actions,
    hold.guidance ? el("div", { class: "hold-guidance", text: hold.guidance }) : "",
  );
}
// What became of it, in a word, once that wait is over.
function settingsHandledCard(handled) {
  return handled.text ? el("div", { class: `settings-hold handled ${handled.status || ""}`, text: handled.text }) : "";
}
// Where the owner's messages stand, under each: what is not there yet (or never got there)
// under every one; that one got there or was handled, only under the latest. The latest one that
// waits says why, and what to do about it, where there is something (bug QA6-1: it said
// "Waiting to be sent" and nothing more).
const SETTLED = new Set(["received", "processed"]);
const WAITING = new Set(["queued", "held"]);
export function showDeliveries() {
  const nodes = [...$("#timeline").querySelectorAll(".msg.owner[data-delivery]")];
  const latest = [...$("#timeline").querySelectorAll(".msg.owner")].pop();
  const waits = new Map();
  for (const node of nodes) {
    node.querySelector(":scope > .delivery").hidden = SETTLED.has(node.dataset.delivery) && node !== latest;
    if (WAITING.has(node.dataset.delivery) && node.dataset.worker) waits.set(node.dataset.worker, node);
  }
  for (const node of nodes) waitNote(node, waits.get(node.dataset.worker) === node);
}
// Why a message to a worker waits, in a few words, and Send now when it can go at once: its
// worker was stopped (it holds it for its next message), or every worker slot is taken. Not
// what keeps it waiting for a reason (memory, disk space, a task it waits for): that is said
// (its details on hover).
function waitWhy(node) {
  const session = byId(node.dataset.worker);
  if (!session || session.role !== "worker" || session.closed_at) return null;
  // Waiting for your word (its folder's settings changed): its timeline says what to do.
  if (WAITING.has(node.dataset.delivery) && /^waiting for you\b/i.test(session.activity || "")) return { text: session.activity, help: session.activity, settings: session.settings_hold };
  if (node.dataset.delivery === "held") return { text: "you stopped this worker", help: "It gets this with your next message, or now with Send now", now: true };
  if (node.dataset.delivery !== "queued" || session.status !== "queued") return null;
  const activity = session.activity || "";
  // "waiting for memory: 12% free on this computer": the reason, its numbers on hover.
  if (/^waiting for (memory|disk space):/i.test(activity)) return { text: activity.split(":")[0], help: activity };
  if (/^waiting for /i.test(activity)) return { text: activity, help: activity };
  const reason = slotReason(session);
  return { text: "waiting for a free worker slot", help: `${reason.text.split(" · ").pop()}. ${reason.help}`, now: true };
}
function waitNote(node, latest) {
  const line = node.querySelector(":scope > .delivery");
  // Its own line on the settings it waits for says it all (settingsHoldCard).
  if (node.querySelector(":scope > .settings-hold:not(.handled)")) {
    line.hidden = true;
    return;
  }
  const why = latest ? waitWhy(node) : null;
  const key = why ? `${why.text}:${why.now}` : "";
  if ((line.dataset.why || "") === key) return;
  line.dataset.why = key;
  if (!why) {
    line.textContent = DELIVERY_WORDS[node.dataset.delivery]?.[0] || "";
    return;
  }
  const sendNow = async (event) => {
    event.stopPropagation();
    event.target.disabled = true;
    try {
      upsertSession(await api(`/api/sessions/${node.dataset.worker}/send-now`, { method: "POST" }));
    } catch (error) {
      event.target.disabled = false;
      toast(error.message, "error");
    }
  };
  // One short line: "Not sent yet: you stopped this worker · Send now".
  line.replaceChildren(
    el("span", { class: "why", text: `Not sent yet: ${why.text}`, title: why.help || undefined }),
    ...(why.settings ? [el("span", { class: "sep", text: " · " }), useSettingsButton(node.dataset.worker, why.settings)] : []),
    ...(why.now ? [el("span", { class: "sep", text: " · " }), el("button", { type: "button", class: "link", text: "Send now", title: "Send it to the worker now", onclick: sendNow })] : []),
  );
}
// The work between messages, folded into one line: its steps, and what the session said on the
// way to them ("Now the tests:"), which is narration, not an answer. The line shows the latest.
function addWork(container, kind, text, cwd) {
  let group = container.lastElementChild;
  if (!group?.classList.contains("steps")) {
    group = el("details", { class: "steps" }, el("summary"));
    container.append(group);
  }
  let latest;
  if (kind === "tool") {
    latest = compactPaths(text, cwd);
    group.append(el("div", { class: "step", text: latest, title: text }));
  } else {
    group.append(el("div", { class: "narration markdown", html: markdown(text) }));
    latest = text.split("\n").find((line) => line.trim()) || text;
    // Its words in a line, without markup (and past a lead-in like "Done:").
    group.dataset.said = gist(text) || firstLine(text);
  }
  group.dataset.latest = latest;
  summarize(group);
}
// A fold's line: how many steps, and the latest of them (or what it said on the way). At the top
// of a chat with earlier messages not loaded, more of its steps may come before: "386+ steps".
function summarize(group) {
  const count = group.querySelectorAll(".step").length;
  const said = group.dataset.said;
  const latest = group.dataset.latest || "";
  const more = group.dataset.partial ? "+" : "";
  group.querySelector("summary").replaceChildren(
    count ? el("span", { class: "count", text: `${count}${more} step${count === 1 && !more ? "" : "s"}`, title: more ? "More before these: load earlier messages" : undefined }) : "",
    el("span", { class: said ? "said" : "last", text: said || latest, title: said || latest }),
  );
}
// Narration: flagged as it arrives; in older timelines, a worker's message right before a step.
function narration(message, next, worker) {
  if (message.kind !== "assistant") return false;
  // The Router's remarks after events and reports: the cards are what matters to the owner.
  if (message.meta?.internal) return true;
  return Boolean(message.meta?.narration) || (worker && next?.kind === "tool");
}
// A session Chief of Staff does not drive, as read from its log: messages and folded work.
export function readOnlyTimeline(entries, cwd) {
  const box = el("div");
  for (const entry of entries) {
    if (entry.kind === "tool" || entry.kind === "narration") addWork(box, entry.kind, entry.text, cwd);
    else if (entry.kind === "owner") box.append(ownerMessage(entry.text));
    else box.append(el("div", { class: "msg agent markdown", html: markdown(entry.text) }));
  }
  return [...box.children];
}

// A report completed after it was shown (its worker wrote the answer in its chat).
export function replaceMessage(message) {
  const old = $("#timeline").querySelector(`[data-id="${message.id}"]`);
  const node = old && renderMessage(message);
  if (!node?.dataset) return;
  Object.assign(node.dataset, { ...old.dataset, id: message.id });
  // A line of a fold on Home (or one that may start one) stays what it was.
  Object.assign(node, { foldKind: old.foldKind, peer: old.peer, started: old.started });
  if (message.kind === "owner") {
    if (DELIVERY_WORDS[message.delivery]) node.dataset.delivery = message.delivery;
    else delete node.dataset.delivery;
  }
  old.replaceWith(node);
  showDeliveries();
}

export function appendMessages(messages) {
  const timeline = $("#timeline");
  timeline.querySelector(".empty")?.remove();
  timeline.querySelector(".working-line")?.remove();
  timeline.querySelector(":scope > .tab-empty")?.remove();
  const shown = state.selected === HOME ? byId(routerId()) : current();
  const viewed = viewedId();
  messages.forEach((message, index) => {
    if (message.id <= state.lastId || message.session_id !== viewed) return;
    state.lastId = message.id;
    place(timeline, message, messages[index + 1], shown);
  });
  // Nothing said yet: what to ask for (unless Copilot must be set up first).
  if (!timeline.children.length && state.selected === HOME && state.snapshot?.runtime.state !== "error") timeline.append(welcomeCard());
  updateWorkingLine();
  updateDraftRecords();
  showDeliveries();
  refreshFolds();
  updateWorkerTabs();
  scrollToBottom();
  followAnchor();
}
// One message at the end of `container`: a step (or narration) joins the fold before it; on
// Home, what workers tell each other and the Router's routing receipts join a run of their kind.
function place(container, message, next, shown) {
  if (message.kind === "tool" || narration(message, next, shown?.role === "worker")) {
    addWork(container, message.kind === "tool" ? "tool" : "narration", asShown(message).text, shown?.cwd);
    return;
  }
  const node = renderMessage(message);
  if (node?.dataset) {
    node.dataset.id = message.id;
    node.dataset.kind = message.kind;
    if (message.meta?.task_id) node.dataset.task = message.meta.task_id;
  }
  const kind = node?.dataset && foldOf(message);
  if (kind) foldInto(container, kind, node, message);
  else container.append(node);
}
// Home's chat is the owner's and the Router's (E129): a run of two or more lines of what workers
// told each other folds into one ("↔ 5 team messages"), as does a run of the Router's routing
// receipts ("4 routing updates"). One alone stays a line.
const ROUTING = /^(Started|Queued|New task|Continued|Told)\b/;
function foldOf(message) {
  if (state.selected !== HOME || message.session_id !== routerId()) return "";
  if (message.kind === "peer") return "team";
  // A second worker started from a busy one is never folded away (E148: it went unseen).
  return message.kind === "notice" && message.meta?.session_id && !message.meta.fork && ROUTING.test(message.text || "") ? "routing" : "";
}
// A line that may start a fold says its kind (not in the page: a line alone is as it was);
// a fold says it in data-fold.
const foldKind = (node) => node?.dataset?.fold || node?.foldKind || "";
// A start line in a run of routing receipts: the worker it started (its mark on the fold's line).
const STARTS = /^(Started|Queued|Restarted)\b/;
function foldInto(container, kind, node, message) {
  node.foldKind = kind;
  if (kind === "team") node.peer = peerOf(message);
  if (kind === "routing" && message.meta?.session_id && STARTS.test(message.text || "")) node.started = message.meta.session_id;
  const last = container.lastElementChild;
  if (foldKind(last) !== kind) {
    container.append(node);
    return;
  }
  const group = last.dataset.fold ? last : wrapFold(last, kind);
  group.lastElementChild.append(node);
  summarizeFold(group);
}
// The second of a run: the first line goes into a fold, in its place.
function wrapFold(first, kind) {
  const group = el("div", { class: `fold ${kind}`, "data-fold": kind }, el("div", { class: "event opens fold-line" }), el("div", { class: "fold-items" }));
  first.replaceWith(group);
  group.lastElementChild.append(first);
  return group;
}
const FOLDS = { team: ["↔", "team message"], routing: ["·", "routing update"] };
// A fold's line: how many, who (a team's workers by their avatars), what still waits, and the
// time of the latest; open, its lines (a team's last three: its thread has them all).
function summarizeFold(group) {
  const items = [...group.querySelector(":scope > .fold-items").children];
  const line = group.querySelector(":scope > .fold-line");
  const kind = group.dataset.fold;
  const [mark, noun] = FOLDS[kind];
  const peers = items.map((node) => node.peer).filter(Boolean);
  const ids = [...new Set(peers.flatMap((peer) => [peer.from, peer.to]).filter(Boolean))];
  const waiting = kind === "team" ? waitingWords(new Set(peers.filter((peer) => peer.kind === "asked").map((peer) => peer.question).filter(Boolean))) : "";
  // The workers a run of start lines started: each one's agent mark, where marks show (more
  // than one agent on), its agent and model on hover.
  const started = [...new Set(items.map((node) => node.started).filter(Boolean))].map((id) => byId(id) || (state.snapshot?.closed || []).find((item) => item.id === id)).filter(Boolean);
  const marks = started.map((worker) => markOf(worker)).filter(Boolean);
  const team = kind === "team" ? teamOfPeers(peers) : null;
  const key = `${items.length}|${ids.join()}|${waiting}|${marks.map((mark) => mark.title).join()}|${team?.goal || ""}`;
  if (line.dataset.key === key) return;
  line.dataset.key = key;
  const titles = new Map(peers.flatMap((peer) => [[peer.from, peer.fromTitle], [peer.to, peer.toTitle]]));
  const shown = ids.slice(0, 5);
  const parts = [
    el("span", { class: "mark", text: mark }),
    el("span", { class: "what", text: `${items.length} ${noun}s` }),
    marks.length ? el("span", { class: "marks" }, ...marks) : null,
    // Between parts of one team: its goal; else the workers by their avatars.
    team ? el("span", { class: "goal-name", text: team.goal }) : ids.length ? el("span", { class: "avatars" }, ...shown.map((id) => avatar(id, titles.get(id) || "")), ids.length > shown.length ? el("span", { class: "more", text: `+${ids.length - shown.length}` }) : null) : null,
    waiting ? el("span", { class: "waiting", text: waiting }) : null,
  ];
  const open = group.classList.contains("open");
  const toggle = el("button", { type: "button", class: "line", "aria-expanded": String(open), onclick: () => toggleFold(group) }, ...parts);
  const last = items.at(-1).peer?.to;
  const view = kind === "team" && last ? el("button", { type: "button", class: "link", text: "Open", title: team ? `${team.goal}: what its parts handed each other` : "Its chat", onclick: (event) => { event.stopPropagation(); openThread(team, last); } }) : null;
  line.replaceChildren(el("div", { class: "row" }, toggle, view, items.at(-1).querySelector("time")?.cloneNode(true) || null));
}
// The team its messages are between, or the chat of the worker last told when in none.
function openThread(team, sessionId) {
  if (team) showTeam(team.id);
  else select(sessionId);
}
function toggleFold(group, open = !group.classList.contains("open")) {
  group.classList.toggle("open", open);
  group.querySelector(":scope > .fold-line button.line")?.setAttribute("aria-expanded", String(open));
}
// What still waits changes as questions are answered: the folds say it as it is now.
function refreshFolds() {
  for (const group of document.querySelectorAll("#timeline .fold.team, #timeline .fold.routing")) summarizeFold(group);
}
export function updateWorkingLine() {
  const timeline = $("#timeline");
  timeline.querySelector(".working-line")?.remove();
  const session = byId(viewedId());
  // Its turn is over, but a command it left running (a dev server) goes on: not thinking.
  const background = session?.background && session.status !== "working";
  if (session?.activity === RECONNECTING) {
    timeline.append(el("div", { class: "working-line" }, duck("working"), el("span", { text: RECONNECTING })));
    scrollToBottom();
    return;
  }
  if (session?.status !== "working" && !background) return;
  timeline.append(
    el(
      "div",
      { class: `working-line${background ? " background" : ""}` },
      // The captain at the wheel while it thinks; on watch while its command runs on.
      duck(background ? "watching" : "working"),
      el("span", { text: background ? "Waiting for background process" : session.activity || "Thinking…", title: background ? "Its turn is over; a command it started (a dev server, a watcher) still runs. Stop ends it." : undefined }),
      el("button", { type: "button", class: "link", text: "Stop", title: "Interrupt what it is doing now", onclick: () => post(`/api/sessions/${session.id}/stop`, "Stopped.") }),
    ),
  );
  scrollToBottom();
}
// A draft in a timeline: while it waits for the owner, a way to its card under Needs you.
export function updateDraftRecords() {
  // Its card under Needs you: waiting for the owner, or sent maybe ("Sent?").
  const pending = new Set([...pendingDrafts(), ...uncertainDrafts()].map((draft) => draft.id));
  for (const node of document.querySelectorAll(".draft-record")) {
    const waiting = pending.has(node.dataset.draftId);
    node.classList.toggle("pending", waiting);
    // A card's head, or a chat line's row (the line itself when it does not open).
    const head = node.querySelector(":scope > .head, :scope > .row") || node;
    let link = head.querySelector(".goto");
    if (waiting && !link) {
      link = el("button", { type: "button", class: "link goto", text: "Waiting for you · Needs you", onclick: (event) => { event.preventDefault(); event.stopPropagation(); focusDraft(node.dataset.draftId); } });
      const time = head.querySelector("time");
      if (time) time.before(link);
      else head.append(link);
    } else if (!waiting) {
      link?.remove();
    }
    // Gone without being sent: it says so (withdrawn, or a newer draft took its place).
    const outcome = state.draftOutcomes.get(node.dataset.draftId);
    if (outcome && !head.querySelector(".outcome")) {
      const note = el("span", { class: "outcome", text: outcome, title: outcome });
      const time = head.querySelector("time");
      if (time) time.before(note);
      else head.append(note);
    }
  }
}
