// Settings → Agents & models (docs/design/settings-v2.md §3): who does the work, and with
// which model. New workers run on (the answer at a glance), one row per agent (its status in
// words, one action, its switch, a meter of its plan; unfolded, its model, effort and limit per
// worker, and its details), and Chief of Staff's own sessions, folded.
//
// Drawn once each time Settings opens, from what is saved and the snapshot (agents_status):
// nothing here asks the server until the page is shown. Then, while another agent is installed
// or on, the look (GET /api/agents: a short process of each agent's own, never its backend, and
// never /api/models?agent=) updates the rows in place, keeping every field's value and whether
// the owner changed it (data-dirty). A menu keeps the saved value it can't list and is sent
// only once the owner changed it (§8.7).
import { $, api, el, toast } from "./dom.js";
import { state } from "./state.js";
import { copyFix, endsLine, modelText, statusOf } from "./agentpick.js";
import { openSetup, retrySetup, setupOf, setupWords } from "./agentsetup.js";
import { resetTime } from "./board.js";
import { codeHtml } from "./text.js";

export const AGENT_LABELS = { copilot: "Copilot", codex: "Codex", claude: "Claude Code" };
const OTHERS = ["codex", "claude"];
export const EFFORT_WORDS = { none: "None", low: "Low", medium: "Medium", high: "High", xhigh: "Extra high", max: "Max" };
// Copilot's efforts where its model's are not known (Copilot's own default, a model not listed).
const ANY_EFFORT = ["low", "medium", "high", "xhigh", "max"];
const ROLES = [
  ["router", "Router"],
  ["slack", "Slack"],
  ["watchdog", "Watchdog"],
];
// The roles that can run on another agent (agents.role_agents), as the look lists them.
const PLACED = ["router", "watchdog"];
const GUIDE = "https://github.com/evanl666/chiefofstaff/blob/main/docs/guide.md#workers-on-other-coding-agents";

const form = () => $("#settings-form");
const field = (name) => form()?.elements.namedItem(name) || null;
const label = (name) => AGENT_LABELS[name] || state.snapshot?.agents?.names?.[name] || name;
// The page's own state, from one opening of Settings to the next.
let looked = null; // The look's answer (GET /api/agents), once it came.
let looking = null; // The look under way.
let failed = ""; // Why the last look failed.
let others = []; // The other agents with a row.

// What can be said ----------------------------------------------------------------------------

// A model menu's choices: its first (""), the models listed, and the chosen one where the list
// does not have it (not offered now, or not loaded yet): a menu never replaces a saved value.
export function modelChoices(list, chosen, first, describe = (model) => model.name || model.id) {
  const listed = (list || []).map((model) => [model.id, describe(model)]);
  const kept = chosen && !listed.some(([id]) => id === chosen) ? [[chosen, (list || []).length ? `${chosen} (not offered now)` : chosen]] : [];
  return [["", first], ...listed, ...kept];
}
// An effort menu's choices: the model's efforts (null: not known, today's list), and the chosen
// one where the model does not offer it.
export function effortChoices(efforts, chosen) {
  const offered = efforts || ANY_EFFORT;
  const listed = offered.map((level) => [level, EFFORT_WORDS[level] || level]);
  const kept = chosen && !offered.includes(chosen) ? [[chosen, `${EFFORT_WORDS[chosen] || chosen} (not offered)`]] : [];
  return [["", "Default"], ...listed, ...kept];
}
function fill(select, choices, value) {
  select.replaceChildren(...choices.map(([id, text]) => el("option", { value: id, text })));
  select.value = choices.some(([id]) => id === value) ? value : "";
}
// A plan's use as its meter shows it: the most-used window, its level (80%: high, 100%: full),
// its words ("41% weekly"), and each window with its reset for the tip and screen readers.
export function usageMeter(usage, now = Date.now()) {
  const windows = (usage || []).filter((entry) => Number.isFinite(entry?.used));
  if (!windows.length) return null;
  const most = windows.reduce((a, b) => (b.used > a.used ? b : a));
  const short = (entry) => String(entry.label || "").replace(/ limit$/, "");
  const reset = (entry) => (entry.resets_at ? ` · resets ${resetTime(entry.resets_at * 1000, now)}` : "");
  return {
    used: most.used,
    level: most.used >= 100 ? "full" : most.used >= 80 ? "high" : "",
    words: `${most.used}% ${short(most)}`,
    label: `${most.used}% of your ${most.label} used`,
    windows: windows.map((entry) => `${entry.used}% of your ${entry.label}${reset(entry)}`),
  };
}
// What must not be folded away, under an agent's line, whatever its status and meter say: its
// plan's limit as its workers last heard it (extra usage, used up with no reset known, a rate
// limit), its sign-in about to end, why it can't run, what its workers do differently
// sandboxed, and a look that failed.
export function agentAlerts(name, { look = null, status = null, sandboxed = false, failure = "" } = {}) {
  const alerts = [];
  if (failure) alerts.push({ tone: "warn", text: `Could not check: ${failure}` });
  if (look?.limit_line) alerts.push({ tone: "warn", text: look.limit_line });
  const ends = endsLine({ ...(status || {}), ...(look || {}), name });
  if (ends) alerts.push({ tone: "warn", text: ends });
  const why = cannotRun(look, status, sandboxed);
  if (why) alerts.push({ tone: "bad", text: why });
  // Its setup by Chief of Staff failed: why in a few words; what it printed and the command to
  // run instead folded under Details.
  const now = { ...(status || {}), ...(look || {}) };
  if (now.setup?.state === "failed") alerts.push({ tone: "warn", text: `${now.said}.`, details: now.setup });
  if (sandboxed && look?.sandbox_note && !why) alerts.push({ tone: "warn", text: look.sandbox_note });
  return alerts;
}
// Why it can't run here, said in full ("" when it can, or when Sign in or Install is what it
// needs).
function cannotRun(look, status, sandboxed) {
  // (What Chief of Staff sets up itself is said by its status, not as a reason it can't run.)
  if (look?.setup || status?.setup) return "";
  const kind = (look || status)?.status;
  if (["signin", "missing"].includes(kind) || look?.found === false) return "";
  if (look && sandboxed && look.sandbox) return look.sandbox;
  if (look?.problem) return look.problem;
  if (["problem", "unchecked"].includes(kind)) return (look || status).said || "";
  return "";
}
// Its status in a few words, with its tone and its one action: words never longer than that
// (a reason is an alert line).
export function agentStatus(name, { look = null, status = null, sandboxed = false, on = false, running = "" } = {}) {
  const now = { ...(status || {}), ...(look || {}) };
  if (running) return { text: running, tone: "warn", action: "show" };
  // Its Agent SDK, which Chief of Staff sets up itself (Claude): calm while it runs; Retry when
  // it failed (Set up when it is needed again); turning it on sets it up.
  if (now.status === "settingup") return { text: now.said || "Setting up…", tone: "off", action: "" };
  if (["failed", "needed"].includes(now.setup?.state)) return { text: "Not set up", tone: "warn", action: "retry", words: now.setup.state === "failed" ? "Retry" : "Set up" };
  if (now.status === "setup") return { text: now.said || "Not set up yet", tone: "off", action: "", turnsOn: true };
  if (now.status === "signin") return { text: "Sign in needed", tone: "warn", action: now.ways?.length ? "signin" : "" };
  // (Listed only while on, to be turned off: Settings offers no install; the @ list does.)
  if (now.status === "missing" || look?.found === false) return { text: "Not installed", tone: "off", action: "" };
  if (cannotRun(look, status, sandboxed)) return { text: "Can't run here", tone: "bad", action: "" };
  if (now.status === "limited") return { text: `At its limit${now.resets_at ? ` · resets ${resetTime(now.resets_at * 1000)}` : ""}`, tone: "warn", action: "", works: true };
  if (now.status === "checking") return { text: "Checking…", tone: "off", action: "" };
  if (now.status === "ready") return on ? { text: "Ready", tone: "ready", action: "", works: true } : { text: "Ready · off for workers", tone: "off", action: "", works: true };
  return { text: now.status ? now.said || "" : "", tone: "off", action: "" };
}

