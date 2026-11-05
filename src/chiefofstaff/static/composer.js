// Images that go along with a message: pasted, dropped or picked, shown small before sending.
import { $, el, toast } from "./dom.js";

const TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const MAX_FILES = 5;
const MAX_BYTES = 5 * 1024 * 1024;
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
