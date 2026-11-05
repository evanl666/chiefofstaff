// Cards for what waits for the owner, under Needs you on Home: drafts to approve, questions,
// decisions, forms and sign-ins. Each has one main action; the others are behind "⋯". A card
// leaves as soon as the owner acts, and comes back with the reason if that did not work.

import { api, el, moreMenu, submits, toast } from "./dom.js";
import { displayPath, markdown } from "./text.js";
import { applyDraft, state } from "./state.js";
import { select } from "./session.js";
import { renderSidebar } from "./sidebar.js";
import { scheduleHome } from "./inbox.js";
// A card: its label (what kind of thing it is), then what it is about.
function card(draft, className, label, ...content) {
  return el("div", { class: `card draft ${className}`.trim(), "data-id": draft.id, "data-session": draft.session_id || undefined }, el("div", { class: "head" }, el("span", { class: "label" }, label)), ...content);
}
// A text box as tall as its text, up to `max` lines; it grows as the owner writes.
export function fitting(text, { label = "", max = 12 } = {}) {
  const box = el("textarea", { rows: 1, "aria-label": label || undefined });
  box.value = text;
  const fit = () => {
    // Not on screen (a closed view, a hidden tab): sized when it shows, by the observer.
    if (!box.isConnected || !box.clientWidth) return;
    box.style.height = "auto";
    const line = parseFloat(getComputedStyle(box).lineHeight) || 21;
    const limit = line * max + 16;
    box.style.height = `${Math.min(box.scrollHeight + 2, limit)}px`;
    box.style.overflowY = box.scrollHeight + 2 > limit ? "auto" : "hidden";
  };
  box.addEventListener("input", fit);
  // Sized once it is on the page, and again when its width changes (a tab shown, a resize).
  let width = 0;
  new window.ResizeObserver(([entry]) => {
    if (Math.abs(entry.contentRect.width - width) < 1) return;
    width = entry.contentRect.width;
    requestAnimationFrame(fit);
  }).observe(box);
  return box;
}
// "Something else…": a field that shows only when the owner wants it.
function later(text, build) {
  const link = el("button", { type: "button", class: "link", text });
  link.addEventListener("click", () => {
    const field = build();
    link.replaceWith(...field);
    field.find((node) => node.matches?.("input, textarea"))?.focus();
  });
  return link;
}
// The actions row: the main action last, the rest behind "⋯".
function actionRow(error, main, more = [], ...extra) {
  return el("div", { class: "actions" }, error, ...extra, moreMenu(more), main);
}
// The owner's decision on a card: it leaves at once, the toast says what was done; if the
// server says no, the card comes back with why (`failed` shows it elsewhere, in a view).
function decider(draft, node, error, failed = null) {
  return async function decide(body, done = "") {
    const key = `draft:${draft.id}`;
    error.textContent = "";
    state.leaving.add(key);
    node.classList.add("leaving");
    renderSidebar();
    scheduleHome();
    try {
      const result = await api(`/api/drafts/${draft.id}`, { method: "POST", body });
      applyDraft(result?.id === draft.id && result.status ? result : { ...draft, status: "answered" });
      if (done) toast(done);
      return true;
    } catch (failure) {
      state.leaving.delete(key);
      node.classList.remove("leaving");
      if (failed) failed(failure.message);
      else error.textContent = failure.message;
      renderSidebar();
      scheduleHome();
      return false;
    }
  };
}
// A one-line reason in place of a card's actions: to reject a draft, or to have it redone.
function reasonForm(actions, { placeholder, label, className = "secondary", submit, back }) {
  const reason = el("input", { placeholder, "aria-label": placeholder, autocomplete: "off" });
  const go = () => submit(reason.value.trim());
  reason.addEventListener("keydown", (event) => {
    if (submits(event)) go();
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      back();
    }
  });
  actions.replaceChildren(reason, el("button", { type: "button", class: "text", text: "Cancel", onclick: back }), el("button", { type: "button", class: className, text: label, onclick: go }));
  reason.focus();
}
// What the owner does not like about a draft: dropped, or sent back to its session to redo.
function rejectForm(actions, decide, back) {
  reasonForm(actions, { placeholder: "Why? (optional, helps the next draft)", label: "Reject", back, submit: (reason) => decide({ action: "reject", reason }, "Dropped.") });
}
function redoForm(actions, decide, back) {
  reasonForm(actions, { placeholder: "What should change?", label: "Send back", back, submit: (reason) => decide({ action: "revise", reason }, "Sent back to redo. The new draft comes here.") });
}
// The Router is not sure which folder a task belongs in: the owner picks one.
function workspaceCard(draft) {
  const target = draft.target;
  const error = el("span", { class: "error" });
  const other = el("input", { placeholder: "Another folder, e.g. ~/code/app" });
  const node = card(draft, "workspace", `Where should “${target.title}” work?`, el("div", { class: "note", text: [draft.text, target.why].filter(Boolean).join(" — ") }));
  const decide = decider(draft, node, error);
  const choose = (choice) => decide({ action: "choose", choice }, "Starting it there.");
  const choices = el("div", { class: "choices" });
  target.options.forEach((option, index) => {
    choices.append(
      el(
        "button",
        { type: "button", class: index ? "secondary" : "", title: option.path, onclick: () => choose(option.path) },
        el("span", { text: option.path.split("/").pop() }),
        el("small", { text: `${displayPath(option.path)}${option.configured ? "" : " · new project"}` }),
      ),
    );
  });
  choices.append(el("button", { type: "button", class: "secondary", onclick: () => choose("") }, el("span", { text: "No project" }), el("small", { text: "its own scratch folder" })));
  const use = () => other.value.trim() && choose(other.value.trim());
  other.addEventListener("keydown", (event) => submits(event) && use());
  node.append(
    choices,
    actionRow(error, el("button", { type: "button", class: "secondary", text: "Use", onclick: use }), [{ text: "Don't start it", onclick: () => decide({ action: "reject" }, "Not started.") }], other),
  );
  return node;
}
const REQUEST_LABELS = { question: "Question", form: "Form", link: "Link to open", signin: "Sign-in" };
const SECRET_FIELD = /pass(word)?|secret|token|api[_-]?key/i;
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
  const target = draft.target;
  const error = el("span", { class: "error" });
  const node = card(draft, `request ${draft.kind}`, REQUEST_LABELS[draft.kind], el("div", { class: "body markdown", html: markdown(draft.text) }));
  const decide = decider(draft, node, error);
  const decline = (text) => ({ text, onclick: () => decide({ action: "reject" }, "Declined.") });
  const link = (url, text) => (/^https?:\/\//.test(url) ? el("a", { class: "button", href: url, target: "_blank", rel: "noreferrer", text }) : null);
  if (draft.kind === "question") {
    const answer = (text) => text.trim() && decide({ action: "answer", text }, "Answered.");
    if (target.choices?.length) {
      node.append(el("div", { class: "choices" }, ...target.choices.map((choice) => el("button", { type: "button", class: "secondary", text: choice, onclick: () => answer(choice) }))));
    }
    const field = () => {
      const input = el("input", { placeholder: "Your answer", autocomplete: "off", "aria-label": "Your answer" });
      input.addEventListener("keydown", (event) => submits(event) && answer(input.value));
      return [input, el("button", { type: "button", class: target.choices?.length ? "secondary" : "", text: "Answer", onclick: () => answer(input.value) })];
    };
    if (!target.choices?.length) node.append(actionRow(error, null, [decline("Skip")], ...field()));
    // With choices to pick from, typing an answer is the exception: there when wanted.
    else node.append(actionRow(error, null, [decline("Skip")], target.allow_freeform !== false ? later("Other answer…", field) : ""));
  } else if (draft.kind === "form") {
    const fields = formFields(target.schema);
    node.append(fields.node, actionRow(error, el("button", { type: "button", text: "Submit", onclick: () => decide({ action: "answer", content: fields.values() }, "Submitted.") }), [decline("Decline")]));
  } else if (draft.kind === "link") {
    node.append(actionRow(error, link(target.url, "Open link"), [{ text: "Done", onclick: () => decide({ action: "answer" }, "Done.") }, decline("Decline")]));
  } else {
    node.append(
      el("div", { class: "note", text: "This card closes by itself once you're signed in." }),
      actionRow(error, link(target.url, `Sign in to ${target.server}`), [{ text: "I signed in", onclick: () => decide({ action: "answer" }, "Done.") }, decline("Not now")]),
    );
  }
  return node;
}
// Cards change as workers join a decision or confirm a changed one.
export const draftKey = (draft) => `draft:${draft.id}:${draft.kind === "decision" ? draft.target.askers.length : draft.kind === "change" ? draft.target.tasks.map((task) => (task.ack ? 1 : 0)).join("") : ""}`;
const taskLink = (item) => el("button", { type: "button", class: "link", text: item.title, onclick: () => select(item.session_id) });
// A decision that is the owner's: why it comes up, the options and what each means, and what
// the worker recommends. Workers that hit the same question wait on the same card.
function decisionCard(draft) {
  const target = draft.target;
  const error = el("span", { class: "error" });
  // Which task asks, in its label; several tasks that wait on it are listed below it.
  const askers = target.askers || [];
  const node = card(
    draft,
    "decision",
    askers.length === 1 ? ["Decision for ", taskLink(askers[0])] : "Decision",
    askers.length > 1 ? el("div", { class: "affects" }, el("span", { text: `For ${askers.length} tasks:` }), ...askers.map(taskLink)) : null,
    el("div", { class: "question", text: target.question }),
    target.why ? el("div", { class: "why", text: target.why }) : null,
  );
  const decide = decider(draft, node, error);
  const answer = (text) => text.trim() && decide({ action: "answer", text }, "Decided.");
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
            { type: "button", class: `option${option === best ? " recommended" : ""}`, onclick: () => answer(option.choice) },
            el("span", { class: "choice", text: option.choice }),
            option === best ? el("span", { class: "tag", text: "Recommended" }) : null,
            option.effect ? el("small", { text: option.effect }) : null,
          ),
        ),
      ),
    );
  }
  if (target.recommendation) {
    // "Per account: it protects each user directly" says why next to the option marked.
    const reason = best && lowered.startsWith(best.choice.toLowerCase()) ? target.recommendation.slice(best.choice.length).replace(/^[\s:,.;—–-]+/, "") : "";
    node.append(el("div", { class: "recommendation", text: reason ? `Why ${best.choice.toLowerCase()}: ${reason}` : `Recommended: ${target.recommendation}` }));
  }
  if (target.precedents?.length) node.append(el("div", { class: "precedents", text: `You decided before: ${target.precedents.join(" · ")}` }));
  const field = () => {
    const input = el("input", { placeholder: "Your decision", autocomplete: "off", "aria-label": "Your decision" });
    input.addEventListener("keydown", (event) => submits(event) && answer(input.value));
    return [input, el("button", { type: "button", class: "secondary", text: "Decide", onclick: () => answer(input.value) })];
  };
  node.append(actionRow(error, null, [{ text: "Not now", onclick: () => decide({ action: "reject" }, "Left for later.") }], target.options?.length ? later("Something else…", field) : field()));
  return node;
}
// The owner changed a decision: which tasks it affects, and which workers confirmed.
function changeCard(draft) {
  const target = draft.target;
  const error = el("span", { class: "error" });
  const told = (item) => item.told !== false;
  const confirmed = target.tasks.filter((item) => told(item) && item.ack).length;
  const redo = target.tasks.filter((item) => told(item) && (item.was === "review" || item.was === "done")).length;
  const node = card(
    draft,
    "change",
    "Decision changed",
    el("div", { class: "question", text: target.decision.text }),
    el("div", { class: "before", text: `Was: ${target.before.join("; ")}` }),
    el("div", { class: "why", text: `${confirmed} of ${target.tasks.length} task${target.tasks.length === 1 ? "" : "s"} confirmed${redo ? `; ${redo} checked result${redo === 1 ? " is" : "s are"} out of date and being redone` : ""}.` }),
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
  const decide = decider(draft, node, error);
  node.append(actionRow(error, el("button", { type: "button", class: "secondary", text: "Got it", onclick: () => decide({ action: "answer" }) })));
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
  const node = card(
    draft,
    "lessons",
    "Learned from your work",
    el("div", { class: "why", text: "Keep what fits how you work; reword or untick the rest. Kept ones go into your sessions' instructions." }),
    el("ul", { class: "lesson-list" }, ...rows.map((row) => row.node)),
  );
  const decide = decider(draft, node, error);
  // Every lesson shown, an unticked one as "": lessons added to the card meanwhile come back.
  const keep = () => decide({ action: "answer", content: Object.fromEntries(rows.map((row) => [row.lesson.id, row.keep.checked ? row.text.value : ""])) }, "Kept.");
  node.append(actionRow(error, el("button", { type: "button", text: "Keep ticked", onclick: keep }), [{ text: "Drop all", onclick: () => decide({ action: "reject" }, "Dropped.") }]));
  return node;
}
// A check learned from a mistake a worker fixed, with the proof that it catches it.
function checkCard(draft) {
  const target = draft.target;
  const error = el("span", { class: "error" });
  const where = target.applies_to.length ? target.applies_to.join(", ") : "any file";
  const caught = (target.failing.output || "").trim().split("\n").slice(-3).join("\n");
  const node = card(
    draft,
    "check",
    "Check learned from a mistake",
    el("div", { class: "question", text: target.name }),
    el("div", { class: "why", text: `${target.why} Runs in ${target.repo} on ${where} when a worker says it is done${target.task ? `; from “${target.task}”` : ""}.` }),
    el("pre", { text: target.command }),
    el(
      "div",
      { class: "proof" },
      el("span", { text: `✓ Fails on the code that had the mistake (exit ${target.failing.exit})${caught ? `: ${caught}` : ""}` }),
      el("span", { text: `✓ Passes on the fixed code (exit ${target.fixed.exit})` }),
    ),
  );
  const decide = decider(draft, node, error);
  node.append(actionRow(error, el("button", { type: "button", text: "Use this check", onclick: () => decide({ action: "answer" }, "Check in use.") }), [{ text: "Drop", onclick: () => decide({ action: "reject" }, "Dropped.") }]));
  return node;
}
// The owner profile a worker distilled: read and edit it here; sessions use it once kept.
function profileCard(draft) {
  const error = el("span", { class: "error" });
  const text = fitting(draft.text, { label: "Profile", max: 14 });
  const node = card(draft, "profile", "Your profile", el("div", { class: "why", text: "Distilled from your work. Correct anything; once you use it, your sessions get it with their instructions." }), text);
  const decide = decider(draft, node, error);
  node.append(actionRow(error, el("button", { type: "button", text: "Use this profile", onclick: () => text.value.trim() && decide({ action: "answer", text: text.value }, "Profile in use.") }), [{ text: "Discard", onclick: () => decide({ action: "reject" }, "Discarded.") }]));
  return node;
}
// One of the owner's own Copilot sessions, which the Router wants to continue here.
function adoptCard(draft) {
  const target = draft.target;
  const error = el("span", { class: "error" });
  const node = card(
    draft,
    "adopt",
    "Continue a session here?",
    el("div", { class: "why", text: `“${target.title}”, one of your Copilot sessions (${displayPath(target.cwd || "")}). The Router wants to give it this, as a new task:` }),
    el("div", { class: "quoted", text: target.text }),
  );
  const decide = decider(draft, node, error);
  node.append(actionRow(error, el("button", { type: "button", text: "Continue it here", onclick: () => decide({ action: "answer" }, "Continuing it here.") }), [{ text: "Not now", onclick: () => decide({ action: "reject" }, "Left as it is.") }]));
  return node;
}
// Where a Slack reply really goes, from its conversation id (D: a direct message, C: a channel,
// G: a private group); what the drafting session calls it is only its description.
const SLACK_KIND = { D: "direct message", C: "channel", G: "private group" };
export function slackDestination(target) {
  const kind = SLACK_KIND[target.channel_id?.[0]] || "conversation";
  return `${kind} ${target.channel_id}${target.thread_ts ? ", in a thread" : ""}`;
}
// A PR review's verdict, published as the owner's: what the card calls it, and its Send button.
const VERDICTS = {
  comment: ["Review", "Send review", "Comment"],
  approve: ["Approval", "Approve & send", "Approve"],
  request_changes: ["Changes requested", "Request changes & send", "Request changes"],
};
// A reply Chief of Staff cannot send itself (mail, a doc, Slack without a token): the owner
// copies it and sends it there.
function replyCard(draft) {
  const target = draft.target;
  const text = fitting(draft.final_text || draft.text, { label: "Reply" });
  const error = el("span", { class: "error" });
  // Where it goes opens the conversation, to paste it there.
  const where = target.where || "conversation";
  const link = /^https?:\/\//.test(target.link || "") ? el("a", { href: target.link, target: "_blank", rel: "noreferrer", title: "Open the conversation", text: `${where} ↗` }) : where;
  const node = card(
    draft,
    "reply",
    [`Reply to ${target.to || "someone"} · `, link],
    draft.note ? el("div", { class: "note", text: draft.note }) : null,
    text,
  );
  const decide = decider(draft, node, error);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text.value);
    } catch {
      error.textContent = "Could not copy: select the text and copy it, then mark it sent.";
      return;
    }
    decide({ action: "send", text: text.value }, "Copied. Paste it there and send it.");
  };
  const actions = el("div", { class: "actions" });
  const show = () =>
    actions.replaceChildren(
      error,
      moreMenu([
        { text: "Ask to redo…", onclick: () => redoForm(actions, decide, show) },
        { text: "Reject…", onclick: () => rejectForm(actions, decide, show) },
      ]),
      el("button", { type: "button", text: "Copy & mark sent", onclick: copy }),
    );
  show();
  node.append(actions);
  return node;
}
export function draftCard(draft) {
  if (draft.kind === "lessons") return lessonsCard(draft);
  if (draft.kind === "check") return checkCard(draft);
  if (draft.kind === "profile") return profileCard(draft);
  if (draft.kind === "adopt") return adoptCard(draft);
  if (draft.kind === "decision") return decisionCard(draft);
  if (draft.kind === "change") return changeCard(draft);
  if (draft.kind === "workspace") return workspaceCard(draft);
  if (draft.kind === "reply") return replyCard(draft);
  if (REQUEST_LABELS[draft.kind]) return requestCard(draft);
  if (draft.kind === "github" && !draft.target.in_reply_to) return reviewCard(draft);
  return messageCard(draft);
}
// A Slack reply, or a reply to a comment on a PR: the text to send, edited in place.
function messageCard(draft) {
  const target = draft.target;
  const pr = () => el("a", { href: target.comment?.url || target.url, target: "_blank", rel: "noreferrer", title: "Open on GitHub", text: `${target.repo}#${target.number}` });
  // Where a Slack reply really goes is in its tooltip, by Slack's id.
  const label =
    draft.kind === "slack"
      ? el("span", { title: slackDestination(target), text: `Slack reply · ${SLACK_KIND[target.channel_id?.[0]] || "conversation"}${target.thread_ts ? ", in a thread" : ""}` })
      : [`Reply on `, pr(), target.comment?.author ? ` · to ${target.comment.author}` : ""];
  const text = fitting(draft.final_text || draft.text, { label: "Draft" });
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  // What it answers, then who it is for and why, in one quiet line.
  const about = [draft.kind === "slack" && target.to ? `For ${target.to}` : "", draft.note].filter(Boolean).join(" · ");
  const comment = target.comment;
  const node = card(
    draft,
    draft.kind,
    label,
    draft.kind === "slack" && target.latest ? el("div", { class: "quoted", text: target.latest }) : null,
    comment ? el("div", { class: "quoted" }, el("strong", { title: comment.path || "", text: `${comment.author}${comment.path ? ` on ${basename(comment.path)}${comment.line ? `:${comment.line}` : ""}` : ""}: ` }), comment.body) : null,
    about ? el("div", { class: "note", text: about }) : null,
    // Not published after an interrupted send: what happened, and the owner's edit kept.
    draft.outcome ? el("div", { class: "note", text: draft.outcome }) : null,
    text,
    actions,
  );
  const decide = decider(draft, node, error);
  const sent = draft.kind === "slack" ? "Sent on Slack." : "Posted on GitHub.";
  const show = () =>
    actions.replaceChildren(
      error,
      moreMenu([
        { text: "Ask to redo…", onclick: () => redoForm(actions, decide, show) },
        { text: "Reject…", onclick: () => rejectForm(actions, decide, show) },
      ]),
      el("button", { type: "button", text: "Send", onclick: () => decide({ action: "send", text: text.value }, sent) }),
    );
  show();
  return node;
}
const basename = (path) => path.split("/").pop();
// A long name keeps its start and its end: "CompanyNa…Normalizer.scala".
const middle = (text, max = 44) => (text.length <= max ? text : `${text.slice(0, Math.ceil(max / 2) - 1)}…${text.slice(-Math.floor(max / 2))}`);
// What the PR is, in a line: “Add greet” by @maya · +3 −1 in 1 file.
function prLine(pr) {
  if (!pr) return "";
  const size = pr.files ? ` · +${pr.additions} −${pr.deletions} in ${pr.files} file${pr.files === 1 ? "" : "s"}` : "";
  return `${pr.title ? `“${pr.title}”` : "This PR"}${pr.author ? ` by @${pr.author}` : ""}${size}`;
}
const VERDICT_CHIP = { comment: "", approve: "done", request_changes: "waiting" };
// A PR review: compact here (what the PR is, the verdict, the summary's start, how many line
// comments); read and edited in its own view, where it is sent from.
function reviewCard(draft) {
  const target = draft.target;
  const comments = target.comments || [];
  const review = {
    summary: draft.final_text || draft.text,
    verdict: VERDICTS[target.verdict] && !target.own ? target.verdict : "comment",
    comments: comments.map((comment) => ({ ...comment, text: comment.body, removed: false })),
  };
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  const pr = el("a", { href: target.url, target: "_blank", rel: "noreferrer", title: "Open on GitHub", text: `${target.repo}#${target.number}` });
  const chip = el("span", { class: "chip" });
  const preview = el("div", { class: "summary-preview" });
  const count = el("div", { class: "note comments-note" });
  const node = card(
    draft,
    "github pr-review",
    ["Review of ", pr],
    target.pr ? el("div", { class: "pr-line", title: target.pr.about || "", text: prLine(target.pr) }) : null,
    draft.outcome ? el("div", { class: "note", text: draft.outcome }) : null,
    preview,
    count,
    actions,
  );
  node.querySelector(".head").append(chip);
  // The card says what the view has now: edits there show here.
  const refresh = () => {
    const [, , verdict] = VERDICTS[review.verdict];
    chip.textContent = verdict;
    chip.className = `chip ${VERDICT_CHIP[review.verdict]}`;
    chip.hidden = review.verdict === "comment";
    const summary = review.summary.trim();
    preview.textContent = summary || "No summary: the line comments only.";
    preview.classList.toggle("empty", !summary);
    const kept = review.comments.filter((comment) => !comment.removed).length;
    count.textContent = !comments.length ? "No line comments" : kept === comments.length ? `${comments.length} line comment${comments.length === 1 ? "" : "s"}` : `${kept} of ${comments.length} line comments`;
  };
  let view = null;
  const decide = decider(draft, node, error);
  // Sent from the view: if it is not posted, the view comes back as the owner left it, with why.
  const decideInView = decider(draft, node, error, (message) => view.failed(message));
  const open = () => {
    view ||= reviewView(draft, review, { decide: decideInView, refresh });
    view.open();
  };
  const show = () =>
    actions.replaceChildren(
      error,
      moreMenu([
        { text: "Ask to redo…", onclick: () => redoForm(actions, decide, show) },
        { text: "Reject…", onclick: () => rejectForm(actions, decide, show) },
      ]),
      el("button", { type: "button", text: "Review…", onclick: open }),
    );
  show();
  refresh();
  return node;
}
// A comment's diff lines, as the PR shows them: added green, removed red.
function diffContext(context) {
  if (!context) return null;
  return el(
    "pre",
    { class: "diff-context" },
    ...context.split("\n").map((line) => el("span", { class: line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "", text: `${line}\n` })),
  );
}
// The review, to read and change before it goes out as the owner's: the summary, each line
// comment with the code it is about (remove one, with Undo), the verdict; sent from here.
function reviewView(draft, review, { decide, refresh }) {
  const target = draft.target;
  const summary = fitting(review.summary, { label: "Summary", max: 14 });
  summary.placeholder = "No summary: post the line comments only";
  summary.addEventListener("input", () => {
    review.summary = summary.value;
    refresh();
  });
  const rows = review.comments.map((comment) => commentRow(comment, refresh));
  const error = el("p", { class: "error", role: "alert" });
  const verdict = target.own ? null : el("select", { class: "verdict", "aria-label": "Verdict", title: "What the review says on GitHub" }, ...Object.entries(VERDICTS).map(([value, [, , text]]) => el("option", { value, text })));
  if (verdict) verdict.value = review.verdict;
  const send = el("button", { type: "button", class: "send" });
  const named = () => {
    send.textContent = VERDICTS[review.verdict][1];
  };
  verdict?.addEventListener("change", () => {
    review.verdict = verdict.value;
    named();
    refresh();
  });
  named();
  const footer = el("footer");
  const close = el("button", { type: "button", class: "icon close", title: "Close (your changes are kept)", "aria-label": "Close", text: "✕" });
  const about = target.pr?.about ? el("p", { class: "pr-about" }, el("span", { class: "clamp", text: target.pr.about })) : null;
  if (about && target.pr.about.length > 90) {
    const more = el("button", { type: "button", class: "link more", text: "more" });
    more.addEventListener("click", () => {
      const opened = about.firstChild.classList.toggle("open");
      more.textContent = opened ? "less" : "more";
    });
    about.append(more);
  }
  const dialog = el(
    "dialog",
    { class: "review-dialog", "data-draft": draft.id, "aria-label": `Review of ${target.repo}#${target.number}` },
    el(
      "header",
      {},
      el("div", { class: "title" }, el("h2", {}, el("span", { text: "Review of" }), el("a", { href: target.url, target: "_blank", rel: "noreferrer", title: "Open on GitHub", text: `${target.repo}#${target.number} ↗` })), close),
      target.pr ? el("p", { class: "pr-line", text: prLine(target.pr) }) : null,
      about,
    ),
    el(
      "div",
      { class: "review-body" },
      el("label", { class: "field" }, el("span", { text: "Summary" }), summary),
      rows.length ? el("h3", {}, "Line comments ", el("span", { class: "count", text: String(rows.length) })) : el("p", { class: "note", text: "No line comments." }),
      el("div", { class: "review-comments" }, ...rows),
    ),
    footer,
  );
  // Everything that would go out, as the owner left it.
  const body = () => ({
    action: "send",
    content: {
      body: review.summary,
      ...(verdict ? { verdict: review.verdict } : {}),
      ...Object.fromEntries(review.comments.map((comment, index) => [`comment:${index}`, comment.removed ? "" : comment.text])),
    },
  });
  const sendIt = async () => {
    error.textContent = "";
    const kept = review.comments.some((comment) => !comment.removed && comment.text.trim());
    if (!review.summary.trim() && !kept && review.verdict !== "approve") {
      error.textContent = "Nothing to post: write a summary or keep a line comment.";
      return;
    }
    dialog.close();
    await decide(body(), "Posted on GitHub.");
  };
  const actions = () => {
    footer.replaceChildren(
      error,
      verdict || el("span", { class: "note", text: "Your own PR: comments only" }),
      el("span", { class: "spacer" }),
      el("button", { type: "button", class: "secondary", text: "Ask to redo…", onclick: redo }),
      send,
    );
  };
  function redo() {
    const reason = el("input", { placeholder: "What should change? The worker drafts it again.", "aria-label": "What should change?", autocomplete: "off" });
    const back = () => {
      actions();
      send.focus();
    };
    const go = async () => {
      dialog.close();
      await decide({ action: "revise", reason: reason.value.trim() }, "Sent back to redo. The new draft comes here.");
    };
    reason.addEventListener("keydown", (event) => {
      if (submits(event)) go();
      if (event.key === "Escape") {
        event.preventDefault();
        back();
      }
    });
    footer.replaceChildren(error, reason, el("button", { type: "button", class: "text", text: "Cancel", onclick: back }), el("button", { type: "button", text: "Send back", onclick: go }));
    reason.focus();
  }
  send.addEventListener("click", sendIt);
  close.addEventListener("click", () => dialog.close());
  actions();
  document.body.append(dialog);
  return {
    open() {
      if (!dialog.isConnected) document.body.append(dialog);
      if (!dialog.open) dialog.showModal();
      summary.focus();
    },
    failed(message) {
      actions();
      error.textContent = message;
      if (!dialog.open) dialog.showModal();
    },
  };
}
// One line comment in the review view: where, the code, the comment; removed, it stays as a
// struck-through line with Undo.
function commentRow(comment, changed) {
  const lines = comment.start_line ? `${comment.start_line}–${comment.line}` : comment.line;
  const name = basename(comment.path);
  const dir = comment.path.slice(0, comment.path.length - name.length);
  const text = fitting(comment.text, { label: `Comment on ${name}:${lines}`, max: 10 });
  text.addEventListener("input", () => {
    comment.text = text.value;
    changed();
  });
  const toggle = el("button", { type: "button", class: "link remove" });
  const row = el(
    "div",
    { class: "review-comment" },
    el(
      "div",
      { class: "where", title: `${comment.path}:${lines}${comment.side === "LEFT" ? " (removed lines)" : ""}` },
      el("span", { class: "file", text: `${middle(name)}:${lines}` }),
      comment.side === "LEFT" ? el("span", { class: "side", text: "old" }) : null,
      dir ? el("span", { class: "dir", text: dir.replace(/\/$/, "") }) : null,
      toggle,
    ),
    diffContext(comment.context),
    text,
  );
  const show = () => {
    row.classList.toggle("removed", comment.removed);
    toggle.textContent = comment.removed ? "Undo" : "✕ Remove";
    toggle.title = comment.removed ? "Keep this comment" : "Leave this comment out";
  };
  toggle.addEventListener("click", () => {
    comment.removed = !comment.removed;
    show();
    changed();
    if (!comment.removed) text.focus();
  });
  show();
  return row;
}
// A draft whose sending a stop interrupted, and that could not be confirmed.
export function uncertainCard(draft) {
  const target = draft.target;
  const where = draft.kind === "slack" ? `Slack reply to the ${slackDestination(target)}` : draft.kind === "reply" ? `Reply to ${target.to}` : `${target.in_reply_to ? "Reply" : "PR review"} on ${target.repo}#${target.number}`;
  const error = el("span", { class: "error" });
  const node = el(
    "div",
    { class: "card draft uncertain" },
    el("div", { class: "head" }, el("span", { class: "label", text: where }), el("span", { class: "chip waiting", text: "Sent?" })),
    el("div", { class: "note", text: draft.outcome || "Chief of Staff stopped while sending this. Check whether it arrived." }),
    el("div", { class: "body quoted", text: draft.final_text || draft.text }),
  );
  const decide = decider(draft, node, error);
  node.append(
    actionRow(error, el("button", { type: "button", text: "Send again", onclick: () => decide({ action: "send", text: draft.final_text || draft.text }, "Sent.") }), [
      { text: "It was sent", onclick: () => decide({ action: "mark_sent" }, "Marked as sent.") },
      { text: "Drop it", onclick: () => decide({ action: "reject" }, "Dropped.") },
    ]),
  );
  return node;
}