// Copilot's models (loaded at startup: state.models) and another agent's (its look's).
const copilotModels = () => state.models || [];
const copilotName = (id) => copilotModels().find((model) => model.id === id)?.name || id;
const lookOf = (name) => looked?.agents?.find((agent) => agent.name === name) || null;
const agentModels = (name) => lookOf(name)?.models || [];
const defaultModel = (name) => agentModels(name).find((model) => model.default) || agentModels(name).find((model) => model.id === "default");
function agentModelName(name, id) {
  const model = id ? agentModels(name).find((item) => item.id === id) : defaultModel(name);
  if (!model) return id || "";
  if (name === "claude" && model.id === "default") {
    // Claude Code's default, by the alias it stands for now ("Opus").
    const alias = agentModels(name).find((item) => item.id !== "default" && item.resolved && item.resolved === model.resolved);
    return alias?.name || model.resolved || "Claude Code's default";
  }
  return model.name || model.id;
}
const savedOn = (name) => (looked ? Boolean(lookOf(name)?.enabled) : Boolean(state.snapshot?.agents?.on?.includes(name)));
const onNow = (name) => Boolean(field(`agent_${name}_enabled`)?.checked);
const sandboxed = () => Boolean(looked?.sandboxed);
function nowOf(name) {
  let look = lookOf(name);
  const status = statusOf(name) || null;
  // Where the setup of its Agent SDK stands changes as it runs: the snapshot's, once it began or
  // ended since the look (agentsStateChanged then looks again).
  if (look && status && (status.setup || look.setup?.state === "running")) look = { ...look, status: status.status, said: status.said, setup: status.setup };
  const running = setupOf(name)?.state === "running" ? setupWords(name) : "";
  return { look, status, sandboxed: sandboxed(), on: onNow(name), running };
}
const canWork = (name) => Boolean(agentStatus(name, nowOf(name)).works);

// Drawn once per opening --------------------------------------------------------------------

// Whether this computer has another agent: then each gets a row and the page looks at them.
const withAgents = () => Boolean(state.snapshot?.agents || state.snapshot?.agents_found?.length);
export function renderAgentsPage() {
  looked = null;
  looking = null;
  failed = "";
  const snap = state.snapshot;
  const found = snap.agents_found || [];
  const on = snap.agents?.on || [];
  // Each agent installed or on: one not installed is not offered here (the @ list does that),
  // and one still on keeps its row, Not installed, to be turned off.
  others = withAgents() ? OTHERS.filter((name) => found.includes(name) || on.includes(name)) : [];
  const page = $("#agents-page");
  const section = $("#settings-page-agents");
  if (others.length) delete section.dataset.looked;
  else section.dataset.looked = "none";
  // (What the last opening drew goes first: nothing of its drafts is read again.)
  page.replaceChildren();
  page.replaceChildren(
    newWorkLine(),
    el("div", { class: "box agent-rows", id: "agent-rows" }, copilotRow(), ...others.map(agentRow)),
    el("small", { id: "agents-none", class: "page-foot", role: "status", hidden: true }),
    ownSessions(),
    ...(others.length ? [el("small", { class: "page-foot", id: "agents-guide" }, "What differs between agents: ", el("a", { class: "link", href: GUIDE, target: "_blank", rel: "noreferrer", text: "the guide" }))] : []),
  );
  const settings = snap.settings;
  refreshEfforts(field("model_worker"), settings.role_efforts.worker || "");
  for (const [role] of ROLES) refreshEfforts(field(`model_${role}`), settings.role_efforts[role] || "");
  for (const name of others) drawRow(name);
  drawCopilot();
  syncRoles();
  syncNewWork(snap.agents?.new_workers || "copilot");
  page.oninput = changed;
  page.onchange = changed;
  page.onclick = clicked;
}
// The page is shown: the look runs, at most once per opening (again after one that failed),
// while another agent is installed or on.
export function agentsShown() {
  if (!others.length || looked || looking) return;
  lookAtAgents();
}
async function lookAtAgents() {
  const section = $("#settings-page-agents");
  const mine = Symbol("look");
  looking = mine;
  failed = "";
  section.dataset.looked = "running";
  let answer;
  try {
    answer = await api("/api/agents");
  } catch (error) {
    if (looking !== mine) return;
    looking = null;
    failed = error.message;
    section.dataset.looked = "failed";
    for (const name of others) drawRow(name);
    return;
  }
  if (looking !== mine) return;
  looking = null;
  looked = answer;
  // None of them found after all (uninstalled since): which it supports, and what to do.
  const none = !answer.agents?.length;
  $("#agents-none").hidden = !none;
  if (none) $("#agents-none").textContent = noAgentWords(answer.supported);
  for (const name of others) {
    const look = lookOf(name);
    if (look) fillAgentFields(name, look);
    drawRow(name);
  }
  fillRoles();
  const choice = field("agent_new_workers");
  if (choice && !choice.dataset.dirty) choice.dataset.saved = answer.new_workers || "copilot";
  syncNewWork(choice && !choice.dataset.dirty ? answer.new_workers : undefined);
  section.dataset.looked = "done";
}

