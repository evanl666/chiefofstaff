// Addressing one worker from the Router chat or the inbox: "@" picks a worker, and the message
// goes straight to it. The Router hears of it with its next message.
import { $, el } from "./dom.js";
import { routerId, sessions, state } from "./state.js";

export const mention = { to: null, options: [], active: 0 };

const INBOX = "inbox";
export const routerSide = () => state.selected === INBOX || state.selected === routerId();

function query(input) {
  const before = input.value.slice(0, input.selectionStart);
  const match = /(^|\s)@([^\s@]*)$/.exec(before);
  return match ? { text: match[2], start: before.length - match[2].length - 1 } : null;
}

function render() {
  const list = $("#mention-list");
  list.hidden = !mention.options.length;
  list.replaceChildren(
    ...mention.options.map((worker, index) =>
      el(
        "li",
        {
          role: "option",
          class: index === mention.active ? "active" : "",
          "aria-selected": String(index === mention.active),
          onmousedown: (event) => {
            event.preventDefault();
            choose(worker);
          },
        },
        el("span", { class: `dot ${worker.status}` }),
        el("span", { class: "name", text: worker.title }),
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
  if (!found) {
    closeMentions();
    return;
  }
  const text = found.text.toLowerCase();
  mention.options = sessions()
    .filter((session) => session.role === "worker" && !session.closed_at && session.title.toLowerCase().includes(text))
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
  mention.to = worker ? { id: worker.id, title: worker.title } : null;
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
    closeMentions();
  } else {
    return false;
  }
  event.preventDefault();
  return true;
}
