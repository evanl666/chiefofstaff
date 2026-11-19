// Coding agents in the page (the agent picker): each agent's mark (a dot of its colour), its
// status (Ready, Sign-in needed, Not installed) with the command that makes it ready (the
// owner's to run: nothing here signs in), whose plan or key it uses, and the one popover that
// picks an agent with its model and effort. Nothing of it shows with Copilot alone.
import { api, el, toast } from "./dom.js";
import { state } from "./state.js";

export const AGENT_NAMES = { copilot: "Copilot", codex: "Codex", claude: "Claude" };
export const agentName = (name) => AGENT_NAMES[name] || state.snapshot?.agents?.names?.[name] || name;
// The agents new work can run on now: Copilot and each one on.
export const agentsOn = () => (state.snapshot?.agents ? ["copilot", ...state.snapshot.agents.on] : []);
// Marks only where they tell something: more than one agent on.
export const marksShown = () => agentsOn().length > 1;
export const statuses = () => state.snapshot?.agents_status || [];
export const statusOf = (name) => statuses().find((found) => found.name === name);
export const ready = (status) => status?.status === "ready";

// A dot of the agent's colour; its name (and model) on hover and for screen readers.
export function agentMark(agent, detail = "") {
  const label = [agentName(agent), detail].filter(Boolean).join(" · ");
  return el("span", { class: "agent-mark", "data-agent": agent, role: "img", "aria-label": label, title: label });
}
// A model as its agent names it (its id until its models were listed), the same everywhere.
export function modelLabel(agent, id) {
  if (!id) return "";
  const listed = agent === "copilot" || !agent ? state.models : state.agentModels?.[agent];
  return state.snapshot?.agents?.model_names?.[agent]?.[id] || listed?.find?.((model) => model.id === id)?.name || id;
}
// A session's mark (its agent, its model on hover; why it fell back to Copilot), where shown.
export function markOf(session) {
  if (!marksShown() || session?.role !== "worker") return null;
  const agent = session.agent || "copilot";
  return agentMark(agent, [modelLabel(agent, session.model), session.agent_fallback].filter(Boolean).join(" · "));
}

// "Ready · Uses your ChatGPT sign-in", "Sign-in needed", "Not installed", or why it can't run.
export function statusText(status) {
  if (!status) return "";
  if (ready(status) && !status.on) return "Ready · off for workers";
  return [status.said, ready(status) ? status.pays : ""].filter(Boolean).join(" · ");
}

// The command that makes it ready, to run in a terminal, with a Copy button.
export function fixLine(fix) {
  if (!fix?.command) return null;
  const copy = el("button", {
    type: "button",
    class: "link copy-fix",
    text: "Copy",
    "aria-label": `Copy ${fix.command}`,
    onmousedown: (event) => event.preventDefault(),
    onclick: async (event) => {
      event.stopPropagation();
      try {
        await navigator.clipboard.writeText(fix.command);
        toast(`Copied: run ${fix.command} in a terminal${fix.then ? `, ${fix.then}` : ""}.`);
      } catch (error) {
        toast(error.message, "error");
      }
    },
  });
  return el("span", { class: "agent-fix" }, "Run ", el("code", { text: fix.command }), fix.then ? ` ${fix.then}` : "", " in a terminal ", copy);
}

// Each agent's status asked again (glanced at where it was not lately) when the picker opens:
// at most every 15 seconds.
let asked = 0;
export async function refreshStatuses(after) {
  if (!statuses().length || Date.now() - asked < 15000) return;
  asked = Date.now();
  try {
    const { agents_status: found } = await api("/api/agents/status", { method: "POST" });
    state.snapshot = { ...state.snapshot, agents_status: found };
    after?.();
  } catch {
    // (Shown as last known.)
  }
}

// An agent's models: Copilot's (as loaded for the page), or another's, listed once asked.
async function modelsOf(agent) {
  if (agent === "copilot") return state.models || [];
  state.agentModels = state.agentModels || {};
  if (!state.agentModels[agent]?.length) {
    try {
      state.agentModels = { ...state.agentModels, [agent]: await api(`/api/models?agent=${encodeURIComponent(agent)}`) };
    } catch {
      return [];
    }
  }
  return state.agentModels[agent] || [];
}
export const EFFORT_WORDS = { none: "none", low: "low", medium: "medium", high: "high", xhigh: "extra high", max: "max" };