function noAgentWords(supported = ["the Codex CLI"]) {
  const listed = supported.length > 1 ? `${supported.slice(0, -1).join(", ")} or ${supported[supported.length - 1]}` : supported[0];
  return `No supported coding agent found. Supported: ${listed} — install ${supported.length > 1 ? "one" : "it"}, then open this again.`;
}

// New workers run on: the agent and its model in words; a menu once another agent is on.
function newWorkLine() {
  const choice = el("select", { id: "new-work", name: "agent_new_workers", "aria-labelledby": "new-work-lead", "aria-describedby": "new-work-model new-work-hint" });
  const shown = state.snapshot.agents?.new_workers || "copilot";
  choice.dataset.saved = shown;
  return el(
    "div",
    { class: "box" },
    el(
      "div",
      { class: "default-line" },
      el("span", { class: "lead", id: "new-work-lead", text: "New workers run on" }),
      el(
        "div",
        { class: "pick" },
        el("span", { class: "agent-mark", id: "new-work-mark", "data-agent": "copilot", "aria-hidden": "true" }),
        withAgents() ? choice : null,
        el("strong", { id: "new-work-plain", text: "Copilot" }),
        el("span", { class: "sep", id: "new-work-sep", "aria-hidden": "true", text: "·" }),
        el("span", { class: "model", id: "new-work-model" }),
      ),
      el("small", { id: "new-work-hint" }),
    ),
  );
}
// As the form has it now (drafts count): the agents on and able to work, Last used; the
// chosen one's model and effort; whose plan; the Default chip on its row.
function syncNewWork(chosen) {
  const choice = field("agent_new_workers");
  const able = others.filter((name) => onNow(name) && canWork(name));
  let name = "copilot";
  if (choice) {
    const keep = chosen ?? (choice.value || choice.dataset.saved);
    const choices = [["copilot", "Copilot"], ...able.map((agent) => [agent, label(agent)]), ["last", "Last used"]];
    fill(choice, choices, choices.some(([id]) => id === keep) ? keep : "copilot");
    choice.hidden = !able.length;
    name = choice.value === "last" ? looked?.last_used || state.snapshot.agents?.new_workers || "copilot" : choice.value;
    if (!able.includes(name)) name = "copilot";
  }
  const menu = Boolean(choice && !choice.hidden);
  $("#new-work-plain").hidden = menu;
  $("#new-work-sep").hidden = menu;
  $("#new-work-mark").dataset.agent = name;
  let model;
  let effort;
  if (name === "copilot") {
    const worker = field("model_worker")?.value || "";
    const fallback = field("model")?.value || state.snapshot.settings.model || "";
    model = worker || fallback ? copilotName(worker || fallback) : "Copilot's default model";
    effort = field("effort_worker")?.value || "";
  } else {
    model = agentModelName(name, field(`agent_${name}_model`)?.value || "") || `${label(name)}'s default model`;
    effort = field(`agent_${name}_effort`)?.value || "";
  }
  $("#new-work-model").textContent = `${model}${effort ? ` · ${(EFFORT_WORDS[effort] || effort).toLowerCase()} effort` : ""}`;
  const pays = name === "copilot" ? "Uses your Copilot plan" : lookOf(name)?.pays || statusOf(name)?.pays || "";
  const now = choice?.value === "last" ? `Now ${label(name)}. ` : "";
  const status = name === "copilot" ? null : agentStatus(name, nowOf(name));
  const resets = statusOf(name)?.resets_at;
  const waits = status?.text.startsWith("At its limit") && resets ? `${label(name)} is at its limit: new workers wait for it until ${resetTime(resets * 1000)}. ` : "";
  const other = able.find((agent) => agent !== name) || (name !== "copilot" ? "copilot" : "");
  const ask = other ? ` For one task, type @${other} or say “do this with ${label(other)}”.` : "";
  $("#new-work-hint").textContent = `${now}${waits}${pays ? `${pays}.` : ""}${ask}`.trim();
  for (const chip of document.querySelectorAll("#agent-rows .default-chip")) chip.hidden = !(able.length && chip.closest(".agent").dataset.agent === name);
}

