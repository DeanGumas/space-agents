"""Scheduler: slot math (active hours, weekdays, jitter) and tick() behavior with a fake clock."""

import random
from datetime import datetime, timedelta

import pytest

from space_agents.manifest import ActiveHours, Manifest, Schedule
from space_agents.runtime.scheduler import Scheduler, in_active_hours, next_run_after
from space_agents.store import Store

MON = datetime(2026, 10, 19)  # a Monday
HOURLY = Schedule(enabled=True, every_minutes=60, active_hours=ActiveHours("07:00", "18:00"))


def at(day_offset, hhmm, base=MON):
    h, m = map(int, hhmm.split(":"))
    return base + timedelta(days=day_offset, hours=h, minutes=m)


def test_runs_are_scheduled_times_plus_jitter():
    early, late = -180, 240  # 3 minutes early, 4 minutes late
    assert next_run_after(HOURLY, 0, at(0, "06:00")) == at(0, "07:00")
    assert next_run_after(HOURLY, early, at(0, "06:00")) == at(0, "06:57")  # may start before active hours
    assert next_run_after(HOURLY, late, at(0, "07:04")) == at(0, "08:04")  # strictly after
    assert next_run_after(HOURLY, late, at(0, "12:34")) == at(0, "13:04")
    assert next_run_after(HOURLY, late, at(0, "17:30")) == at(1, "07:04")  # 17:00 was the last scheduled time
    assert next_run_after(HOURLY, early, at(0, "18:30")) == at(1, "06:57")
    assert next_run_after(HOURLY, 0, at(4, "18:30")) == at(7, "07:00")  # Friday evening → Monday
    assert next_run_after(Schedule(enabled=True, weekdays=[]), 0, at(0, "06:00")) is None
    all_day = Schedule(enabled=True, every_minutes=60, active_hours=ActiveHours("00:00", "23:59"))
    assert next_run_after(all_day, 300, at(0, "00:02")) == at(0, "00:05")
    assert next_run_after(all_day, 300, at(-1, "23:59")) == at(0, "00:05")  # Sunday 23:00 + 5 min is in the past
    assert next_run_after(all_day, -300, at(0, "23:50")) == at(0, "23:55")  # Tuesday's 00:00 − 5 min comes first


def test_in_active_hours():
    assert in_active_hours(HOURLY, at(0, "07:00")) and not in_active_hours(HOURLY, at(0, "18:00"))
    assert not in_active_hours(HOURLY, at(5, "10:00"))  # Saturday


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Events:
    def __init__(self):
        self.items = []

    def emit(self, type_, **data):
        self.items.append({"type": type_, **data})


def make_scheduler(store, clock, events, started, flags, jitter_minutes=5, seed=7):
    return Scheduler(
        store=store,
        events=events,
        start_run=lambda agent, catch_up: started.append((agent.id, clock.now, catch_up)),
        has_scheduled_run=lambda agent_id: flags["pending"],
        can_run=lambda: flags["model"],
        jitter_minutes=jitter_minutes,
        now=clock,
        rng=random.Random(seed),
    )


@pytest.fixture
def sched(tmp_path):
    store = Store(tmp_path)
    clock, events, started = Clock(at(0, "06:00")), Events(), []
    flags = {"pending": False, "model": True}
    scheduler = make_scheduler(store, clock, events, started, flags)
    agent = store.save_agent(Manifest(name="PO Bot", task="Check the folder.", schedule=Schedule(**vars(HOURLY))))
    scheduler.tick()
    j = timedelta(seconds=store.schedule_states()[agent.id]["jitter_seconds"])
    yield scheduler, store, clock, events, started, flags, agent, j
    store.close()


def test_each_agent_draws_and_keeps_its_own_jitter(tmp_path):
    store, clock, events, started = Store(tmp_path), Clock(at(0, "06:00")), Events(), []
    flags = {"pending": False, "model": True}
    scheduler = make_scheduler(store, clock, events, started, flags, seed=1)
    for i in range(40):
        store.save_agent(Manifest(name=f"Agent {i}", task="t", schedule=Schedule(**vars(HOURLY))))
    scheduler.tick()
    jitters = {a: s["jitter_seconds"] for a, s in store.schedule_states().items()}
    assert all(-300 <= j <= 300 for j in jitters.values()) and len(set(jitters.values())) > 30  # spread out
    scheduler.tick()
    assert {a: s["jitter_seconds"] for a, s in store.schedule_states().items()} == jitters  # stable

    narrower = make_scheduler(store, clock, events, started, flags, jitter_minutes=1, seed=2)
    narrower.tick()  # the setting shrank: offsets beyond ±60 s are redrawn, the rest kept
    after = {a: s["jitter_seconds"] for a, s in store.schedule_states().items()}
    assert all(-60 <= j <= 60 for j in after.values())
    assert all(after[a] == j for a, j in jitters.items() if abs(j) <= 60)

    none = make_scheduler(store, clock, events, started, flags, jitter_minutes=0)
    none.tick()
    assert {s["jitter_seconds"] for s in store.schedule_states().values()} == {0}
    store.close()


