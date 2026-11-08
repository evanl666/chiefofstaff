// Where each task stands, task by task (not worker by worker): a worker with several tasks
// may be on one now, have one waiting for the owner and one being watched. Four places, one
// question each: does the owner have to act (Needs you), is the task being worked on now
// (Running), is it waiting on something that will wake it (Monitoring), or is it over
// (Recently finished). Queued, overdue, delivered, a report to read and the like are markers
// within them. Pure: no page, so it is tested on its own (tests/test_ui_text.py). Home, the
// sidebar and the cards all place tasks with taskPhase, so they never disagree.

export const SECTIONS = [
  ["needs", "Needs you"],
  ["running", "Running"],
  ["monitoring", "Monitoring"],
  ["finished", "Recently finished"],
];
// The dot of a section: amber needs you, blue at work, a blue ring monitoring, gray over.
export const SECTION_DOT = { needs: "waiting", running: "working", monitoring: "watching", finished: "" };
// Markers, in words: what kind of "needs you", "monitoring" or "finished" it is.
export const MARKERS = {
  blocked: "Blocked",
  stopped: "Stopped",
  question: "Question",
  failed: "Failed",
  review: "To review",
  update: "Update",
  stalled: "No progress",
  "check-failed": "Check-back failed",
  following: "Following up",
  terminal: "In a terminal",
  queued: "Queued",
  turn: "Up next",
  paused: "Paused",
  "check-back": "Checks back",
  watching: "Waiting on",
  waiting: "Waiting for you",
  overdue: "Overdue",
  idle: "Nothing to wait on",
  delivered: "Delivered",
  "pr-failing": "CI failing",
  "pr-changes": "Changes requested",
  mergeable: "Ready to merge",
  reviewed: "Reviewed",
  "still-following": "Still following",
  report: "Report",
  done: "Done",
  "set-aside": "Set aside",
  cancelled: "Cancelled",
};

