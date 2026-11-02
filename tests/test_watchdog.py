from datetime import UTC, datetime

from work_buddy.watchdog import WatchConfig, flags

NOW = 1_800_000_000.0


def at(offset: float) -> str:
    return datetime.fromtimestamp(NOW + offset, UTC).isoformat()


def message(kind, text="", offset=0.0, prompt=False):
    return {
        "kind": kind,
        "text": text,
        "created_at": at(offset),
        "meta": {"prompt": text} if prompt else {},
    }


def worker(status="working", updated=0.0):
    return {"status": status, "updated_at": at(updated), "created_at": at(-4000)}


TASK = message("router", "migrate db", -1200, prompt=True)
CONFIG = WatchConfig()


def test_busy_recent_worker_is_fine():
    assert (
        flags(worker(updated=-60), [TASK, message("tool", "bash pytest", -60)], NOW, CONFIG) == []
    )


def test_silence_repetition_and_long_runs_are_flagged():
    silent = flags(
        worker(updated=-1000), [TASK, message("tool", "bash migrate", -1000)], NOW, CONFIG
    )
    assert silent == ["no activity for 17 min"]
    steps = [message("tool", "bash ./run_tests.sh", -50 + i) for i in range(7)]
    [looping] = flags(worker(updated=-10), [TASK, *steps], NOW, CONFIG)
    assert looping.startswith("repeated `bash ./run_tests.sh` 7 times")
    old_task = message("router", "big refactor", -3000, prompt=True)
    [long] = flags(worker(updated=-5), [old_task, message("tool", "edit a.py", -5)], NOW, CONFIG)
    assert long == "working on the same instructions for 50 min"


def test_idle_worker_without_a_report_is_flagged():
    stopped = [TASK, message("assistant", "I'll wait for the build.", -1000)]
    assert flags(worker("idle", -1000), stopped, NOW, CONFIG) == [
        "stopped without reporting for 17 min"
    ]
    reported = [*stopped, message("report", "blocked on access", -990)]
    assert flags(worker("idle", -990), reported, NOW, CONFIG) == []
    adopted = [message("assistant", "terminal work", -5000)]
    assert flags(worker("idle", -5000), adopted, NOW, CONFIG) == []