// The one popover: agents on the left (those that can run work now; the others dimmed, their
// fix under them), the chosen one's models and effort on the right, whose plan it uses under
// it. `onPick({agent, model, effort})` on each choice; it stays open until a click elsewhere
// or Escape.
let openPopover = null;
// Toasts never under an open popover: above it while it is open (they stack upwards from
// there), back where they were once it closes.
function toastsClear(node) {
  const toasts = document.getElementById("toasts");
  if (!toasts) return;
  if (!node) {
    toasts.style.bottom = "";
    return;
  }
  const box = node.getBoundingClientRect();
  const spare = window.innerHeight - box.bottom;
  // Below it where there is room for a toast (a popover high on the page); else above it.
  toasts.style.bottom = spare >= 160 ? "" : `${Math.max(8, window.innerHeight - box.top + 8)}px`;
}
export function closeAgentPopover() {
  if (openPopover?.place) window.removeEventListener("resize", openPopover.place);
  if (openPopover) toastsClear(null);
  openPopover?.remove();
  openPopover = null;
  document.removeEventListener("mousedown", outside, true);
  document.removeEventListener("keydown", escape, true);
}
function outside(event) {
  if (openPopover && !openPopover.contains(event.target) && !openPopover.anchor?.contains(event.target)) closeAgentPopover();
}
function escape(event) {
  if (event.key !== "Escape" || !openPopover) return;
  event.stopPropagation();
  const anchor = openPopover.anchor;
  closeAgentPopover();
  anchor?.focus();
}
export function agentPopover(anchor, chosen, onPick, { title = "Runs on" } = {}) {
  if (openPopover?.anchor === anchor) {
    closeAgentPopover();
    return;
  }
  closeAgentPopover();
  let picked = { agent: chosen.agent || "copilot", model: chosen.model || "", effort: chosen.effort || "" };
  const left = el("div", { class: "agent-pop-agents", role: "radiogroup", "aria-label": title });
  const right = el("div", { class: "agent-pop-models" });
  const pays = el("small", { class: "agent-pop-pays" });
  const node = el("div", { class: "agent-popover", role: "dialog", "aria-label": title }, el("div", { class: "agent-pop-body" }, left, right), pays);
  node.anchor = anchor;
  const pick = (change) => {
    picked = { ...picked, ...change };
    onPick(picked);
    draw();
  };
  async function drawModels() {
    const agent = picked.agent;
    const models = await modelsOf(agent);
    if (openPopover !== node || picked.agent !== agent) return;
    const model = el("select", { "aria-label": "Model" }, el("option", { value: "", text: "Default" }), ...models.map((item) => el("option", { value: item.id, text: item.name || item.id })));
    model.value = models.some((item) => item.id === picked.model) ? picked.model : "";
    const info = models.find((item) => item.id === picked.model) || models.find((item) => item.default);
    const efforts = info?.efforts || [];
    const effort = el("select", { "aria-label": "Effort" }, el("option", { value: "", text: "Default" }), ...efforts.map((level) => el("option", { value: level, text: EFFORT_WORDS[level] || level })));
    effort.value = efforts.includes(picked.effort) ? picked.effort : "";
    model.onchange = () => pick({ model: model.value, effort: "" });
    effort.onchange = () => pick({ effort: effort.value });
    right.replaceChildren(
      el("label", {}, el("span", { text: "Model" }), model),
      efforts.length ? el("label", {}, el("span", { text: "Effort" }), effort) : "",
    );
    node.place?.();  // (Its height may have changed: toasts kept clear of it.)
  }
  function draw() {
    const names = ["copilot", ...Object.keys(AGENT_NAMES).filter((name) => name !== "copilot")];
    left.replaceChildren(
      ...names
        .map((name) => statusOf(name) || (name === "copilot" ? { name, status: "ready", on: true, said: "Ready" } : null))
        .filter(Boolean)
        .map((status) => {
          const can = ready(status) && status.on;
          const button = el(
            "button",
            {
              type: "button",
              role: "radio",
              class: `agent-pop-agent${can ? "" : " dim"}`,
              "aria-checked": String(status.name === picked.agent),
              "aria-disabled": String(!can),
              onclick: () => can && status.name !== picked.agent && pick({ agent: status.name, model: "", effort: "" }),
            },
            agentMark(status.name),
            el("span", { class: "name", text: agentName(status.name) }),
            el("small", { class: "status", text: can ? "" : statusText(status) }),
          );
          return el("div", { class: "agent-pop-row" }, button, can ? "" : fixLine(status.fix) || "");
        }),
    );
    pays.textContent = statusOf(picked.agent)?.pays || (picked.agent === "copilot" ? "Uses your Copilot plan" : "");
    right.replaceChildren(el("small", { class: "agent-pop-loading", text: "Models…" }));
    drawModels();
  }
  document.body.append(node);
  openPopover = node;
  draw();
  // Beside its anchor, inside the window (again as the window changes size).
  node.place = () => {
    const at = anchor.getBoundingClientRect();
    const width = Math.min(440, window.innerWidth - 16);
    node.style.width = `${width}px`;
    node.style.left = `${Math.max(8, Math.min(at.left, window.innerWidth - width - 8))}px`;
    const below = at.bottom + 6;
    const height = node.offsetHeight;
    node.style.top = `${below + height > window.innerHeight - 8 ? Math.max(8, at.top - height - 6) : below}px`;
    toastsClear(node);
  };
  node.place();
  window.addEventListener("resize", node.place);
  document.addEventListener("mousedown", outside, true);
  document.addEventListener("keydown", escape, true);
  refreshStatuses(() => openPopover === node && draw());
  node.querySelector("[aria-checked=true]")?.focus();
}
