"""The shepherd's control plane (control.py): claims, attempts, the journal and the ledger, each
decision a compare-and-set in one transaction."""

import threading

import pytest

from chiefofstaff.control import (
    APPLYING,
    DISPATCHING,
    HANDING,
    IDLE,
    PAUSED,
    PLANNED,
    PREPARED,
    QUEUED,
    RUNNING,
    STEPPING,
    Control,
    today,
)
from chiefofstaff.store import Store

T = "github:acme/api#12"
H0, H1, H2 = "a" * 40, "b" * 40, "c" * 40


@pytest.fixture
def control(tmp_path):
    made = Control(Store(tmp_path / "data"))
    made.ensure()
    return made


def test_no_tables_until_first_turned_on(tmp_path):
    store = Store(tmp_path / "data")
    control = Control(store)
    assert not control.exists()
    assert control.claim(T) is None and control.claims() == [] and control.steps() == []
    tables = {row["name"] for row in store.all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & {"pr_claims", "shepherd_steps", "actions", "ledger_events", "verdicts"}
    control.ensure()
    tables = {row["name"] for row in store.all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"pr_claims", "shepherd_steps", "actions", "ledger_events", "verdicts"} <= tables


def test_a_claim_is_given_to_its_task_as_its_origin(control):
    claim = control.give(T, "t1", H0)
    assert (claim["holder"], claim["holder_kind"], claim["origin_task"]) == ("t1", "task", "t1")
    assert claim["state"] == IDLE and claim["resumed"] == H0
    # The head when it was given is explained; any other is not.
    assert control.explains(claim, H0) and not control.explains(claim, H1)


def test_only_published_or_resumed_heads_are_explained(control):
    control.give(T, "t1", H0)
    control.published(T, H1)
    claim = control.claim(T)
    assert control.explains(claim, H1) and control.explains(claim, H0)
    assert not control.explains(claim, H2) and not control.explains(claim, "")
    control.pause(T, "new commits not from a plan: ccccccc", H2)
    assert control.resume(T, H2)
    claim = control.claim(T)
    assert control.explains(claim, H2) and not control.explains(claim, H0)


def test_origin_acceptance_and_approved_heads_survive_every_transition(control):
    control.give(T, "t1", H0)
    control.accept(T, H0, "t1")
    control.published(T, H1)
    for change in (
        lambda: control.to_owner(T, "its worker was closed", H1),
        lambda: control.give(T, "t1", H1, paused="its worker was reopened"),
        lambda: control.resume(T, H1),
        lambda: control.to_owner(T, "its task was cancelled"),
        lambda: control.give(T, "t2", H1),
    ):
        before = control.claim(T)
        change()
        claim = control.claim(T)
        assert claim["origin_task"] == "t1"
        assert claim["accepted"]["sha"] == H0
        assert claim["approved"] == [H1]
        assert claim["generation"] > before["generation"]


def test_accepting_again_clears_the_approved_chain(control):
    control.give(T, "t1", H0)
    control.accept(T, H0, "t1")
    control.published(T, H1)
    control.accept(T, H1, "t1")
    claim = control.claim(T)
    assert claim["accepted"]["sha"] == H1 and claim["approved"] == []


def test_to_owner_pauses_and_a_closed_claim_stays_closed(control):
    control.give(T, "t1", H0)
    claim = control.to_owner(T, "its worker was closed", H0)
    assert (claim["holder"], claim["holder_kind"], claim["state"]) == ("", "owner", PAUSED)
    control.end(T, "merged")
    assert control.to_owner(T, "x") is None
    assert control.pause(T, "x") is None
    assert control.claim(T)["state"] == "ended"


def test_resume_only_from_paused(control):
    control.give(T, "t1", H0)
    assert control.resume(T, H1) is None
    control.pause(T, "x", H1)
    assert control.resume(T, H1)["state"] == IDLE


def test_set_state_is_a_compare_and_set_on_holder_and_generation(control):
    claim = control.give(T, "t1", H0)
    assert not control.set_state(T, ("t2", claim["generation"]), HANDING)
    assert not control.set_state(T, ("t1", claim["generation"] + 1), HANDING)
    assert control.set_state(T, ("t1", claim["generation"]), HANDING)
    assert control.claim(T)["state"] == HANDING


def test_update_claim_takes_only_fields_that_decide_nothing(control):
    control.give(T, "t1", H0)
    with pytest.raises(ValueError, match="holder"):
        control.update_claim(T, holder="t2")
    control.update_claim(T, keep=0, details={"head": H1})
    assert control.claim(T)["keep"] == 0 and control.claim(T)["details"] == {"head": H1}


def step_of(control, clicked=0, **values):
    claim = control.claim(T)
    values = {"h0": H0, **values}
    return control.add_step(
        T,
        "fix-ci",
        "prepare",
        holder=claim["holder"],
        generation=claim["generation"],
        consent=control.generation(),
        clicked=clicked,
        **values,
    )


def admit(control, step, budget=10.0, reserve=5.0):
    claim = control.claim(T)
    return control.admit(
        step["id"],
        claim["holder"],
        claim["generation"],
        control.generation(),
        budget,
        reserve,
        reserve - 1,
    )


def test_admission_checks_holder_generation_consent_and_state(control):
    control.give(T, "t1", H0)
    step = step_of(control)
    claim = control.claim(T)
    assert control.admit(step["id"], "t2", claim["generation"], 0, 10, 1, 1) == (
        "the PR changed hands"
    )
    assert control.admit(step["id"], "t1", claim["generation"], 5, 10, 1, 1) == (
        "Prepared for you was turned off meanwhile"
    )
    control.pause(T, "x")
    claim = control.claim(T)
    assert control.admit(step["id"], "t1", claim["generation"], 0, 10, 1, 1) == "it is paused"
    control.resume(T, H0)
    assert admit(control, step) == ""
    assert control.step(step["id"])["state"] == RUNNING
    assert control.claim(T)["state"] == STEPPING
    assert admit(control, step) == "it is no longer waiting"


def test_unclicked_attempts_fit_the_days_budget_and_clicked_ones_are_never_held(control):
    control.give(T, "t1", H0)
    first = step_of(control)
    assert admit(control, first, budget=10, reserve=6) == ""
    control.move_step(first["id"], RUNNING, state="published")
    control.set_state(T, (control.claim(T)["holder"], control.claim(T)["generation"]), IDLE)
    second = step_of(control)
    # 6 reserved of 10: another 6 does not fit.
    assert admit(control, second, budget=10, reserve=6) == "today's budget is used"
    clicked = step_of(control, clicked=1)
    assert admit(control, clicked, budget=0, reserve=6) == ""
    # Settled at 2: the reservation goes, the spend stays.
    control.settle(first["id"], [("e1", 1.5), ("e2", 0.5)], final=True)
    control.set_state(T, (control.claim(T)["holder"], control.claim(T)["generation"]), IDLE)
    assert admit(control, second, budget=10, reserve=6) == ""
    spent = control.spent_today()
    assert spent == {"settled": 2.0, "reserved": 6.0, "clicked": 0.0}


def test_concurrent_admissions_never_go_over_the_budget(tmp_path):
    store = Store(tmp_path / "data")
    control = Control(store)
    control.ensure()
    targets = [f"github:acme/api#{n}" for n in range(12)]
    steps = []
    for target in targets:
        claim = control.give(target, "t1", H0)
        steps.append(
            control.add_step(
                target,
                "fix-ci",
                "prepare",
                holder="t1",
                generation=claim["generation"],
                consent=0,
                h0=H0,
            )
        )
    results: list[str] = []
    barrier = threading.Barrier(len(steps))

    def go(step):
        barrier.wait()
        claim = control.claim(step["target"])
        results.append(control.admit(step["id"], "t1", claim["generation"], 0, 20.0, 6.0, 5.0))

    workers = [threading.Thread(target=go, args=(step,)) for step in steps]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert results.count("") == 3  # 3 reservations of 6 fit 20; 4 would not
    assert control.spent_today()["reserved"] == 18.0


def test_settlement_counts_each_usage_event_once(control):
    control.give(T, "t1", H0)
    step = step_of(control)
    assert control.settle(step["id"], [("e1", 1.0), ("e2", 2.0)], final=False) == 3.0
    assert control.step(step["id"])["settled"] is None  # Not before a proven end.
    # Read again after a restart, with one more: each id once.
    assert control.settle(step["id"], [("e1", 1.0), ("e2", 2.0), ("e3", 0.5)], final=True) == 3.5
    assert control.step(step["id"])["settled"] == 3.5


def test_caps_count_every_attempt_whatever_its_end(control):
    control.give(T, "t1", H0)
    assert control.cap_reached(T, "fix-ci", "f1", H0) == ""
    first = step_of(control, fingerprint="f1", started_at="x")
    control.move_step(first["id"], QUEUED, state="interrupted")
    assert control.cap_reached(T, "fix-ci", "f1", H0) == "tried already on this commit"
    assert control.cap_reached(T, "fix-ci", "f1", H0, clicked=True) == ""  # The owner's click.
    assert control.cap_reached(T, "fix-ci", "f1", H1) == ""
    for sha in (H1, H2):
        step_of(control, fingerprint=sha, started_at="x", h0=sha)
    assert control.cap_reached(T, "fix-ci", "f9", "d" * 40) == "3 attempts today"
    assert control.cap_reached(T, "fix-ci", "f9", "d" * 40, clicked=True) == "3 attempts today"
    assert control.cap_reached(T, "address-review", "", H0) == ""


def test_a_look_at_why_is_not_a_fix(control):
    control.give(T, "t1", H0)
    claim = control.claim(T)
    control.add_step(
        T, "fix-ci", "suggest", holder=claim["holder"], generation=claim["generation"],
        consent=control.generation(), fingerprint="f1", started_at="x", h0=H0,
    )  # fmt: skip
    assert control.cap_reached(T, "fix-ci", "f1", H0) == ""


def test_two_rounds_per_review_thread(control):
    control.give(T, "t1", H0)
    for _ in range(2):
        control.add_step(T, "address-review", "prepare", h0=H0, threads=["th1"], started_at="x")
    assert control.cap_reached(T, "address-review", "", H0, ["th1"]) == (
        "2 rounds on each of these comments"
    )
    assert control.cap_reached(T, "address-review", "", H0, ["th1", "th2"]) == ""


def test_comments_a_decided_plan_had_are_not_tried_again(control):
    control.give(T, "t1", H0)
    signal = {"threads": [{"thread": "th1", "last": 501}, {"thread": "th2", "last": 502}]}
    step = control.add_step(
        T, "address-review", "prepare", h0=H0, threads=["th1", "th2"], signal=signal,
        started_at="x",
    )  # fmt: skip
    control.move_step(step["id"], QUEUED, state="stale")
    # A plan gone stale was never decided on: its comments may be tried again.
    assert control.cap_reached(T, "address-review", "", H1, ["th2"], said=[502]) == ""
    control.move_step(step["id"], "stale", state="published")
    assert control.cap_reached(T, "address-review", "", H1, ["th2"], said=[502]) == (
        "you decided on these comments already"
    )
    assert control.cap_reached(T, "address-review", "", H1, ["th2"], said=[502, 503]) == ""
    assert control.cap_reached(T, "address-review", "", H1, ["th2"], said=[502],
                               clicked=True) == ""  # fmt: skip


def test_one_approval_wins_and_operations_are_journaled(control):
    control.give(T, "t1", H0)
    step = step_of(control, state=PREPARED)
    claim = control.claim(T)
    operations = [{"kind": "push"}, {"kind": "reply", "text": "Done"}]
    assert control.journal(step["id"], "t1", claim["generation"], 0, operations, "me") == ""
    assert control.journal(step["id"], "t1", claim["generation"], 0, operations, "me") == (
        "it was decided already"
    )
    actions = control.actions(step["id"])
    assert [(a["id"], a["kind"], a["state"]) for a in actions] == [
        (f"{step['id']}.0", "push", PLANNED),
        (f"{step['id']}.1", "reply", PLANNED),
    ]
    assert control.step(step["id"])["state"] == APPLYING


def test_approval_refused_after_a_change_of_hands_or_turning_off(control):
    control.give(T, "t1", H0)
    step = step_of(control, state=PREPARED)
    generation = control.claim(T)["generation"]
    control.give(T, "t2", H0)
    assert control.journal(step["id"], "t1", generation, 0, [{"kind": "push"}], "me") == (
        "the PR changed hands since"
    )
    claim = control.claim(T)
    control.bump()
    assert control.journal(step["id"], "t2", claim["generation"], 0, [{"kind": "push"}], "me") == (
        "Prepared for you was turned off"
    )


def test_the_dispatch_barrier(control):
    control.give(T, "t1", H0)
    step = step_of(control, state=PREPARED)
    claim = control.claim(T)
    control.journal(
        step["id"], "t1", claim["generation"], 0, [{"kind": "push"}, {"kind": "x"}], "me"
    )
    first, second = control.actions(step["id"])
    assert control.dispatching(first["id"], "t1", claim["generation"], 0) == ""
    assert control.action(first["id"])["state"] == DISPATCHING
    assert control.dispatching(first["id"], "t1", claim["generation"], 0) == (
        "it is not waiting to go out"
    )
    control.bump()  # Turned off: nothing more goes out.
    assert control.dispatching(second["id"], "t1", claim["generation"], 0) == (
        "Prepared for you was turned off"
    )
    control.finish(first["id"], "sent", link="https://x")
    assert control.action(first["id"])["state"] == "sent"
    with pytest.raises(ValueError, match="Not an operation's end"):
        control.finish(second["id"], "dispatching")


def test_move_step_is_a_compare_and_set(control):
    control.give(T, "t1", H0)
    step = step_of(control, state=PREPARED)
    assert control.move_step(step["id"], QUEUED, state="withdrawn") is None
    assert control.move_step(step["id"], (QUEUED, PREPARED), state="dismissed")["state"] == (
        "dismissed"
    )


def test_one_open_attempt_per_pr_and_per_worker(control):
    control.give(T, "t1", H0)
    step = step_of(control, session_id="w1")
    assert control.open_step(T)["id"] == step["id"]
    assert control.session_step("w1") is None
    admit(control, step)
    assert control.session_step("w1")["id"] == step["id"]


def test_verdicts_are_recorded(control):
    control.give(T, "t1", H0)
    step = step_of(control)
    control.verdict(step, "approved-edited")
    assert [(v["kind"], v["repo"], v["verdict"]) for v in control.verdicts()] == [
        ("fix-ci", "acme/api", "approved-edited")
    ]


def test_the_day_is_the_owners(control):
    assert (
        today(0) == __import__("datetime").datetime.fromtimestamp(0).astimezone().date().isoformat()
    )