// What a PR's state comes to, in a word or two, worst first: one rule for Artifacts, a card's
// PR chip, what Monitoring says it waits on and where its task is placed (changes requested
// wins over failing CI: the author has to act on the review either way).
export function prStatus(pr) {
  if (pr.state === "MERGED") return ["merged", "good"];
  if (pr.state === "CLOSED") return ["closed", "muted"];
  if (pr.draft) return ["draft", "active"];
  if (pr.review === "CHANGES_REQUESTED") return ["changes requested", "bad"];
  if (pr.checks === "failing") return ["CI failing", "bad"];
  if (pr.awaiting?.length) return ["awaiting approval", "wait"];
  if (pr.checks === "pending") return ["CI running", "active"];
  // GitHub would merge it now (approved, or no review required): a Ready-to-merge card says so.
  if (["CLEAN", "HAS_HOOKS"].includes(pr.merge_state)) return ["ready to merge", "good"];
  if (pr.review === "APPROVED") return ["approved", "good"];
  return ["in review", "active"];
}
// Where a delivered task's own open PRs that it follows stand, the one that matters most
// first: one to fix (changes requested, CI failing), then one ready to merge. Null when none
// of them asks for anything (in review, awaiting approval, CI running).
const PR_MARKERS = { "changes requested": "pr-changes", "CI failing": "pr-failing", "ready to merge": "mergeable" };
// The markers of a delivered task by where its PR stands (one row per followed PR, see boardOf).
const DELIVERED = ["delivered", "reviewed", "mergeable", "pr-failing", "pr-changes"];
function ownPrStanding(task) {
  const watched = new Set(followedKeys(task).map((key) => /^github:(.+#\d+)$/.exec(key)?.[1]?.toLowerCase()).filter(Boolean));
  const found = (task.receipts?.prs || [])
    .filter((pr) => pr.own && watched.has(pr.label.toLowerCase()))
    .map((pr) => ({ pr, word: prStatus(pr)[0] }))
    .filter((item) => PR_MARKERS[item.word]);
  const rank = (item) => Object.keys(PR_MARKERS).indexOf(item.word);
  const [first] = found.sort((a, b) => rank(a) - rank(b));
  if (!first) return null;
  const { pr, word } = first;
  const short = pr.label.replace(/^[^/]+\//, "");
  const failing = word === "CI failing" && pr.failing?.length ? ` (${pr.failing.join(", ")})` : "";
  const why = { "changes requested": `Changes requested on ${short}`, "CI failing": `CI failing on ${short}${failing}`, "ready to merge": `Ready to merge: ${short}` }[word];
  return { marker: PR_MARKERS[word], why };
}

// What a task follows: those read lately and those that could not be (as they stood last).
const followedKeys = (task) => [...(task.watching || []), ...(task.watch_unreadable || [])];
// The most pressing first when one PR's row stands for several tasks (see boardOf).
const URGENCY = { "pr-changes": 0, "pr-failing": 0, mergeable: 1 };

const hours = (from, now) => (from ? (now - Date.parse(from)) / 3600000 : 0);
// A delivered task whose PR (or thread) has not changed for this long goes with the finished
// ones, still followed: news there brings it back as an Update.
export const QUIET_HOURS = 24;
const howLong = (hours) => (hours >= 48 ? `${Math.floor(hours / 24)}d` : `${Math.floor(hours)}h`);
const working = (worker) => Boolean(worker) && ["working", "starting"].includes(worker.status);

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

// A result the owner has to look at before it counts: it changed code, opened a PR, has
// acceptance criteria not all verified, or a check flagged it. A report that only informs
// (an answer, an analysis) is over once delivered: it is read, not accepted.
export function needsAcceptance(task) {
  // The Router said the owner wants to see it (a mockup, a plan): even a report.
  if (task.review_required) return true;
  const receipts = task.receipts || {};
  const changed = receipts.changed || {};
  if (changed.files || changed.commits || changed.uncommitted) return true;
  if ((receipts.prs || []).some((pr) => pr.own && !["MERGED", "CLOSED"].includes(pr.state))) return true;
  if ((receipts.criteria || []).length && receipts.verdict !== "verified") return true;
  return (receipts.flags || []).length > 0;
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
// accept, a report).
export function taskPhase(task, worker, { active = "", activeTitle = "", stoppedTitle = "", now = Date.now(), stallHours = 4, onCard = false } = {}) {
  const at = (section, marker, why = "") => ({ section, marker, why });
  const watching = task.watching || [];
  const followed = followedKeys(task);
  const checkBack = task.check_back;
  if (task.blocked) return at("needs", "blocked", "It waits for a task that ended without finishing");
  if (task.status === "waiting" && task.stopped) return at("needs", "stopped", `Stopped by ${task.stopped}: tell it to go on, or cancel it`);
  // Its question is about its own draft: decided on that draft's card, which holds it.
  if (task.status === "waiting" && onCard && task.question_draft) return at("", "", "On its draft's card under Needs you");
  if (task.status === "waiting") return at("needs", "question", "It waits for your answer");
  if (task.status === "failed") return at("needs", "failed", "");
  if (task.status === "cancelled") return at("finished", "cancelled");
  if (checkBack?.attempts >= 3) return at("needs", "check-failed", `Its check-back could not wake its worker: ${checkBack.error || "unknown error"}`);
  const finished = task.status === "review" || task.status === "done";
  if (finished) {
    if (onCard) return at("", task.news ? "update" : task.status === "review" && needsAcceptance(task) ? "review" : "report", "On its card under Needs you");
    // The owner asked more about it, and its worker is at it now.
    if (working(worker) && active === task.id && task.follow_up) return at("running", "following", "Following up on your message; its result stays yours to review");
    if (task.news) return at("needs", "update", "");
    if (task.status === "review" && needsAcceptance(task)) return at("needs", "review", "");
    // Delivered, still followed for what comes next (a review, the merge, a reply): the
    // owner's own work waiting on others, or a PR they reviewed waiting on its author. Nothing
    // new there for a day, it goes with the finished ones (news brings it back).
    if (followed.length) {
      // Its own PR as GitHub has it (as Artifacts and its chip say it): one to fix is the
      // owner's to act on; one ready to merge is offered on a Merge card (shown above when
      // there is one), else it waits here, said as it is, not as "Delivered".
      const standing = ownPrStanding(task);
      if (standing && standing.marker !== "mergeable") return at("needs", standing.marker, standing.why);
      const quiet = hours(task.watch_changed_at, now);
      if (task.watch_changed_at && quiet >= QUIET_HOURS) return at("finished", "still-following", `Still following; nothing new there for ${howLong(quiet)}`);
      if (standing) return at("monitoring", standing.marker, standing.why);
      return at("monitoring", task.watch_kind === "reviewed" ? "reviewed" : "delivered", "");
    }
    if (task.set_aside) return at("finished", "set-aside", "Cancelled after it finished: not accepted");
    return at("finished", task.status === "review" ? "report" : "done");
  }
  if (task.status === "queued") return at("monitoring", "queued", "");
  if (!worker) return at("running", "", "");
  if (worker.elsewhere) return at("running", "terminal", "");
  // At work on it now.
  if (working(worker) && active === task.id) return at("running", "", "");
  // Waiting on something registered for this task: whatever its worker does meanwhile.
  if (checkBack) return at("monitoring", "check-back", "");
  // Its worker is on another task: this one waits its turn when it was not taken up yet, or when
  // it was finished and the owner followed up on it (it runs again, its worker gets to it after
  // the task it is on: not the PR it followed for its result); taken up and left, it is paused
  // (it may be delivered, or wait on the owner).
  const on = activeTitle ? `“${activeTitle}”` : "another task";
  if (working(worker) && (task.result || task.follow_up)) return at("monitoring", "turn", `Up next: following up after ${on}`);
  if (watching.length) return at("monitoring", "watching", "");
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

// The board: per section, one box per worker with its tasks there, the busiest first; a
// worker shows in every section one of its tasks is in. `workers` maps a session id to its
// session; `onCards` holds the ids of tasks shown on a card under Needs you.
export function boardOf(tasks, workers, { now = Date.now(), stallHours = 4, onCards = new Set() } = {}) {
  const bySession = new Map();
  for (const task of tasks) bySession.set(task.session_id, [...(bySession.get(task.session_id) || []), task]);
  const phases = new Map();
  for (const [sessionId, list] of bySession) {
    const worker = workers.get(sessionId);
    const context = workerContext(worker, list);
    for (const task of list) phases.set(task.id, taskPhase(task, worker, { ...context, now, stallHours, onCard: onCards.has(task.id) }));
  }
  // A PR a worker follows for two of its delivered tasks (one split off the other, say) is
  // one row, on the newest: the other is with the finished ones, still followed.
  const followed = new Map();
  const urgency = (task) => URGENCY[phases.get(task.id).marker] ?? 2;
  for (const task of [...tasks].sort((a, b) => urgency(a) - urgency(b) || b.updated_at.localeCompare(a.updated_at))) {
    if (!DELIVERED.includes(phases.get(task.id).marker)) continue;
    const key = `${task.session_id}|${followedKeys(task).sort().join(",")}`;
    const first = followed.get(key);
    if (first) phases.set(task.id, { section: "finished", marker: "still-following", why: `Still following, for “${first.title}”` });
    else followed.set(key, task);
  }
  const sections = Object.fromEntries(SECTIONS.map(([key]) => [key, new Map()]));
  for (const task of tasks) {
    const phase = phases.get(task.id);
    if (!phase.section || phase.section === "finished") continue;
    const boxes = sections[phase.section];
    boxes.set(task.session_id, [...(boxes.get(task.session_id) || []), task]);
  }
  const newest = (list) => list.reduce((latest, task) => (task.updated_at > latest ? task.updated_at : latest), "");
  const ordered = {};
  for (const [key, boxes] of Object.entries(sections)) {
    if (key === "finished") continue;
    const entries = [...boxes].map(([sessionId, list]) => ({ sessionId, tasks: [...list].sort((a, b) => b.updated_at.localeCompare(a.updated_at)) }));
    entries.sort((a, b) => newest(b.tasks).localeCompare(newest(a.tasks)));
    ordered[key] = entries;
  }
  // Finished: the newest first across all workers, a row each (not worker by worker, which
  // hid newer results behind a busy worker's old ones).
  const finished = tasks.filter((task) => phases.get(task.id)?.section === "finished").sort((a, b) => endedAt(b).localeCompare(endedAt(a)));
  return { sections: ordered, phases, finished, monitoring: monitoringRows(tasks, phases) };
}
export const endedAt = (task) => task.ended_at || task.updated_at || "";
// Monitoring, a row per task: what went quiet first (overdue, nothing to wait on), then the
// rest, newest first, then the queued ones in the order they start (the oldest first); the
// owner's deliveries waiting on others and the PRs they reviewed in groups of their own.
const URGENT = { overdue: 0, idle: 1, queued: 3 };
export function monitoringRows(tasks, phases) {
  const rank = (task) => URGENT[phases.get(task.id).marker] ?? 2;
  const order = (a, b) => (rank(a) === URGENT.queued ? a.created_at.localeCompare(b.created_at) : b.updated_at.localeCompare(a.updated_at));
  const list = tasks.filter((task) => phases.get(task.id)?.section === "monitoring").sort((a, b) => rank(a) - rank(b) || order(a, b));
  const marked = (...markers) => list.filter((task) => markers.includes(phases.get(task.id).marker));
  // Ready to merge is not waiting on others: a row of its own, not in that group.
  return { waiting: list.filter((task) => !["delivered", "reviewed"].includes(phases.get(task.id).marker)), delivered: marked("delivered"), reviewed: marked("reviewed") };
}
// Workers at work with none of their tasks under Running and none open (news on a PR a
// finished task follows, a question about it): a card each, so their work shows. One with a
// running task shows it there instead.
export function followingUp(workers, tasks, running, terminal = new Set()) {
  const open = new Set(tasks.filter((task) => ["running", "waiting"].includes(task.status)).map((task) => task.session_id));
  return workers.filter((worker) => worker.status === "working" && !running.has(worker.id) && !terminal.has(worker.id) && !open.has(worker.id));
}
// How many tasks a section holds (not boxes).
export const countOf = (entries) => entries.reduce((sum, entry) => sum + entry.tasks.length, 0);
// A worker's place in the sidebar: the most pressing of its tasks' sections.
export function workerSection(phases, list) {
  const sections = new Set(list.map((task) => phases.get(task.id)?.section));
  return ["needs", "running", "monitoring"].find((section) => sections.has(section)) || "";
}