// An agent's row: one line (fold, mark, name, status; on the right the Default chip, its meter,
// its one action, its switch), its alert lines, and what it unfolds.
function rowShell(name, { aside, body }) {
  const id = `agent-${name}`;
  return el(
    "div",
    { class: "agent", id, "data-agent": name },
    el(
      "div",
      { class: "agent-line" },
      el("button", { type: "button", class: "fold", "aria-expanded": "false", "aria-controls": `${id}-body`, "aria-label": `${label(name)}: ${name === "copilot" ? "model, effort and details" : "model, effort, limit and details"}` }),
      el("div", { class: "who" }, el("span", { class: "agent-mark", "data-agent": name, "aria-hidden": "true" }), el("strong", { text: label(name) }), el("span", { class: "status", id: `${id}-status`, role: "status" })),
      el("div", { class: "aside" }, el("span", { class: "chip default-chip", hidden: true, text: "Default" }), ...aside),
    ),
    el("div", { class: "agent-alerts", id: `${id}-alerts`, hidden: true }),
    el("div", { class: "agent-body", id: `${id}-body`, hidden: true }, ...body),
  );
}
function copilotRow() {
  const settings = state.snapshot.settings;
  const model = el("select", { name: "model_worker", "data-models": "copilot", "data-first": "workers" });
  const effort = el("select", { name: "effort_worker", "data-efforts-for": "model_worker" });
  const limit = el("input", { name: "worker_credit_limit", type: "number", min: "0", step: "1", placeholder: "No limit", value: settings.worker_credit_limit ? String(settings.worker_credit_limit) : "" });
  const command = el("input", { name: "copilot_command", autocomplete: "off", spellcheck: "false", value: settings.copilot_command || "" });
  const reconnect = el("button", { type: "button", class: "setup-action compact", id: "copilot-reconnect", hidden: true, text: "Reconnect", "aria-label": "Reconnect to Copilot" });
  const row = rowShell("copilot", {
    aside: [reconnect, el("span", { class: "always", text: "Always on" })],
    body: [
      el(
        "div",
        { class: "agent-fields" },
        el("label", {}, el("span", { text: "Model for workers" }), model),
        el("label", {}, el("span", { text: "Effort" }), effort),
        el("label", {}, el("span", { text: "Limit per worker (credits)" }), limit, el("small", { text: "Empty: none. At least 30." })),
      ),
      el(
        "div",
        { class: "agent-details" },
        el("span", { text: "Uses your Copilot plan" }),
        el("span", { id: "copilot-warning", hidden: true }),
        el("label", { class: "cmd" }, el("span", { text: "Command" }), command),
      ),
    ],
  });
  fillCopilotModel(model, settings.role_models.worker || "");
  return row;
}
// Copilot's status, from the runtime: Ready, Starting…, or Can't start (and Reconnect).
function drawCopilot() {
  const runtime = state.snapshot.runtime || {};
  const status = $("#agent-copilot-status");
  const words = runtime.state === "error" ? ["Can't start", "bad"] : runtime.state === "starting" ? ["Starting…", "off"] : ["Ready", "ready"];
  status.textContent = words[0];
  status.className = `status ${words[1]}`;
  $("#copilot-reconnect").hidden = runtime.state !== "error";
  alertsInto("copilot", runtime.state === "error" && runtime.error ? [{ tone: "bad", text: runtime.error }] : []);
  const warning = $("#copilot-warning");
  warning.textContent = runtime.warning || "";
  warning.hidden = !runtime.warning;
}
function agentRow(name) {
  const box = el("input", { type: "checkbox", name: `agent_${name}_enabled`, "aria-label": `Use for workers: ${label(name)}`, "aria-describedby": `agent-${name}-status agent-${name}-alerts` });
  box.checked = savedOn(name);
  const model = el("select", { name: `agent_${name}_model`, "data-models": name, disabled: true });
  const effort = el("select", { name: `agent_${name}_effort`, "data-efforts-for": `agent_${name}_model`, disabled: true });
  const errorId = `agent-${name}-limit-error`;
  const limit = el("input", { name: `agent_${name}_limit`, type: "text", inputmode: "numeric", placeholder: "No limit", autocomplete: "off", disabled: true, "aria-describedby": errorId });
  limit.addEventListener("blur", () => {
    const count = checkLimit(limit);
    if (count) limit.value = grouped(count);
  });
  limit.addEventListener("input", () => limit.hasAttribute("aria-invalid") && checkLimit(limit));
  // Saved before the look answers (the snapshot names it for an agent on): kept until then.
  const configured = state.snapshot.agents?.configured_models?.[name] || "";
  const shown = name === "claude" && configured === "default" ? "" : configured;
  fill(model, modelChoices([], shown, `${label(name)}'s default`), shown);
  fill(effort, effortChoices([], ""), "");
  return rowShell(name, {
    aside: [
      el("span", { class: "meter-slot" }),
      el("span", { class: "action-slot" }),
      el("label", { class: "check use" }, box, " Use for workers"),
    ],
    body: [
      el("p", { class: "agent-note setup-note", hidden: true }),
      el("div", { class: "agent-details setup-details", hidden: true }),
      el("p", { class: "agent-note limited-note", hidden: true }),
      el(
        "div",
        { class: "agent-fields" },
        el("label", {}, el("span", { text: "Model" }), model),
        el("label", {}, el("span", { text: "Effort" }), effort),
        el("label", {}, el("span", { text: "Limit per worker (tokens)" }), limit, el("small", { id: errorId, class: "field-error", hidden: true })),
      ),
      el("small", { class: "agent-note for-roles", text: `For its workers, and for the Router or the Watchdog when they run on ${label(name)}.` }),
      el("div", { class: "agent-details about", hidden: true }),
      el("small", { class: "agent-note plan-note", hidden: true }),
    ],
  });
}
// Its fields as saved, once the look says (a value the owner changed meanwhile stays).
function fillAgentFields(name, look) {
  const model = field(`agent_${name}_model`);
  const effort = field(`agent_${name}_effort`);
  const limit = field(`agent_${name}_limit`);
  const savedModel = name === "claude" && look.model === "default" ? "" : look.model || "";
  for (const input of [model, effort, limit]) {
    input.dataset.saved = "1";
    input.disabled = false;
  }
  model.dataset.savedValue = look.model || "";
  effort.dataset.savedValue = look.effort || "";
  limit.dataset.savedValue = String(look.limit || 0);
  fillAgentModel(name, model, model.dataset.dirty ? model.value : savedModel);
  refreshEfforts(model, effort.dataset.dirty ? effort.value : look.effort || "");
  if (!limit.dataset.dirty) limit.value = look.limit ? grouped(look.limit) : "";
  const box = field(`agent_${name}_enabled`);
  if (!box.dataset.dirty) box.checked = Boolean(look.enabled);
}
function fillAgentModel(name, select, value) {
  const first = `${label(name)}'s default${defaultModel(name) ? ` (${agentModelName(name, "")})` : ""}`;
  const listed = agentModels(name).filter((model) => !(name === "claude" && model.id === "default"));
  fill(select, modelChoices(listed, value, first, (model) => modelText(name, model)), value);
}
const defaultModelNow = () => field("model")?.value ?? state.snapshot.settings.model ?? "";
function fillCopilotModel(select, value) {
  const fallback = defaultModelNow();
  const first = select.dataset.first === "workers" ? (fallback ? `Default (${copilotName(fallback)})` : "Copilot's default") : select.dataset.first === "copilot" ? "Copilot's default" : "Default";
  fill(select, modelChoices(copilotModels(), value, first), value);
}
// An effort menu, as its model offers them (where that model is known), keeping its value.
function refreshEfforts(modelSelect, value) {
  if (!modelSelect) return;
  const effort = modelSelect.closest(".agent-fields, .cells")?.querySelector(`select[data-efforts-for="${modelSelect.name}"]`);
  if (!effort) return;
  const keep = value ?? effort.value;
  const agent = modelSelect.dataset.models;
  let efforts = null;
  if (agent === "copilot") {
    const id = modelSelect.value || (modelSelect.name === "model" ? "" : defaultModelNow());
    const info = id ? copilotModels().find((model) => model.id === id) : null;
    efforts = info ? info.efforts || [] : null;
  } else {
    const info = modelSelect.value ? agentModels(agent).find((model) => model.id === modelSelect.value) : defaultModel(agent);
    efforts = info ? info.efforts || [] : lookOf(agent) ? null : [];
  }
  fill(effort, effortChoices(efforts, keep), keep);
  // A model that takes no effort (Auto): nothing to choose, its saved one kept.
  effort.disabled = Boolean(efforts && !efforts.length && !keep) || (agent !== "copilot" && !lookOf(agent));
}
// Copilot's models came (loadModels, after Settings opened): each menu again, its value kept.
export function modelsArrived() {
  if (!$("#settings-dialog")?.open) return;
  for (const select of form().querySelectorAll('select[data-models="copilot"]')) {
    fillCopilotModel(select, select.value);
    refreshEfforts(select);
  }
  syncNewWork();
  ownSummary();
}

