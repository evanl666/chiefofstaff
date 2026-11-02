// Cards for what waits for the owner: drafts to approve, questions, forms and sign-ins.

import { $, api, el } from "./dom.js";
import { displayPath, markdown } from "./text.js";
import { byId, draftsFor, state } from "./state.js";
export function renderDrafts() {
  const box = $("#drafts");
  const visible = draftsFor(state.selected);
  const ids = new Set(visible.map((draft) => draft.id));
  for (const node of [...box.children]) {
    if (!ids.has(node.dataset.id)) node.remove();
  }
  for (const draft of visible) {
    if (!box.querySelector(`[data-id="${draft.id}"]`)) box.append(draftCard(draft));
  }
}
// The Router is not sure which folder a task belongs in: the owner picks one.
function workspaceCard(draft) {
  const target = draft.target;
  const error = el("span", { class: "error" });
  const other = el("input", { placeholder: "Another folder, e.g. ~/code/app" });
  const node = el(
    "div",
    { class: "card draft workspace", "data-id": draft.id },
    el(
      "div",
      { class: "head" },
      el("span", { class: "label", text: `Where should “${target.title}” work?` }),
      el("span", { class: "chip waiting", text: "Waiting for you" }),
    ),
    el("div", { class: "note", text: [draft.text, target.why].filter(Boolean).join(" — ") }),
  );
  const choices = el("div", { class: "choices" });
  async function decide(body) {
    for (const button of node.querySelectorAll("button")) button.disabled = true;
    error.textContent = "";
    try {
      await api(`/api/drafts/${draft.id}`, { method: "POST", body });
    } catch (failure) {
      error.textContent = failure.message;
      for (const button of node.querySelectorAll("button")) button.disabled = false;
    }
  }
  target.options.forEach((option, index) => {
    choices.append(
      el(
        "button",
        { type: "button", class: index ? "secondary" : "", title: option.path, onclick: () => decide({ action: "choose", choice: option.path }) },
        el("span", { text: option.path.split("/").pop() }),
        el("small", { text: `${displayPath(option.path)}${option.configured ? "" : " · new project"}` }),
      ),
    );
  });
  choices.append(
    el("button", { type: "button", class: "secondary", onclick: () => decide({ action: "choose", choice: "" }) }, el("span", { text: "No project" }), el("small", { text: "its own scratch folder" })),
  );
  const use = () => other.value.trim() && decide({ action: "choose", choice: other.value.trim() });
  other.addEventListener("keydown", (event) => event.key === "Enter" && use());
  node.append(
    choices,
    el(
      "div",
      { class: "actions" },
      other,
      el("button", { type: "button", class: "secondary", text: "Use", onclick: use }),
      el("button", { type: "button", class: "text", text: "Don't start", onclick: () => decide({ action: "reject" }) }),
    ),
    error,
  );
  return node;
}
const REQUEST_LABELS = { question: "Question", form: "Form", link: "Link to open", signin: "Sign-in" };
const SECRET_FIELD = /pass(word)?|secret|token|api[_-]?key/i;
// Fields for a form an MCP tool asks the owner to fill in (a flat JSON schema).
function formFields(schema) {
  const properties = schema?.properties || {};
  const required = new Set(schema?.required || []);
  const inputs = {};
  const rows = Object.entries(properties).map(([name, spec]) => {
    const title = `${spec.title || name}${required.has(name) ? "" : " (optional)"}`;
    let input;
    const options = (item) => (item?.enum || []).map((value) => ({ value, title: value })).concat((item?.oneOf || item?.anyOf || []).filter((option) => option && "const" in option).map((option) => ({ value: option.const, title: option.title || option.const })));
    const choices = options(spec).length ? options(spec) : options(spec.items);
    if (spec.type === "boolean") {
      input = el("input", { type: "checkbox", checked: spec.default === true });
      inputs[name] = () => input.checked;
      return el("label", { class: "check" }, input, el("span", { text: title }));
    }
    if (choices.length) {
      input = el("select", { multiple: spec.type === "array" }, ...choices.map((choice) => el("option", { value: choice.value, text: choice.title })));
      if (spec.default !== undefined) input.value = spec.default;
      inputs[name] = () => (spec.type === "array" ? [...input.selectedOptions].map((option) => option.value) : input.value);
    } else {
      const numeric = spec.type === "number" || spec.type === "integer";
      input = el("input", { type: numeric ? "number" : SECRET_FIELD.test(name) ? "password" : "text", value: spec.default ?? "", autocomplete: "off" });
      inputs[name] = () => input.value;
    }
    return el("label", {}, el("span", { text: title }), spec.description ? el("small", { text: spec.description }) : null, input);
  });
  return {
    node: el("div", { class: "fields" }, ...rows),
    values: () => Object.fromEntries(Object.entries(inputs).map(([name, read]) => [name, read()])),
  };
}
// Something a session needs from the owner: an answer, a form, a link to open or a sign-in.
function requestCard(draft) {
  const owner = byId(draft.session_id);
  const who = owner?.role === "worker" ? owner.title : "";
  const target = draft.target;
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  const node = el(
    "div",
    { class: `card draft request ${draft.kind}`, "data-id": draft.id },
    el(
      "div",
      { class: "head" },
      el("span", { class: "label", text: `${REQUEST_LABELS[draft.kind]}${who ? ` · ${who}` : ""}` }),
      el("span", { class: "chip waiting", text: "Waiting for you" }),
    ),
    el("div", { class: "body markdown", html: markdown(draft.text) }),
  );
  async function decide(body) {
    for (const button of node.querySelectorAll("button")) button.disabled = true;
    error.textContent = "";
    try {
      await api(`/api/drafts/${draft.id}`, { method: "POST", body });
    } catch (failure) {
      error.textContent = failure.message;
      for (const button of node.querySelectorAll("button")) button.disabled = false;
    }
  }
  const decline = (text) => el("button", { type: "button", class: "text", text, onclick: () => decide({ action: "reject" }) });
  const link = (url, text) => (/^https?:\/\//.test(url) ? el("a", { class: "button", href: url, target: "_blank", rel: "noreferrer", text }) : null);
  if (draft.kind === "question") {
    if (target.choices?.length) {
      node.append(el("div", { class: "choices" }, ...target.choices.map((choice) => el("button", { type: "button", class: "secondary", text: choice, onclick: () => decide({ action: "answer", text: choice }) }))));
    }
    if (target.allow_freeform || !target.choices?.length) {
      const input = el("input", { placeholder: "Your answer", autocomplete: "off" });
      input.addEventListener("keydown", (event) => event.key === "Enter" && decide({ action: "answer", text: input.value }));
      actions.append(input, el("button", { type: "button", text: "Answer", onclick: () => decide({ action: "answer", text: input.value }) }));
    }
    actions.append(decline("Skip"));
  } else if (draft.kind === "form") {
    const fields = formFields(target.schema);
    node.append(fields.node);
    actions.append(decline("Decline"), el("button", { type: "button", text: "Submit", onclick: () => decide({ action: "answer", content: fields.values() }) }));
  } else if (draft.kind === "link") {
    actions.append(link(target.url, "Open link"), el("button", { type: "button", class: "secondary", text: "Done", onclick: () => decide({ action: "answer" }) }), decline("Decline"));
  } else {
    node.append(el("div", { class: "note", text: "Sign in in the tab that opens. This card closes by itself once it worked; every session then uses the sign-in." }));
    actions.append(
      link(target.url, `Sign in to ${target.server}`),
      el("button", { type: "button", class: "secondary", text: "Done", title: "I signed in", onclick: () => decide({ action: "answer" }) }),
      decline("Not now"),
    );
  }
  node.append(actions, error);
  return node;
}
export function draftCard(draft) {
  if (draft.kind === "workspace") return workspaceCard(draft);
  if (REQUEST_LABELS[draft.kind]) return requestCard(draft);
  const owner = byId(draft.session_id);
  const target = draft.target;
  const label =
    draft.kind === "slack"
      ? `Slack reply${target.thread_ts ? " in thread" : ""} · ${target.to || target.channel_id}`
      : `PR review · ${target.repo}#${target.number}`;
  const text = el("textarea", { rows: Math.min(10, draft.text.split("\n").length + 1) });
  text.value = draft.text;
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  const node = el(
    "div",
    { class: "card draft", "data-id": draft.id },
    el(
      "div",
      { class: "head" },
      el("span", { class: "label", text: `${label}${owner && owner.role === "worker" ? ` · from ${owner.title}` : ""}` }),
      el("span", { class: "chip waiting", text: "Waiting for you" }),
    ),
    draft.note ? el("div", { class: "note", text: draft.note }) : null,
    draft.kind === "github" ? el("a", { href: target.url, target: "_blank", rel: "noreferrer", text: target.url }) : null,
    text,
    actions,
  );

  async function decide(body) {
    for (const button of actions.querySelectorAll("button")) button.disabled = true;
    error.textContent = "";
    try {
      await api(`/api/drafts/${draft.id}`, { method: "POST", body });
    } catch (failure) {
      error.textContent = failure.message;
      for (const button of actions.querySelectorAll("button")) button.disabled = false;
    }
  }

  function showDefault() {
    actions.replaceChildren(
      error,
      el("button", { type: "button", class: "secondary", text: "Reject", onclick: showReject }),
      el("button", { type: "button", text: "Send", onclick: () => decide({ action: "send", text: text.value }) }),
    );
  }

  function showReject() {
    const reason = el("input", { placeholder: "Why? (optional, helps the next draft)" });
    actions.replaceChildren(
      reason,
      el("button", { type: "button", class: "text", text: "Cancel", onclick: showDefault }),
      el("button", { type: "button", class: "secondary", text: "Reject", onclick: () => decide({ action: "reject", reason: reason.value }) }),
    );
    reason.focus();
  }

  showDefault();
  return node;
}
