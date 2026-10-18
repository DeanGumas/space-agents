"""The scheduler (PLAN.md §5): runs agents on their schedule while the app is open and the computer is awake.

When an agent runs. Its schedule defines scheduled times on its active days, inside its active hours:

    start, start + every, start + 2·every, …   (strictly before the end of active hours)

Each agent actually runs at those times plus its own **jitter**: a random offset of up to ±`schedule_jitter_minutes`
(default 5). Without it, hundreds of users' hourly agents would all hit the shared AI server at precisely :00. The
offset is drawn once per agent and saved (`schedule_state.jitter_seconds`), so each agent's times stay predictable
("Next run 10:03", then 11:03, …) while differing between users. It's random rather than derived from the agent id,
because agents made from the same template share an id on every machine. It's redrawn only if the setting shrinks
below it. With jitter, a run can start a few minutes before active hours begin or after they end. All times are local
wall-clock time.

How. Every 30 s, `tick()` compares the wall clock with each agent's stored `next_run_at`, so sleep, wake, and
restarts are handled the same way:
- **On time** (due within the last few minutes): queue a scheduled run through the agent's work queue.
- **Missed** (the computer slept or the app was closed): run once, not once per missed slot. Only one upcoming time
  is ever stored per agent, so an hourly agent left closed for three days still runs once. Only if the agent has
  "Catch up after a missed run" on and it's within its active hours now; otherwise it simply waits for its next
  time. Catch-up runs go one at a time across agents (the work queue handles that).
- **Still busy:** if the agent's previous scheduled run is still queued or running, the slot is skipped, so a slow
  agent can't pile up runs.
- **Afterwards:** next_run_at moves to the next slot after now.
- **Edited schedules:** editing a schedule recomputes next_run_at from now (keyed by the schedule's settings).

A global cap on concurrent runs is enforced by the work queue (`Dispatcher`), not here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Any

from space_agents.core import EventBus
from space_agents.manifest import WEEKDAYS, Manifest, Schedule
from space_agents.store import Store

log = logging.getLogger(__name__)

TICK_SECONDS = 30
FIRST_TICK_DELAY = 15  # after launch, so the window (and the sandbox it hosts) is up first
LATE_GRACE = timedelta(minutes=3)  # due longer ago than this means the slot was missed (sleep, app closed)
LOOKAHEAD_DAYS = 8


def next_run_after(schedule: Schedule, jitter_seconds: int, after: datetime) -> datetime | None:
    """The first run (scheduled time + jitter) strictly after `after` (naive local time); None if there are none."""
    if not schedule.weekdays:
        return None
    start, end = _clock(schedule.active_hours.start), _clock(schedule.active_hours.end)
    offset = timedelta(seconds=jitter_seconds)
    every = timedelta(minutes=schedule.every_minutes)
    for day in range(-1, LOOKAHEAD_DAYS):  # from yesterday: its last run, jittered, may fall after midnight
        date = (after + timedelta(days=day)).date()
        if WEEKDAYS[date.weekday()] not in schedule.weekdays:
            continue
        # Scheduled times run from start, strictly before end; each actual run is that time + the jitter.
        slot, stop = datetime.combine(date, start) + offset, datetime.combine(date, end) + offset
        if slot <= after:  # skip ahead to the first run after `after`
            slot += every * ((after - slot) // every + 1)
        if slot < stop:
            return slot
    return None


def in_active_hours(schedule: Schedule, when: datetime) -> bool:
    if WEEKDAYS[when.weekday()] not in schedule.weekdays:
        return False
    return _clock(schedule.active_hours.start) <= when.time() < _clock(schedule.active_hours.end)


def schedule_key(schedule: Schedule) -> str:
    """Identifies the settings next_run_at was computed for; a change means it's recomputed."""
    return json.dumps(
        {
            "every": schedule.every_minutes,
            "hours": [schedule.active_hours.start, schedule.active_hours.end],
            "days": schedule.weekdays,
        },
        sort_keys=True,
    )


def _clock(hhmm: str) -> time:
    hours, minutes = hhmm.split(":")
    return time(int(hours), int(minutes))


def _iso(when: datetime | None) -> str | None:
    return when.isoformat(timespec="seconds") if when else None