def next_at(store, agent):
    return store.schedule_states()[agent.id]["next_run_at"]


def test_runs_on_time_once_per_slot(sched):
    scheduler, store, clock, events, started, flags, agent, j = sched
    assert next_at(store, agent) == (at(0, "07:00") + j).isoformat() and started == []
    assert events.items[-1] == {"type": "agent.schedule", "agent_id": agent.id, "next_run_at": next_at(store, agent)}
    assert scheduler.next_run(agent) == next_at(store, agent)  # what the UI shows

    clock.now = at(0, "07:00") + j + timedelta(seconds=20)
    scheduler.tick()
    scheduler.tick()  # same slot: no second run
    assert [s[2] for s in started] == [False]  # on time, not a catch-up
    assert next_at(store, agent) == (at(0, "08:00") + j).isoformat()


def test_slot_skipped_while_previous_scheduled_run_is_still_going(sched):
    scheduler, store, clock, _, started, flags, agent, j = sched
    scheduler.tick()
    flags["pending"] = True
    clock.now = at(0, "07:00") + j + timedelta(seconds=5)
    scheduler.tick()
    assert started == [] and next_at(store, agent) == (at(0, "08:00") + j).isoformat()


def test_missed_slots_catch_up_once_within_active_hours(sched):
    scheduler, store, clock, _, started, _, agent, j = sched
    scheduler.tick()
    clock.now = at(0, "12:30")  # asleep (or app closed) since before 07:00: five slots missed
    scheduler.tick()
    scheduler.tick()
    assert started == [(agent.id, at(0, "12:30"), True)]  # a catch-up
    assert next_at(store, agent) == (at(0, "13:00") + j).isoformat()


def test_missed_slots_outside_active_hours_or_without_catch_up_dont_run(sched):
    scheduler, store, clock, _, started, _, agent, j = sched
    scheduler.tick()
    clock.now = at(0, "19:00")  # back after hours
    scheduler.tick()
    assert started == [] and next_at(store, agent) == (at(1, "07:00") + j).isoformat()

    agent.schedule.run_on_startup_if_missed = False
    store.save_agent(agent)
    clock.now = at(1, "12:30")
    scheduler.tick()
    assert started == []


def test_edits_disabling_no_model_and_deleted_agents(sched):
    scheduler, store, clock, events, started, flags, agent, j = sched
    scheduler.tick()
    agent.schedule.every_minutes = 30
    store.save_agent(agent)
    scheduler.tick()  # recomputed from now for the new settings, without running
    assert next_at(store, agent) == next_run_after(agent.schedule, int(j.total_seconds()), clock.now).isoformat()

    flags["model"] = False
    clock.now = datetime.fromisoformat(next_at(store, agent)) + timedelta(seconds=5)
    scheduler.tick()
    assert started == []  # no model: the slot passes

    agent.schedule.enabled = False
    store.save_agent(agent)
    scheduler.tick()
    assert agent.id not in store.schedule_states() and scheduler.next_run(agent) is None
    assert events.items[-1]["next_run_at"] is None

    store.set_schedule_state("ghost", "{}", None)
    scheduler.tick()
    assert "ghost" not in store.schedule_states()


def test_closed_for_three_days_runs_once(sched):
    scheduler, store, clock, _, started, _, agent, j = sched
    clock.now = at(3, "10:30")  # an hourly agent; the app was closed from Monday 06:00 until Thursday 10:30
    for _ in range(5):
        scheduler.tick()
        clock.now += timedelta(seconds=30)
    assert started == [(agent.id, at(3, "10:30"), True)]  # once, not ~30 times
    assert next_at(store, agent) == (at(3, "11:00") + j).isoformat()  # then back on its normal times


def test_closed_for_three_days_and_opened_after_hours_waits_for_the_next_time(sched):
    scheduler, store, clock, _, started, _, agent, j = sched
    clock.now = at(3, "20:00")
    scheduler.tick()
    assert started == [] and next_at(store, agent) == (at(4, "07:00") + j).isoformat()
