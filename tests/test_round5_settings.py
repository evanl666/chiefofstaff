"""Bug bash 5: Settings (field errors, empty numbers), Go on once, links and text as shown."""

# The page's JavaScript the tests run in node is written on long lines.
# ruff: noqa: E501

import json
import shutil
import subprocess

import pytest
from conftest import call_tool, settle
from test_ui_text import STATIC, STUBS

# A form field as the page has it: its value, and the label it sits in.
FORM = """
const field = (name, value = "", label = "") => ({
  name, value, checked: false, dataset: {}, addEventListener() {},
  closest: () => (label ? { querySelector: () => ({ textContent: label }) } : null),
});
const node = () => ({ value: "", hidden: false, textContent: "", classList: { toggle() {} } });
function page(fields) {
  const form = new Proxy({}, { get: (_, name) => fields[name] || field(name) });
  document.querySelector = (selector) => {
    if (selector === "#settings-form") return form;
    const name = /^\\[name="(.+)"\\]$/.exec(selector)?.[1];
    return name ? fields[name] || null : node();
  };
  document.querySelectorAll = () => [];
}
"""


def run_ui(body: str):
    """Run `body` (an async function body returning a JSON-able value) with the page's modules:
    d (dom.js), t (text.js), s (state.js), settings (settings.js), a (agentspage.js)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    modules = {
        "d": "dom.js",
        "t": "text.js",
        "s": "state.js",
        "settings": "settings.js",
        "a": "agentspage.js",
    }
    script = (
        STUBS
        + FORM
        + "".join(
            f"const {name} = await import({json.dumps((STATIC / file).as_uri())});\n"
            for name, file in modules.items()
        )
        + f"const result = await (async () => {{ {body} }})();\n"
        + "console.log(JSON.stringify(result)); process.exit(0);\n"
    )
    done = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


async def test_go_on_from_two_tabs_goes_to_the_worker_once(harness):
    """Bug bash 5 (A14): Go on from two tabs sent "Go on where you stopped." twice: the second
    went to a task already going on again."""
    runtime = await harness.connect()
    result = await call_tool(runtime, "spawn_worker", harness.router_id, title="Sleep",
                             instructions="Sleep a minute", why_new="test")  # fmt: skip
    worker = json.loads(result.text_result_for_llm)["session_id"]
    session = harness.session(worker)
    session.emit("user.message", content=session.sent[0])
    session.emit("assistant.turn_start")
    await settle(runtime)
    runtime.update_session(worker, status="working")
    task = runtime.tasks.current(worker)
    await runtime.workers.interrupt(worker)
    assert runtime.task_actions.view(runtime.tasks.get(task["id"]))["stopped"]
    for _ in range(2):
        going = await runtime.task_actions.follow_up(task["id"], "Go on where you stopped.")
        assert going["status"] == "running"
    said = [m["text"] for m in harness.store.messages(worker) if m["kind"] == "owner"]
    assert said.count("Go on where you stopped.") == 1


async def test_a_saved_mcp_server_copilot_would_refuse_is_off_and_the_others_still_work(harness):
    """Code review (A3): one server saved by an earlier version that the stricter check refuses
    lost every MCP server on load. The valid ones still go to workers; the other is shown in
    Settings with what is wrong, and a save from there keeps it as it was."""
    good = {"command": "probe", "tools": ["*"]}
    bad = {"url": "ftp://mcp.example.com", "tools": ["*"]}
    saved = {"mcp_servers": {"probe": good, "old": bad}}
    harness.store.execute("INSERT OR REPLACE INTO settings VALUES (1, ?)", (json.dumps(saved),))
    runtime = await harness.connect()
    result = await call_tool(runtime, "spawn_worker", harness.router_id, title="Probe",
                             instructions="Run the probe", why_new="test")  # fmt: skip
    worker = json.loads(result.text_result_for_llm)["session_id"]
    assert harness.session(worker).options["mcp_servers"] == {"probe": good}
    written = json.dumps(json.dumps(saved["mcp_servers"]))  # As the Settings box shows them.
    shown = run_ui(
        f"""
        s.state.snapshot = {{ settings: {json.dumps(harness.store.settings().model_dump())} }};
        page({{ mcp_servers: field("mcp_servers", {written}) }});
        const servers = settings.mcpServers();
        const note = settings.mcpNote(servers);
        page({{ mcp_servers: field("mcp_servers", '{{"old": {{"url": "ftp://other"}}}}') }});
        try {{
          settings.mcpServers();
          return [Object.keys(servers), note, "saved"];
        }} catch (error) {{
          return [Object.keys(servers), note, error.message];
        }}
        """
    )
    assert shown == [
        ["probe", "old"],
        "MCP server old is off (workers do not get it) until fixed: url must be an http:// "
        "or https:// address.",
        "MCP server old: url must be an http:// or https:// address.",
    ]


def test_an_emptied_number_in_settings_keeps_its_saved_value():
    """Bug bash 5 (A5): Parallel workers or Slack check cleared, Save sent 0 (Number("")), and
    the server's answer did not say which field."""
    body = run_ui(
        """
        s.state.snapshot = { settings: { projects: [], max_workers: 6, slack_poll_seconds: 90,
          worker_idle_hours: 12, watch_minutes: 7, role_models: {}, role_efforts: {} } };
        page({ max_workers: field("max_workers", ""), slack_poll_seconds: field("x", " "),
               worker_idle_hours: field("worker_idle_hours", "30") });
        const sent = settings.settingsBody();
        return [sent.max_workers, sent.slack_poll_seconds, sent.worker_idle_hours,
                sent.watch_minutes];
        """
    )
    assert body == [6, 90, 30, 7]


