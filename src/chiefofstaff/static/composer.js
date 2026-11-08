// Images that go along with a message: pasted, dropped or picked, shown small before sending.
import { $, el, toast } from "./dom.js";

const TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const MAX_FILES = 5;
const MAX_BYTES = 5 * 1024 * 1024;
// What one message may hold (as the server counts: characters, not bytes); the count shows near it.
export const MAX_CHARS = 20000;
const NEAR_CHARS = 18000;
const pending = [];  // { name, type, url }: url is a data: URL, for the thumbnail and the upload.

const composer = () => $("#composer");
const attachable = () => !composer().hidden && !["locked", "no-attach"].some((name) => composer().classList.contains(name));

function read(file) {
  return new Promise((resolve, reject) => {
    const reader = new window.FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
export async function addFiles(files) {
  for (const file of files) {
    const name = file.name || `image.${file.type.split("/")[1] || "png"}`;
    if (!TYPES.includes(file.type)) {
      toast(`${name}: only images (PNG, JPEG, GIF or WebP) can go along.`, "error");
      continue;
    }
    if (file.size > MAX_BYTES) {
      toast(`${name} is larger than 5 MB.`, "error");
      continue;
    }
    if (pending.length >= MAX_FILES) {
      toast(`At most ${MAX_FILES} images go along with one message.`, "error");
      break;
    }
    try {
      const url = await read(file);
      if (pending.length < MAX_FILES) pending.push({ name, type: file.type, url });
    } catch {
      toast(`Could not read ${name}.`, "error");
    }
  }
  render();
}
function render() {
  const box = $("#attachments");
  box.hidden = !pending.length;
  box.replaceChildren(
    ...pending.map((item) =>
      el(
        "div",
        { class: "thumb", title: item.name },
        el("img", { src: item.url, alt: item.name }),
        el("button", {
          type: "button",
          class: "remove",
          text: "×",
          title: "Remove",
          "aria-label": `Remove ${item.name}`,
          onclick: () => {
            pending.splice(pending.indexOf(item), 1);
            render();
            $("#composer-input").focus();
          },
        }),
      ),
    ),
  );
}
export const hasAttachments = () => pending.length > 0 && attachable();
const number = (count) => count.toLocaleString("en-US");
// Over the limit: what to say instead of sending (the text stays in the box).
export function tooLong(text) {
  const count = [...text].length;
  return count > MAX_CHARS ? `Too long: ${number(count)} characters, the limit is ${number(MAX_CHARS)}` : "";
}
// Near the limit, how much it holds; over it, by how much (sending is refused).
export function showLength() {
  const node = $("#composer-count");
  const text = $("#composer-input").value.trim();
  const count = text.length >= NEAR_CHARS ? [...text].length : 0;
  node.hidden = count < NEAR_CHARS;
  node.classList.toggle("over", count > MAX_CHARS);
  node.textContent = node.hidden ? "" : tooLong(text) || `${number(count)} of ${number(MAX_CHARS)} characters`;
}
// What goes with the message now; put back with restoreAttachments if sending fails.
export function takeAttachments() {
  if (!attachable()) return [];
  const taken = pending.splice(0);
  render();
  return taken;
}
export function restoreAttachments(items) {
  pending.unshift(...items);
  pending.splice(MAX_FILES);
  render();
}
// For the API: the file as base64, without the data: prefix.
export const attachmentBody = (items) => items.map(({ name, type, url }) => ({ name, type, data: url.slice(url.indexOf(",") + 1) }));
export function bindAttachments() {
  const picker = $("#attach-input");
  $("#attach-button").addEventListener("click", () => picker.click());
  picker.addEventListener("change", () => {
    addFiles([...picker.files]);
    picker.value = "";
  });
  $("#composer-input").addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files || [])].filter((file) => file.type.startsWith("image/"));
    if (!files.length || !attachable()) return;
    event.preventDefault();
    addFiles(files);
  });
  const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
  const zone = $("#conversation");
  zone.addEventListener("dragover", (event) => {
    if (!hasFiles(event) || !attachable()) return;
    event.preventDefault();
    composer().classList.add("dropping");
  });
  zone.addEventListener("dragleave", (event) => {
    if (!zone.contains(event.relatedTarget)) composer().classList.remove("dropping");
  });
  zone.addEventListener("drop", (event) => {
    composer().classList.remove("dropping");
    if (!hasFiles(event) || !attachable()) return;
    event.preventDefault();
    addFiles([...event.dataTransfer.files]);
    $("#composer-input").focus();
  });
  // A file dropped anywhere else must not replace the page.
  window.addEventListener("dragover", (event) => hasFiles(event) && event.preventDefault());
  window.addEventListener("drop", (event) => hasFiles(event) && event.preventDefault());
}
