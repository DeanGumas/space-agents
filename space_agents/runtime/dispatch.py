"""Starting agent work: one queue per agent, so its chat replies and runs never overlap (PLAN.md §3.3).

    chat(agent, text)    the user's message + a queued reply      ──┐
    run(agent, trigger)  a run of the agent's task + its report   ──┼─► per-agent queue (FIFO) ─► AgentRunner
    (later: room turns, scheduled runs)                           ──┘

A message sent while the agent is busy isn't refused: its reply waits as 'queued' ("Waiting for … to finish") and
starts when the agent is free. When a job starts, its message becomes 'streaming' (and its run 'running'), and
`message.started` tells the UI. Stop cancels everything the agent is doing or waiting to do. The agent's manifest
is read when a job starts, so edits made while it waited apply.

Different agents work in parallel. Runs (scheduled or Run now, not chat replies) also share a global cap
(`max_concurrent_runs`, default 2) to protect the shared AI server. Catch-up runs (scheduled runs missed while the
app was closed or the computer slept) go one at a time across all agents: after a long absence every scheduled agent
catches up at once, and running them serially spares the user's machine and the server. A run waiting for its turn
stays 'queued' and holds its agent's place in line.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from space_agents.changes import ChangeManager
from space_agents.core import Core, EventBus
from space_agents.manifest import Manifest
from space_agents.runtime.dummy import dummy_reply
from space_agents.runtime.loop import AgentRunner
from space_agents.store import Store, now_iso

log = logging.getLogger(__name__)

STATUS_FOR_KIND = {"chat": "responding", "run": "running"}


@dataclass
class Job:
    agent_id: str
    kind: str  # "chat" | "run"
    message_id: int
    run_id: int | None = None
    trigger: str | None = None  # runs: "manual" | "schedule"
    catch_up: bool = False  # a scheduled run making up for missed ones: these go one at a time
    revising: bool = False  # a reply to "Discard & tell agent": the agent stays in Activity until it's done
    # State below changes only on the core loop, so a job is never started and stopped at the same time.
    task: asyncio.Task | None = None
    started: bool = False
    cancelled: bool = False  # stopped before it started
    finished: bool = False


class Dispatcher:
    def __init__(
        self,
        *,
        store: Store,
        core: Core,
        events: EventBus,
        runner: AgentRunner,
        changes: ChangeManager,
        on_finished: Callable[[str, int], None] | None = None,
        max_concurrent_runs: int = 2,
    ):
        self._store = store
        self._core = core
        self._events = events
        self._runner = runner
        self._changes = changes
        self.on_finished = on_finished  # (agent_id, message_id) after any job ends; the bridge notifies from here
        self._jobs: dict[str, list[Job]] = {}  # per agent: the running job (started) and the queued ones, in order
        self._mutex = threading.Lock()  # _jobs is touched from bridge threads and the core loop
        self._locks: dict[str, asyncio.Lock] = {}  # per agent; only used on the core loop
        self._run_slots = asyncio.Semaphore(max_concurrent_runs)  # bind to the core loop on first use
        self._catch_up_turn = asyncio.Semaphore(1)

    # --- state ------------------------------------------------------------------------------------------------

    def status(self, agent_id: str) -> str:
        """idle, responding (chat), or running (a run)."""
        with self._mutex:
            current = next((j for j in self._jobs.get(agent_id, []) if j.started), None)
        return STATUS_FOR_KIND[current.kind] if current else "idle"

    def busy(self, agent_id: str) -> bool:
        with self._mutex:
            return bool(self._jobs.get(agent_id))

    def has_run(self, agent_id: str, trigger: str) -> bool:
        """Is a run with this trigger queued or running for the agent? (The scheduler skips a slot if so.)"""
        with self._mutex:
            return any(j.kind == "run" and j.trigger == trigger for j in self._jobs.get(agent_id, []))

    # --- work -------------------------------------------------------------------------------------------------

    def chat(self, agent: Manifest, text: str, revising: bool = False) -> dict[str, Any]:
        """Store the user's message and queue the agent's reply. Returns both messages."""
        user_message = self._store.add_message(agent.id, "user", text)
        reply = self._store.add_message(agent.id, "agent", "", status="queued")
        if revising:
            self._changes.set_revising(agent.id, True)

        def work(current: Manifest) -> Awaitable[None]:
            if self._runner.config.llm_ready:
                return self._runner.chat(current, reply["id"])
            # No model configured: placeholder replies, so the UI can still be exercised.
            return dummy_reply(self._store, self._events, current, text, reply["id"])

        self._submit(Job(agent.id, "chat", reply["id"], revising=revising), work)
        reply.update(tool_calls=[], changes=[], run=None)
        return {"user_message": user_message, "agent_message": reply}

    def run(self, agent: Manifest, trigger: str = "manual", catch_up: bool = False) -> dict[str, Any]:
        """Queue a run of the agent's task. Returns its (queued) report message, with the run attached."""
        run = self._store.add_run(agent.id, trigger, agent.task)
        self._submit(
            Job(agent.id, "run", run["message_id"], run_id=run["id"], trigger=trigger, catch_up=catch_up),
            lambda current: self._runner.run(current, self._store.get_run(run["id"])),
        )
        message = self._store.attach_runs([self._store.get_message(run["message_id"])])[0]
        message.update(tool_calls=[], changes=[])
        return message

    def stop(self, agent_id: str) -> bool:
        """Cancel what the agent is doing and everything it's waiting to do. False if it had nothing to stop."""
        with self._mutex:
            jobs = list(self._jobs.get(agent_id, []))
        if jobs:
            self._core.loop.call_soon_threadsafe(self._cancel, jobs)
        return bool(jobs)

    # --- internals --------------------------------------------------------------------------------------------

    def _submit(self, job: Job, work: Callable[[Manifest], Awaitable[None]]) -> None:
        async def wrapper() -> None:
            job.task = asyncio.current_task()
            lock = self._locks.setdefault(job.agent_id, asyncio.Lock())
            try:
                # Order matters: a catch-up waits for its turn before taking one of the shared run slots.
                async with (
                    lock,
                    self._catch_up_turn if job.catch_up else contextlib.nullcontext(),
                    self._run_slots if job.kind == "run" else contextlib.nullcontext(),
                ):
                    if job.cancelled:
                        return
                    self._started(job)
                    await work(self._store.get_agent(job.agent_id))
            except asyncio.CancelledError:
                if not job.started:
                    self._stopped_before_start(job)
                raise
            except Exception:
                log.exception("Agent work failed")
            finally:
                self._finished(job)

        with self._mutex:
            self._jobs.setdefault(job.agent_id, []).append(job)
        self._core.submit(wrapper())

    def _cancel(self, jobs: list[Job]) -> None:
        """On the core loop: running jobs are cancelled (their runner records "Stopped"); queued ones end here."""
        for job in jobs:
            if not job.started:
                self._stopped_before_start(job)
                self._finished(job)
            if job.task:
                job.task.cancel()

    def _started(self, job: Job) -> None:
        job.started = True
        self._store.update_message(job.message_id, status="streaming")
        if job.run_id:
            self._store.update_run(job.run_id, status="running", started_at=now_iso())
        message = self._store.attach_runs([self._store.get_message(job.message_id)])[0]
        self._events.emit("message.started", agent_id=job.agent_id, message=message)

    def _stopped_before_start(self, job: Job) -> None:
        if job.cancelled:
            return
        job.cancelled = True
        self._store.update_message(job.message_id, content="_Stopped before it started._", status="error")
        if job.run_id:
            self._store.update_run(job.run_id, status="stopped", finished_at=now_iso())
        message = self._store.attach_runs([self._store.get_message(job.message_id)])[0]
        self._events.emit("message.done", agent_id=job.agent_id, message=message)

    def _finished(self, job: Job) -> None:
        if job.finished:
            return
        job.finished = True
        with self._mutex:
            jobs = [j for j in self._jobs.get(job.agent_id, []) if j is not job]
            if jobs:
                self._jobs[job.agent_id] = jobs
            else:
                self._jobs.pop(job.agent_id, None)
        try:
            if job.revising:
                self._changes.set_revising(job.agent_id, False)
            if self.on_finished and job.started:
                self.on_finished(job.agent_id, job.message_id)
        except Exception:  # a notification problem must never break the queue
            log.exception("After-work hook failed")