class Scheduler:
    def __init__(
        self,
        *,
        store: Store,
        events: EventBus,
        start_run: Callable[[Manifest, bool], Any],
        has_scheduled_run: Callable[[str], bool],
        can_run: Callable[[], bool],
        jitter_minutes: int = 5,
        now: Callable[[], datetime] = datetime.now,
        rng: random.Random | None = None,
    ):
        self._store = store
        self._events = events
        self._start_run = start_run  # queue a scheduled run: (agent, catch_up) → Dispatcher.run(agent, "schedule", …)
        self._has_scheduled_run = has_scheduled_run  # is a scheduled run for this agent queued or running?
        self._can_run = can_run  # is a model configured?
        self._jitter = max(0, jitter_minutes) * 60  # ± seconds (setting `schedule_jitter_minutes`)
        self._now = now  # naive local time; injectable for tests
        self._rng = rng or random.SystemRandom()

    async def run_forever(self) -> None:
        await asyncio.sleep(FIRST_TICK_DELAY)
        while True:
            try:
                self.tick()
            except Exception:  # one bad tick must not stop scheduling
                log.exception("Scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    def next_run(self, agent: Manifest) -> str | None:
        """When the agent will next run on its schedule (ISO, local time), for the UI. None if it isn't scheduled."""
        if not (agent.schedule.enabled and agent.task.strip()):
            return None
        state = self._store.schedule_states().get(agent.id)
        if state and state["schedule_key"] == schedule_key(agent.schedule) and state["next_run_at"]:
            return state["next_run_at"]
        return _iso(next_run_after(agent.schedule, self._jitter_for(agent.id, state), self._now()))

    def _jitter_for(self, agent_id: str, state: dict[str, Any] | None) -> int:
        """The agent's saved jitter; drawn (and saved) the first time, or again if the setting shrank below it."""
        saved = state.get("jitter_seconds") if state else None
        if saved is not None and abs(saved) <= self._jitter:
            return saved
        drawn = self._rng.randint(-self._jitter, self._jitter)
        if state:
            self._store.set_schedule_state(agent_id, state["schedule_key"], state["next_run_at"], jitter_seconds=drawn)
        else:
            self._store.set_schedule_state(agent_id, "", None, jitter_seconds=drawn)  # tick() fills in the rest
        return drawn

    def tick(self) -> None:
        now = self._now()
        states = self._store.schedule_states()
        agents = self._store.list_agents()
        for agent_id in states.keys() - {a.id for a in agents}:
            self._store.clear_schedule_state(agent_id)
        for agent in agents:
            self._tick_agent(agent, states.get(agent.id), now)

    def _tick_agent(self, agent: Manifest, state: dict[str, Any] | None, now: datetime) -> None:
        schedule = agent.schedule
        if not (schedule.enabled and agent.task.strip()):
            if state:
                self._store.clear_schedule_state(agent.id)
                self._emit(agent.id, None)
            return
        key = schedule_key(schedule)
        jitter = self._jitter_for(agent.id, state)
        if state is None or state["schedule_key"] != key or not state["next_run_at"]:
            self._set_next(agent, key, next_run_after(schedule, jitter, now))
            return
        due = datetime.fromisoformat(state["next_run_at"])
        if now < due:
            return
        missed = now - due > LATE_GRACE
        upcoming = next_run_after(schedule, jitter, now)
        if missed and not (schedule.run_on_startup_if_missed and in_active_hours(schedule, now)):
            log.info("Skipping missed run of %s (due %s)", agent.id, state["next_run_at"])
            self._set_next(agent, key, upcoming)
            return
        if self._has_scheduled_run(agent.id):
            log.info("Skipping a scheduled run of %s: its previous one is still going", agent.id)
        elif not self._can_run():
            log.info("Skipping a scheduled run of %s: no model is configured", agent.id)
        else:
            log.info("Scheduled run of %s (%s)", agent.id, "catch-up" if missed else "on time")
            self._start_run(agent, missed)
            self._set_next(agent, key, upcoming, last_run_at=now)
            return
        self._set_next(agent, key, upcoming)

    def _set_next(self, agent: Manifest, key: str, upcoming: datetime | None, last_run_at: datetime | None = None):
        self._store.set_schedule_state(agent.id, key, _iso(upcoming), _iso(last_run_at))
        self._emit(agent.id, _iso(upcoming))

    def _emit(self, agent_id: str, next_run_at: str | None) -> None:
        self._events.emit("agent.schedule", agent_id=agent_id, next_run_at=next_run_at)
