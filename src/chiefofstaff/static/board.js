// Where each task stands, task by task (not worker by worker): a worker with several tasks
// may be on one now, have one waiting for the owner and one being watched. Pure: no page, so
// it is tested on its own (tests/test_ui_text.py).

// The board's sections, in order; "needs" shows under Needs you.
export const SECTIONS = [
  ["needs", "Needs you"],
  ["running", "Running"],
  ["monitoring", "Monitoring"],
  ["stalled", "Stalled"],
  ["queued", "Queued"],
  ["review", "Ready to review"],
  ["finished", "Recently finished"],
];
// The dot of a section, and so of the worker box in it: few colors (amber needs you, blue at
// work, a blue ring watching, green to review, red failed; gray otherwise).
export const SECTION_DOT = { needs: "waiting", running: "working", monitoring: "watching", stalled: "stalled", queued: "queued", review: "done", finished: "" };

const hours = (from, now) => (from ? (now - Date.parse(from)) / 3600000 : 0);

// The task a worker's steps are for now: the one its latest instruction named, when that is
// open; else the one it is on; else its newest running one.
export function activeTaskId(worker, list) {
  if (!worker) return "";
  const running = list.filter((task) => task.status === "running").sort((a, b) => a.created_at.localeCompare(b.created_at));
  const turn = list.find((task) => task.id === worker.turn_task);
  if (turn && (turn.status === "running" || ["working", "starting"].includes(worker.status))) return turn.id;
  if (running.some((task) => task.id === worker.task?.task_id)) return worker.task.task_id;
  return running.length ? running[running.length - 1].id : "";
}

// One task's section and why, in a few words.
export function taskPhase(task, worker, { active = "", now = Date.now(), stallHours = 4, onCard = false } = {}) {
  const working = Boolean(worker) && ["working", "starting"].includes(worker.status);
  if (task.blocked) return { section: "needs", why: "Waits for a task that ended without finishing" };
  if (task.status === "waiting") return { section: "needs", why: "Waits for your answer" };
  if (task.status === "failed") return { section: "needs", why: "Failed" };
  if (task.status === "queued") return { section: "queued", why: "" };
  if (task.status === "review" || (task.status === "done" && task.news)) {
    // The owner asked more about it, and its worker is at it now.
    if (working && active === task.id) return { section: "running", why: "Following up" };
    return onCard ? { section: "", why: "On its card under Needs you" } : { section: "review", why: "" };
  }
  if (task.status !== "running") return { section: "finished", why: "" };
  if (!worker || worker.elsewhere) return { section: "running", why: "" };
  if (["queued", "starting"].includes(worker.status) && !working) return { section: "queued", why: "" };
  if (working) {
    if (active === task.id) return { section: "running", why: "" };
    const on = worker.task?.task_id === active ? worker.task.title : "another task";
    return { section: "queued", why: `Waits its turn: its worker is on “${on}”` };
  }
  if (worker.status === "waiting" && active === task.id) return { section: "running", why: "Waits for your answer under Needs you" };
  if (task.check_back) return { section: "monitoring", why: `Checks back ${task.check_back.at ? `at ${new Date(task.check_back.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "later"}: ${task.check_back.what}` };
  if ((task.watching || []).length) return { section: "monitoring", why: "" };
  if (/^waiting for /i.test(worker.activity || "")) return { section: "monitoring", why: `W${worker.activity.slice(1)}` };
  const idle = hours(task.active_at || task.updated_at, now);
  if (idle >= stallHours) return { section: "stalled", why: `Nothing has happened on it for ${Math.floor(idle)}h` };
  return { section: "running", why: "Between steps" };
}

// The board: per section, one box per worker with its tasks there, the busiest box first.
// `workers` maps a session id to its session; `onCards` holds the ids of tasks shown on a card
// under Needs you.
export function boardOf(tasks, workers, { now = Date.now(), stallHours = 4, onCards = new Set() } = {}) {
  const bySession = new Map();
  for (const task of tasks) bySession.set(task.session_id, [...(bySession.get(task.session_id) || []), task]);
  const sections = Object.fromEntries(SECTIONS.map(([key]) => [key, new Map()]));
  const phases = new Map();
  for (const [sessionId, list] of bySession) {
    const worker = workers.get(sessionId);
    const active = activeTaskId(worker, list);
    for (const task of list) {
      const phase = taskPhase(task, worker, { active, now, stallHours, onCard: onCards.has(task.id) });
      phases.set(task.id, phase);
      if (!phase.section) continue;
      const boxes = sections[phase.section];
      boxes.set(sessionId, [...(boxes.get(sessionId) || []), task]);
    }
  }
  const newest = (list) => list.reduce((latest, task) => (task.updated_at > latest ? task.updated_at : latest), "");
  const ordered = {};
  for (const [key, boxes] of Object.entries(sections)) {
    const entries = [...boxes].map(([sessionId, list]) => ({ sessionId, tasks: sortTasks(key, list) }));
    if (key === "queued") entries.sort((a, b) => a.tasks[0].created_at.localeCompare(b.tasks[0].created_at));
    else if (key === "finished") entries.sort((a, b) => (b.tasks[0].ended_at || "").localeCompare(a.tasks[0].ended_at || ""));
    else entries.sort((a, b) => newest(b.tasks).localeCompare(newest(a.tasks)));
    ordered[key] = entries;
  }
  return { sections: ordered, phases };
}
function sortTasks(section, list) {
  if (section === "queued") return [...list].sort((a, b) => a.created_at.localeCompare(b.created_at));
  if (section === "finished") return [...list].sort((a, b) => (b.ended_at || "").localeCompare(a.ended_at || ""));
  return [...list].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}
// How many tasks a section holds (not boxes).
export const countOf = (entries) => entries.reduce((sum, entry) => sum + entry.tasks.length, 0);
