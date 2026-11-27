// Sign in and Install for another coding agent (agentsetup.py): where an agent says "Sign-in
// needed" or "Not installed", one click runs that agent's own CLI for the owner (Chief of Staff
// never signs in for them: they finish on the agent's own page). One dialog shows the run: the
// page to open (Open sign-in page, Copy link), a one-time code, a field for a code it asks to be
// pasted, its output; it turns Ready by itself. Install shows its command, word for word, first.
import { api, el, toast } from "./dom.js";
import { state } from "./state.js";
import { agentMark, agentName, closeAgentPopover, copyFix, statusOf, statuses } from "./agentpick.js";

const TITLES = { codex: "Codex", claude: "Claude Code" };
const title = (name) => TITLES[name] || agentName(name);
// What a run is doing now, from its events (agent_setup) or the page's last snapshot.
export const setupOf = (name) => state.agentSetups?.[name] || state.snapshot?.agent_setups?.[name] || null;
const running = (name) => setupOf(name)?.state === "running";
// The status words while a run of it is on: "Signing in…", "Installing…".
export function setupWords(name) {
  const run = setupOf(name);
  if (run?.state !== "running") return "";
  return run.kind === "install" ? "Installing…" : "Signing in…";
}

// The button that makes an agent ready, where a control can be: Sign in (its sign-in needed),
// Install (not installed), Retry (what Chief of Staff sets up itself failed: Claude's Agent SDK;
// Set up when it is needed again); none for one that is ready or can't run for another reason.
export function setupAction(status, { compact = false, after } = {}) {
  const kind = setupKind(status);
  if (!kind) return null;
  if (kind === "retry") {
    const words = status.setup.state === "failed" ? "Retry" : "Set up";
    return el("button", {
      type: "button",
      class: `setup-action${compact ? " compact" : ""}`,
      text: words,
      "aria-label": `Set up ${title(status.name)}${words === "Retry" ? " again" : ""}`,
      onmousedown: (event) => event.preventDefault(),
      onclick: (event) => {
        event.stopPropagation();
        retrySetup(status.name, after);
      },
    });
  }
  const busy = running(status.name);
  const words = busy ? setupWords(status.name) : kind === "install" ? "Install" : "Sign in";
  const label = busy ? `Show the ${kind === "install" ? "install" : "sign-in"} of ${title(status.name)}` : kind === "install" ? `Install ${title(status.name)}` : `Sign in to ${title(status.name)}`;
  return el("button", {
    type: "button",
    class: `setup-action${compact ? " compact" : ""}`,
    text: words,
    "aria-label": label,
    onmousedown: (event) => event.preventDefault(),
    onclick: (event) => {
      event.stopPropagation();
      openSetup(status.name);
    },
  });
}
export function setupKind(status) {
  if (!status || status.status === "ready") return "";
  if (["failed", "needed"].includes(status.setup?.state)) return "retry";
  if (status.status === "signin" && status.ways?.length) return "signin";
  if (status.status === "missing" && status.install?.length) return "install";
  return "";
}
// Chief of Staff sets up again what it sets up itself (Claude's Agent SDK): the page then says
// "Setting up Claude…" until it is done (its status, from the snapshot).
export async function retrySetup(name, after) {
  try {
    const { agents_status: found } = await api(`/api/agents/${name}/setup/retry`, { method: "POST" });
    state.snapshot = { ...state.snapshot, agents_status: found };
    after?.();
  } catch (error) {
    toast(error.message, "error");
  }
}

// Sign in (or Install), and the command to run it in a terminal instead, copied.
export function setupLine(status, after) {
  const action = setupAction(status, { compact: true, after });
  if (!action) return null;
  const copy = status.fix?.command
    ? el("button", {
        type: "button",
        class: "link copy-fix",
        text: "Copy command",
        title: `Copy ${status.fix.command}, to run it in a terminal yourself`,
        onmousedown: (event) => event.preventDefault(),
        onclick: (event) => {
          event.stopPropagation();
          copyFix(status.fix);
        },
      })
    : "";
  return el("span", { class: "agent-fix setup-line" }, action, copy);
}

