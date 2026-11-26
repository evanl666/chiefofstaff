// Settings → Agents & models (docs/design/settings-v2.md §3, agents-anywhere.md §5): who does
// the work, and with which model. Who does the work comes first: new workers and each of Chief
// of Staff's own sessions, each with the agent it runs on, and its model and effort there (a
// pair of menus per agent, the chosen agent's shown: switching keeps each agent's), and once
// what one does if its agent can't run it. Then one row per agent (its status in words, one
// action, its switch, a meter of its plan; unfolded, its limit per worker and its details).
//
// Drawn once each time Settings opens, from what is saved and the snapshot (work,
// agents_status): nothing here asks the server until the page is shown. Then, while another
// agent is installed or on, the look (GET /api/agents: a short process of each agent's own,
// never its backend, and never /api/models?agent=) updates the rows and menus in place, keeping
// every field's value and whether the owner changed it (data-dirty). A menu keeps the saved
// value it can't list and is sent only once the owner changed it (§8.7).
import { $, api, el, toast } from "./dom.js";
import { state } from "./state.js";
import { copyFix, endsLine, modelText, statusOf } from "./agentpick.js";
import { openSetup, retrySetup, setupOf, setupWords } from "./agentsetup.js";
import { resetTime } from "./board.js";
import { codeHtml } from "./text.js";

export const AGENT_LABELS = { copilot: "Copilot", codex: "Codex", claude: "Claude Code" };
// As the @ list and the menus say them.
const SHORT = { copilot: "Copilot", codex: "Codex", claude: "Claude" };
const OTHERS = ["codex", "claude"];
const ALL = ["copilot", ...OTHERS];
export const EFFORT_WORDS = { none: "None", low: "Low", medium: "Medium", high: "High", xhigh: "Extra high", max: "Max" };
// Copilot's efforts where its model's are not known (Copilot's own default, a model not listed).
const ANY_EFFORT = ["low", "medium", "high", "xhigh", "max"];
// Who does the work: new workers, then Chief of Staff's own sessions (agents.KINDS), each with
// what it does on hover.
const KINDS = [
  ["workers", "New workers", "The agent, model and effort new work starts with. One task can go elsewhere: type @ and an agent."],
  ["router", "Router", "Reads what you ask and hands it to workers."],
  ["watchdog", "Watchdog", "Checks workers for stalls and drift."],
  ["slack", "Slack", "Drafts your Slack replies for your OK."],
  ["connector", "Watch for you", "Runs your watches, and learns how you write from what you wrote elsewhere."],
  ["helpers", "Helpers", "Learn how you work, suggest checks and your profile, and message you when you're away."],
];
const ROLE_KINDS = KINDS.slice(1).map(([kind]) => kind);
// Copilot's model and effort Settings holds for these (role_models, role_efforts).
const SETTINGS_ROLE = { workers: "worker", router: "router", slack: "slack", watchdog: "watchdog" };
const GUIDE = "https://github.com/evanl666/chiefofstaff/blob/main/docs/guide.md#workers-on-other-coding-agents";
const COPILOT_PAYS = "Uses your Copilot plan";