def test_a_field_the_server_refuses_is_named_as_the_page_names_it():
    """Bug bash 5 (A5): "Input should be greater than or equal to 1", not which field."""
    said = run_ui(
        """
        page({ max_workers: field("max_workers", "0", "Workers at once") });
        const problems = [
          { type: "greater_than_equal", loc: ["body", "max_workers"], ctx: { ge: 1 },
            msg: "Input should be greater than or equal to 1" },
          { type: "int_parsing", loc: ["body", "away_minutes"],
            msg: "Input should be a valid integer" },
          { type: "value_error", loc: ["body", "mcp_servers"],
            msg: "Value error, MCP server a: url is missing" },
        ];
        globalThis.fetch = async () => ({ status: 422, ok: false,
          json: async () => ({ detail: problems }) });
        try {
          await d.api("/api/settings", { method: "POST", body: {} });
        } catch (error) {
          return error.message;
        }
        """
    )
    assert said == (
        "Workers at once: Input should be greater than or equal to 1; "
        "Away minutes: Input should be a valid integer; MCP server a: url is missing"
    )


def test_ask_first_is_worker_yolo_the_other_way_round():
    """Settings v2 §7.2: Ask first checked sends worker_yolo false; unchecked, true; whatever
    else is saved goes back as it was."""
    sent = run_ui(
        """
        const saved = { projects: [], max_workers: 4, worker_yolo: true, role_models: {},
          role_efforts: {} };
        s.state.snapshot = { settings: saved };
        const ask = (checked) => {
          page({ ask_first: { ...field("ask_first"), checked } });
          return settings.settingsBody().worker_yolo;
        };
        return [ask(true), ask(false)];
        """
    )
    assert sent == [False, True]


async def test_desktop_notifications_are_offered_only_where_they_can_show(harness, monkeypatch):
    """Settings v2 §12.2: off macOS a banner never shows (notify.py), so the page is told and
    leaves out the line; on macOS the snapshot says nothing (as before)."""
    import sys

    runtime = await harness.connect()
    monkeypatch.setattr(sys, "platform", "linux")
    assert runtime.snapshot()["desktop_notifications"] is False
    monkeypatch.setattr(sys, "platform", "darwin")
    assert "desktop_notifications" not in runtime.snapshot()