// The dialog: one at a time, for one agent.
let dialog = null;
let ticker = 0;
const kept = { code: "", key: "" };
export function openSetup(name, { again = false } = {}) {
  closeSetup();
  closeAgentPopover();
  const status = statusOf(name) || { name };
  dialog = el("dialog", { class: "agent-setup", "aria-labelledby": "agent-setup-title" });
  dialog.agent = name;
  dialog.mode = "";
  dialog.addEventListener("close", () => {
    clearInterval(ticker);
    dialog?.remove();
    dialog = null;
  });
  document.body.append(dialog);
  dialog.showModal();
  ticker = setInterval(tick, 1000);
  const run = setupOf(name);
  if (run?.state === "running") return draw();
  if (setupKind(status) === "install") {
    dialog.mode = "consent";
    return draw();
  }
  if (status.status === "ready" && !again) return draw();
  signIn(name, "browser", { confirm: false });
}
export function closeSetup() {
  if (dialog?.open) dialog.close();
}
// What the event stream says of a run: shown at once where its dialog is open.
export function heardSetup(view) {
  // Only what is newer than what it shows: one it replaced (ending), a request's answer
  // after the events of the run it started.
  const known = setupOf(view.agent);
  const older = known && (view.started < known.started || (view.started === known.started && view.seq < known.seq));
  if (!older) state.agentSetups = { ...(state.agentSetups || {}), [view.agent]: view };
  if (dialog?.agent === view.agent) draw();
  // Done: its row says Ready once the page's state is read again (a "state" event follows).
}

async function signIn(name, method, { confirm = false, key = "" } = {}) {
  dialog.mode = "starting";
  dialog.method = method;
  draw();
  try {
    const view = await api(`/api/agents/${name}/signin`, { method: "POST", body: { method, confirm, key } });
    kept.key = "";
    if (dialog) dialog.mode = "";
    heardSetup(view);
  } catch (error) {
    if (!dialog) return;
    if (error.anyway) {
      // Signed in already: asked first (Codex signs out as it begins).
      dialog.mode = "confirm";
      dialog.confirm = { text: error.message, anyway: error.anyway, method, key };
    } else {
      dialog.mode = "error";
      dialog.error = error.message;
    }
    draw();
  }
}
async function install(name, method) {
  dialog.mode = "starting";
  draw();
  try {
    const view = await api(`/api/agents/${name}/install`, { method: "POST", body: { method } });
    if (dialog) dialog.mode = "";
    heardSetup(view);
  } catch (error) {
    if (!dialog) return;
    dialog.mode = "error";
    dialog.error = error.message;
    draw();
  }
}
async function cancel(name) {
  try {
    heardSetup(await api(`/api/agents/${name}/setup/cancel`, { method: "POST" }));
  } catch (error) {
    toast(error.message, "error");
  }
}
async function sendCode(name, input, button) {
  const text = input.value.trim();
  if (!text) return input.focus();
  button.disabled = true;
  try {
    heardSetup(await api(`/api/agents/${name}/setup/input`, { method: "POST", body: { text } }));
    kept.code = "";
  } catch (error) {
    button.disabled = false;
    toast(error.message, "error");
  }
}
async function copy(text, said) {
  try {
    await navigator.clipboard.writeText(text);
    for (const earlier of document.querySelectorAll("#toasts .toast.copied-fix")) earlier.remove();
    toast(said, "copied-fix");
  } catch (error) {
    toast(error.message, "error");
  }
}
const elapsed = (since) => {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - since));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
};
function tick() {
  const run = dialog && setupOf(dialog.agent);
  const clock = dialog?.querySelector(".setup-elapsed");
  if (run?.state === "running" && clock) clock.textContent = elapsed(run.started);
}