// What an agent's row says now: in place, its fields as they are.
function drawRow(name) {
  const row = $(`#agent-${name}`);
  if (!row) return;
  // Uninstalled since Settings opened, and off: the look no longer lists it.
  row.hidden = Boolean(looked) && !lookOf(name);
  const now = nowOf(name);
  const status = agentStatus(name, now);
  const words = row.querySelector(".status");
  words.textContent = status.text;
  words.className = `status ${status.tone}`;
  row.classList.toggle("unready", !status.works);
  // Turning it on waits until it can work; turning it off never waits.
  const box = field(`agent_${name}_enabled`);
  const on = savedOn(name);
  box.disabled = !on && !box.checked && !status.works && !status.turnsOn;
  box.closest("label").classList.toggle("locked", box.disabled);
  // The meter: where the agent said how much of its plan is used.
  const meter = usageMeter(now.look?.usage);
  const slot = row.querySelector(".meter-slot");
  slot.replaceChildren();
  if (meter) {
    const windows = `agent-${name}-usage`;
    const filled = el("span", { class: "fill" });
    filled.style.width = `${Math.min(100, meter.used)}%`; // (Not a style attribute: the page's CSP.)
    slot.append(
      el(
        "span",
        { class: `plan-meter ${meter.level}`, tabindex: "0", role: "meter", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.min(100, meter.used)), "aria-label": `${label(name)}: ${meter.label}`, "aria-describedby": windows, "data-tip": meter.windows.join("\n") },
        el("span", { class: "track", "aria-hidden": "true" }, filled),
        meter.words,
      ),
      el("span", { id: windows, hidden: true, text: meter.windows.join(". ") }),
    );
  }
  // Its one action.
  const action = row.querySelector(".action-slot");
  const said = { signin: "Sign in", install: "Install", show: "Show", retry: status.words }[status.action];
  const named = { signin: `Sign in to ${label(name)}`, install: `Install ${label(name)}`, show: `Show the ${setupOf(name)?.kind === "install" ? "install" : "sign-in"} of ${label(name)}`, retry: `Set up ${label(name)}${status.words === "Retry" ? " again" : ""}` }[status.action];
  const target = status.action === "retry" ? { "data-retry": name } : { "data-setup": name };
  action.replaceChildren(...(said ? [el("button", { type: "button", class: "setup-action compact", text: said, "aria-label": named, ...target })] : []));
  alertsInto(name, agentAlerts(name, { look: now.look, status: now.status, sandboxed: now.sandboxed, failure: failed }));
  // What it unfolds: how to make it ready, that its work waits, its fields, its details.
  const fix = (now.look || now.status)?.fix;
  const setupNote = row.querySelector(".setup-note");
  const setupDetails = row.querySelector(".setup-details");
  const kind = (now.look || now.status)?.status;
  const unready = Boolean(now.running) || kind === "signin" || status.text === "Not installed";
  setupNote.hidden = !unready && !status.turnsOn;
  setupDetails.hidden = !(unready && fix?.command);
  if (status.turnsOn) setupNote.textContent = "Turning on Use for workers sets it up: Chief of Staff installs what Claude workers need, in a minute or so.";
  if (unready) {
    setupNote.textContent = setupOf(name)?.kind === "install"
      ? "Install runs the official installer in your account and shows its output; then Sign in."
      : status.text === "Not installed"
      ? `${label(name)} isn't installed on this computer, so its work waits. Install it with this command in a terminal, or turn off Use for workers.`
      : `Sign in opens ${label(name)}'s own sign-in page in your browser; Chief of Staff never reads your sign-in. Or run the command in a terminal yourself.`;
    if (fix?.command) {
      setupDetails.replaceChildren(
        el("span", { class: "cmd" }, "Command ", el("code", { text: fix.command })),
        el("button", { type: "button", class: "link", text: "Copy command", "aria-label": `Copy ${fix.command}`, "data-copy": name }),
      );
    }
  }
  const limitedNote = row.querySelector(".limited-note");
  const resets = statusOf(name)?.resets_at;
  limitedNote.hidden = !(status.text.startsWith("At its limit") && resets);
  if (!limitedNote.hidden) limitedNote.textContent = `Work for ${label(name)} waits, and goes on by itself at ${resetTime(resets * 1000)}. For one task meanwhile, type @copilot.`;
  const missing = status.text === "Not installed";
  row.querySelector(".agent-fields").hidden = missing;
  row.querySelector(".for-roles").hidden = missing;
  const about = row.querySelector(".about");
  const look = lookOf(name);
  const signedIn = look?.state ? look.state.split(" · ").slice(1).join(" · ") : "";
  const pays = look?.pays || now.status?.pays || "";
  const parts = [
    look?.version ? `Version ${look.version}` : "",
    signedIn ? `${signedIn[0].toUpperCase()}${signedIn.slice(1)}` : "",
    pays,
  ].filter(Boolean);
  const again = status.works && (now.status?.ways?.length || look?.ways?.length);
  about.replaceChildren(...parts.map((text) => el("span", { text })), ...(again ? [el("button", { type: "button", class: "link", text: "Sign in again", "aria-label": `Sign in to ${label(name)} again`, "data-again": name })] : []));
  about.hidden = !about.children.length;
  const note = row.querySelector(".plan-note");
  note.textContent = look?.note || "";
  note.hidden = !look?.note;
}
function alertsInto(name, alerts) {
  const box = $(`#agent-${name}-alerts`);
  if (!box) return;
  // (A command in one shows as code, not with its marks.) Details stay folded as they were.
  const open = box.querySelector("details")?.open;
  box.replaceChildren(...alerts.flatMap((alert) => [el("p", { class: `agent-alert ${alert.tone}`, html: codeHtml(alert.text) }), ...(alert.details ? [setupDetailsFold(name, alert.details, open)] : [])]));
  box.hidden = !alerts.length;
}
// What a setup that failed printed last, and the command to run instead: a last resort.
function setupDetailsFold(name, setup, open) {
  const fold = el(
    "details",
    { class: "setup-fold" },
    el("summary", { text: "Details" }),
    ...(setup.output?.length ? [el("pre", { text: setup.output.join("\n") })] : []),
    ...(setup.command
      ? [
          el(
            "span",
            { class: "cmd" },
            "Or install it yourself in a terminal: ",
            el("code", { text: setup.command }),
            " ",
            el("button", { type: "button", class: "link", text: "Copy command", "aria-label": `Copy ${setup.command}`, "data-copy-setup": name }),
          ),
        ]
      : []),
  );
  fold.open = Boolean(open);
  return fold;
}
// A setup run's news (agentsetup.js): its row says it at once.
export function setupChanged() {
  if (!$("#settings-dialog")?.open) return;
  for (const name of others) drawRow(name);
  syncNewWork();
}
// The snapshot changed (an agent's status, Copilot's): the rows say so, fields untouched.
export function agentsStateChanged() {
  if (!$("#settings-dialog")?.open || !$("#agent-rows")) return;
  // Set up meanwhile (its Agent SDK): looked at again, for its version, sign-in and models.
  if (looked && !looking && others.some((name) => lookOf(name)?.setup && !statusOf(name)?.setup)) lookAtAgents();
  for (const name of others) drawRow(name);
  drawCopilot();
  syncNewWork();
}

