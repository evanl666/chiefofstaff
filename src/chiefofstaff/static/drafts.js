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
    if (!box.isConnected) return;
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
// server says no, the card comes back with why.
function decider(draft, node, error) {
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
    } catch (failure) {
      state.leaving.delete(key);
      node.classList.remove("leaving");
      error.textContent = failure.message;
      renderSidebar();
      scheduleHome();
    }
  };
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
// Rejecting asks why (optional): it helps the next draft.
function rejectForm(actions, decide, back) {
  const reason = el("input", { placeholder: "Why? (optional, helps the next draft)" });
  reason.addEventListener("keydown", (event) => submits(event) && decide({ action: "reject", reason: reason.value }, "Dropped."));
  actions.replaceChildren(
    reason,
    el("button", { type: "button", class: "text", text: "Cancel", onclick: back }),
    el("button", { type: "button", class: "secondary", text: "Reject", onclick: () => decide({ action: "reject", reason: reason.value }, "Dropped.") }),
  );
  reason.focus();
}
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
    actions.replaceChildren(error, moreMenu([{ text: "Reject…", onclick: () => rejectForm(actions, decide, show) }]), el("button", { type: "button", text: "Copy & mark sent", onclick: copy }));
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
  const target = draft.target;
  // A review (not a reply to a comment) carries a verdict the owner can change before sending.
  const verdict =
    draft.kind === "github" && !target.in_reply_to && !target.own && VERDICTS[target.verdict]
      ? el("select", { class: "verdict", "aria-label": "Verdict", title: "What the review says on GitHub" }, ...Object.entries(VERDICTS).map(([value, [, , text]]) => el("option", { value, text })))
      : null;
  if (verdict) verdict.value = target.verdict;
  const pr = () => el("a", { href: target.comment?.url || target.url, target: "_blank", rel: "noreferrer", title: "Open on GitHub", text: `${target.repo}#${target.number}` });
  // Where a Slack reply really goes is in its tooltip, by Slack's id.
  const label = () =>
    draft.kind === "slack"
      ? el("span", { title: slackDestination(target), text: `Slack reply · ${SLACK_KIND[target.channel_id?.[0]] || "conversation"}${target.thread_ts ? ", in a thread" : ""}` })
      : target.in_reply_to
        ? [`Reply on `, pr(), target.comment?.author ? ` · to ${target.comment.author}` : ""]
        : [`${verdict ? VERDICTS[verdict.value][0] : "PR review"} of `, pr()];
  const text = fitting(draft.final_text || draft.text, { label: "Draft" });
  // A review's comments on lines of the diff: each editable; emptied, it is not sent.
  const inline = (draft.kind === "github" && target.comments) || [];
  const comments = inline.map((comment) => {
    // As tall as its text wraps to (about 90 characters a line), one to six lines.
    const wrapped = comment.body.split("\n").reduce((sum, line) => sum + Math.max(1, Math.ceil(line.length / 90)), 0);
    const box = fitting(comment.body, { label: `Comment on ${comment.path}:${comment.line}`, max: 6 });
    box.rows = Math.min(6, wrapped);
    const lines = comment.start_line ? `${comment.start_line}-${comment.line}` : comment.line;
    return { box, node: el("div", { class: "inline-comment" }, el("div", { class: "where", text: `${comment.path}:${lines}${comment.side === "LEFT" ? " (old)" : ""}` }), box) };
  });
  const content = () => Object.fromEntries(comments.map((comment, index) => [`comment:${index}`, comment.box.value]));
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  // What it answers, then who it is for and why, in one quiet line.
  const about = [draft.kind === "slack" && target.to ? `For ${target.to}` : "", draft.note].filter(Boolean).join(" · ");
  const node = card(
    draft,
    "",
    label(),
    draft.kind === "slack" && target.latest ? el("div", { class: "quoted", text: target.latest }) : null,
    about ? el("div", { class: "note", text: about }) : null,
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
    text,
    comments.length ? el("div", { class: "note comments-note", title: "Empty a comment to leave it out", text: `${comments.length} line comment${comments.length === 1 ? "" : "s"}` }) : null,
    ...comments.map((comment) => comment.node),
    actions,
  );
  const decide = decider(draft, node, error);
  const sent = draft.kind === "slack" ? "Sent on Slack." : "Posted on GitHub.";
  const send = el("button", { type: "button", onclick: () => decide({ action: "send", text: text.value, content: verdict ? { ...content(), verdict: verdict.value } : content() }, sent) });
  const named = () => {
    send.textContent = verdict ? VERDICTS[verdict.value][1] : "Send";
    node.querySelector(".head .label").replaceChildren(...[label()].flat());
  };
  verdict?.addEventListener("change", named);
  named();
  const show = () => actions.replaceChildren(error, verdict || "", moreMenu([{ text: "Reject…", onclick: () => rejectForm(actions, decide, show) }]), send);
  show();
  return node;
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
