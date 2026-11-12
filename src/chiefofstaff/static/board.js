// Where each task stands, task by task (not worker by worker): a worker with several tasks
// may be on one now, have one waiting for the owner and one being watched. Four places, one
// question each: does the owner have to act (Needs you), is the task being worked on now
// (Running), is it waiting on something that will wake it (Monitoring), or is it over
// (Recently finished). Queued, overdue, a result to accept and the like are markers within
// them. Pure: no page, so it is tested on its own (tests/test_ui_text.py). Home, the sidebar
// and the cards all place tasks with taskPhase, so they never disagree.

export const SECTIONS = [
  ["needs", "Needs you"],
  ["running", "Running"],
  ["monitoring", "Monitoring"],
  ["finished", "Recently finished"],
];
// The dot of a section: amber needs you, blue at work, a blue ring monitoring, gray over.
export const SECTION_DOT = { needs: "waiting", running: "working", monitoring: "watching", finished: "" };
// A row's status in a word: its marker's (what kind of "needs you", "monitoring" or "finished"
// it is), else its place's.
export const MARKERS = {
  blocked: "Blocked",
  stopped: "Stopped",
  question: "Question",
  failed: "Failed",
  review: "Awaiting acceptance",
  update: "Update",
  stalled: "No progress",
  "check-failed": "Check-back failed",
  following: "Following up",
  terminal: "In a terminal",
  background: "Background process",
  queued: "Queued",
  turn: "Up next",
  paused: "Paused",
  "check-back": "Checks back",
  watching: "Monitoring",
  waiting: "Waiting for you",
  overdue: "Overdue",
  idle: "Nothing to wait on",
  delivered: "Delivered",
  "pr-failing": "CI failing",
  "pr-changes": "Changes requested",
  "pr-conflicts": "Merge conflicts",
  "runs-unknown": "Status unknown",
  mergeable: "Ready to merge",
  reviewed: "Reviewed",
  "still-following": "Still following",
  done: "Done",
  "set-aside": "Set aside",
  cancelled: "Cancelled",
};
const PLACE_WORDS = { needs: "Needs you", running: "Running", monitoring: "Monitoring", finished: "Done" };
export const statusWord = (phase) => MARKERS[phase.marker] || PLACE_WORDS[phase.section] || "";