const form = () => $("#settings-form");
const field = (name) => form()?.elements.namedItem(name) || null;
const label = (name) => AGENT_LABELS[name] || state.snapshot?.agents?.names?.[name] || name;
const short = (name) => SHORT[name] || label(name);
// A kind's model and effort fields on an agent: Settings' own for Copilot where it holds them,
// an agent's own for its workers, else the kind's on it (agents.MODELS).
function fieldsOf(kind, agent) {
  if (agent === "copilot" && SETTINGS_ROLE[kind]) return [`model_${SETTINGS_ROLE[kind]}`, `effort_${SETTINGS_ROLE[kind]}`];
  if (kind === "workers") return [`agent_${agent}_model`, `agent_${agent}_effort`];
  return [`kind_${kind}_${agent}_model`, `kind_${kind}_${agent}_effort`];
}
// Who does the work as saved (the snapshot's `work`).
const work = () => state.snapshot.work || { agents: ["copilot"], default: "copilot", new_workers: state.snapshot.agents?.new_workers || "copilot", roles: {}, unavailable: {}, models: {} };
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
  if (now.status === "ready") return on ? { text: "Ready", tone: "ready", action: "", works: true } : { text: "Ready · off", tone: "off", action: "", works: true };
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
// A model as the page names it on its agent ("" for its default: the agent's default by name).
function modelName(agent, id) {
  if (agent === "copilot") return id ? copilotName(id) : copilotName(work().copilot_default || "") || "Copilot's default";
  return agentModelName(agent, id) || `${label(agent)}'s default`;
}
const savedOn = (name) => name === "copilot" || (looked ? Boolean(lookOf(name)?.enabled) : Boolean(state.snapshot?.agents?.on?.includes(name)));
const onNow = (name) => name === "copilot" || Boolean(field(`agent_${name}_enabled`)?.checked);
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
// The agents on now (drafts count), Copilot's place first: what each kind of work can run on.
const agentsOnNow = () => ALL.filter((name) => (name === "copilot" || others.includes(name)) && onNow(name));
const paysOf = (name) => (name === "copilot" ? COPILOT_PAYS : lookOf(name)?.pays || statusOf(name)?.pays || "");
const lower = (text) => (text ? `${text[0].toLowerCase()}${text.slice(1)}` : "");

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
  const activity = el("button", { type: "button", id: "watchdog-activity", class: "link quiet", hidden: true, text: "Its activity" });
  page.replaceChildren(
    workGrid(),
    el("small", { class: "page-foot", id: "work-note" }, "Helpers learn how you work, suggest checks and your profile, and message you when you're away. The Watchdog checks workers for stalls and drift. ", activity),
    el("div", { class: "section-label", id: "agents-label", text: "Agents" }),
    el("div", { class: "box agent-rows", id: "agent-rows", "aria-labelledby": "agents-label" }, copilotRow(), ...others.map(agentRow)),
    el("small", { id: "agents-none", class: "page-foot", role: "status", hidden: true }),
    agentsFoot(),
  );
  for (const select of form().querySelectorAll('#agents-page select[data-models="copilot"]')) refreshEfforts(select);
  for (const name of others) drawRow(name);
  drawCopilot();
  syncWork();
  page.oninput = changed;
  page.onchange = changed;
  page.onclick = clicked;
}
// What else it runs on: the agents not installed here, and where to read how (the @ list
// installs them too); with all three here, what differs between them.
function agentsFoot() {
  const missing = OTHERS.filter((name) => !others.includes(name)).map(label);
  if (!missing.length) return el("small", { class: "page-foot", id: "agents-guide" }, "What differs between agents: ", el("a", { class: "link", href: GUIDE, target: "_blank", rel: "noreferrer", text: "the guide" }));
  return el(
    "small",
    { class: "page-foot", id: "agents-guide" },
    `Chief of Staff also runs on ${missing.join(" and ")}: install ${missing.length > 1 ? "either" : "it"} and it shows here. `,
    el("a", { class: "link", href: GUIDE, target: "_blank", rel: "noreferrer", text: "How" }),
  );
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
  syncWork();
  section.dataset.looked = "done";
}

function noAgentWords(supported = ["the Codex CLI"]) {
  const listed = supported.length > 1 ? `${supported.slice(0, -1).join(", ")} or ${supported[supported.length - 1]}` : supported[0];
  return `No supported coding agent found. Supported: ${listed} — install ${supported.length > 1 ? "one" : "it"}, then open this again.`;
}

// Who does the work ----------------------------------------------------------------------------