// Chief of Staff's own sessions --------------------------------------------------------------

function ownSessions() {
  const settings = state.snapshot.settings;
  const placed = withAgents();
  const cells = [];
  cells.push(el("span", { class: "h", "aria-hidden": "true", text: "Session" }), el("span", { class: "h", "aria-hidden": "true", text: "Runs on" }), el("span", { class: "h", "aria-hidden": "true", text: "Model and effort" }));
  for (const [role, name] of ROLES) {
    const model = el("select", { name: `model_${role}`, "aria-label": `${name} model on Copilot`, "data-models": "copilot", "data-first": "plain" });
    const effort = el("select", { name: `effort_${role}`, "aria-label": `${name} effort on Copilot`, "data-efforts-for": `model_${role}` });
    fillCopilotModel(model, settings.role_models[role] || "");
    const runs = placed && PLACED.includes(role)
      ? el("span", { class: "runs" }, el("select", { name: `role_${role}`, "aria-label": `${name} runs on`, disabled: true }), el("span", { class: "fixed", text: "Copilot" }))
      : el("span", { class: "fixed", text: "Copilot" });
    const watchdog = role === "watchdog" ? el("input", { type: "checkbox", name: "watchdog_enabled", "aria-label": "Watchdog on: checks workers for stalls and drift" }) : null;
    if (watchdog) watchdog.checked = Boolean(settings.watchdog_enabled);
    cells.push(
      el(watchdog ? "label" : "span", { class: "role" }, watchdog, name),
      runs,
      el("span", { class: "cells", "data-cells": role }, model, effort),
      el("span", { class: "inherits", "data-inherits": role, hidden: true }),
    );
    if (placed && PLACED.includes(role)) {
      const unavailable = el("select", { name: `unavailable_${role}`, "aria-label": `If its agent can't run the ${name}` }, el("option", { value: "copilot", text: "Use Copilot and tell me" }), el("option", { value: "ask", text: "Ask me" }));
      cells.push(
        el("span", { class: "sub", "data-sub": role, hidden: true }, "If it can't run there: ", unavailable, el("span", { class: "on-copilot", "data-on-copilot": role })),
        el("small", { class: "sub role-line", id: `role-${role}-line`, hidden: true }),
      );
    }
  }
  const fallback = el("select", { name: "model", "aria-label": "Default model", "data-models": "copilot", "data-first": "copilot" });
  fillCopilotModel(fallback, settings.model || "");
  cells.push(el("span", { class: "role", text: "Default" }), el("span", { class: "fixed", text: "Copilot" }), el("span", { class: "cells" }, fallback, el("span")), el("span", { class: "inherits", hidden: true }));
  const activity = el("button", { type: "button", id: "watchdog-activity", class: "link quiet", hidden: true, text: "View activity" });
  return el(
    "details",
    { class: "section own", id: "own-sessions" },
    el("summary", {}, el("span", { text: "Chief of Staff's own sessions" }), el("small", { id: "own-summary" })),
    el(
      "div",
      { class: "section-body" },
      el("div", { class: "own-grid" }, ...cells),
      el("small", { class: "own-foot" }, "Default is the model of any session on Copilot without one of its own. Watch for you and helpers use the workers' model (Copilot's row). The Watchdog checks workers for stalls and drift · ", activity),
    ),
  );
}
// Where the Router and the Watchdog run, as the look says (chosen; why one runs on Copilot for
// now; what it does if its agent can't run it).
function fillRoles() {
  for (const role of looked?.roles || []) {
    const choice = field(`role_${role.role}`);
    if (!choice) continue;
    choice.dataset.saved = role.chosen || "copilot";
    choice.dataset.agents = (role.agents || []).join(" ");
    choice.disabled = false;
    if (!choice.dataset.dirty) choice.dataset.keep = role.chosen || "copilot";
    const fallback = field(`unavailable_${role.role}`);
    if (fallback && role.unavailable) {
      fallback.dataset.saved = role.unavailable;
      if (!fallback.dataset.dirty) fallback.value = role.unavailable;
    }
    const line = $(`#role-${role.role}-line`);
    if (line) {
      line.textContent = role.line || "";
      line.dataset.line = role.line || "";
      if (role.line) choice.setAttribute("aria-describedby", line.id);
      else choice.removeAttribute("aria-describedby");
    }
  }
  syncRoles();
}
// Each role's Runs on menu: Copilot and the agents on (drafts count) that can run it; on another
// agent, that agent's model and effort said instead of Copilot's menus (kept, unsent).
function syncRoles() {
  for (const role of PLACED) {
    const choice = field(`role_${role}`);
    if (!choice) continue;
    const can = (choice.dataset.agents || OTHERS.join(" ")).split(" ");
    const on = others.filter((name) => onNow(name) && can.includes(name));
    const keep = choice.dataset.dirty ? choice.value : choice.dataset.keep || (role === "router" ? state.snapshot.agents?.router_on : "") || "copilot";
    const choices = [["copilot", "Copilot"], ...on.map((name) => [name, label(name)])];
    fill(choice, choices, choices.some(([id]) => id === keep) ? keep : "copilot");
    // A menu only while there is a choice: else it runs on Copilot, said as such.
    choice.hidden = !on.length;
    choice.nextElementSibling.hidden = Boolean(on.length);
    const where = choice.value;
    const elsewhere = where !== "copilot";
    form().querySelector(`[data-cells="${role}"]`).hidden = elsewhere;
    const inherits = form().querySelector(`[data-inherits="${role}"]`);
    inherits.hidden = !elsewhere;
    inherits.textContent = elsewhere ? `${label(where)}'s model and effort (its row)` : "";
    const sub = form().querySelector(`[data-sub="${role}"]`);
    sub.hidden = !elsewhere;
    if (elsewhere) {
      const model = field(`model_${role}`).value;
      const effort = field(`effort_${role}`).value;
      sub.querySelector(".on-copilot").textContent = ` · on Copilot: ${model ? copilotName(model) : "the default model"}, ${(EFFORT_WORDS[effort] || "default").toLowerCase()} effort`;
    }
    const line = $(`#role-${role}-line`);
    line.hidden = !line.dataset.line || where === "copilot";
  }
  ownSummary();
}
function ownSummary() {
  const summary = $("#own-summary");
  if (!summary) return;
  const away = PLACED.map((role) => [role, field(`role_${role}`)]).filter(([, choice]) => choice && choice.value !== "copilot");
  const fallback = field("model")?.value || "";
  const watchdog = field("watchdog_enabled")?.checked === false ? " · Watchdog off" : "";
  // Where each runs now: on Copilot for now where its agent can't run it (its line says why).
  const where = ([role, choice]) => {
    const name = role === "router" ? "Router" : "Watchdog";
    const forNow = choice.value === choice.dataset.saved && $(`#role-${role}-line`)?.dataset.line;
    return forNow ? `${name} on Copilot for now` : `${name} on ${label(choice.value)}`;
  };
  summary.textContent = away.length
    ? `${away.map(where).join(" · ")} · others on Copilot${watchdog}`
    : `All on Copilot · ${fallback ? copilotName(fallback) : "Copilot's default model"}${watchdog}`;
}

