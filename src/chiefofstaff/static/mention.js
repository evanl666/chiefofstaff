// Addressing one worker from Home: "@" picks a worker, and the message goes straight to it. The
// Router hears of it with its next message. Where another coding agent is installed, "@" also
// lists every agent with its status (Ready, Sign-in needed, Not installed; one not ready dimmed,
// with the command that makes it ready): one picked is what new work runs on (its chip opens
// its models and effort); two picked, the task runs on each, side by side (Compare).
import { $, api, el, toast } from "./dom.js";
import { byId, HOME, sessions, state, workerTitle } from "./state.js";
import { agentMark, agentName, agentPopover, closeAgentPopover, ready, refreshStatuses, statuses, statusText } from "./agentpick.js";

export const mention = { to: null, agent: null, agents: [], options: [], active: 0, dismissed: -1 };
// Agents on at once for one task, at most (Compare).
const MOST_COMPARED = 3;

export const routerSide = () => state.selected === HOME;

// "@" and what follows it up to the cursor: a name may have spaces ("@Fix ch"), not a line break
// or another "@", and does not start with a space ("@ " is not a mention).
function query(input) {
  const before = input.value.slice(0, input.selectionStart);
  const match = /(^|\s)@((?:[^\s@][^@\n]*)?)$/.exec(before);
  return match ? { text: match[2], start: before.length - match[2].length - 1 } : null;
}

function render() {
  const list = $("#mention-list");
  const input = $("#composer-input");
  list.hidden = !mention.options.length;
  // The message box is a combobox: screen readers hear the list open and the worker picked.
  input.setAttribute("aria-expanded", String(!list.hidden));
  if (list.hidden) input.removeAttribute("aria-activedescendant");
  else input.setAttribute("aria-activedescendant", `mention-${mention.active}`);
  list.replaceChildren(
    ...mention.options.map((worker, index) =>
      el(
        "li",
        {
          id: `mention-${index}`,
          role: "option",
          class: `${index === mention.active ? "active" : ""}${worker.picks ? " agent" : ""}${worker.dim ? " dim" : ""}`.trim(),
          "aria-selected": String(index === mention.active),
          onmousedown: (event) => {
            event.preventDefault();
            choose(worker);
          },
        },
        // (An agent to run on, not a worker, which has an agent too.)
        ...(worker.picks
          ? agentOption(worker)
          : [
              el("span", { class: `dot ${worker.status}` }),
              el("span", { class: "name", text: workerTitle(worker), title: worker.title !== workerTitle(worker) ? `Started as “${worker.title}”` : undefined }),
              el("span", { class: "status", text: worker.activity || worker.status }),
            ]),
      ),
    ),
  );
}

// An agent in the list: its mark, its name and status; one not ready, the command that makes
// it ready (picking it copies that command: never a control inside an option), or, ready but
// off for workers, that picking it turns it on.
function agentOption(option) {
  const status = option.status;
  const fix = option.dim && status && !ready(status) ? status.fix : null;
  const off = option.dim && ready(status) && !status.on;
  return [
    agentMark(option.agent),
    el("span", { class: "name", text: option.label }),
    el(
      "span",
      { class: "status" },
      el("span", { text: status ? statusText(status) : `Run it on ${option.label}` }),
      fix?.command
        ? el("span", { class: "fix agent-fix" }, "Run ", el("code", { text: fix.command }), fix.then ? ` ${fix.then}` : "", " in a terminal · ", el("span", { class: "hint", text: "pick to copy" }))
        : off
          ? el("span", { class: "fix agent-fix" }, el("span", { class: "hint", text: "Pick to turn it on for workers" }))
          : "",
    ),
  ];
}
// One not ready picked: its command copied (to run in a terminal; nothing here signs in); one
// ready and off: on, as its first-run card's Allow does (new workers stay where they start),
// then chosen.
async function pickUnready(option) {
  const status = option.status;
  if (status?.fix?.command) {
    try {
      await navigator.clipboard.writeText(status.fix.command);
      toast(`Copied: run ${status.fix.command} in a terminal${status.fix.then ? `, ${status.fix.then}` : ""}.`);
    } catch (error) {
      toast(error.message, "error");
    }
    return;
  }
  if (!(ready(status) && !status.on)) return;
  try {
    const fresh = await api(`/api/agents/${option.agent}/offer`, { method: "POST", body: { allow: true } });
    state.snapshot = { ...state.snapshot, ...fresh };
    toast(`${option.label} is available for workers.`);
    setAgent(option);
  } catch (error) {
    toast(error.message, "error");
  }
}

export function closeMentions() {
  mention.options = [];
  render();
}

export function onComposerInput() {
  const input = $("#composer-input");
  const found = routerSide() ? query(input) : null;
  // Escape closed the list for this "@": typing on does not bring it back (a fresh "@" does).
  if (found && !found.text) mention.dismissed = -1;
  if (!found || found.start === mention.dismissed) {
    if (!found) mention.dismissed = -1;
    closeMentions();
    return;
  }
  mention.dismissed = -1;
  // By the name it shows now (the task it is on), or the one it started with: every word typed
  // is in it ("fix ch" finds "Fix the checkout test").
  const text = found.text.toLowerCase().trim();
  const words = text.split(/\s+/);
  const named = (session) =>
    [workerTitle(session), session.title].some((name) => {
      const lower = (name || "").toLowerCase();
      return lower.includes(text) || words.every((word) => lower.includes(word));
    });
  // Every agent first, by its name's start, with its status (Copilot only while another is
  // on: else there is nothing to choose); one not ready or off is listed, dimmed.
  const on = state.snapshot.agents?.on || [];
  const chosen = mention.agents.length ? mention.agents : mention.agent ? [mention.agent.name] : [];
  const agents = statuses()
    .filter((status) => status.name !== "copilot" || on.length)
    .map((status) => ({ picks: true, agent: status.name, label: agentName(status.name), status, dim: !(ready(status) && status.on), chosen: chosen.includes(status.name) }))
    .filter((option) => !option.chosen && (option.agent.startsWith(text) || option.label.toLowerCase().startsWith(text)));
  if (agents.length) refreshStatuses(() => mention.options.length && onComposerInput());
  const workers = sessions().filter((session) => session.role === "worker" && !session.helper && !session.closed_at && named(session));
  mention.options = [...agents, ...workers].slice(0, 8);
  mention.active = 0;
  render();
}

