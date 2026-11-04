// Reading a file a worker wrote (a report, a walkthrough, results) right here.
import { $, api, el, toast } from "./dom.js";
import { markdown } from "./text.js";

function copy(path) {
  navigator.clipboard.writeText(path).then(
    () => toast("Path copied"),
    () => toast(path),
  );
}

export async function openFile(path, session = "") {
  let file;
  try {
    file = await api(`/api/file?path=${encodeURIComponent(path)}${session ? `&session=${encodeURIComponent(session)}` : ""}`);
  } catch (error) {
    // A folder, or a path outside the workers' folders: copying it is what is left.
    copy(path);
    toast(`${error.message} The path was copied.`);
    return;
  }
  const dialog = $("#file-dialog");
  dialog.dataset.session = session;
  $("#file-name").textContent = file.name;
  $("#file-path").textContent = file.path;
  const markdownFile = /\.(md|markdown)$/i.test(file.name);
  const body = markdownFile ? el("div", { class: "markdown", html: markdown(file.text) }) : el("pre", {}, el("code", { text: file.text }));
  $("#file-body").replaceChildren(body, file.truncated ? el("p", { class: "note", text: "Only the first part of this large file is shown." }) : "");
  $("#file-copy").onclick = () => copy(file.path);
  $("#file-close").onclick = () => dialog.close();
  if (!dialog.open) dialog.showModal();
  $("#file-body").scrollTop = 0;
}
