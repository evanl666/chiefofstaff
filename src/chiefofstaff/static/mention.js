// Addressing one worker from Home: "@" picks a worker, and the message goes straight to it. The
// Router hears of it with its next message.
import { $, el } from "./dom.js";
import { HOME, sessions, state, workerTitle } from "./state.js";

export const mention = { to: null, options: [], active: 0, dismissed: -1 };

export const routerSide = () => state.selected === HOME;

// "@" and what follows it up to the cursor: a name may have spaces ("@Fix ch"), not a line break
// or another "@", and does not start with a space ("@ " is not a mention).
function query(input) {
  const before = input.value.slice(0, input.selectionStart);
  const match = /(^|\s)@((?:[^\s@][^@\n]*)?)$/.exec(before);
  return match ? { text: match[2], start: before.length - match[2].length - 1 } : null;
}

function render() {
  const list = $("#mention-list");
  const input = $("#composer-input");
  list.hidden = !mention.options.length;
  // The message box is a combobox: screen readers hear the list open and the worker picked.
  input.setAttribute("aria-expanded", String(!list.hidden));
  if (list.hidden) input.removeAttribute("aria-activedescendant");
  else input.setAttribute("aria-activedescendant", `mention-${mention.active}`);
  list.replaceChildren(
    ...mention.options.map((worker, index) =>
      el(
        "li",
        {
          id: `mention-${index}`,
          role: "option",
          class: index === mention.active ? "active" : "",
          "aria-selected": String(index === mention.active),
          onmousedown: (event) => {
            event.preventDefault();
            choose(worker);
          },
        },
        el("span", { class: `dot ${worker.status}` }),
        el("span", { class: "name", text: workerTitle(worker), title: worker.title !== workerTitle(worker) ? `Started as “${worker.title}”` : undefined }),
        el("span", { class: "status", text: worker.activity || worker.status }),
      ),
    ),
  );
}

export function closeMentions() {
  mention.options = [];
  render();
}

export function onComposerInput() {
  const input = $("#composer-input");
  const found = routerSide() ? query(input) : null;
  // Escape closed the list for this "@": typing on does not bring it back (a fresh "@" does).
  if (found && !found.text) mention.dismissed = -1;
  if (!found || found.start === mention.dismissed) {
    if (!found) mention.dismissed = -1;
    closeMentions();
    return;
  }
  mention.dismissed = -1;
  // By the name it shows now (the task it is on), or the one it started with: every word typed
  // is in it ("fix ch" finds "Fix the checkout test").
  const text = found.text.toLowerCase().trim();
  const words = text.split(/\s+/);
  const named = (session) =>
    [workerTitle(session), session.title].some((name) => {
      const lower = (name || "").toLowerCase();
      return lower.includes(text) || words.every((word) => lower.includes(word));
    });
  mention.options = sessions()
    .filter((session) => session.role === "worker" && !session.helper && !session.closed_at && named(session))
    .slice(0, 8);
  mention.active = 0;
  render();
}

function choose(worker) {
  const input = $("#composer-input");
  const found = query(input);
  if (found) {
    input.value = input.value.slice(0, found.start) + input.value.slice(input.selectionStart).replace(/^\s*/, "");
    input.setSelectionRange(found.start, found.start);
  }
  closeMentions();
  setRecipient(worker);
  input.focus();
}

export function setRecipient(worker) {
  mention.to = worker ? { id: worker.id, title: workerTitle(worker) } : null;
  const chip = $("#composer-to");
  chip.hidden = !mention.to;
  chip.replaceChildren(
    ...(mention.to
      ? [
          el("span", { text: "To " }),
          el("strong", { text: mention.to.title }),
          el("button", { type: "button", class: "clear", "aria-label": "Send to your chief of staff instead", title: "Send to your chief of staff instead", text: "×", onclick: () => setRecipient(null) }),
        ]
      : []),
  );
}

// Arrow keys, Enter and Tab pick from the open list; Escape closes it. True when handled.
export function onMentionKey(event) {
  // Keys that pick or confirm characters in an input method (Chinese, Japanese) are its own.
  if (event.isComposing || event.keyCode === 229) return false;
  if (!mention.options.length) {
    if (event.key === "Backspace" && mention.to && !$("#composer-input").value) {
      setRecipient(null);
      return true;
    }
    return false;
  }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    const step = event.key === "ArrowDown" ? 1 : -1;
    mention.active = (mention.active + step + mention.options.length) % mention.options.length;
    render();
  } else if (event.key === "Enter" || event.key === "Tab") {
    choose(mention.options[mention.active]);
  } else if (event.key === "Escape") {
    mention.dismissed = query($("#composer-input"))?.start ?? -1;
    closeMentions();
  } else {
    return false;
  }
  event.preventDefault();
  return true;
}