def test_a_link_whose_words_are_another_address_says_where_it_goes():
    """Bug bash 5 (A11): a Slack link labelled with a GitHub URL went to evil.example.com."""
    shown = run_ui(
        """
        const link = (text) => /<a [^>]*>(.*?)<\\/a>/.exec(t.markdown(t.slackText(text)))[1];
        return [
          "<https://evil.example.com|https://github.com/evan666-lgtm/cos-approve-test>",
          "[github.com](https://evil.example.com/login)",
          "[www.github.com/o/r](https://evil.example.com)",
          "<https://github.com/o/r/pull/1|https://github.com/o/r/pull/1>",
          "[README.md](https://github.com/o/r/blob/main/README.md)",
          "[the plan](https://docs.example.com/plan)",
          "<https://www.github.com/o/r|github.com/o/r>",
        ].map(link);
        """
    )
    assert shown == [
        "https://github.com/evan666-lgtm/cos-approve-test (evil.example.com)",
        "github.com (evil.example.com)",
        "www.github.com/o/r (evil.example.com)",
        "https://github.com/o/r/pull/1",
        "README.md",
        "the plan",
        "github.com/o/r",
    ]


def test_text_cannot_reverse_what_follows_it():
    """Bug bash 5 (A15): a right-to-left override in the owner's or Slack's text showed what
    follows it reversed ("edirrevo LTR"); right-to-left languages still read as written."""
    shown = run_ui(
        """
        document.createElement = (tag) => ({ tag, attributes: {}, children: [],
          setAttribute(key, value) { this.attributes[key] = value; },
          append(child) { this.children.push(child); } });
        const persian = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645 \u05e9\u05dc\u05d5\u05dd";
        const owner = d.el("div", { text: "a \u202eoverride LTR", title: "\u2067x\u2069" });
        return [
          owner.textContent, owner.attributes.title,
          d.el("p", {}, "b \u202dc\u202c").children[0],
          t.markdown("**\u202eoverride** LTR"),
          t.slackText("<@U0BOB001> \u202aquoted\u202c \u2066isolated\u2069"),
          t.markdown(persian), d.el("p", { text: persian }).textContent,
        ];
        """
    )
    persian = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645 \u05e9\u05dc\u05d5\u05dd"
    assert shown == [
        "a override LTR",
        "x",
        "b c",
        "<p><strong>override</strong> LTR</p>",
        "@U0BOB001 quoted isolated",
        f"<p>{persian}</p>",
        persian,
    ]


# Settings v2 (docs/design/settings-v2.md, phase 1): its places, its connections' statuses, its
# agents' meters, warnings and statuses, its tooltip, and the model loader.

# A page with no DOM to speak of: any element there is, and does nothing.
ANY_DOM = """
const any = () => new Proxy(function () {}, { get: (_, key) => (key === Symbol.toPrimitive ?
  () => "" : key === "then" || key === Symbol.iterator ? undefined : any()), set: () => true,
  apply: () => any() });
document.querySelectorAll = () => [];
document.createElement = () => any();
document.getElementById = () => any();
"""


def test_settings_opens_each_name_old_or_new_where_it_is_now():
    """§9.2: an old tab's name (a link, a toast, a timeline line's `meta.settings`, the last
    page kept) opens its new place; connections/<row> opens that connection (a row it does not
    have, the list); anything else, General."""
    names = ["general", "workers", "advanced", "proactive", "models", "agents", "connections",
             "you", "connections/slack", "connections/github", "connections/watches",
             "connections/away", "connections/tools", "connections/send", "connections/nope",
             "abilities", ""]  # fmt: skip
    places = run_ui(
        f"return {json.dumps(names)}.map((name) => [name, settings.settingsPlace(name)]);"
    )
    assert dict(places) == {
        "general": {"tab": "general"},
        # Phase 2: Workers, Advanced and Prepared for you are parts of General now.
        "workers": {"tab": "general", "scroll": "general-workers"},
        "advanced": {"tab": "general", "unfold": "more"},
        "proactive": {"tab": "general", "unfold": "prs"},
        "models": {"tab": "agents", "unfold": "models"},
        "agents": {"tab": "agents"},
        "connections": {"tab": "connections"},
        "you": {"tab": "you"},
        **{
            f"connections/{row}": {"tab": "connections", "connection": row}
            for row in ("slack", "github", "watches", "away", "tools", "send")
        },
        "connections/nope": {"tab": "connections"},
        "abilities": {"tab": "general"},
        "": {"tab": "general"},
    }