// The owner's drafts -------------------------------------------------------------------------

// What changed is marked (a menu then sends its value) and what depends on it said again in
// place; nothing is drawn anew.
function changed(event) {
  const input = event.target;
  if (!input.name) return;
  input.dataset.dirty = "1";
  if (input.matches("select[data-models]")) {
    refreshEfforts(input);
    if (input.name === "model") {
      // Workers' first choice says what Default is; menus on Default take its efforts.
      const workers = field("model_worker");
      fillCopilotModel(workers, workers.value);
      for (const select of form().querySelectorAll('select[data-models="copilot"]')) if (!select.value && select !== input) refreshEfforts(select);
    }
  }
  const agent = /^agent_(\w+)_enabled$/.exec(input.name)?.[1];
  if (agent) drawRow(agent);
  if (agent || /^(role|model|effort)_/.test(input.name) || ["model", "watchdog_enabled"].includes(input.name)) syncRoles();
  if (input.name === "agent_new_workers") {
    // Picked there, an agent is used for workers (its switch says so).
    const box = field(`agent_${input.value}_enabled`);
    if (box && !box.checked) {
      box.checked = true;
      box.dataset.dirty = "1";
      drawRow(input.value);
    }
  }
  syncNewWork();
}
function clicked(event) {
  const fold = event.target.closest(".agent-line .fold");
  if (fold) {
    const open = fold.getAttribute("aria-expanded") !== "true";
    fold.setAttribute("aria-expanded", String(open));
    document.getElementById(fold.getAttribute("aria-controls")).hidden = !open;
    return;
  }
  const setup = event.target.closest("[data-setup]");
  if (setup) return openSetup(setup.dataset.setup);
  const again = event.target.closest("[data-again]");
  if (again) return openSetup(again.dataset.again, { again: true });
  const retry = event.target.closest("[data-retry]");
  if (retry) return retrySetup(retry.dataset.retry, agentsStateChanged);
  const copySetup = event.target.closest("[data-copy-setup]");
  if (copySetup) {
    const setup = statusOf(copySetup.dataset.copySetup)?.setup || lookOf(copySetup.dataset.copySetup)?.setup;
    if (setup?.command) copyFix({ command: setup.command });
    return;
  }
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    const name = copy.dataset.copy;
    const fix = (lookOf(name) || statusOf(name))?.fix;
    if (fix) copyFix(fix);
    return;
  }
  if (event.target.closest("#copilot-reconnect")) reconnect();
}
async function reconnect() {
  try {
    state.snapshot = { ...state.snapshot, ...(await api("/api/runtime/reconnect", { method: "POST" })) };
    drawCopilot();
  } catch (error) {
    toast(error.message, "error");
  }
}

