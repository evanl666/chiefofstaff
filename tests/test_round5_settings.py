"""Bug bash 5: Settings (field errors, empty numbers), Go on once, links and text as shown."""

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
    d (dom.js), t (text.js), s (state.js), settings (settings.js)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    modules = {"d": "dom.js", "t": "text.js", "s": "state.js", "settings": "settings.js"}
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
        page({ max_workers: field("max_workers", "0", "Parallel workers") });
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
        "Parallel workers: Input should be greater than or equal to 1; "
        "Away minutes: Input should be a valid integer; MCP server a: url is missing"
    )


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