def test_each_connection_says_where_it_stands_in_one_line_with_one_action_at_most():
    """§5: a row's status from what its details say, its tone, and an action only where there
    is one next step."""
    found = run_ui(
        """
        const ago = (seconds) => new Date(Date.now() - seconds * 1000).toISOString();
        const base = { settings: { slack_enabled: false, github_enabled: false, github_repos: [],
          watches: [], away_notify: false, mcp_servers: {}, mcp_servers_off: {}, plugins: [],
          send_to_chief: false }, slack: {}, github: {}, connector: {}, abilities: { skills: [],
          plugins: {} } };
        const at = (key, change = {}, settingsChange = {}, extra = {}) => settings.connectionStatus(
          key, { ...base, ...change, settings: { ...base.settings, ...settingsChange } }, extra);
        const watches = [{ what: "a", how: "", enabled: true }, { what: "b", how: "", enabled: false }];
        return {
          slackOff: at("slack"),
          slackToken: at("slack", {}, { slack_enabled: true }),
          slackEnv: at("slack", { slack: { token_source: "env", connected: true } }, { slack_enabled: true }),
          slackOn: at("slack", { slack: { token_saved: true, connected: true, last_checked: ago(120) } }, { slack_enabled: true }),
          slackBad: at("slack", { slack: { token_saved: true, error: "invalid_auth" } }, { slack_enabled: true }),
          githubOff: at("github", {}, { github_repos: ["acme/api", "acme/web"] }),
          githubOn: at("github", { github: { connected: true } }, { github_enabled: true, github_repos: ["acme/api"] }),
          githubBad: at("github", { github: { error: "gh: not signed in" } }, { github_enabled: true }),
          watchesNone: at("watches"),
          watches: at("watches", { connector: { scanned_at: ago(180), last: { found: 1 } } }, { watches }),
          watchesDraft: at("watches", {}, {}, { written: watches, unsaved: true }),
          watchesGap: at("watches", { connector: { scanned_at: ago(60), last: { found: 0, incomplete: [{ watch: 1, reason: "no Glean", searched: false }] } } }, { watches }),
          watchesFailed: at("watches", { connector: { failed: { at: ago(300), reason: "timed out", count: 1 } } }, { watches }),
          watchesConnect: at("watches", { connector: { connection_needed: { source: "Glean", server: "glean" } } }, { watches }),
          awayOff: at("away"),
          awayOn: at("away", { connector: { away: { where: "Slack DM" } } }, { away_notify: true }),
          awayFailed: at("away", { connector: { away: { last: { at: ago(300), ok: false } } } }, { away_notify: true }),
          toolsNone: at("tools"),
          tools: at("tools", { abilities: { skills: [{ name: "x" }, { name: "y" }], plugins: { "o/p": { state: "installed" } } } }, { mcp_servers: { docs: {} } }),
          toolsFailed: at("tools", { abilities: { skills: [], plugins: { "o/p": { state: "failed" } } } }, { mcp_servers: { docs: {} } }),
          toolsOff: at("tools", {}, { mcp_servers_off: { jira: {} } }),
          sendOff: at("send"),
          sendOn: at("send", {}, { send_to_chief: true }),
          sendBad: at("send", { send_problem: "socket in use" }, { send_to_chief: true }),
        };
        """
    )
    said = {key: (value["text"], value.get("tone", ""), (value.get("action") or [""])[-1]) for key, value in found.items()}  # fmt: skip
    assert said == {
        "slackOff": ("Off", "", ""),
        "slackToken": ("Token needed", "warn", "Set up"),
        "slackEnv": ("Watching", "ok", ""),
        "slackOn": ("Watching · checked 2m ago", "ok", ""),
        "slackBad": ("Problem: invalid_auth", "bad", ""),
        "githubOff": ("Off · 2 repositories listed", "", ""),
        "githubOn": ("Watching 1 repository", "ok", ""),
        "githubBad": ("Problem: gh: not signed in", "bad", ""),
        "watchesNone": ("None", "", ""),
        "watches": ("1 on · scanned 3m ago · 1 found", "ok", "Scan now"),
        "watchesDraft": ("1 on", "", "Scan now"),
        "watchesGap": ("1 on · scanned 1m ago · 0 found · 1 not searched", "warn", "Scan now"),
        "watchesFailed": ("Scan failed 5m ago: timed out", "bad", "Scan now"),
        "watchesConnect": ("Connection needed: Glean", "warn", "Open card"),
        "awayOff": ("Off", "", ""),
        "awayOn": ("On · Slack DM", "ok", ""),
        "awayFailed": ("Away notice not delivered · 5m ago", "bad", "Send a test"),
        "toolsNone": ("None added", "", ""),
        "tools": ("2 skills · 1 plugin · 1 MCP server", "", ""),
        "toolsFailed": ("1 plugin could not be installed · 1 MCP server", "warn", "Retry"),
        "toolsOff": ("MCP server jira is off until fixed", "warn", ""),
        "sendOff": ("Off", "", ""),
        "sendOn": ("On", "ok", ""),
        "sendBad": ("Problem: socket in use", "bad", ""),
    }