function choose(option) {
  const input = $("#composer-input");
  if (option.dim) {
    closeMentions();
    pickUnready(option);
    input.focus();
    return;
  }
  const found = query(input);
  if (found) {
    input.value = input.value.slice(0, found.start) + input.value.slice(input.selectionStart).replace(/^\s*/, "");
    input.setSelectionRange(found.start, found.start);
  }
  closeMentions();
  if (option.picks) setAgent(option);
  else setRecipient(option);
  input.focus();
}

export function setRecipient(worker) {
  mention.to = worker ? { id: worker.id, title: workerTitle(worker) } : null;
  if (worker) {
    mention.agent = null;  // A worker runs on its own agent.
    mention.agents = [];
  }
  showChip();
}

// The agent the owner chose for what this message asks (removable): the Router starts the work
// on it. A second one picked: the task runs on each (Compare).
export function setAgent(option) {
  closeAgentPopover();
  if (!option) {
    mention.agent = null;
    mention.agents = [];
  } else if (mention.agent && mention.agent.name !== option.agent) {
    mention.agents = [mention.agent.name, option.agent];
    mention.agent = null;
  } else if (mention.agents.length) {
    if (!mention.agents.includes(option.agent) && mention.agents.length < MOST_COMPARED) mention.agents = [...mention.agents, option.agent];
  } else {
    mention.agent = { name: option.agent, label: option.label, model: "", effort: "" };
  }
  if (option) mention.to = null;
  showChip();
}

function showChip() {
  const chip = $("#composer-to");
  chip.hidden = !mention.to && !mention.agent && !mention.agents.length;
  const clear = (label, onclick) => el("button", { type: "button", class: "clear", "aria-label": label, title: label, text: "×", onclick });
  // Several finished tasks, the latest one the Router gave it: what is written goes as a task of
  // its own (Runtime.chat_task), said here before it is sent (review M1).
  const apart = mention.to && byId(mention.to.id)?.chat_task?.id === "";
  chip.replaceChildren(
    ...(mention.to
      ? [
          el("span", { text: "To " }),
          el("strong", { text: mention.to.title, title: mention.to.title }),
          apart ? el("span", { class: "apart", text: "· as a new task", title: "It is about none of its tasks in particular: it becomes a task of its own, named from your message" }) : "",
          clear("Send to your chief of staff instead", () => setRecipient(null)),
        ]
      : mention.agents.length
        ? [
            el("span", { text: "Compare on " }),
            ...mention.agents.flatMap((name, index) => [index ? el("span", { text: index === mention.agents.length - 1 ? " and " : ", " }) : "", el("strong", { class: "agent-chip-name" }, agentMark(name), agentName(name))]),
            clear("Let Settings choose the agent", () => setAgent(null)),
          ]
        : mention.agent
          ? [el("span", { text: "On " }), agentChip(), clear("Let Settings choose the agent", () => setAgent(null))]
          : []),
  );
}
// The chosen agent as a button: its popover changes the agent, its model and its effort.
function agentChip() {
  const chosen = mention.agent;
  const detail = [chosen.model, chosen.effort].filter(Boolean).join(" · ");
  const button = el(
    "button",
    { type: "button", class: "agent-chip", "aria-haspopup": "dialog", title: "Agent, model and effort" },
    agentMark(chosen.name),
    el("strong", { text: chosen.label }),
    detail ? el("span", { class: "detail", text: detail }) : "",
    el("span", { class: "caret", "aria-hidden": "true", text: "▾" }),
  );
  button.onclick = () =>
    agentPopover(button, { agent: mention.agent.name, model: mention.agent.model, effort: mention.agent.effort }, (picked) => {
      mention.agent = { name: picked.agent, label: agentName(picked.agent), model: picked.model, effort: picked.effort };
      const fresh = agentChip();
      button.replaceChildren(...fresh.childNodes);
    });
  return button;
}

// Arrow keys, Enter and Tab pick from the open list; Escape closes it. True when handled.
export function onMentionKey(event) {
  // Keys that pick or confirm characters in an input method (Chinese, Japanese) are its own.
  if (event.isComposing || event.keyCode === 229) return false;
  if (!mention.options.length) {
    if (event.key === "Backspace" && (mention.to || mention.agent || mention.agents.length) && !$("#composer-input").value) {
      if (mention.to) setRecipient(null);
      else setAgent(null);
      return true;
    }
    return false;
  }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    const step = event.key === "ArrowDown" ? 1 : -1;
    mention.active = (mention.active + step + mention.options.length) % mention.options.length;
    render();
  } else if (event.key === "Enter" || event.key === "Tab") {
    choose(mention.options[mention.active]);
  } else if (event.key === "Escape") {
    mention.dismissed = query($("#composer-input"))?.start ?? -1;
    closeMentions();
  } else {
    return false;
  }
  event.preventDefault();
  return true;
}
