// Cards for what waits for the owner: drafts to approve, questions, forms and sign-ins.

import { $, api, el, submits } from "./dom.js";
import { displayPath, markdown } from "./text.js";
import { byId, draftsFor, state } from "./state.js";
import { select } from "./session.js";
export function renderDrafts() {
  const box = $("#drafts");
  const visible = draftsFor(state.selected);
  const ids = new Set(visible.map((draft) => draft.id));
  for (const node of [...box.children]) {
    if (!ids.has(node.dataset.id)) node.remove();
  }
  for (const draft of visible) {
    const node = box.querySelector(`[data-id="${draft.id}"]`);
    if (node && node.dataset.key === draftKey(draft)) continue;
    // New, or changed (a worker joined a decision, one confirmed a change): built afresh.
    const card = draftCard(draft);
    card.dataset.key = draftKey(draft);
    if (node) node.replaceWith(card);
    else box.append(card);
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
  other.addEventListener("keydown", (event) => submits(event) && use());
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
      input.addEventListener("keydown", (event) => submits(event) && decide({ action: "answer", text: input.value }));
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
// Cards change as workers join a decision or confirm a changed one.
export const draftKey = (draft) => `draft:${draft.id}:${draft.kind === "decision" ? draft.target.askers.length : draft.kind === "change" ? draft.target.tasks.map((task) => (task.ack ? 1 : 0)).join("") : ""}`;
function cardActions(draft, node, error) {
  return async function decide(body) {
    for (const button of node.querySelectorAll("button")) button.disabled = true;
    error.textContent = "";
    try {
      await api(`/api/drafts/${draft.id}`, { method: "POST", body });
    } catch (failure) {
      error.textContent = failure.message;
      for (const button of node.querySelectorAll("button")) button.disabled = false;
    }
  };
}
const taskLink = (item) => el("button", { type: "button", class: "link", text: item.title, onclick: () => select(item.session_id) });
// A decision that is the owner's: why it comes up, the options and what each means, and what
// the worker recommends. Workers that hit the same question wait on the same card.
function decisionCard(draft) {
  const target = draft.target;
  const error = el("span", { class: "error" });
  const node = el(
    "div",
    { class: "card draft decision", "data-id": draft.id },
    el("div", { class: "head" }, el("span", { class: "label", text: "Decision" }), el("span", { class: "chip waiting", text: "Waiting for you" })),
    el("div", { class: "question", text: target.question }),
    target.why ? el("div", { class: "why", text: target.why }) : null,
  );
  const decide = cardActions(draft, node, error);
  const lowered = (target.recommendation || "").toLowerCase();
  const best = (target.options || []).find((option) => lowered.includes(option.choice.toLowerCase()));
  if (target.options?.length) {
    node.append(
      el(
        "div",
        { class: "options" },
        ...target.options.map((option) =>
          el(
            "button",
            { type: "button", class: `option${option === best ? " recommended" : ""}`, onclick: () => decide({ action: "answer", text: option.choice }) },
            el("span", { class: "choice", text: option.choice }),
            option === best ? el("span", { class: "tag", text: "Recommended" }) : null,
            option.effect ? el("small", { text: option.effect }) : null,
          ),
        ),
      ),
    );
  }
  if (target.recommendation) node.append(el("div", { class: "recommendation" }, el("strong", { text: "Recommendation: " }), target.recommendation));
  if (target.precedents?.length) node.append(el("div", { class: "precedents" }, el("strong", { text: "You decided before: " }), target.precedents.join(" · ")));
  const askers = target.askers || [];
  node.append(el("div", { class: "affects" }, el("span", { text: askers.length > 1 ? `Affects ${askers.length} tasks:` : "For" }), ...askers.map(taskLink)));
  const input = el("input", { placeholder: "Or decide something else", autocomplete: "off" });
  input.addEventListener("keydown", (event) => submits(event) && input.value.trim() && decide({ action: "answer", text: input.value }));
  node.append(
    el("div", { class: "actions" }, input, el("button", { type: "button", class: "secondary", text: "Decide", onclick: () => decide({ action: "answer", text: input.value }) }), el("button", { type: "button", class: "text", text: "Not now", onclick: () => decide({ action: "reject" }) })),
    error,
  );
  return node;
}
// The owner changed a decision: which tasks it affects, and which workers confirmed.
function changeCard(draft) {
  const target = draft.target;
  const error = el("span", { class: "error" });
  const told = (item) => item.told !== false;
  const confirmed = target.tasks.filter((item) => told(item) && item.ack).length;
  const redo = target.tasks.filter((item) => told(item) && (item.was === "review" || item.was === "done")).length;
  const node = el(
    "div",
    { class: "card draft change", "data-id": draft.id },
    el("div", { class: "head" }, el("span", { class: "label", text: "Decision changed" }), el("span", { class: "chip progress", text: `${confirmed}/${target.tasks.length} confirmed` })),
    el("div", { class: "question", text: target.decision.text }),
    el("div", { class: "before", text: `Was: ${target.before.join("; ")}` }),
    el("div", { class: "why", text: `Affects ${target.tasks.length} task${target.tasks.length === 1 ? "" : "s"}${redo ? `; ${redo} checked result${redo === 1 ? " is" : "s are"} out of date and being redone` : ""}.` }),
    el(
      "ul",
      { class: "impact" },
      ...target.tasks.map((item) =>
        !told(item)
          ? el("li", { class: "failed" }, el("span", { class: "mark", text: "✗" }), taskLink(item), el("span", { class: "state", text: `not told: ${item.error}` }))
          : el("li", { class: item.ack ? "acked" : "" }, el("span", { class: "mark", text: item.ack ? "✓" : "…" }), taskLink(item), el("span", { class: "state", text: item.ack ? item.ack : item.was === "review" || item.was === "done" ? "result out of date, updating" : "adjusting" })),
      ),
    ),
  );
  const decide = cardActions(draft, node, error);
  node.append(el("div", { class: "actions" }, el("button", { type: "button", class: "text", text: "Hide", onclick: () => decide({ action: "answer" }) })), error);
  return node;
}
// Lessons learned from the owner's feedback: each kept (maybe reworded) or dropped.
function lessonsCard(draft) {
  const error = el("span", { class: "error" });
  const rows = draft.target.lessons.map((lesson) => {
    const keep = el("input", { type: "checkbox", checked: true, "aria-label": "Keep" });
    const text = el("input", { value: lesson.text, "aria-label": "Lesson" });
    const because = lesson.because.map((item) => `${item.owner}`.trim()).filter(Boolean);
    const where = !lesson.scope ? "everywhere" : lesson.scope === "slack" ? "Slack replies" : `in ${lesson.scope.replace("project:", "").split("/").pop()}`;
    const node = el(
      "li",
      {},
      keep,
      el("div", { class: "lesson" }, text, el("small", { text: `${where}${because.length ? ` · because you said: “${because.slice(0, 2).join("”, “")}”` : ""}` })),
    );
    return { lesson, keep, text, node };
  });
  const node = el(
    "div",
    { class: "card draft lessons", "data-id": draft.id },
    el("div", { class: "head" }, el("span", { class: "label", text: "Learned from your feedback" }), el("span", { class: "chip waiting", text: "Check before use" })),
    el("div", { class: "why", text: "Keep what fits how you work; reword or untick the rest. Kept lessons go into your sessions' instructions." }),
    el("ul", { class: "lesson-list" }, ...rows.map((row) => row.node)),
  );
  const decide = cardActions(draft, node, error);
  const keep = () => decide({ action: "answer", content: Object.fromEntries(rows.filter((row) => row.keep.checked).map((row) => [row.lesson.id, row.text.value])) });
  node.append(el("div", { class: "actions" }, el("button", { type: "button", class: "text", text: "Drop all", onclick: () => decide({ action: "reject" }) }), el("button", { type: "button", text: "Keep ticked", onclick: keep })), error);
  return node;
}
// The owner profile a worker distilled: read and edit it here; sessions use it once kept.
function profileCard(draft) {
  const error = el("span", { class: "error" });
  const text = el("textarea", { rows: Math.min(18, draft.text.split("\n").length + 1), "aria-label": "Profile" });
  text.value = draft.text;
  const node = el(
    "div",
    { class: "card draft profile", "data-id": draft.id },
    el("div", { class: "head" }, el("span", { class: "label", text: "Your profile" }), el("span", { class: "chip waiting", text: "Check before use" })),
    el("div", { class: "why", text: "Distilled from your work. Correct anything; once you use it, your sessions get it with their instructions." }),
    text,
  );
  const decide = cardActions(draft, node, error);
  node.append(
    el(
      "div",
      { class: "actions" },
      error,
      el("button", { type: "button", class: "text", text: "Discard", onclick: () => decide({ action: "reject" }) }),
      el("button", { type: "button", text: "Use this profile", onclick: () => text.value.trim() && decide({ action: "answer", text: text.value }) }),
    ),
  );
  return node;
}
// One of the owner's own Copilot sessions, which the Router wants to continue here.
function adoptCard(draft) {
  const target = draft.target;
  const error = el("span", { class: "error" });
  const node = el(
    "div",
    { class: "card draft adopt", "data-id": draft.id },
    el("div", { class: "head" }, el("span", { class: "label", text: "Continue a session here?" }), el("span", { class: "chip waiting", text: "Waiting for you" })),
    el("div", { class: "why", text: `“${target.title}”, one of your Copilot sessions (${displayPath(target.cwd || "")}). The Router wants to give it this, as a new task:` }),
    el("div", { class: "quoted", text: target.text }),
  );
  const decide = cardActions(draft, node, error);
  node.append(
    el(
      "div",
      { class: "actions" },
      error,
      el("button", { type: "button", class: "text", text: "Not now", onclick: () => decide({ action: "reject" }) }),
      el("button", { type: "button", text: "Continue it here", onclick: () => decide({ action: "answer" }) }),
    ),
  );
  return node;
}
// Where a Slack reply really goes, from its conversation id (D: a direct message, C: a channel,
// G: a private group); what the drafting session calls it is only its description.
const SLACK_KIND = { D: "direct message", C: "channel", G: "private group" };
export function slackDestination(target) {
  const kind = SLACK_KIND[target.channel_id?.[0]] || "conversation";
  return `${kind} ${target.channel_id}${target.thread_ts ? ", in a thread" : ""}`;
}
export function draftCard(draft) {
  if (draft.kind === "lessons") return lessonsCard(draft);
  if (draft.kind === "profile") return profileCard(draft);
  if (draft.kind === "adopt") return adoptCard(draft);
  if (draft.kind === "decision") return decisionCard(draft);
  if (draft.kind === "change") return changeCard(draft);
  if (draft.kind === "workspace") return workspaceCard(draft);
  if (REQUEST_LABELS[draft.kind]) return requestCard(draft);
  const owner = byId(draft.session_id);
  const target = draft.target;
  const label =
    draft.kind === "slack"
      ? `Slack reply · ${slackDestination(target)}`
      : target.in_reply_to
        ? `Reply on ${target.repo}#${target.number}${target.comment?.author ? ` · to ${target.comment.author}` : ""}`
        : `PR review · ${target.repo}#${target.number}`;
  const shown = draft.final_text || draft.text;
  const text = el("textarea", { rows: Math.min(10, shown.split("\n").length + 1) });
  text.value = shown;
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  const node = el(
    "div",
    { class: "card draft", "data-id": draft.id, "data-session": draft.session_id },
    el(
      "div",
      { class: "head" },
      el("span", { class: "label", text: `${label}${owner && owner.role === "worker" ? ` · from ${owner.title}` : ""}` }),
      el("span", { class: "chip waiting", text: "Waiting for you" }),
    ),
    draft.kind === "slack" && target.to ? el("div", { class: "note", text: `The session says it is for: ${target.to}` }) : null,
    draft.kind === "slack" && target.latest ? el("div", { class: "note", text: "Latest there, from Slack:" }) : null,
    draft.kind === "slack" && target.latest ? el("div", { class: "quoted", text: target.latest }) : null,
    draft.note ? el("div", { class: "note", text: draft.note }) : null,
    // Not published after an interrupted send: what happened, and the owner's edit kept.
    draft.outcome ? el("div", { class: "note", text: draft.outcome }) : null,
    draft.kind === "github" && target.comment
      ? el(
          "div",
          { class: "quoted" },
          el("strong", { text: `${target.comment.author}${target.comment.path ? ` on ${target.comment.path}${target.comment.line ? `:${target.comment.line}` : ""}` : ""}: ` }),
          target.comment.body,
        )
      : null,
    draft.kind === "github" ? el("a", { href: target.comment?.url || target.url, target: "_blank", rel: "noreferrer", text: target.comment?.url || target.url }) : null,
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
