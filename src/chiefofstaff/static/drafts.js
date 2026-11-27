// Cards for what waits for the owner, under Needs you on Home: drafts to approve, questions,
// decisions, forms and sign-ins. Each has one main action; the others are behind "⋯". A card
// leaves as soon as the owner acts, and comes back with the reason if that did not work.

import { api, el, moreMenu, once, submits, toast } from "./dom.js";
import { clock, codeHtml, displayPath, inlineHtml, markdown, slackText } from "./text.js";
import { applyDraft, finalText, pendingDrafts, state } from "./state.js";
import { select } from "./session.js";
import { renderSidebar } from "./sidebar.js";
import { agentName } from "./agentpick.js";
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
// A card holding its task's result to accept (inbox.js withTask): its own action accepts that
// too, and says so ("Copy & accept"): one decision for both (bug QA6-2).
export function relabel(node) {
  const accepts = Boolean(node.querySelector(":scope > .card.task.in-draft.review"));
  for (const button of node.querySelectorAll(":scope > .actions > button[data-alone]")) {
    const text = accepts ? button.dataset.withResult : button.dataset.alone;
    if (button.textContent !== text) button.textContent = text;
  }
}
// The owner's decision on a card: it leaves at once, the toast says what was done; if the
// server says no, the card comes back with why (`failed` shows it elsewhere, in a view).
function decider(draft, node, error, failed = null) {
  // One decision at a time: a second click while the first is out does nothing.
  return (body, done = "") => once(`draft:${draft.id}`, node, () => decide(body, done));
  async function decide(body, done) {
    const key = `draft:${draft.id}`;
    // Sent from a card that holds its task's result (to accept, or news on it): that is
    // settled with it, one decision (bug QA6-2).
    const settles = body.action === "send" && ["reply", "slack", "github"].includes(draft.kind);
    const result = settles ? node.querySelector(":scope > .card.task.in-draft") : null;
    if (result?.dataset.task) body = { ...body, accept: result.dataset.task };
    if (done && result?.classList.contains("review")) done = `${done} Result accepted.`;
    error.textContent = "";
    state.leaving.add(key);
    node.classList.add("leaving");
    renderSidebar();
    scheduleHome();
    try {
      const result = await api(`/api/drafts/${draft.id}`, { method: "POST", body });
      if (draft.kind === "connection" && result?.status === "pending") {
        // Still needed until a retry works: the card stays, saying its sessions retry.
        state.leaving.delete(key);
        node.classList.remove("leaving");
      }
      applyDraft(result?.id === draft.id && result.status ? result : { ...draft, status: "answered" });
      // What it came to, as the server says (a note, not a decision), when `done` asks.
      const said = typeof done === "function" ? done(result) : done;
      if (said) toast(said);
      return true;
    } catch (failure) {
      state.leaving.delete(key);
      node.classList.remove("leaving");
      // Said where it cannot be missed first: a card that cannot be sent again leaves (and its
      // view with it), so nothing after this may keep the toast from showing.
      toast(failure.message, "error");
      try {
        if (failed) failed(failure.message);
        else error.textContent = failure.message;
      } finally {
        renderSidebar();
        scheduleHome();
      }
      return false;
    }
  }
}
// A one-line reason in place of a card's actions: to reject a draft, or to have it redone
// (`required`: not without one).
function reasonForm(actions, { placeholder, label, className = "secondary", required = false, submit, back }) {
  const reason = el("input", { placeholder, "aria-label": placeholder, autocomplete: "off" });
  const error = el("span", { class: "error", role: "alert" });
  const go = () => {
    if (required && !reason.value.trim()) {
      error.textContent = "Say what should change.";
      reason.setAttribute("aria-invalid", "true");
      reason.focus();
      return;
    }
    submit(reason.value.trim());
  };
  reason.addEventListener("input", () => {
    error.textContent = "";
    reason.removeAttribute("aria-invalid");
  });
  reason.addEventListener("keydown", (event) => {
    if (submits(event)) go();
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      back();
    }
  });
  actions.replaceChildren(error, reason, el("button", { type: "button", class: "text", text: "Cancel", onclick: back }), el("button", { type: "button", class: className, text: label, onclick: go }));
  reason.focus();
}
// What the owner does not like about a draft: dropped, or sent back to its session to redo.
function rejectForm(actions, decide, back) {
  reasonForm(actions, { placeholder: "Why? (optional, helps the next draft)", label: "Reject", back, submit: (reason) => decide({ action: "reject", reason }, "Dropped.") });
}
function redoForm(actions, decide, back) {
  reasonForm(actions, { placeholder: "What should change?", label: "Send back", required: true, back, submit: (reason) => decide({ action: "revise", reason }, "Sent back to redo. The new draft comes here.") });
}
// The Router is not sure which folder a task belongs in: the owner picks one.
function workspaceCard(draft) {
  const target = draft.target;
  const error = el("span", { class: "error" });
  const other = el("input", { placeholder: "Another folder, e.g. ~/code/app" });
  const node = card(
    draft,
    "workspace",
    `Where should “${target.title}” work?`,
    el("div", { class: "note", text: [draft.text, target.why].filter(Boolean).join(" — ") }),
    // Picked for the owner (the only project that fits), and Changed: what it is in now.
    target.picked ? el("div", { class: "note", text: `Picked ${displayPath(target.picked)} for you: choose it again to keep it.` }) : null,
  );
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
  const label = draft.kind === "form" && draft.target?.watch ? "New watch" : REQUEST_LABELS[draft.kind];
  const node = card(draft, `request ${draft.kind}`, label, el("div", { class: "body markdown", html: markdown(draft.text) }));
  const decide = decider(draft, node, error);
  const decline = (text) => ({ text, onclick: () => decide({ action: "reject" }, "Declined.") });
  const link = (url, text) => (/^https?:\/\//.test(url) ? el("a", { class: "button", href: url, target: "_blank", rel: "noreferrer", text }) : null);
  if (draft.kind === "question") {
    const answer = (text) => text.trim() && decide({ action: "answer", text }, "Answered.");
    if (target.choices?.length) {
      node.append(el("div", { class: "choices" }, ...target.choices.map((choice) => el("button", { type: "button", class: "secondary", text: choice, onclick: () => answer(choice) }))));
    }
    const field = () => {
      const input = keepTyped(el("input", { placeholder: "Your answer", autocomplete: "off", "aria-label": "Your answer" }), `${draft.id}:answer`);
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
      // Another agent's sign-in is run in a terminal: the card cannot tell when it is done.
      target.command
        ? el("div", { class: "note", html: codeHtml(`Run \`${target.command}\` in a terminal, then press I signed in.`) })
        : el("div", { class: "note", text: "This card closes by itself once you're signed in." }),
      actionRow(error, link(target.url, `Sign in to ${target.server}`), [{ text: "I signed in", onclick: () => decide({ action: "answer" }, "Done.") }, decline("Not now")]),
    );
  }
  return node;
}
// What a connection card says: for the app the server keeps the connection for (Copilot CLI's
// unnamed; a Claude Code or Codex worker's named, E137), and once connected but still failing
// (E138), where to check it works: that app, never another one.
export function connectionWords(target) {
  const what = target.source || "your account";
  const app = target.app || "";
  if (!target.still_failing) return { body: `${target.server} needs you to connect ${what}${app ? ` for ${app}` : ""}`, note: "", button: "I've done it" };
  return {
    body: app ? `${target.server} still can't use ${what} for ${app}` : `${target.server} still can't use ${what} from Chief of Staff`,
    note: `You connected it, and a retry still failed the same way: the server may keep your connection for another app. Check that it works in ${app || "Copilot CLI"} in your terminal (connect it there if not), then try again.`,
    button: "Try again",
  };
}
// An account an MCP tool needs connected (accounts.py): one card for every session it stops.
// The tool's error and link are untrusted: plain text, and the link opens only on a click.
function connectionCard(draft) {
  const target = draft.target;
  const words = connectionWords(target);
  const error = el("span", { class: "error" });
  const count = target.affected_sessions;
  const lastError = target.last_error.length > 400 ? `${target.last_error.slice(0, 400)}…` : target.last_error;
  const node = card(
    draft,
    "request connection",
    "Connection needed",
    el("div", { class: "body", text: words.body }),
    // Retried after "I've done it" and failed the same way (E138): said so, not asked again.
    words.note ? el("div", { class: "note", text: words.note }) : null,
    el("div", { class: "note", text: `${count} ${count === 1 ? "session" : "sessions"} waiting${target.last_failed_at ? ` · last failed ${clock(target.last_failed_at)}` : ""}` }),
    lastError ? el("div", { class: "connection-error", text: lastError, title: target.last_error }) : null,
    target.url ? el("div", { class: "connection-link" }, el("code", { text: target.url }), el("small", { text: target.known_origin ? `${target.server}'s own address` : `${target.url_note}: check it before opening` })) : null,
    target.retrying ? el("div", { class: "note", text: "Each session retries once; this closes when one works." }) : null,
  );
  const decide = decider(draft, node, error);
  const open = /^https?:\/\//i.test(target.url) ? el("a", { class: "button secondary", href: target.url, target: "_blank", rel: "noreferrer", text: "Open link" }) : null;
  node.append(
    el(
      "div",
      { class: "actions" },
      error,
      el("button", { type: "button", class: "text", text: "Not now", onclick: () => decide({ action: "reject" }, "Not now.") }),
      open,
      el("button", { type: "button", text: words.button, onclick: () => decide({ action: "answer" }, "Each session retries once.") }),
    ),
  );
  return node;
}
// Cards change as workers join a decision or confirm a changed one.
export const draftKey = (draft) => `draft:${draft.id}:${draft.kind === "decision" ? `${draft.target.askers.length}:${draft.target.updated_at || ""}` : draft.kind === "change" ? draft.target.tasks.map((task) => (task.ack ? 1 : 0)).join("") : draft.kind === "lessons" ? draft.target.lessons.map((lesson) => lesson.id).join(",") : draft.kind === "connection" ? `${draft.target.affected_sessions}:${draft.target.last_failed_at}:${draft.target.retrying}:${draft.target.still_failing}:${draft.target.source}:${draft.target.url}` : ""}:${draft.outcome || ""}`;
// What the owner typed on a card, by card and field: a card drawn again (another worker joins
// a decision, a lesson is added) keeps it.
const typed = new Map();
function keepTyped(input, key) {
  if (typed.has(key)) input.value = typed.get(key);
  input.addEventListener("input", () => typed.set(key, input.value));
  return input;
}
const taskLink = (item) => el("button", { type: "button", class: "link", text: item.title, onclick: () => select(item.session_id) });
// A decision that is the owner's: why it comes up, the options and what each means, and what
// the worker recommends. Workers that hit the same question wait on the same card.
function decisionCard(draft) {
  const target = draft.target;
  const error = el("span", { class: "error" });
  // Which task asks, in its label; several tasks that wait on it are listed below it.
  const askers = target.askers || [];
  // A reconsideration: what the owner answered, when, and why it is asked again, first.
  const kept = target.replaces ? (target.previous || "").split(" → ").slice(1).join(" → ") || target.previous || "" : "";
  const answered = target.replaces ? el("div", { class: "answered", html: inlineHtml(`You answered this ${target.previous_at ? `at ${clock(target.previous_at)}` : "before"} (“${kept}”).${target.why ? ` Asked again because: ${target.why}` : ""}`) }) : null;
  const node = card(
    draft,
    "decision",
    askers.length === 1 ? ["Decision for ", taskLink(askers[0])] : "Decision",
    askers.length > 1 ? el("div", { class: "affects" }, el("span", { text: `For ${askers.length} tasks:` }), ...askers.map(taskLink)) : null,
    answered,
    el("div", { class: "question", html: inlineHtml(target.question) }),
    target.why && !answered ? el("div", { class: "why", html: inlineHtml(target.why) }) : null,
  );
  // Asked again by its worker with what it found since: the same card, saying when.
  if (target.updated_at) node.querySelector(":scope > .head").append(el("span", { class: "updated", text: `updated ${clock(target.updated_at)}` }));
  const decide = decider(draft, node, error);
  // On a reconsideration, words that name none of its options are a note to the worker, not a
  // decision (the server says which it was).
  const outcome = (result) => (/sent your note/i.test(result?.outcome || "") ? `${result.outcome}.` : "Decided.");
  const answer = (text) => text.trim() && decide({ action: "answer", text }, outcome);
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
            el("span", { class: "choice", html: codeHtml(option.choice) }),
            option === best ? el("span", { class: "tag", text: "Recommended" }) : null,
            option.effect ? el("small", { html: codeHtml(option.effect) }) : null,
          ),
        ),
      ),
    );
  }
  if (target.recommendation) {
    // "Per account: it protects each user directly" says why next to the option marked.
    const reason = best && lowered.startsWith(best.choice.toLowerCase()) ? target.recommendation.slice(best.choice.length).replace(/^[\s:,.;—–-]+/, "") : "";
    node.append(el("div", { class: "recommendation", html: inlineHtml(reason ? `Why ${best.choice.toLowerCase()}: ${reason}` : `Recommended: ${target.recommendation}`) }));
  }
  if (!answered && target.precedents?.length) node.append(el("div", { class: "precedents", html: inlineHtml(`You decided before: ${target.precedents.join(" · ")}`) }));
  const options = Boolean(target.options?.length);
  // On a reconsideration, words that name none of its options are a note (the answer stays);
  // on a first card, they are the decision.
  const notes = Boolean(answered && options);
  const field = () => {
    const label = notes ? "Your decision, or a note" : "Your decision";
    const input = keepTyped(el("input", { placeholder: label, autocomplete: "off", "aria-label": label }), `${draft.id}:decision`);
    input.addEventListener("keydown", (event) => submits(event) && answer(input.value));
    return [input, el("button", { type: "button", class: answered ? "text" : "secondary", text: notes ? "Send" : "Decide", onclick: () => answer(input.value) })];
  };
  // Typed into before it was drawn again: the field is open with it.
  const open = options && !typed.get(`${draft.id}:decision`) ? later("Something else…", field) : field();
  // A reconsideration's one main action keeps the answer in force; Not now keeps it too, for
  // now (never the change it recommends).
  const keep = answered ? el("button", { type: "button", text: "Keep", "data-tip": `Keep your answer (“${kept}”): the worker goes on with it`, onclick: () => decide({ action: "choose", choice: "keep" }, "Kept your answer.") }) : null;
  const notNow = { text: "Not now", onclick: () => decide({ action: "reject" }, answered ? "Kept your answer for now." : "Left for later.") };
  node.append(actionRow(error, keep, [notNow], ...[open].flat()));
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
    el("div", { class: "question", html: inlineHtml(target.decision.text) }),
    el("div", { class: "before", html: inlineHtml(`Was: ${target.before.join("; ")}`) }),
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
    const keep = el("input", { type: "checkbox", checked: typed.get(`${draft.id}:keep:${lesson.id}`) !== "0", "aria-label": "Keep" });
    keep.addEventListener("change", () => typed.set(`${draft.id}:keep:${lesson.id}`, keep.checked ? "1" : "0"));
    // As tall as the lesson: a long one is read whole, not cut at the box's edge.
    const text = keepTyped(fitting(lesson.text, { label: "Lesson", max: 8 }), `${draft.id}:lesson:${lesson.id}`);
    const because = lesson.because.map((item) => `${item.owner}`.trim()).filter(Boolean);
    const where = !lesson.scope ? "everywhere" : lesson.scope === "slack" ? "Slack replies" : `in ${lesson.scope.replace("project:", "").split("/").pop()}`;
    const node = el(
      "li",
      {},
      keep,
      el("div", { class: "lesson" }, text, el("small", { html: inlineHtml(`${where}${because.length ? ` · because you said: “${because.slice(0, 2).join("”, “")}”` : ""}`) })),
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
  // Only those shown: one added meanwhile comes back on the card.
  const dropAll = () => decide({ action: "answer", content: Object.fromEntries(rows.map((row) => [row.lesson.id, ""])) }, "Dropped.");
  node.append(actionRow(error, el("button", { type: "button", text: "Keep ticked", onclick: keep }), [{ text: "Drop all", onclick: dropAll }]));
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
    el("div", { class: "why", text: `“${target.title}”, one of your ${agentName(target.agent || "copilot")} sessions (${displayPath(target.cwd || "")}). The Router wants to give it this, as a new task:` }),
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
  const text = fitting(finalText(draft), { label: "Reply" });
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
    if (!text.value.trim()) {
      error.textContent = "The reply is empty.";
      return;
    }
    try {
      await navigator.clipboard.writeText(text.value);
    } catch {
      error.textContent = "Could not copy: select the text and copy it, then mark it sent.";
      return;
    }
    decide({ action: "send", text: text.value }, "Copied. Paste it there and send it.");
  };
  const actions = el("div", { class: "actions" });
  const show = () => {
    actions.replaceChildren(
      error,
      moreMenu([
        { text: "Ask to redo…", onclick: () => redoForm(actions, decide, show) },
        { text: "Reject…", onclick: () => rejectForm(actions, decide, show) },
      ]),
      el("button", { type: "button", text: "Copy & mark sent", "data-alone": "Copy & mark sent", "data-with-result": "Copy & accept", onclick: copy }),
    );
    relabel(node);
  };
  node.append(actions);
  show();
  return node;
}
// Your OK (E123; drafts.py YOUR_OK): the approvals on one card under Needs you, a line each with
// its own button, the rest of it behind "⋯" (Details shows its whole card). What speaks as the
// owner, their decisions and workers' questions keep cards of their own. Safe (local, undone in
// Settings): a changed decision seen, a check used, a lesson kept that is not about secrets,
// money or what goes out: Approve all safe takes those, never a merge, a sign-in, a link, a
// folder, a profile or a session to take over.
export const YOUR_OK = new Set(["change", "lessons", "check", "profile", "adopt", "workspace", "merge", "signin", "link"]);
// Whose whole card is open on Your OK, by draft id: kept as the card is drawn again.
const okOpen = new Set();
const lessonWhere = (lesson) => (!lesson.scope ? "" : lesson.scope === "slack" ? " · Slack replies" : ` · in ${lesson.scope.replace("project:", "").split("/").pop()}`);
// A card's lines on Your OK: { kind, text, safe, main, more }; `main` and each of `more` is
// { text, body, done } (what the card's own button sends), { text, details } (its whole card)
// or { text, href } (a link to open).
function okLines(draft) {
  const target = draft.target || {};
  const details = (text) => ({ text, details: true });
  if (draft.kind === "change") {
    const told = target.tasks.filter((item) => item.told !== false);
    const confirmed = told.filter((item) => item.ack).length;
    const changing = told.filter((item) => item.ack && item.changes !== false).length;
    const counts = `${confirmed} of ${target.tasks.length} confirmed${changing ? `, ${changing} changing` : ""}`;
    return [{ kind: "Decision changed", text: `${target.decision.text} · ${counts}`, safe: true, main: { text: "Got it", body: { action: "answer" }, done: "Seen." } }];
  }
  if (draft.kind === "lessons") {
    return target.lessons.map((lesson) => ({
      lesson,
      kind: "Lesson",
      text: `${lesson.text}${lessonWhere(lesson)}`,
      safe: lesson.sensitive === false,
      main: { text: "Keep", body: { action: "answer", content: { [lesson.id]: lesson.text } }, done: "Kept." },
      more: [{ text: "Drop", body: { action: "answer", content: { [lesson.id]: "" } }, done: "Dropped." }],
    }));
  }
  if (draft.kind === "check") return [{ kind: "Check", text: `${target.name} · ${target.repo}`, safe: true, main: { text: "Use", body: { action: "answer" }, done: "Check in use." }, more: [{ text: "Drop", body: { action: "reject" }, done: "Dropped." }] }];
  if (draft.kind === "profile") return [{ kind: "Profile", text: "Your profile, distilled from your work", main: details("Review"), more: [{ text: "Discard", body: { action: "reject" }, done: "Discarded." }] }];
  if (draft.kind === "adopt") return [{ kind: "Continue here?", text: `“${target.title}”: ${target.text || draft.text}`, main: { text: "Continue it here", body: { action: "answer" }, done: "Continuing it here." }, more: [{ text: "Not now", body: { action: "reject" }, done: "Left as it is." }] }];
  if (draft.kind === "workspace") return [{ kind: "Folder", text: target.picked ? `Where should “${target.title}” work? Picked ${target.picked.split("/").pop()} for you` : `Where should “${target.title}” work?`, main: details("Choose…"), more: [{ text: "Don't start it", body: { action: "reject" }, done: "Not started." }] }];
  if (draft.kind === "merge") {
    const what = `${target.label.replace(/^[^/]+\//, "")}${target.title ? ` “${target.title}”` : ""}`;
    // GitHub merging its stack in the background: said in its line, the PR to look at.
    if (merging(draft)) return [{ kind: "Merging", text: `${what} · ${draft.outcome.replace(/^Merging: /, "")}`, main: { text: "Open PR", href: target.url }, more: [{ text: "Not now", body: { action: "reject" }, done: "Not merged." }] }];
    return [{ kind: "Ready to merge", text: `${what} · ${target.checks === "none" ? "no checks" : "checks pass"}${stackBelow(target)}`, main: { text: mergeButton(target), body: { action: "send" }, done: `Merging ${target.label}…` }, more: [{ text: "Open PR", href: target.url }, { text: "Not now", body: { action: "reject" }, done: "Not merged." }] }];
  }
  if (draft.kind === "signin") return [{ kind: "Sign-in", text: draft.text || `Sign in to ${target.server}`, main: target.url ? { text: "Sign in", href: target.url } : details("Details"), more: [{ text: "I signed in", body: { action: "answer" }, done: "Done." }, { text: "Not now", body: { action: "reject" }, done: "Declined." }] }];
  return [{ kind: "Link to open", text: draft.text, main: target.url ? { text: "Open link", href: target.url } : details("Details"), more: [{ text: "Done", body: { action: "answer" }, done: "Done." }, { text: "Decline", body: { action: "reject" }, done: "Declined." }] }];
}
// What Your OK shows: drawn again when any of its cards changes or one is opened or closed.
export const yourOkKey = (drafts) => `ok:${drafts.map(draftKey).join("|")}:${drafts.filter((draft) => okOpen.has(draft.id)).map((draft) => draft.id).join(",")}`;
// Your OK, as one card: a line per approval, then Approve all safe when two or more are.
export function yourOkCard(drafts) {
  const error = el("span", { class: "error" });
  const rows = [];
  const list = el("ul", { class: "ok-list" });
  for (const draft of drafts) {
    const lines = okLines(draft);
    lines.forEach((line, index) => {
      const row = okRow(draft, line);
      rows.push({ ...line, draft, node: row });
      list.append(row);
      // Its whole card, under its last line, while open.
      if (index === lines.length - 1 && okOpen.has(draft.id)) list.append(el("li", { class: "ok-details" }, draftCard(draft)));
    });
  }
  const safe = rows.filter((row) => row.safe);
  const node = el("div", { class: "card draft your-ok", "data-id": "your-ok" }, el("div", { class: "head" }, el("span", { class: "label", text: "Your OK" }), el("span", { class: "spacer" }), el("span", { class: "ok-count", text: String(rows.length) })), list);
  if (safe.length > 1) {
    const all = async () => {
      for (const row of safe) row.node.classList.add("leaving");
      error.textContent = "";
      try {
        const done = await api("/api/your-ok/approve-safe", { method: "POST", body: { items: safe.map((row) => ({ draft: row.draft.id, lesson: row.lesson?.id || "" })) } });
        toast(done.left ? `Approved ${done.approved}; ${done.left} changed meanwhile and still wait${done.left === 1 ? "s" : ""} here.` : `Approved ${done.approved}.`);
      } catch (failure) {
        for (const row of safe) row.node.classList.remove("leaving");
        error.textContent = failure.message;
      }
    };
    node.append(el("div", { class: "actions" }, error, el("button", { type: "button", text: `Approve all safe (${safe.length})`, title: "Only the lines marked safe: local, and undone in Settings", onclick: () => once("your-ok", node, all) })));
  }
  return node;
}
function okRow(draft, line) {
  const error = el("span", { class: "error" });
  const row = el("li", { class: "ok-row", "data-id": draft.id, "data-lesson": line.lesson?.id });
  const act = (action) => {
    if (action.href) return () => window.open(action.href, "_blank", "noreferrer");
    if (action.details) {
      return () => {
        if (!okOpen.delete(draft.id)) okOpen.add(draft.id);
        scheduleHome();
      };
    }
    return () =>
      once(`draft:${draft.id}`, row, async () => {
        error.textContent = "";
        row.classList.add("leaving");
        try {
          const result = await api(`/api/drafts/${draft.id}`, { method: "POST", body: action.body });
          applyDraft(result?.id === draft.id && result.status ? result : { ...draft, status: "answered" });
          if (action.done) toast(action.done);
        } catch (failure) {
          row.classList.remove("leaving");
          error.textContent = failure.message;
          toast(failure.message, "error");
        }
      });
  };
  const main = line.main.href
    ? el("a", { class: "button secondary", href: line.main.href, target: "_blank", rel: "noreferrer", text: line.main.text })
    : el("button", { type: "button", class: "secondary", text: line.main.text, onclick: act(line.main) });
  const more = [...(line.more || []), ...(line.main.details ? [] : [{ text: "Details", details: true }])];
  // Its whole card open below: its actions are there, once; the line only folds it again.
  const actions = okOpen.has(draft.id)
    ? [el("button", { type: "button", class: "link quiet", text: "Hide details", onclick: act({ details: true }) })]
    : [moreMenu(more.map((item) => ({ text: item.text, onclick: act(item) }))), main];
  row.append(
    el(
      "div",
      { class: "ok-main" },
      el("div", { class: "ok-kind" }, line.kind, line.safe ? el("span", { class: "ok-safe", text: " · safe", title: "Local, and undone in Settings: Approve all safe includes it" }) : null),
      el("div", { class: "ok-text", text: line.text, title: line.text }),
    ),
    el("span", { class: "ok-actions" }, error, ...actions),
  );
  return row;
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
  if (draft.kind === "merge") return mergeCard(draft);
  if (draft.kind === "connection") return connectionCard(draft);
  if (REQUEST_LABELS[draft.kind]) return requestCard(draft);
  if (draft.kind === "github" && !draft.target.in_reply_to && !draft.target.reply_to_comment) return reviewCard(draft);
  return messageCard(draft);
}
// A PR in a stack on GitHub above open PRs (drafts.py: StackBelow): merging it merges them too,
// said on its card and its button once it is known.
const stackBelow = (target) => (target.stack_below?.length ? ` · with ${target.stack_below.map((pr) => `#${pr.number}`).join(", ")} below it` : "");
const mergeButton = (target) => (target.stack_below?.length ? `Merge all ${target.stack_below.length + 1}` : "Merge");
// GitHub merging its stack in the background (drafts.py MERGING, STILL_MERGING).
const merging = (draft) => /^(Merging:|GitHub is still merging)/.test(draft.outcome || "");
// A PR of the owner's that GitHub would merge now (checks pass, nothing blocking): merge it
// from here, as it was when offered (anything pushed since stops the merge).
function mergeCard(draft) {
  const target = draft.target;
  const error = el("span", { class: "error" });
  const link = el("a", { href: target.url, target: "_blank", rel: "noreferrer", title: "Open on GitHub", text: target.label.replace(/^[^/]+\//, "") });
  const review = { APPROVED: "Approved", REVIEW_REQUIRED: "No review yet" }[target.review] || "";
  const node = card(
    draft,
    "merge",
    ["Ready to merge · ", link],
    target.title ? el("div", { class: "note strong", text: `“${target.title}”` }) : null,
    el("div", { class: "note", text: [target.checks === "none" ? "No checks" : "Checks pass", review, `head ${String(target.head || "").slice(0, 7)}`].filter(Boolean).join(" · ") }),
    draft.outcome ? el("div", { class: "note", text: draft.outcome }) : null,
  );
  const decide = decider(draft, node, error);
  node.append(
    actionRow(
      error,
      merging(draft)
        ? el("button", { type: "button", text: "Open PR", onclick: () => window.open(target.url, "_blank", "noreferrer") })
        : el("button", { type: "button", text: mergeButton(target), onclick: () => decide({ action: "send" }, `Merging ${target.label}…`) }),
      [
        ...(merging(draft) ? [] : [{ text: "Open PR", onclick: () => window.open(target.url, "_blank", "noreferrer") }]),
        { text: "Not now", onclick: () => decide({ action: "reject" }, "Not merged.") },
      ],
    ),
  );
  return node;
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
  const text = fitting(finalText(draft), { label: "Draft" });
  const error = el("span", { class: "error" });
  const actions = el("div", { class: "actions" });
  // What it answers, then who it is for and why, in one quiet line.
  const about = [draft.kind === "slack" && target.to ? `For ${slackText(target.to)}` : "", draft.note].filter(Boolean).join(" · ");
  // It says what a review says: it goes once that review is posted (still waiting on its card).
  const review = draft.kind === "slack" && target.after_review && pendingDrafts().some((other) => other.id === target.after_review.id) ? target.after_review : null;
  const comment = target.comment;
  // A reply on a line can resolve its conversation too (ticked when its worker proposed it).
  const resolve = draft.kind === "github" && target.in_reply_to ? el("input", { type: "checkbox", checked: Boolean(target.resolve) }) : null;
  const node = card(
    draft,
    draft.kind,
    label,
    draft.kind === "slack" ? latestQuote(target) : null,
    comment ? el("div", { class: "quoted" }, el("strong", { title: comment.path || "", text: `${comment.author}${comment.path ? ` on ${basename(comment.path)}${comment.line ? `:${comment.line}` : ""}` : ""}: ` }), comment.body) : null,
    about ? el("div", { class: "note", text: about }) : null,
    review ? el("div", { class: "note warn-note waits-review", text: `Waits for ${review.label || "its review"} to be posted first: post that, then send this.` }) : null,
    // Not published after an interrupted send: what happened, and the owner's edit kept. Held
    // because someone wrote since: that message is quoted above, not said again here.
    draft.outcome && !(target.moved_on && /\bwrote since\b/.test(draft.outcome)) ? el("div", { class: "note", text: draft.outcome }) : null,
    text,
    resolve ? el("label", { class: "check resolve" }, resolve, " Resolve the conversation") : null,
    actions,
  );
  const decide = decider(draft, node, error);
  const sent = draft.kind === "slack" ? "Sent on Slack." : "Posted on GitHub.";
  // Not an empty reply: cleared, it is not the draft that goes.
  const send = () => {
    if (!text.value.trim()) {
      error.textContent = "The reply is empty.";
      return;
    }
    const content = resolve ? { resolve: resolve.checked } : null;
    decide({ action: "send", text: text.value, ...(content ? { content } : {}) }, resolve?.checked ? "Posted on GitHub; resolving the conversation." : sent);
  };
  // Held because someone wrote there since it was drafted (`moved_on`): one action, Send anyway,
  // now that the owner sees what they wrote. It is checked again on the way (the server knows
  // that message now): held again only if someone wrote after it.
  const held = draft.kind === "slack" && Boolean(target.moved_on);
  const show = () => {
    actions.replaceChildren(
      error,
      moreMenu([
        { text: "Ask to redo…", onclick: () => redoForm(actions, decide, show) },
        { text: "Reject…", onclick: () => rejectForm(actions, decide, show) },
      ]),
      el("button", { type: "button", class: review ? "secondary" : "", text: held ? "Send anyway" : "Send", title: held ? "Send it as it is, now that you have seen what they wrote" : review ? "It waits for the review to be posted first" : undefined, ...(held || review ? {} : { "data-alone": "Send", "data-with-result": "Send & accept" }), onclick: () => send() }),
    );
    relabel(node);
  };
  show();
  return node;
}
// The newest message where a Slack reply goes, as Slack writes it (its links and names
// readable); one written since the draft, marked new (the reply waits for the owner to send it
// anyway); or one plain line when it could not be read (never what Slack answered: a card made
// before 0.1.53 kept that).
function latestQuote(target) {
  if (target.latest_error) return el("div", { class: "note warn-note", text: `Couldn't read this conversation from Slack just now; the reply still goes to ${target.to ? slackText(target.to) : "this conversation"}.` });
  const moved = target.moved_on;
  // Its quote, else what the server kept of it.
  const said = target.latest || (moved ? moved.text || "" : "");
  if (!said) return null;
  const quote = el("div", { class: "quoted markdown", html: markdown(slackText(said)) });
  if (!moved) return quote;
  quote.classList.add("fresh");
  const who = slackText(moved.who || "Someone");
  return el("div", { class: "moved-on" }, el("div", { class: "fresh-label", text: `New since the draft: ${who} wrote${moved.at ? ` at ${clock(moved.at)}` : ""} · not sent yet` }), quote);
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
// What a review's card and view say: a review of someone's PR; on the owner's own PR, a reply to
// its reviewers (comments only, no verdict).
export const reviewWords = (target) => (target.own ? { label: "Reply on ", open: "Reply…", send: "Send reply" } : { label: "Review of ", open: "Review…", send: "" });
// What a review with no summary posts: its line comments, the approval alone, or nothing (then
// it needs one). `asked`: as the summary box's placeholder, what to do.
function emptySummary(review, asked = false) {
  if (review.comments.some((comment) => !comment.removed)) return asked ? "No summary: post the line comments only" : "No summary: the line comments only";
  if (review.verdict === "approve") return asked ? "No summary: approve without a comment" : "No summary: the approval only";
  return asked ? "What the review says (it needs a summary: no line comments)" : "No summary yet: it needs one";
}
// A PR review: compact here (what the PR is, the verdict, the summary's start, how many line
// comments); read and edited in its own view, where it is sent from.
function reviewCard(draft) {
  const target = draft.target;
  const comments = target.comments || [];
  const review = {
    summary: finalText(draft),
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
    [reviewWords(target).label, pr],
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
    // Its `code` as code, as GitHub shows it (and the rest escaped).
    if (summary) preview.innerHTML = inlineHtml(summary);
    else preview.textContent = `${emptySummary(review)}.`;
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
      el("button", { type: "button", text: reviewWords(target).open, onclick: open }),
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
  // What leaving it empty posts says what is there: the line comments, or the approval alone.
  const changed = () => {
    summary.placeholder = emptySummary(review, true);
    refresh();
  };
  summary.addEventListener("input", () => {
    review.summary = summary.value;
    refresh();
  });
  const rows = review.comments.map((comment) => commentRow(comment, changed));
  const error = el("p", { class: "error", role: "alert" });
  const verdict = target.own ? null : el("select", { class: "verdict", "aria-label": "Verdict", title: "What the review says on GitHub" }, ...Object.entries(VERDICTS).map(([value, [, , text]]) => el("option", { value, text })));
  if (verdict) verdict.value = review.verdict;
  const send = el("button", { type: "button", class: "send" });
  const named = () => {
    send.textContent = reviewWords(target).send || VERDICTS[review.verdict][1];
  };
  verdict?.addEventListener("change", () => {
    review.verdict = verdict.value;
    named();
    changed();
  });
  summary.placeholder = emptySummary(review, true);
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
    { class: "review-dialog", "data-draft": draft.id, "aria-label": `${reviewWords(target).label}${target.repo}#${target.number}` },
    el(
      "header",
      {},
      el("div", { class: "title" }, el("h2", {}, el("span", { text: reviewWords(target).label.trim() }), el("a", { href: target.url, target: "_blank", rel: "noreferrer", title: "Open on GitHub", text: `${target.repo}#${target.number} ↗` })), close),
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
      if (!reason.value.trim()) {
        error.textContent = "Say what should change.";
        reason.setAttribute("aria-invalid", "true");
        reason.focus();
        return;
      }
      dialog.close();
      await decide({ action: "revise", reason: reason.value.trim() }, "Sent back to redo. The new draft comes here.");
    };
    reason.addEventListener("input", () => {
      error.textContent = "";
      reason.removeAttribute("aria-invalid");
    });
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
    // Not posted: the view comes back as the owner left it, with why; unless its draft is gone
    // meanwhile (GitHub refused it for good: the toast says why, nothing to send again).
    failed(message) {
      actions();
      error.textContent = message;
      if (!pendingDrafts().some((item) => item.id === draft.id)) {
        dialog.remove();
        return;
      }
      if (!dialog.isConnected) document.body.append(dialog);
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
    { class: "card draft uncertain", "data-id": draft.id, "data-session": draft.session_id || undefined },
    el("div", { class: "head" }, el("span", { class: "label", text: where }), el("span", { class: "chip waiting", text: "Sent?" })),
    el("div", { class: "note", text: draft.outcome || "Chief of Staff stopped while sending this. Check whether it arrived." }),
    el("div", { class: "body quoted", text: finalText(draft) }),
  );
  const decide = decider(draft, node, error);
  node.append(
    actionRow(error, el("button", { type: "button", text: "Send again", onclick: () => decide({ action: "send", text: finalText(draft) }, "Sent.") }), [
      { text: "It was sent", onclick: () => decide({ action: "mark_sent" }, "Marked as sent.") },
      { text: "Drop it", onclick: () => decide({ action: "reject" }, "Dropped.") },
    ]),
  );
  return node;
}
