"""The per-agent work queue: chat replies and runs take turns per agent, run in parallel across agents, and stop."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from space_agents.changes import ChangeManager
from space_agents.core import Core
from space_agents.manifest import Manifest
from space_agents.runtime.dispatch import Dispatcher
from space_agents.store import Store


class Events:
    def __init__(self):
        self.items = []

    def emit(self, type_, **data):
        self.items.append({"type": type_, **data})


class GatedRunner:
    """Stands in for AgentRunner: each job waits until the test releases it."""

    def __init__(self):
        self.config = SimpleNamespace(llm_ready=True)
        self.started: list[tuple[str, int]] = []
        self.gates: dict[tuple[str, int], asyncio.Event] = {}

    async def chat(self, agent, message_id):
        await self._work(("chat", message_id))

    async def run(self, agent, run):
        assert run["status"] == "running"  # marked by the dispatcher when the job starts
        await self._work(("run", run["message_id"]))

    async def _work(self, key):
        self.gates[key] = asyncio.Event()
        self.started.append(key)
        await self.gates[key].wait()


def wait_until(condition, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline and not condition():
        time.sleep(0.01)
    assert condition(), "timed out"


@pytest.fixture
def setup(tmp_path):
    store, core, events, runner = Store(tmp_path), Core(), Events(), GatedRunner()
    core.start()
    finished = []
    dispatcher = Dispatcher(
        store=store,
        core=core,
        events=events,
        runner=runner,
        changes=ChangeManager(store, tmp_path, events),
        on_finished=lambda agent_id, message_id: finished.append(message_id),
    )
    for name in ("Alpha", "Beta"):
        store.save_agent(Manifest(name=name, task="Check the folder."))

    def release(key):
        wait_until(lambda: key in runner.gates)
        core.loop.call_soon_threadsafe(runner.gates[key].set)

    yield SimpleNamespace(
        store=store, events=events, runner=runner, dispatch=dispatcher, finished=finished, release=release
    )
    core.stop()
    store.close()


def test_work_for_one_agent_takes_turns_in_order(setup):
    s = setup
    alpha = s.store.get_agent("alpha")
    first = s.dispatch.chat(alpha, "hi")["agent_message"]["id"]
    run = s.dispatch.run(alpha)["id"]
    second = s.dispatch.chat(alpha, "and then?")["agent_message"]["id"]
    wait_until(lambda: s.runner.started == [("chat", first)])
    assert s.dispatch.status("alpha") == "responding"
    assert s.store.get_message(run)["status"] == "queued" and s.store.get_message(second)["status"] == "queued"

    s.release(("chat", first))
    wait_until(lambda: s.runner.started[-1] == ("run", run))
    assert s.dispatch.status("alpha") == "running"
    s.release(("run", run))
    s.release(("chat", second))
    wait_until(lambda: not s.dispatch.busy("alpha"))
    assert s.runner.started == [("chat", first), ("run", run), ("chat", second)]
    assert s.finished == [first, run, second]
    started = [e["message"]["id"] for e in s.events.items if e["type"] == "message.started"]
    assert started == [first, run, second]


def test_different_agents_work_in_parallel(setup):
    s = setup
    a = s.dispatch.chat(s.store.get_agent("alpha"), "hi")["agent_message"]["id"]
    b = s.dispatch.run(s.store.get_agent("beta"))["id"]
    wait_until(lambda: set(s.runner.started) == {("chat", a), ("run", b)})
    s.release(("chat", a))
    s.release(("run", b))
    wait_until(lambda: not s.dispatch.busy("alpha") and not s.dispatch.busy("beta"))


def test_stop_cancels_current_and_queued_work(setup):
    s = setup
    alpha = s.store.get_agent("alpha")
    current = s.dispatch.chat(alpha, "hi")["agent_message"]["id"]
    queued_run = s.dispatch.run(alpha)
    wait_until(lambda: s.runner.started == [("chat", current)])
    assert s.dispatch.stop("alpha") is True
    wait_until(lambda: not s.dispatch.busy("alpha"))
    assert s.runner.started == [("chat", current)]  # the queued run never started
    assert s.store.get_message(queued_run["id"])["content"] == "_Stopped before it started._"
    assert s.store.get_run(queued_run["run"]["id"])["status"] == "stopped"
    assert s.finished == [current]  # notifications only for work that actually ran
    assert s.dispatch.status("alpha") == "idle" and s.dispatch.stop("alpha") is False


def test_runs_share_a_global_cap_but_chats_dont(tmp_path):
    store, core, events, runner = Store(tmp_path), Core(), Events(), GatedRunner()
    core.start()
    dispatch = Dispatcher(
        store=store,
        core=core,
        events=events,
        runner=runner,
        changes=ChangeManager(store, tmp_path, events),
        max_concurrent_runs=1,
    )
    for name in ("Alpha", "Beta", "Gamma"):
        store.save_agent(Manifest(name=name, task="Check the folder."))
    try:
        a = dispatch.run(store.get_agent("alpha"), "schedule")["id"]
        b = dispatch.run(store.get_agent("beta"), "schedule")["id"]
        c = dispatch.chat(store.get_agent("gamma"), "hi")["agent_message"]["id"]
        wait_until(lambda: set(runner.started) == {("run", a), ("chat", c)})
        assert store.get_message(b)["status"] == "queued"  # waiting for a run slot
        assert dispatch.has_run("beta", "schedule") and not dispatch.has_run("beta", "manual")
        wait_until(lambda: ("run", a) in runner.gates)
        core.loop.call_soon_threadsafe(runner.gates[("run", a)].set)
        wait_until(lambda: ("run", b) in runner.started)
        for key in (("run", b), ("chat", c)):
            core.loop.call_soon_threadsafe(runner.gates[key].set)
        wait_until(lambda: not any(dispatch.busy(x) for x in ("alpha", "beta", "gamma")))
    finally:
        core.stop()
        store.close()


def test_catch_up_runs_go_one_at_a_time(setup):
    s = setup  # max_concurrent_runs is 2, but catch-ups (missed scheduled runs) take turns across agents
    a = s.dispatch.run(s.store.get_agent("alpha"), "schedule", catch_up=True)["id"]
    b = s.dispatch.run(s.store.get_agent("beta"), "schedule", catch_up=True)["id"]
    wait_until(lambda: len(s.runner.started) == 1)
    first, second = (a, b) if s.runner.started[0] == ("run", a) else (b, a)
    assert s.store.get_message(second)["status"] == "queued"
    s.release(("run", first))
    wait_until(lambda: ("run", second) in s.runner.started)
    s.release(("run", second))
    wait_until(lambda: not s.dispatch.busy("alpha") and not s.dispatch.busy("beta"))