// What Save sends ------------------------------------------------------------------------------

// A token count as the page shows it (200,000), and as the owner types it: grouped or not, as
// their locale groups digits; null when it is not a whole number of tokens.
const grouped = (count) => count.toLocaleString();
const GROUP = new Intl.NumberFormat().formatToParts(10000).find((part) => part.type === "group")?.value || ",";
const MOST_TOKENS = 1e12; // As agents.MOST_TOKENS: a typo's extra zeros are refused.
export function tokenCount(raw) {
  const digits = raw.trim().split(GROUP).join("").replace(/[\s,]/g, "");
  if (digits === "") return 0;
  return /^\d+$/.test(digits) && Number(digits) <= MOST_TOKENS ? Number(digits) : null;
}
// Its limit field says what is wrong with what was typed, beside it (and only then).
function checkLimit(input) {
  const count = tokenCount(input.value);
  const error = document.getElementById(input.getAttribute("aria-describedby"));
  const wrong = count === null;
  if (error) {
    error.textContent = wrong ? "A whole number of tokens, or empty for no limit." : "";
    error.hidden = !wrong;
  }
  if (wrong) input.setAttribute("aria-invalid", "true");
  else input.removeAttribute("aria-invalid");
  return count;
}
// The agents' group of a Save: only what changed. A switch is read whatever the look did (an
// agent on can be turned off while it is looked at, or after a look that failed); a field only
// once the look filled it and the owner changed it.
export function agentsBody() {
  if (!others.length) return null;
  const body = {};
  for (const name of others) {
    const entry = {};
    const box = field(`agent_${name}_enabled`);
    if (box && box.checked !== savedOn(name)) entry.enabled = box.checked;
    const look = lookOf(name);
    if (look) {
      const model = field(`agent_${name}_model`);
      const effort = field(`agent_${name}_effort`);
      const limit = field(`agent_${name}_limit`);
      if (model.dataset.dirty && model.value !== (name === "claude" && look.model === "default" ? "" : look.model || "")) entry.model = model.value;
      if (effort.dataset.dirty && effort.value !== (look.effort || "")) entry.effort = effort.value;
      if (limit.dataset.dirty) {
        const count = checkLimit(limit);
        if (count === null) throw Object.assign(new Error("Limit per worker: enter a whole number of tokens."), { field: limit });
        if (count !== (look.limit || 0)) entry.limit = count;
      }
    }
    if (Object.keys(entry).length) body[name] = entry;
  }
  const choice = field("agent_new_workers");
  if (choice?.dataset.dirty && !choice.hidden && choice.value !== choice.dataset.saved) body.new_workers = choice.value;
  const roles = {};
  const unavailable = {};
  for (const role of PLACED) {
    const where = field(`role_${role}`);
    if (where?.dataset.dirty && !where.disabled && where.value !== where.dataset.saved) roles[role] = where.value;
    const fallback = field(`unavailable_${role}`);
    if (fallback?.dataset.dirty && fallback.dataset.saved && fallback.value !== fallback.dataset.saved) unavailable[role] = fallback.value;
  }
  if (Object.keys(roles).length) body.roles = roles;
  if (Object.keys(unavailable).length) body.role_unavailable = unavailable;
  return Object.keys(body).length ? body : null;
}
// After a save: what it saved is what the page shows as saved (the next opening draws again).
export function agentsSaved() {
  looked = null;
}