// What a PR's state comes to, in a word or two, worst first: one rule for Artifacts, a card's
// PR chip, what Monitoring says it waits on and where its task is placed (changes requested
// wins over merge conflicts, and those over failing CI: the author has to act on the review
// either way, and CI is of little use until the branch merges cleanly).
export function prStatus(pr) {
  if (pr.state === "MERGED") return ["merged", "good"];
  if (pr.state === "CLOSED") return ["closed", "muted"];
  if (pr.draft) return ["draft", "active"];
  if (pr.review === "CHANGES_REQUESTED") return ["changes requested", "bad"];
  if (pr.merge_state === "DIRTY") return ["merge conflicts", "bad"];
  if (pr.checks === "failing") return ["CI failing", "bad"];
  if (pr.awaiting?.length) return ["awaiting approval", "wait"];
  if (pr.checks === "pending") return ["CI running", "active"];
  // GitHub would merge it now (approved, or no review required): a Ready-to-merge card says so.
  if (["CLEAN", "HAS_HOOKS"].includes(pr.merge_state)) return ["ready to merge", "good"];
  if (pr.review === "APPROVED") return ["approved", "good"];
  return ["in review", "active"];
}
// Where an accepted task's own open PRs that it follows stand, the one that matters most first:
// one to fix (changes requested, merge conflicts, CI failing), then one ready to merge. Null
// when none of them asks for anything (in review, awaiting approval, CI running). The server's
// word on it (`attention`) when its receipts do not say.
const PR_MARKERS = { "changes requested": "pr-changes", "merge conflicts": "pr-conflicts", "CI failing": "pr-failing", "ready to merge": "mergeable" };
// Its own PR to fix: the owner asks its worker to (or acts on GitHub).
export const PR_TO_FIX = ["pr-changes", "pr-conflicts", "pr-failing"];
// The markers of an accepted task by where its PR stands (one row per followed PR, see boardOf).
const DELIVERED = ["delivered", "mergeable", ...PR_TO_FIX];
const prKey = (key) => /^github:(.+#\d+)$/i.exec(key || "")?.[1]?.toLowerCase() || "";
const prOfUrl = (url) => {
  const found = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/.exec(url || "");
  return found ? `${found[1]}#${found[2]}`.toLowerCase() : "";
};
function ownPrStanding(task) {
  const watched = new Set([...followedKeys(task).map(prKey), ...(Array.isArray(task.monitoring) ? task.monitoring.flatMap((item) => [prKey(item.key), prOfUrl(item.url)]) : [])].filter(Boolean));
  const found = (task.receipts?.prs || [])
    .filter((pr) => pr.own && watched.has(pr.label.toLowerCase()))
    .map((pr) => ({ pr, word: prStatus(pr)[0] }))
    .filter((item) => PR_MARKERS[item.word]);
  const rank = (item) => Object.keys(PR_MARKERS).indexOf(item.word);
  const [first] = found.sort((a, b) => rank(a) - rank(b));
  if (!first) {
    const marker = { merge: "mergeable" }[task.attention] || task.attention;
    return [...PR_TO_FIX, "mergeable"].includes(marker) ? { marker, why: task.attention_why || "" } : null;
  }
  const { pr, word } = first;
  const short = pr.label.replace(/^[^/]+\//, "");
  const failing = word === "CI failing" && pr.failing?.length ? ` (${pr.failing.join(", ")})` : "";
  const why = { "changes requested": `Changes requested on ${short}`, "merge conflicts": `Merge conflicts on ${short}`, "CI failing": `CI failing on ${short}${failing}`, "ready to merge": `Ready to merge: ${short}` }[word];
  return { marker: PR_MARKERS[word], why };
}

// What a task follows: those read lately and those that could not be (as they stood last).
const followedKeys = (task) => [...(task.watching || []), ...(task.watch_unreadable || [])];
// The most pressing first when one PR's row stands for several tasks (see boardOf).
const URGENCY = { "pr-changes": 0, "pr-conflicts": 0, "pr-failing": 0, mergeable: 1 };
// Then the task the PR is: 0 it made or changed one it follows, 1 it reviewed one, 2 it only
// names them (receipts from before roles were kept: by their flags).
const PR_OWNERSHIP = { created: 0, modified: 0, reviewed: 1 };
function ownership(task) {
  const keys = [...followedKeys(task), ...(Array.isArray(task.monitoring) ? task.monitoring.map((item) => item.key) : [])];
  const followedPrs = new Set(keys.map(prKey).filter(Boolean));
  const ranks = (task.receipts?.prs || [])
    .filter((pr) => followedPrs.has(String(pr.label).toLowerCase()))
    .map((pr) => PR_OWNERSHIP[pr.role || (pr.own ? "created" : pr.reviewed ? "reviewed" : "")] ?? 2);
  return Math.min(2, ...ranks);
}

const hours = (from, now) => (from ? (now - Date.parse(from)) / 3600000 : 0);
export const howLong = (hours) => (hours >= 48 ? `${Math.floor(hours / 24)}d` : `${Math.floor(hours)}h`);
const working = (worker) => Boolean(worker) && ["working", "starting"].includes(worker.status);

// What a PR it follows waits for, by where it stands (an older server's tasks: see obligationsOf).
const PR_WAITS = { "awaiting approval": "approval", "CI running": "CI", "in review": "review", draft: "review", approved: "merge", "ready to merge": "merge", "CI failing": "CI", "changes requested": "changes", "merge conflicts": "conflicts resolved" };
// What wakes a task: what it waits for, and when it is looked at.
const TRIGGERS = {
  pr: "News on the PR (a review, CI, a reply, the merge) wakes its worker",
  thread: "A new message there wakes its worker",
  check_back: "It said when to look again; it is woken then",
};
// What a task still waits on that will wake it, each { key, kind, label, url, waiting_for,
// last_check, next_check, trigger, changed_at }: the server's `monitoring`. From an older
// server, what its PRs and threads and its check-back say; a PR the owner reviewed for someone
// else is not one once its task is accepted (what its author does about it is theirs).
export function obligationsOf(task) {
  // A task still open that follows a PR or a thread for itself waits on it, whatever the server
  // counts as the owner's to watch.
  const open = !["review", "done", "cancelled", "failed"].includes(task.status);
  if (Array.isArray(task.monitoring) && (task.monitoring.length || !open || !followedKeys(task).length)) return task.monitoring;
  const found = [];
  const ended = ["review", "done"].includes(task.status);
  if (!(ended && task.watch_kind === "reviewed")) {
    const prs = task.receipts?.prs || [];
    for (const key of [...new Set(followedKeys(task))]) {
      const label = prKey(key);
      if (label) {
        const pr = prs.find((item) => item.label.toLowerCase() === label);
        const [owner, number] = label.split("#");
        found.push({ key, kind: "pr", label: key.slice("github:".length), url: pr?.url || `https://github.com/${owner}/pull/${number}`, waiting_for: pr ? PR_WAITS[prStatus(pr)[0]] || "" : "", last_check: pr?.at || null, next_check: null, trigger: TRIGGERS.pr, changed_at: task.watch_changed_at || pr?.changed_at || null });
      } else {
        found.push({ key, kind: "thread", label: key, url: "", waiting_for: "a reply", last_check: null, next_check: null, trigger: TRIGGERS.thread, changed_at: task.watch_changed_at || null });
      }
    }
  }
  const back = task.check_back;
  if (back && !ended) found.push({ key: "check_back", kind: "check_back", label: back.what || "", url: "", waiting_for: back.what || "its check-back", last_check: null, next_check: back.at || null, trigger: TRIGGERS.check_back, changed_at: null });
  return found;
}
// What a monitored task waits on, in parts: what its first obligation waits for ("Waiting for
// approval"), how long nothing has changed there ("No updates for 26h"; it stays monitored
// however long that is: news there brings it under Needs you), that obligation, and how many
// more there are. A run nothing told of for a day is no wait: where it stands is not known
// ("Status unknown · reported running 2d ago"), said only when nothing else is waited for.
// Null when it waits on nothing (a check-back is said apart).
export function monitorParts(task, now = Date.now()) {
  const all = obligationsOf(task);
  // A check-back is said on its own (when it wakes it), unless it is all there is.
  const others = all.filter((item) => item.kind !== "check_back");
  const waits = others.length ? others : all;
  const first = waits.find((item) => !item.unknown) || waits[0];
  if (!first) return null;
  const more = waits.length - 1;
  if (first.unknown) {
    // Since the last word on it: its worker's, or GitHub's as Chief of Staff last read it (bug
    // E59).
    const last = [first.reported_at, first.last_check].filter(Boolean).sort().pop();
    const said = last ? hours(last, now) : 0;
    return { what: "Status unknown", quiet: said >= 1 ? `reported ${first.status || "running"} ${howLong(said)} ago` : "", first, more };
  }
  const changed = waits.filter((item) => !item.unknown).map((item) => item.changed_at).filter(Boolean).sort().pop() || task.watch_changed_at || "";
  const quiet = changed ? hours(changed, now) : 0;
  return { what: first.waiting_for ? `Waiting for ${first.waiting_for}` : "Following", quiet: quiet >= 1 ? `No updates for ${howLong(quiet)}` : "", first, more };
}
// Why runs whose status is unknown need the owner, in a line: "Nightly export: reported
// running 2d ago · its later report no longer lists it".
export function unknownWhy(task, now = Date.now()) {
  const parts = monitorParts(task, now);
  if (!parts) return "";
  const { first, more, quiet } = parts;
  const what = `${first.label || first.key}${more ? ` and ${more} more` : ""}`;
  return [quiet ? `${what}: ${quiet}` : what, first.unlisted ? "its later report no longer lists it" : ""].filter(Boolean).join(" · ");
}
// Its reason in a line: "Waiting for approval · No updates for 26h".
export function monitorWhy(task, now = Date.now()) {
  const parts = monitorParts(task, now);
  if (!parts) return "";
  return [parts.what === "Following" ? `Following ${parts.first.label || parts.first.key}` : parts.what, parts.quiet].filter(Boolean).join(" · ");
}

// The task a worker's steps are for now: the one its latest instruction named while that is
// open, or finished and the owner asked about it since (a follow-up); else the one it is on;
// else its newest running one. A task it reported is not what it works on: a note about it
// does not make it so either.
export function activeTaskId(worker, list) {
  if (!worker) return "";
  const running = list.filter((task) => task.status === "running").sort((a, b) => a.created_at.localeCompare(b.created_at));
  const turn = list.find((task) => task.id === worker.turn_task);
  if (turn && (["running", "waiting"].includes(turn.status) || (turn.follow_up && ["review", "done"].includes(turn.status)))) return turn.id;
  if (running.some((task) => task.id === worker.task?.task_id)) return worker.task.task_id;
  return running.length ? running[running.length - 1].id : "";
}

// What a worker's tasks need to know of each other: the one its turn is on (`active`, its
// title) and one stopped mid-work that waits for the owner's Go on (`stoppedTitle`).
export function workerContext(worker, list) {
  const active = activeTaskId(worker, list);
  return {
    active,
    activeTitle: list.find((task) => task.id === active)?.title || "",
    stoppedTitle: list.find((task) => task.status === "waiting" && task.stopped)?.title || "",
  };
}
// One task's place: { section, marker, why }. `active`, `activeTitle`, `stoppedTitle`: see
// workerContext; `onCard`: shown on a card under Needs you already (a draft for it), so not
// again: in no section, its marker says what that card shows of it (news on it, a result to
// accept).
export function taskPhase(task, worker, { active = "", activeTitle = "", stoppedTitle = "", now = Date.now(), stallHours = 4, onCard = false } = {}) {
  const at = (section, marker, why = "") => ({ section, marker, why });
  const watching = task.watching || [];
  const checkBack = task.check_back;
  if (task.blocked) return at("needs", "blocked", "It waits for a task that ended without finishing");
  // Its token limit stopped it (another agent's worker): what it spent is unknown, or its limit used.
  if (task.status === "waiting" && task.stopped) return at("needs", "stopped", task.spend_why || `Stopped by ${task.stopped}: tell it to go on, or cancel it`);
  // Its question is about its own draft: decided on that draft's card, which holds it.
  if (task.status === "waiting" && onCard && task.question_draft) return at("", "", "On its draft's card under Needs you");
  if (task.status === "waiting") return at("needs", "question", "It waits for your answer");
  if (task.status === "failed") return at("needs", "failed", "");
  if (task.status === "cancelled") return at("finished", "cancelled");
  if (checkBack?.attempts >= 3) return at("needs", "check-failed", `Its check-back could not wake its worker: ${checkBack.error || "unknown error"}`);
  if (task.status === "review" || task.status === "done") {
    if (onCard) return at("", task.news && task.status !== "review" ? "update" : "review", "On its card under Needs you");
    // The owner asked more about it, and its worker is at it now.
    if (working(worker) && active === task.id && task.follow_up) return at("running", "following", "Following up on your message; its result stays yours to accept");
    const standing = ownPrStanding(task);
    // Every result waits for the owner's Accept, a report too: it is never over before that,
    // and it is always said so, with Accept and Request changes at hand. Its own PR to fix
    // (changes requested, conflicts, failing CI) or news on it since is why, in its reason.
    if (task.status === "review") {
      const news = task.news ? `News: ${task.news.text || task.news.summary || "something new on it"}` : "";
      const issue = standing && standing.marker !== "mergeable" ? standing.why : news;
      return at("needs", "review", issue || (task.attention === "acceptance" ? task.attention_why || "" : ""));
    }
    if (task.news) return at("needs", "update", "");
    // Cancelled after it finished, before it was accepted: over, not done.
    if (task.set_aside) return at("finished", "set-aside", "Cancelled after it finished: not accepted");
    // Accepted, and still waiting on what comes next (a review, the merge, a reply): monitored
    // however long that takes. Its own PR as GitHub has it (as Artifacts and its chip say it):
    // one to fix is the owner's to act on; one ready to merge is offered on a Merge card (under
    // Needs you when there is one), else it waits here.
    if (obligationsOf(task).length) {
      if (standing && standing.marker !== "mergeable") return at("needs", standing.marker, standing.why);
      // All it is monitored for is runs nothing tells of now (bug E56): nothing will come of
      // waiting, the owner says what became of them.
      if (obligationsOf(task).every((item) => item.unknown)) return at("needs", "runs-unknown", unknownWhy(task, now));
      if (standing) return at("monitoring", standing.marker, standing.why);
      return at("monitoring", "delivered", monitorWhy(task, now));
    }
    if (standing && standing.marker !== "mergeable") return at("needs", standing.marker, standing.why);
    // A PR the owner reviewed for someone else: nothing of theirs to wait on (news there comes
    // under Needs you as an Update).
    if (followedKeys(task).length && task.watch_kind === "reviewed") return at("finished", "reviewed", "Reviewed; its author's next step comes as an update");
    return at("finished", "done");
  }
  if (task.status === "queued") return at("monitoring", "queued", "");
  if (!worker) return at("running", "", "");
  if (worker.elsewhere) return at("running", "terminal", "");
  // At work on it now.
  if (working(worker) && active === task.id) return at("running", "", "");
  // Its turn is over, and a command it left running for this task (a dev server) goes on.
  if (worker.background && active === task.id) return at("running", "background", "Waiting for background process");
  // Waiting on something registered for this task: whatever its worker does meanwhile.
  if (checkBack) return at("monitoring", "check-back", "");
  // Its worker is on another task: this one waits its turn when it was not taken up yet, or when
  // it was finished and the owner followed up on it (it runs again, its worker gets to it after
  // the task it is on: not the PR it followed for its result); taken up and left, it is paused
  // (it may be delivered, or wait on the owner).
  const on = activeTitle ? `“${activeTitle}”` : "another task";
  if (working(worker) && (task.result || task.follow_up)) return at("monitoring", "turn", `Up next: following up after ${on}`);
  if (watching.length || obligationsOf(task).length) return at("monitoring", "watching", monitorWhy(task, now));
  if (["queued", "starting"].includes(worker.status)) return at("monitoring", "queued", "");
  if (working(worker)) {
    if (task.started === false) return at("monitoring", "turn", `Up next: its worker is on ${on} now`);
    return at("monitoring", "paused", `Paused: its worker is on ${on} now`);
  }
  // Its worker was stopped on another task: this one comes once the owner says go on there.
  if (task.started === false && stoppedTitle) return at("monitoring", "turn", `Up next: after you say Go on to “${stoppedTitle}”`);
  if (worker.status === "waiting" && active === task.id) return at("monitoring", "waiting", "It waits for your answer on a card under Needs you");
  if (/^waiting for /i.test(worker.activity || "")) return at("monitoring", "watching", `W${worker.activity.slice(1)}`);
  // Its worker is not at work and it waits on nothing: soon a report or a nudge; long quiet,
  // its worker is asked where it stands, and if that brings nothing, the owner decides.
  const quiet = hours(task.active_at || task.updated_at, now);
  if (quiet >= stallHours) {
    const nudged = task.nudged_at && Date.parse(task.nudged_at) >= Date.parse(task.active_at || task.updated_at) - 1000;
    if (nudged && hours(task.nudged_at, now) >= 1) return at("needs", "stalled", `No progress for ${Math.floor(quiet)}h; its worker did not answer when asked`);
    return at("monitoring", "overdue", `No progress for ${Math.floor(quiet)}h${nudged ? "; its worker was asked where it stands" : ""}`);
  }
  return at("monitoring", "idle", "Its worker stopped between steps with nothing registered to wait on");
}

// The board: per section, a row per task (not a box per worker). `workers` maps a session id
// to its session; `onCards` holds the ids of tasks shown on a card under Needs you.
export function boardOf(tasks, workers, { now = Date.now(), stallHours = 4, onCards = new Set() } = {}) {
  const bySession = new Map();
  for (const task of tasks) bySession.set(task.session_id, [...(bySession.get(task.session_id) || []), task]);
  const phases = new Map();
  for (const [sessionId, list] of bySession) {
    const worker = workers.get(sessionId);
    const context = workerContext(worker, list);
    for (const task of list) phases.set(task.id, taskPhase(task, worker, { ...context, now, stallHours, onCard: onCards.has(task.id) }));
  }
  // A PR a worker follows for two of its accepted tasks (one split off the other, say) is one
  // row, on the one it needs the owner for, else the one the PR is (it made or changed it, else
  // reviewed it: not one that only names it), else the newest: the other is with the finished
  // ones, still followed.
  const followed = new Map();
  const urgency = (task) => URGENCY[phases.get(task.id).marker] ?? 2;
  const owning = new Map(tasks.map((task) => [task.id, ownership(task)]));
  for (const task of [...tasks].sort((a, b) => urgency(a) - urgency(b) || owning.get(a.id) - owning.get(b.id) || b.updated_at.localeCompare(a.updated_at))) {
    if (task.status !== "done" || !DELIVERED.includes(phases.get(task.id).marker)) continue;
    const keys = obligationsOf(task).filter((item) => item.kind !== "check_back").map((item) => String(item.key).toLowerCase());
    const key = `${task.session_id}|${(keys.length ? keys : followedKeys(task)).sort().join(",")}`;
    const first = followed.get(key);
    if (first) phases.set(task.id, { section: "finished", marker: "still-following", why: `Still following, for “${first.title}”` });
    else followed.set(key, task);
  }
  const sections = Object.fromEntries(SECTIONS.map(([key]) => [key, tasks.filter((task) => phases.get(task.id)?.section === key)]));
  const newest = (a, b) => b.updated_at.localeCompare(a.updated_at);
  sections.needs.sort(newest);
  // Running in the order the tasks came (the newest first), not by their latest step: a row
  // does not jump each time its worker moves on.
  sections.running.sort((a, b) => b.created_at.localeCompare(a.created_at));
  sections.monitoring = monitoringRows(sections.monitoring, phases);
  // Finished: the newest first across all workers.
  sections.finished.sort((a, b) => endedAt(b).localeCompare(endedAt(a)));
  return { sections, phases };
}
export const endedAt = (task) => task.ended_at || task.updated_at || "";
// Monitoring, one list: what went quiet first (overdue, nothing to wait on), then the rest,
// newest first, then the queued ones in the order they start (the oldest first).
const URGENT = { overdue: 0, idle: 1, queued: 3 };
export function monitoringRows(list, phases) {
  const rank = (task) => URGENT[phases.get(task.id).marker] ?? 2;
  const order = (a, b) => (rank(a) === URGENT.queued ? a.created_at.localeCompare(b.created_at) : b.updated_at.localeCompare(a.updated_at));
  return [...list].sort((a, b) => rank(a) - rank(b) || order(a, b));
}
// Workers at work with none of their tasks under Running and none open (news on a PR a
// finished task follows, a question about it): a row each, so their work shows. One with a
// running task shows it there instead.
export function followingUp(workers, tasks, running, terminal = new Set()) {
  const open = new Set(tasks.filter((task) => ["running", "waiting"].includes(task.status)).map((task) => task.session_id));
  return workers.filter((worker) => worker.status === "working" && !running.has(worker.id) && !terminal.has(worker.id) && !open.has(worker.id));
}
// A worker's place in the sidebar: the most pressing of its tasks' sections.
export function workerSection(phases, list) {
  const sections = new Set(list.map((task) => phases.get(task.id)?.section));
  return ["needs", "running", "monitoring"].find((section) => sections.has(section)) || "";
}