// One grid: a row per kind of work (where it runs; its model and effort there), then what one
// does if its agent can't run it.
function workGrid() {
  const saved = work();
  const how = new Set(ROLE_KINDS.map((kind) => saved.unavailable?.[kind] || "next"));
  const fallback = el(
    "select",
    { name: "work_unavailable", "aria-label": "If its agent can't run it" },
    el("option", { value: "next", text: "Use another that can, and tell me" }),
    el("option", { value: "ask", text: "Wait, and ask me" }),
    how.size > 1 ? el("option", { value: "mixed", text: "As before (differs by session)" }) : null,
  );
  fallback.dataset.saved = how.size > 1 ? "mixed" : [...how][0];
  fallback.value = fallback.dataset.saved;
  return el(
    "div",
    { class: "box work-grid", role: "group", "aria-label": "Who does the work" },
    el("div", { class: "work-head", "aria-hidden": "true" }, el("span"), el("span", { text: "Runs on" }), el("span", { text: "Model" }), el("span", { text: "Effort" })),
    ...KINDS.map(([kind, name, tip]) => workRow(kind, name, tip)),
    el(
      "div",
      { class: "work-foot", id: "work-foot" },
      el("span", { class: "alone", id: "work-alone", hidden: true }),
      el("span", { class: "choose", id: "work-choose" }, el("span", { text: "If its agent can't run it" }), fallback, el("span", { class: "order", id: "work-order" })),
    ),
  );
}
function workRow(kind, name, tip) {
  const saved = work();
  const chosen = kind === "workers" ? saved.new_workers : saved.roles?.[kind] || saved.default;
  const select = el("select", { name: kind === "workers" ? "agent_new_workers" : `work_${kind}_agent`, "aria-label": `${name}: runs on`, "aria-describedby": `work-${kind}-line` });
  select.dataset.saved = chosen || "copilot";
  select.dataset.kind = kind;
  const runs = el("span", { class: "runs" }, el("span", { class: "agent-mark", "data-agent": "copilot", "aria-hidden": "true" }), select, el("span", { class: "plain" }));
  const watchdog = kind === "watchdog" ? el("input", { type: "checkbox", name: "watchdog_enabled", "aria-label": "Watchdog on: checks workers for stalls and drift" }) : null;
  if (watchdog) watchdog.checked = Boolean(state.snapshot.settings.watchdog_enabled);
  return el(
    "div",
    { class: `work-row${kind === "workers" ? " lead" : ""}`, "data-kind": kind },
    el(watchdog ? "label" : "span", { class: "name", title: tip }, watchdog, name),
    runs,
    ...ALL.filter((agent) => agent === "copilot" || others.includes(agent)).map((agent) => modelPair(kind, name, agent)),
    el("small", { class: "line warn", id: `work-${kind}-line`, hidden: true }),
    kind === "workers" ? el("small", { class: "line", id: "work-workers-hint" }) : null,
  );
}
// A kind's model and effort on one agent: shown while it runs there, kept while it does not.
function modelPair(kind, name, agent) {
  const [modelField, effortField] = fieldsOf(kind, agent);
  const saved = work().models?.[kind]?.[agent] || {};
  const model = el("select", { name: modelField, "data-models": agent, "data-first": "plain", "aria-label": `${name}: model on ${label(agent)}` });
  const effort = el("select", { name: effortField, "data-efforts-for": modelField, "aria-label": `${name}: effort on ${label(agent)}` });
  const shown = agent === "claude" && saved.model === "default" ? "" : saved.model || "";
  model.dataset.savedValue = saved.model || "";
  effort.dataset.savedValue = saved.effort || "";
  if (agent === "copilot") fillCopilotModel(model, shown);
  else {
    // Kept as saved until the look lists its models (agents.configured_models said them).
    fill(model, modelChoices([], shown, firstChoice(agent)), shown);
    model.disabled = true;
    effort.disabled = true;
  }
  fill(effort, effortChoices(agent === "copilot" ? null : [], saved.effort || ""), saved.effort || "");
  return el("span", { class: "cells", "data-cells": kind, "data-agent": agent, hidden: true }, model, effort);
}
// A model menu's first choice: the agent's own default, by name where known.
function firstChoice(agent) {
  if (agent === "copilot") {
    const named = work().copilot_default ? copilotName(work().copilot_default) : "";
    return named ? `Default (${named})` : "Copilot's default";
  }
  // (By the model it stands for now, where its agent says: else plainly its agent's.)
  const named = defaultModel(agent) ? agentModelName(agent, "") : "";
  return named && named !== "Claude Code's default" ? `Default (${named})` : `${label(agent)}'s default`;
}
// Each agent's menus, once the look listed its models (a value the owner changed meanwhile stays).
function fillWorkModels(name) {
  for (const model of form().querySelectorAll(`#agents-page .cells[data-agent="${name}"] select[data-models]`)) {
    const effort = model.closest(".cells").querySelector("select[data-efforts-for]");
    const saved = name === "claude" && model.dataset.savedValue === "default" ? "" : model.dataset.savedValue || "";
    model.disabled = false;
    fillAgentModel(name, model, model.dataset.dirty ? model.value : saved);
    refreshEfforts(model, effort.dataset.dirty ? effort.value : effort.dataset.savedValue || "");
  }
}
// The agent a Runs on menu means now (Last used: the one last chosen for a worker).
function meant(select) {
  if (select.value !== "last") return select.value;
  const last = looked?.last_used || state.snapshot.agents?.new_workers || "copilot";
  return agentsOnNow().includes(last) ? last : agentsOnNow()[0] || "copilot";
}
// An agent in a Runs on menu: its name, and what keeps it from working now, if anything.
function choiceText(name) {
  if (name === "copilot") return short(name);
  const status = agentStatus(name, nowOf(name));
  return status.works ? short(name) : `${short(name)} · ${status.text}`;
}
// As the form has it now (drafts count): each row's menu (the agents on), the menus of the agent
// it runs on, where it runs for now and why, and what the foot says.
function syncWork() {
  const on = agentsOnNow();
  const several = on.length > 1;
  for (const [kind] of KINDS) {
    const row = form().querySelector(`#agents-page .work-row[data-kind="${kind}"]`);
    if (!row) continue;
    const select = row.querySelector("select[data-kind]");
    const keep = select.dataset.dirty ? select.value : select.dataset.saved;
    const choices = on.map((name) => [name, choiceText(name)]);
    if (kind === "workers") choices.push(["last", "Last used"]);
    // One turned off (in this draft too): it runs on the first agent on, as Save leaves it.
    fill(select, choices, choices.some(([id]) => id === keep) ? keep : on[0] || "copilot");
    select.hidden = !several;
    const agent = meant(select);
    const plain = row.querySelector(".runs .plain");
    plain.textContent = short(agent);
    plain.hidden = several;
    row.querySelector(".runs .agent-mark").dataset.agent = agent;
    for (const cells of row.querySelectorAll(".cells")) cells.hidden = cells.dataset.agent !== agent;
    const line = $(`#work-${kind}-line`);
    line.textContent = select.value === select.dataset.saved ? lineOf(kind) : "";
    line.hidden = !line.textContent;
  }
  const workers = field("agent_new_workers");
  const now = workers.value === workers.dataset.saved && looked?.workers?.now ? looked.workers.now : meant(workers);
  const other = on.find((name) => name !== now);
  const pays = paysOf(now);
  $("#work-workers-hint").textContent = `${workers.value === "last" ? `Now ${short(now)}. ` : ""}${pays ? `${pays}.` : ""}${other ? ` For one task, type @${other} or say “do this with ${short(other)}”.` : ""}`.trim();
  $("#work-alone").hidden = several;
  $("#work-alone").textContent = several ? "" : `If ${short(on[0] || "copilot")} can't run one of these, it waits, and Home says why.`;
  $("#work-choose").hidden = !several;
  $("#work-order").textContent = several ? `In this order: ${on.map(label).join(", ")}.` : "";
}
// Where a kind runs for now and why, as the look found it: on another agent (its model there,
// whose plan), or waiting ("" while it runs where it was put, or before the look).
function lineOf(kind) {
  const view = kind === "workers" ? looked?.workers : looked?.roles?.find((role) => role.role === kind);
  if (!view?.why) return "";
  if (!view.now) return kind === "workers" ? `New workers wait for ${short(view.chosen)}: ${view.why}` : `Waits for ${short(view.chosen)}: ${view.why}`;
  const pays = paysOf(view.now);
  const there = `${modelName(view.now, view.now_model)}${pays ? `, ${lower(pays)}` : ""}`;
  if (kind === "workers") return `New workers start on ${short(view.now)} (${there}) until then: ${view.why}`;
  if (view.waits) return `Waits for ${short(view.chosen)}: ${view.why}`;
  return `On ${short(view.now)} for now (${there}): ${view.why}`;
}

