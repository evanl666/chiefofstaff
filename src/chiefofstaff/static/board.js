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
  report: "Report",
  done: "Done",
  cancelled: "Cancelled",
};

const hours = (from, now) => (from ? (now - Date.parse(from)) / 3600000 : 0);
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
  const receipts = task.receipts || {};
  const changed = receipts.changed || {};
  if (changed.files || changed.commits || changed.uncommitted) return true;
  if ((receipts.prs || []).some((pr) => pr.own && !["MERGED", "CLOSED"].includes(pr.state))) return true;
  if ((receipts.criteria || []).length && receipts.verdict !== "verified") return true;
  return (receipts.flags || []).length > 0;
}

// One task's place: { section, marker, why }. `active`: the task its worker's turn is on
// (`activeTitle`, its title); `onCard`: shown on a card under Needs you already (a draft for
// it), so not again.
export function taskPhase(task, worker, { active = "", activeTitle = "", now = Date.now(), stallHours = 4, onCard = false } = {}) {
  const at = (section, marker, why = "") => ({ section, marker, why });
  const watching = task.watching || [];
  const checkBack = task.check_back;
  if (task.blocked) return at("needs", "blocked", "It waits for a task that ended without finishing");
  if (task.status === "waiting") return at("needs", "question", "It waits for your answer");
  if (task.status === "failed") return at("needs", "failed", "");
  if (task.status === "cancelled") return at("finished", "cancelled");
  if (checkBack?.attempts >= 3) return at("needs", "check-failed", `Its check-back could not wake its worker: ${checkBack.error || "unknown error"}`);
  const finished = task.status === "review" || task.status === "done";
  if (finished) {
    if (onCard) return at("", "", "On its card under Needs you");
    // The owner asked more about it, and its worker is at it now.
    if (working(worker) && active === task.id && task.follow_up) return at("running", "following", "Following up on your message; its result stays yours to review");
    if (task.news) return at("needs", "update", "");
    if (task.status === "review" && needsAcceptance(task)) return at("needs", "review", "");
    // Delivered, still followed for what comes next (a review, the merge, a reply).
    if (watching.length) return at("monitoring", "delivered", "");
    return at("finished", task.status === "review" ? "report" : "done");
  }
  if (task.status === "queued") return at("monitoring", "queued", "");
  if (!worker) return at("running", "", "");
  if (worker.elsewhere) return at("running", "terminal", "");
  // At work on it now.
  if (working(worker) && active === task.id) return at("running", "", "");
  // Waiting on something registered for this task: whatever its worker does meanwhile.
  if (checkBack) return at("monitoring", "check-back", "");
  if (watching.length) return at("monitoring", "watching", "");
  if (["queued", "starting"].includes(worker.status)) return at("monitoring", "queued", "");
  // Its worker is on another task: this one waits its turn when it was not taken up yet;
  // taken up and left, it is paused (not up next: it may be delivered, or wait on the owner).
  const on = activeTitle ? `“${activeTitle}”` : "another task";
  if (working(worker)) return task.started === false ? at("monitoring", "turn", `Up next: its worker is on ${on} now`) : at("monitoring", "paused", `Paused: its worker is on ${on} now`);
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
  const sections = Object.fromEntries(SECTIONS.map(([key]) => [key, new Map()]));
  const phases = new Map();
  for (const [sessionId, list] of bySession) {
    const worker = workers.get(sessionId);
    const active = activeTaskId(worker, list);
    const activeTitle = list.find((task) => task.id === active)?.title || "";
    for (const task of list) {
      const phase = taskPhase(task, worker, { active, activeTitle, now, stallHours, onCard: onCards.has(task.id) });
      phases.set(task.id, phase);
      if (!phase.section || phase.section === "finished") continue;
      const boxes = sections[phase.section];
      boxes.set(sessionId, [...(boxes.get(sessionId) || []), task]);
    }
  }
  const newest = (list) => list.reduce((latest, task) => (task.updated_at > latest ? task.updated_at : latest), "");
  const ordered = {};
  for (const [key, boxes] of Object.entries(sections)) {
    if (key === "finished") continue;
    const entries = [...boxes].map(([sessionId, list]) => ({ sessionId, tasks: [...list].sort((a, b) => b.updated_at.localeCompare(a.updated_at)) }));
    entries.sort((a, b) => newest(b.tasks).localeCompare(newest(a.tasks)));
    ordered[key] = entries;
  }
  // Finished: the newest first across all workers (not worker by worker, which hid newer
  // results behind a busy worker's old ones); a worker's tasks next to each other share a box.
  const finished = tasks.filter((task) => phases.get(task.id)?.section === "finished").sort((a, b) => endedAt(b).localeCompare(endedAt(a)));
  return { sections: ordered, phases, finished };
}
export const endedAt = (task) => task.ended_at || task.updated_at || "";
// The first `limit` finished tasks, in boxes: consecutive tasks of one worker share a box.
export function finishedBoxes(finished, limit) {
  const boxes = [];
  for (const task of finished.slice(0, limit)) {
    const last = boxes[boxes.length - 1];
    if (last && last.sessionId === task.session_id) last.tasks.push(task);
    else boxes.push({ sessionId: task.session_id, tasks: [task], key: task.id });
  }
  return boxes;
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