function draw() {
  if (!dialog) return;
  const name = dialog.agent;
  const status = statusOf(name) || { name };
  const run = setupOf(name);
  const active = document.activeElement;
  const focusKey = dialog.contains(active) ? active.dataset.key : "";
  // A fold the owner opened (Other ways, Show output) stays open as what it shows is drawn again.
  const opened = new Set([...dialog.querySelectorAll("details[open]")].map((fold) => fold.className));
  const isInstall = dialog.mode === "consent" || (run?.kind === "install" && !["starting", "confirm"].includes(dialog.mode));
  const heading = el("h2", { id: "agent-setup-title" }, agentMark(name), ` ${isInstall ? "Install" : "Sign in to"} ${title(name)}`);
  const body = el("div", { class: "agent-setup-body" });
  const footer = el("footer", {}, el("span", { class: "spacer" }));
  const close = el("button", { type: "button", class: "text", text: "Close", "data-key": "close", onclick: () => dialog.close() });
  if (dialog.mode === "consent") consent(body, footer, status);
  else if (dialog.mode === "starting") body.append(waiting(isInstall ? "Starting the installer…" : `Starting ${title(name)}’s sign-in…`));
  else if (dialog.mode === "confirm") {
    body.append(el("p", { text: dialog.confirm.text }));
    footer.append(
      el("button", { type: "button", class: "text", text: "Keep it", "data-key": "keep", onclick: () => dialog.close() }),
      el("button", { type: "button", text: dialog.confirm.anyway, "data-key": "anyway", onclick: () => signIn(name, dialog.confirm.method, { confirm: true, key: dialog.confirm.key }) }),
    );
  } else if (dialog.mode === "error") {
    body.append(el("p", { class: "setup-error", role: "alert", text: dialog.error }));
    footer.append(close);
  } else if (dialog.mode === "key") keyForm(body, footer, name);
  else if (!run && status.status === "ready") {
    body.append(el("p", { text: `${title(name)} is ready.${status.pays ? ` ${status.pays}.` : ""}` }));
    footer.append(close, el("button", { type: "button", class: "secondary", text: "Sign in again", "data-key": "again", onclick: () => signIn(name, "browser") }));
  } else if (run) runView(body, footer, run, status, close);
  for (const fold of body.querySelectorAll("details")) if (opened.has(fold.className)) fold.open = true;
  dialog.replaceChildren(heading, body, footer);
  const again = focusKey && dialog.querySelector(`[data-key="${focusKey}"]`);
  (again || dialog.querySelector("[data-primary]") || footer.querySelector("button:last-child"))?.focus();
}
function waiting(text, since = 0) {
  return el("p", { class: "setup-waiting", role: "status" }, el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { text }), since ? el("span", { class: "setup-elapsed", text: elapsed(since) }) : "");
}
function output(run, open = false) {
  if (!run.tail?.length) return "";
  return el("details", { class: "setup-output", open: open || undefined }, el("summary", { text: "Show output" }), el("pre", { text: run.tail.join("\n") }));
}
// Install, before it runs: the exact command as the owner's consent, Copy command, and the
// other installers here (Homebrew, npm) under Other ways. On Windows: shown, to run yourself.
function consent(body, footer, status) {
  const [first, ...others] = status.install || [];
  if (!first) return;
  const name = status.name;
  if (first.shown) {
    body.append(
      el("p", { text: `Run this in PowerShell, then come back: ${agentName(name)} turns Ready by itself.` }),
      el("pre", { class: "setup-command" }, el("code", { text: first.command })),
    );
    footer.append(el("button", { type: "button", "data-primary": "1", "data-key": "copy", text: "Copy command", onclick: () => copy(first.command, "Copied: run it in PowerShell.") }));
    return;
  }
  body.append(el("p", { text: "Chief of Staff runs this for you, in your account:" }), el("pre", { class: "setup-command" }, el("code", { text: first.command })));
  if (others.length) {
    body.append(
      el(
        "details",
        { class: "setup-other" },
        el("summary", { text: "Other ways" }),
        ...others.map((way) => el("div", { class: "setup-way" }, el("code", { text: way.command }), el("button", { type: "button", class: "secondary", "data-key": `way-${way.method}`, text: "Use this", onclick: () => install(name, way.method) }))),
      ),
    );
  }
  footer.append(
    el("button", { type: "button", class: "text", text: "Copy command", "data-key": "copy", onclick: () => copy(first.command, `Copied: run ${first.command} in a terminal.`) }),
    el("button", { type: "button", "data-primary": "1", "data-key": "install", text: "Install", onclick: () => install(name, first.method) }),
  );
}
function keyForm(body, footer, name) {
  const input = el("input", { type: "password", autocomplete: "off", spellcheck: "false", "aria-label": "API key", placeholder: "sk-…", "data-key": "key-input", "data-primary": "1", value: kept.key });
  input.oninput = () => (kept.key = input.value);
  const save = () => input.value.trim() && signIn(name, "key", { key: input.value.trim() });
  input.onkeydown = (event) => event.key === "Enter" && (event.preventDefault(), save());
  body.append(el("p", { text: "Your OpenAI API key goes to Codex itself (codex login --with-api-key); Chief of Staff keeps no copy." }), el("label", {}, el("span", { text: "API key" }), input));
  footer.append(
    el("button", { type: "button", class: "text", text: "Back", "data-key": "back", onclick: () => ((dialog.mode = ""), draw()) }),
    el("button", { type: "button", text: "Save key", "data-key": "save", onclick: save }),
  );
}
function otherWays(name, status, run) {
  const ways = (status.ways || [{ method: "browser" }]).filter((way) => way.method !== run?.method);
  if (!ways.length) return "";
  return el(
    "details",
    { class: "setup-other" },
    el("summary", { text: "Other ways" }),
    ...ways.map((way) =>
      el("button", {
        type: "button",
        class: "link",
        "data-key": `way-${way.method}`,
        text: way.label || "Sign in in the browser",
        onclick: () => (way.method === "key" ? ((dialog.mode = "key"), draw()) : signIn(name, way.method, { confirm: true })),
      }),
    ),
  );
}
function runView(body, footer, run, status, close) {
  const name = run.agent;
  if (run.kind === "install") {
    if (run.state === "running") {
      body.append(waiting(`Installing ${title(name)}…`, run.started), el("pre", { class: "setup-command" }, el("code", { text: run.command })));
      if (run.tail?.length) body.append(el("pre", { class: "setup-tail", "aria-live": "polite", text: run.tail.slice(-3).join("\n") }));
      body.append(output(run));
      footer.append(el("button", { type: "button", class: "secondary", text: "Cancel", "data-key": "cancel", onclick: () => cancel(name) }));
      return;
    }
    if (run.state === "done" && run.follows) {
      body.append(waiting(`Installed. Signing in to ${title(name)} next…`));
      footer.append(close);
      return;
    }
  } else if (run.state === "running") {
    const device = run.method === "device";
    body.append(
      el("p", { text: device ? "On any device, open this page and enter the code:" : run.url ? `Finish signing in on ${title(name)}’s page in your browser.` : `${title(name)} opens its sign-in page in your browser.` }),
    );
    if (run.url) {
      body.append(
        el(
          "div",
          { class: "setup-links" },
          el("a", { class: "button", href: run.url, target: "_blank", rel: "noreferrer noopener", "data-key": "open", "data-primary": device ? undefined : "1", text: "Open sign-in page" }),
          el("button", { type: "button", class: "secondary", text: "Copy link", "data-key": "copy-link", onclick: () => copy(run.url, "Copied the sign-in link.") }),
        ),
      );
    }
    if (run.code) {
      body.append(el("div", { class: "setup-code" }, el("code", { text: run.code }), el("button", { type: "button", class: "secondary", text: "Copy code", "data-key": "copy-code", "data-primary": "1", onclick: () => copy(run.code, "Copied the code.") })));
    }
    if (run.asks_code) {
      const input = el("input", { type: "text", autocomplete: "off", spellcheck: "false", "aria-label": "Paste code", placeholder: "Paste the code the page showed", "data-key": "code-input", "data-primary": "1", value: kept.code });
      const send = el("button", { type: "button", text: "Send", "data-key": "send" });
      input.oninput = () => (kept.code = input.value);
      input.onkeydown = (event) => event.key === "Enter" && (event.preventDefault(), sendCode(name, input, send));
      send.onclick = () => sendCode(name, input, send);
      body.append(el("label", { class: "setup-paste" }, el("span", { text: "Paste code" }), el("span", { class: "setup-paste-row" }, input, send)));
    }
    body.append(waiting("Waiting for you to sign in…", run.started), output(run), otherWays(name, status, run));
    if (name === "codex" && run.method !== "key") body.append(el("small", { class: "setup-note", text: "Codex signed you out as this began: Cancel leaves it signed out." }));
    footer.append(el("button", { type: "button", class: "secondary", text: "Cancel", "data-key": "cancel", onclick: () => cancel(name) }));
    return;
  }
  if (run.state === "done") {
    const pays = status.status === "ready" && status.pays ? ` · ${status.pays}` : "";
    body.append(el("p", { class: "setup-done", role: "status" }, el("span", { class: "setup-check", "aria-hidden": "true", text: "✓" }), ` ${agentName(name)} is ready${pays}`));
    footer.append(el("button", { type: "button", "data-primary": "1", "data-key": "done", text: "Done", onclick: () => dialog.close() }));
    return;
  }
  const failed = run.state === "failed";
  body.append(el("p", { class: failed ? "setup-error" : "", role: failed ? "alert" : "status", text: failed ? run.error || "It did not finish." : "Cancelled." }), output(run, failed && !run.error));
  if (run.kind === "signin") body.append(otherWays(name, status, null));
  const retry = () => (run.kind === "install" ? ((dialog.mode = "consent"), draw()) : signIn(name, run.method === "key" ? "browser" : run.method, { confirm: true }));
  footer.append(close, el("button", { type: "button", "data-primary": "1", "data-key": "retry", text: "Try again", onclick: retry }));
}

// Back to the page: an agent that could not run is looked at again (signed in or installed in a
// terminal meanwhile, its row turns Ready), at most every 5 seconds.
let looked = 0;
export function watchFocus() {
  window.addEventListener("focus", async () => {
    if (!statuses().some((status) => status.status === "signin" || status.status === "missing") || Date.now() - looked < 5000) return;
    looked = Date.now();
    try {
      const { agents_status: found } = await api("/api/agents/status", { method: "POST" });
      state.snapshot = { ...state.snapshot, agents_status: found };
    } catch {
      // (Shown as last known.)
    }
  });
}