// The agents ------------------------------------------------------------------------------------

// An agent's row: one line (fold, mark, name, status; on the right its meter, its one action,
// its switch), its alert lines, and what it unfolds.
function rowShell(name, { aside, body }) {
  const id = `agent-${name}`;
  return el(
    "div",
    { class: "agent", id, "data-agent": name },
    el(
      "div",
      { class: "agent-line" },
      el("button", { type: "button", class: "fold", "aria-expanded": "false", "aria-controls": `${id}-body`, "aria-label": `${label(name)}: ${name === "copilot" ? "limit and details" : "limit, sign-in and details"}` }),
      el("div", { class: "who" }, el("span", { class: "agent-mark", "data-agent": name, "aria-hidden": "true" }), el("strong", { text: label(name) }), el("span", { class: "status", id: `${id}-status`, role: "status" })),
      el("div", { class: "aside" }, ...aside),
    ),
    el("div", { class: "agent-alerts", id: `${id}-alerts`, hidden: true }),
    el("div", { class: "agent-body", id: `${id}-body`, hidden: true }, ...body),
  );
}
function copilotRow() {
  const settings = state.snapshot.settings;
  const limit = el("input", { name: "worker_credit_limit", type: "number", min: "0", step: "1", placeholder: "No limit", value: settings.worker_credit_limit ? String(settings.worker_credit_limit) : "" });
  const command = el("input", { name: "copilot_command", autocomplete: "off", spellcheck: "false", value: settings.copilot_command || "" });
  const reconnect = el("button", { type: "button", class: "setup-action compact", id: "copilot-reconnect", hidden: true, text: "Reconnect", "aria-label": "Reconnect to Copilot" });
  return rowShell("copilot", {
    aside: [reconnect, el("span", { class: "always", text: "Always on" })],
    body: [
      el("div", { class: "agent-fields" }, el("label", {}, el("span", { text: "Limit per worker (credits)" }), limit, el("small", { text: "Empty: none. At least 30." }))),
      el(
        "div",
        { class: "agent-details" },
        el("span", { text: COPILOT_PAYS }),
        el("span", { id: "copilot-warning", hidden: true }),
        el("label", { class: "cmd" }, el("span", { text: "Command" }), command),
      ),
    ],
  });
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
  const box = el("input", { type: "checkbox", name: `agent_${name}_enabled`, "aria-label": `Use ${label(name)}`, "aria-describedby": `agent-${name}-status agent-${name}-alerts` });
  box.checked = savedOn(name);
  const errorId = `agent-${name}-limit-error`;
  const limit = el("input", { name: `agent_${name}_limit`, type: "text", inputmode: "numeric", placeholder: "No limit", autocomplete: "off", disabled: true, "aria-describedby": errorId });
  limit.addEventListener("blur", () => {
    const count = checkLimit(limit);
    if (count) limit.value = grouped(count);
  });
  limit.addEventListener("input", () => limit.hasAttribute("aria-invalid") && checkLimit(limit));
  return rowShell(name, {
    aside: [
      el("span", { class: "meter-slot" }),
      el("span", { class: "action-slot" }),
      el("label", { class: "check use" }, box, " Use"),
    ],
    body: [
      el("p", { class: "agent-note setup-note", hidden: true }),
      el("div", { class: "agent-details setup-details", hidden: true }),
      el("p", { class: "agent-note limited-note", hidden: true }),
      el("div", { class: "agent-fields" }, el("label", {}, el("span", { text: "Limit per worker (tokens)" }), limit, el("small", { id: errorId, class: "field-error", hidden: true }))),
      el("div", { class: "agent-details about", hidden: true }),
      el("small", { class: "agent-note plan-note", hidden: true }),
    ],
  });
}
// Its fields as saved, once the look says (a value the owner changed meanwhile stays): its
// switch and limit, and every menu of its models (Who does the work).
function fillAgentFields(name, look) {
  const limit = field(`agent_${name}_limit`);
  limit.dataset.saved = "1";
  limit.disabled = false;
  limit.dataset.savedValue = String(look.limit || 0);
  if (!limit.dataset.dirty) limit.value = look.limit ? grouped(look.limit) : "";
  // Its workers' model and effort as saved: what the look says, over the snapshot's.
  const model = field(`agent_${name}_model`);
  const effort = field(`agent_${name}_effort`);
  if (model && effort) {
    model.dataset.savedValue = look.model || "";
    effort.dataset.savedValue = look.effort || "";
    model.dataset.saved = effort.dataset.saved = "1";
  }
  fillWorkModels(name);
  const box = field(`agent_${name}_enabled`);
  if (!box.dataset.dirty) box.checked = Boolean(look.enabled);
}
function fillAgentModel(name, select, value) {
  const listed = agentModels(name).filter((model) => !(name === "claude" && model.id === "default"));
  fill(select, modelChoices(listed, value, firstChoice(name), (model) => modelText(name, model)), value);
}
function fillCopilotModel(select, value) {
  fill(select, modelChoices(copilotModels(), value, firstChoice("copilot")), value);
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
    const id = modelSelect.value || work().copilot_default || "";
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
  if (!$("#settings-dialog")?.open || !$("#agents-page .work-grid")) return;
  for (const select of form().querySelectorAll('#agents-page select[data-models="copilot"]')) {
    fillCopilotModel(select, select.value);
    refreshEfforts(select);
  }
  syncWork();
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
  box.disabled = (!on && !box.checked && !status.works && !status.turnsOn) || (box.checked && agentsOnNow().length === 1);
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
  if (status.turnsOn) setupNote.textContent = "Turning on Use sets it up: Chief of Staff installs what Claude workers need, in a minute or so.";
  if (unready) {
    setupNote.textContent = setupOf(name)?.kind === "install"
      ? "Install runs the official installer in your account and shows its output; then Sign in."
      : status.text === "Not installed"
      ? `${label(name)} isn't installed on this computer, so its work waits. Install it with this command in a terminal, or turn off Use.`
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
  row.querySelector(".agent-fields").hidden = status.text === "Not installed";
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
  if (!$("#settings-dialog")?.open || !$("#agents-page .work-grid")) return;
  for (const name of others) drawRow(name);
  syncWork();
}
// The snapshot changed (an agent's status, Copilot's): the rows say so, fields untouched.
export function agentsStateChanged() {
  if (!$("#settings-dialog")?.open || !$("#agent-rows")) return;
  // Set up meanwhile (its Agent SDK): looked at again, for its version, sign-in and models.
  if (looked && !looking && others.some((name) => lookOf(name)?.setup && !statusOf(name)?.setup)) lookAtAgents();
  for (const name of others) drawRow(name);
  drawCopilot();
  syncWork();
}

// The owner's drafts -------------------------------------------------------------------------

// What changed is marked (a menu then sends its value) and what depends on it said again in
// place; nothing is drawn anew.
function changed(event) {
  const input = event.target;
  if (!input.name) return;
  input.dataset.dirty = "1";
  if (input.matches("select[data-models]")) refreshEfforts(input);
  const agent = /^agent_(\w+)_enabled$/.exec(input.name)?.[1];
  if (agent) {
    for (const name of others) drawRow(name);
    drawCopilot();
  }
  if (input.matches("select[data-kind]") && ALL.includes(input.value)) {
    // Picked there, an agent is used (its switch says so).
    const box = field(`agent_${input.value}_enabled`);
    if (box && !box.checked) {
      box.checked = true;
      box.dataset.dirty = "1";
      drawRow(input.value);
    }
  }
  syncWork();
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
    const fix = (name === "copilot" ? statusOf(name) : lookOf(name) || statusOf(name))?.fix;
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
// agent on can be turned off while it is looked at, or after a look that failed); another
// agent's field only once the look filled it and the owner changed it; where each kind of work
// runs, and Copilot's models of Watch for you and the helpers, once changed.
export function agentsBody() {
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
      if (model?.dataset.dirty && model.value !== (name === "claude" && look.model === "default" ? "" : look.model || "")) entry.model = model.value;
      if (effort?.dataset.dirty && effort.value !== (look.effort || "")) entry.effort = effort.value;
      if (limit.dataset.dirty) {
        const count = checkLimit(limit);
        if (count === null) throw Object.assign(new Error("Limit per worker: enter a whole number of tokens."), { field: limit });
        if (count !== (look.limit || 0)) entry.limit = count;
      }
    }
    if (Object.keys(entry).length) body[name] = entry;
  }
  const workers = field("agent_new_workers");
  if (workers?.dataset.dirty && workers.value !== workers.dataset.saved) body.new_workers = workers.value;
  const roles = {};
  for (const kind of ROLE_KINDS) {
    const where = field(`work_${kind}_agent`);
    if (where?.dataset.dirty && where.value !== where.dataset.saved) roles[kind] = where.value;
  }
  if (Object.keys(roles).length) body.roles = roles;
  const how = field("work_unavailable");
  if (how?.dataset.dirty && how.value !== how.dataset.saved && how.value !== "mixed") body.role_unavailable = Object.fromEntries(ROLE_KINDS.map((kind) => [kind, how.value]));
  // Each kind's model and effort on an agent that Settings does not hold elsewhere.
  const models = {};
  for (const select of form().querySelectorAll('#agents-page select[name^="kind_"]')) {
    const [, kind, agent, key] = /^kind_(\w+?)_(copilot|codex|claude)_(model|effort)$/.exec(select.name) || [];
    if (!kind || !select.dataset.dirty || select.disabled || select.value === (select.dataset.savedValue || "")) continue;
    if (agent !== "copilot" && !lookOf(agent)) continue;
    models[kind] = models[kind] || {};
    models[kind][agent] = { ...models[kind][agent], [key]: select.value };
  }
  if (Object.keys(models).length) body.models = models;
  return Object.keys(body).length ? body : null;
}
// After a save: what it saved is what the page shows as saved (the next opening draws again).
export function agentsSaved() {
  looked = null;
}