def test_an_agents_meter_shows_its_most_used_window_amber_from_80_red_at_100():
    meters = run_ui(
        """
        const now = Date.now();
        const window = (label, used) => ({ label, used, resets_at: 0 });
        return [
          a.usageMeter([window("5-hour limit", 38), window("weekly limit", 41)], now),
          a.usageMeter([window("5-hour limit", 80)], now),
          a.usageMeter([window("weekly Opus limit", 100), window("5-hour limit", 12)], now),
          a.usageMeter([], now),
          a.usageMeter(undefined, now),
        ];
        """
    )
    assert [(m["words"], m["level"]) if m else None for m in meters] == [
        ("41% weekly", ""),
        ("80% 5-hour", "high"),
        ("100% weekly Opus", "full"),
        None,
        None,
    ]
    assert meters[0]["windows"] == ["38% of your 5-hour limit", "41% of your weekly limit"]
    assert meters[0]["label"] == "41% of your weekly limit used"


def test_a_limit_or_a_sign_in_ending_is_said_under_an_agent_whatever_its_status_and_meter():
    """§3.3 (review finding 4): what costs money or stops work is never folded away: extra
    usage, a limit used up with no reset known, a rate limit, a close limit; a sign-in about to
    end; the full reason it can't run. Pinned against the look's own words (runtime.py
    looked_at_agent, agents.limit_line)."""
    said = run_ui(
        """
        const soon = Date.now() / 1000 + 3 * 86400;
        const full = [{ label: "weekly limit", used: 100 }];
        const some = [{ label: "5-hour limit", used: 12 }];
        const extra = "Your Claude plan's weekly limit is reached; workers go on with extra usage until it resets at 3:40 pm.";
        const usedUp = "Codex's weekly limit is used up; it didn't say when it resets";
        const rate = "Claude is refusing requests for a rate limit; it didn't say for how long";
        const close = "Your Claude plan's 5-hour limit is close; it resets at 3:40 pm.";
        const row = (name, look, status = null, sandboxed = false) => ({
          status: a.agentStatus(name, { look, status, sandboxed, on: true }).text,
          alerts: a.agentAlerts(name, { look, status, sandboxed }).map((alert) => `${alert.tone}: ${alert.text}`),
        });
        return [
          row("claude", { status: "ready", usage: some, limit_line: extra, expires: soon }),
          row("codex", { status: "ready", usage: full, limit_line: usedUp }),
          row("claude", { status: "limited", resets_at: soon, usage: full, limit_line: rate }),
          row("claude", { status: "ready", usage: [], limit_line: close }),
          row("claude", { status: "signin", usage: some, limit_line: extra, expires: soon }),
          row("codex", { status: "ready", problem: "Stopped unexpectedly; starts again with its next worker's message." }),
          row("codex", { status: "ready", sandbox: "Codex can't run while workers are sandboxed." }, null, true),
          row("claude", null, { status: "ready", expires: Date.now() / 1000 - 60 }),
        ];
        """
    )
    ends = "warn: Claude sign-in ends "
    assert said[0]["status"] == "Ready" and said[0]["alerts"][0] == (
        "warn: Your Claude plan's weekly limit is reached; workers go on with extra usage until "
        "it resets at 3:40 pm."
    )
    assert said[0]["alerts"][1].startswith(ends), said[0]
    assert said[1] == {
        "status": "Ready",
        "alerts": ["warn: Codex's weekly limit is used up; it didn't say when it resets"],
    }
    assert said[2]["status"].startswith("At its limit · resets ")
    assert said[2]["alerts"] == [
        "warn: Claude is refusing requests for a rate limit; it didn't say for how long"
    ]
    assert said[3]["alerts"] == [
        "warn: Your Claude plan's 5-hour limit is close; it resets at 3:40 pm."
    ]
    assert said[4]["status"] == "Sign in needed" and len(said[4]["alerts"]) == 2, said[4]
    assert said[5] == {
        "status": "Can't run here",
        "alerts": ["bad: Stopped unexpectedly; starts again with its next worker's message."],
    }
    assert said[6] == {
        "status": "Can't run here",
        "alerts": ["bad: Codex can't run while workers are sandboxed."],
    }
    assert said[7]["alerts"] == ["warn: Claude sign-in ended"]


def test_a_menu_keeps_a_value_it_cannot_list():
    """§8.7: a saved model the list does not have (or before it loaded), an effort the model
    does not offer, `none` (Settings never offered it): each stays a choice."""
    found = run_ui(
        """
        const listed = [{ id: "smart", name: "Smart", efforts: ["low", "high"] }];
        return [
          a.modelChoices(listed, "gpt-5.2", "Default"),
          a.modelChoices([], "gpt-5.2", "Default"),
          a.modelChoices(listed, "smart", "Default"),
          a.effortChoices(["low", "high"], "medium"),
          a.effortChoices(["low", "high"], "none"),
          a.effortChoices(null, ""),
          a.effortChoices([], "high"),
        ];
        """
    )
    assert found[0] == [
        ["", "Default"],
        ["smart", "Smart"],
        ["gpt-5.2", "gpt-5.2 (not offered now)"],
    ]
    assert found[1] == [["", "Default"], ["gpt-5.2", "gpt-5.2"]]
    assert found[2] == [["", "Default"], ["smart", "Smart"]]
    assert found[3] == [["", "Default"], ["low", "Low"], ["high", "High"],
                        ["medium", "Medium (not offered)"]]  # fmt: skip
    assert found[4][-1] == ["none", "None (not offered)"]
    assert [value for value, _ in found[5]] == ["", "low", "medium", "high", "xhigh", "max"]
    assert found[6] == [["", "Default"], ["high", "High (not offered)"]]


def test_our_tooltip_shows_inside_an_open_modal_dialog():
    """§8.4 (finding 8): #tip went to the page's body, under a modal dialog's top layer: a
    data-tip in Settings never showed."""
    where = run_ui(
        ANY_DOM
        + """
        const made = [];
        const host = (name) => ({ name, appendChild(node) { made.push(name); node.parentElement = this; return node; } });
        const dialog = host("settings-dialog");
        globalThis.document.body = { ...document.body, ...host("body") };
        document.getElementById = () => null;
        document.querySelector = (selector) => (selector === "dialog[open]:modal" ? dialog : null);
        d.tipBox();
        document.querySelector = () => null;
        d.tipBox();
        return made;
        """
    )
    assert where == ["settings-dialog", "body"]


def test_copilots_models_load_without_the_old_model_list_and_say_so():
    """§10.1 (review finding 9): the loader filled #model-options and threw once it was gone,
    before the header was drawn."""
    found = run_ui(
        ANY_DOM
        + """
        const heard = [];
        document.querySelector = (selector) => (selector === "#model-options" ? null : any());
        globalThis.dispatchEvent = (event) => heard.push(event.type);
        globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => [{ id: "m1", name: "M1", efforts: [] }] });
        s.state.snapshot = { runtime: { state: "ready" }, sessions: [], settings: { model: "" }, tasks: [] };
        let error = "";
        try { await s.loadModels(); } catch (problem) { error = String(problem); }
        return { error, models: s.state.models.map((model) => model.id), heard };
        """
    )
    assert found == {"error": "", "models": ["m1"], "heard": ["models"]}
