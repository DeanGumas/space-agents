"""Agent loop end-to-end with a scripted model and a fake sandbox (no network, no webview)."""

import json
import shutil
from pathlib import Path

import pytest

from space_agents.changes import ChangeManager
from space_agents.config import load_config
from space_agents.documents.library import DocumentLibrary
from space_agents.llm.client import ChatResult, LLMError, ToolCall
from space_agents.manifest import FileChanges, FolderGrant, Manifest
from space_agents.runtime.loop import AgentRunner
from space_agents.sandbox.pyodide import RunResult
from space_agents.store import Store
from space_agents.tools.registry import XLSX_DIFF


class Events:
    def __init__(self):
        self.items = []

    def emit(self, type_, **data):
        self.items.append({"type": type_, **data})

    def of(self, type_):
        return [e for e in self.items if e["type"] == type_]


class ScriptedClient:
    """Plays back a list of ChatResults; records the messages it was sent."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    async def chat(self, *, model, messages, tools=None, on_text=None, **kw):
        self.requests.append({"model": model, "messages": json.loads(json.dumps(messages)), "tools": tools, **kw})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if on_text and item.content:
            await on_text(item.content)
        return item


class FakeSandbox:
    """Pretends to run code: copies inputs to outputs per `writes` = {output name: content or input name}."""

    def __init__(self, tmp: Path, writes=None):
        self.tmp, self.writes, self.runs = tmp, writes or {}, []

    async def run(self, code, inputs, timeout=90, aliases=None):
        self.runs.append((code, dict(inputs)))
        self.aliases = aliases or {}
        if code == XLSX_DIFF:
            diff = {"sheets": [{"name": "S", "rows": 3, "old_rows": 2, "changed_cells": 1}], "samples": []}
            return RunResult(ok=True, stdout=json.dumps(diff))
        out = self.tmp / f"out{len(self.runs)}"
        out.mkdir()
        for name, content in self.writes.items():
            if isinstance(content, Path):
                (out / name).write_bytes(content.read_bytes())
            else:
                (out / name).write_text(content)
        return RunResult(ok=True, stdout="done\n", outputs=sorted(out.iterdir()), duration_s=0.1)

    def release(self, result):
        pass


@pytest.fixture
def env(tmp_path, sample_docs):
    work = tmp_path / "work"
    shutil.copytree(sample_docs, work)
    data = tmp_path / "data"
    store = Store(data)
    events = Events()
    docs = DocumentLibrary(store)
    changes = ChangeManager(store, data, events)
    config = load_config(environ={}, dotenv_path=False, cli={"data_dir": data})
    agent = Manifest(name="PO Bot", id="po-bot", filesystem=[FolderGrant(str(work), "read_write")])
    store.save_agent(agent)
    yield store, events, docs, changes, config, agent, work
    docs.close()
    store.close()


def make_runner(env, client, sandbox):
    store, events, docs, changes, config, *_ = env
    return AgentRunner(
        store=store,
        events=events,
        docs=docs,
        sandbox=sandbox,
        changes=changes,
        config=config,
        client_factory=lambda c: client,
    )


def call(name, **args):
    return ToolCall(id=f"call_{name}", name=name, arguments=json.dumps(args))


async def ask(env, runner, text):
    store, *_, agent, _ = env
    store.add_message(agent.id, "user", text)
    reply = store.add_message(agent.id, "agent", "", status="streaming")
    agent.model.name = "test-model"
    await runner.chat(agent, reply["id"])
    return store.get_message(reply["id"])


async def test_reads_a_document_then_answers(env, tmp_path):
    store, events, *_ = env
    client = ScriptedClient(
        [
            ChatResult(tool_calls=[call("read_document", path="Acme Supplier Update.pdf")], usage={"total_tokens": 7}),
            ChatResult(content="PO 45821 now arrives **Oct 14**.", usage={"total_tokens": 3}),
        ]
    )
    msg = await ask(env, make_runner(env, client, FakeSandbox(tmp_path)), "Any Acme delays?")
    assert msg["status"] == "done" and "Oct 14" in msg["content"]
    assert json.loads(msg["usage"])["total_tokens"] == 10
    tool_msg = client.requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "October 14, 2026" in tool_msg["content"]
    system = client.requests[0]["messages"][0]["content"]
    assert "PO Bot" in system and "read & write" in system
    assert [e["call"]["status"] for e in events.of("tool.done")] == ["done"]
    assert events.of("tool.done")[0]["call"]["summary"] == "Read Acme Supplier Update.pdf"
    assert [e["status"] for e in events.of("agent.status")] == ["responding", "idle"]


async def test_run_python_output_becomes_pending_change(env, tmp_path):
    *_, work = env
    original = (work / "PO Tracker.xlsx").read_bytes()
    sandbox = FakeSandbox(tmp_path, {"PO Tracker.xlsx": "new workbook bytes", "summary.csv": "a,b\n"})
    client = ScriptedClient(
        [
            ChatResult(
                tool_calls=[
                    call(
                        "run_python",
                        description="Mark PO 45821 delayed",
                        code="...",
                        input_files=["PO Tracker.xlsx"],
                    )
                ]
            ),
            ChatResult(content="I've proposed the update for your review."),
        ]
    )
    runner = make_runner(env, client, sandbox)
    msg = await ask(env, runner, "Update the tracker")
    assert msg["status"] == "done"
    assert sandbox.runs[0][1] == {"PO Tracker.xlsx": work / "PO Tracker.xlsx"}
    changes = env[3].for_messages([msg["id"]])[msg["id"]]
    by_name = {c["name"]: c for c in changes}
    assert by_name["PO Tracker.xlsx"]["status"] == "pending"  # existing file: waits for review
    assert by_name["PO Tracker.xlsx"]["summary"]["xlsx"]["sheets"][0]["changed_cells"] == 1
    assert by_name["summary.csv"]["status"] == "applied"  # new file: saved by default
    assert (work / "PO Tracker.xlsx").read_bytes() == original
    result = client.requests[1]["messages"][-1]["content"]
    assert "change card" in result and "Created" in result


async def test_access_errors_are_returned_to_the_model(env, tmp_path):
    client = ScriptedClient(
        [
            ChatResult(tool_calls=[call("read_document", path="/etc/hosts")]),
            ChatResult(content="I can't access that file."),
        ]
    )
    await ask(env, make_runner(env, client, FakeSandbox(tmp_path)), "read /etc/hosts")
    assert "outside this agent's folders" in client.requests[1]["messages"][-1]["content"]


async def test_read_only_folder_blocks_outputs(env, tmp_path):
    *_, agent, work = env
    agent.filesystem = [FolderGrant(str(work), "read")]
    agent.file_changes = FileChanges()
    sandbox = FakeSandbox(tmp_path, {"PO Tracker.xlsx": "x"})
    client = ScriptedClient(
        [
            ChatResult(tool_calls=[call("run_python", description="d", code="c", input_files=["PO Tracker.xlsx"])]),
            ChatResult(content="Couldn't save."),
        ]
    )
    await ask(env, make_runner(env, client, sandbox), "update")
    assert "read-only for this agent" in client.requests[1]["messages"][-1]["content"]


async def test_llm_errors_end_the_message_gracefully(env, tmp_path):
    client = ScriptedClient([LLMError("The model server rejected the API key.", 401)])
    msg = await ask(env, make_runner(env, client, FakeSandbox(tmp_path)), "hi")
    assert msg["status"] == "error" and "rejected the API key" in msg["content"]


async def test_tool_budget_forces_final_answer(env, tmp_path):
    *_, agent, _ = env
    agent.limits.max_tool_calls = 1
    client = ScriptedClient(
        [
            ChatResult(tool_calls=[call("list_files")]),
            ChatResult(content="Here's what I found."),
        ]
    )
    await ask(env, make_runner(env, client, FakeSandbox(tmp_path)), "look around")
    assert client.requests[1]["tools"] is None
    assert "Tool limit reached" in client.requests[1]["messages"][-1]["content"]


async def test_host_paths_in_code_become_inputs(env, tmp_path):
    *_, work = env
    sandbox = FakeSandbox(tmp_path)
    host_path = str(work / "PO Tracker.xlsx")
    client = ScriptedClient(
        [
            ChatResult(
                tool_calls=[
                    call(
                        "run_python",
                        description="d",
                        code=f"import openpyxl\nwb = openpyxl.load_workbook({host_path!r})",
                    )
                ]
            ),
            ChatResult(content="ok"),
        ]
    )
    await ask(env, make_runner(env, client, sandbox), "inspect")
    assert sandbox.runs[0][1] == {"PO Tracker.xlsx": work / "PO Tracker.xlsx"}
    assert sandbox.aliases == {host_path: "PO Tracker.xlsx"}


async def test_identical_output_is_a_no_op(env, tmp_path):
    *_, work = env
    sandbox = FakeSandbox(tmp_path, {"Kickoff Meeting Notes.docx": work / "Kickoff Meeting Notes.docx"})
    client = ScriptedClient(
        [
            ChatResult(
                tool_calls=[
                    call("run_python", description="d", code="pass", input_files=["Kickoff Meeting Notes.docx"])
                ]
            ),
            ChatResult(content="ok"),
        ]
    )
    msg = await ask(env, make_runner(env, client, sandbox), "touch it")
    assert "no changes" in client.requests[1]["messages"][-1]["content"]
    assert env[3].for_messages([msg["id"]]) == {}


async def test_in_and_out_literals_become_inputs(env, tmp_path):
    sandbox = FakeSandbox(tmp_path)
    code = "import openpyxl\nwb = openpyxl.load_workbook('in/PO Tracker.xlsx')\nwb.save('out/PO Tracker.xlsx')"
    client = ScriptedClient(
        [ChatResult(tool_calls=[call("run_python", description="d", code=code)]), ChatResult(content="ok")]
    )
    await ask(env, make_runner(env, client, sandbox), "edit")
    assert list(sandbox.runs[0][1]) == ["PO Tracker.xlsx"]
    assert sandbox.aliases == {"out/PO Tracker.xlsx": "PO Tracker.xlsx"}


async def test_revisions_supersede_the_pending_change_and_build_on_it(env, tmp_path):
    *_, work = env
    sandbox = FakeSandbox(tmp_path, {"notes.csv": "v1"})
    (work / "notes.csv").write_text("v0")
    edit = call("run_python", description="edit", code="...", input_files=["notes.csv"])
    client = ScriptedClient([ChatResult(tool_calls=[edit]), ChatResult(tool_calls=[edit]), ChatResult(content="done")])
    runner = make_runner(env, client, sandbox)
    store, *_, agent, _ = env
    store.add_message(agent.id, "user", "edit twice")
    reply = store.add_message(agent.id, "agent", "", status="streaming")
    agent.model.name = "m"

    original_run = sandbox.run

    async def second_writes_v2(code, inputs, timeout=90, aliases=None):
        if len(sandbox.runs) == 1:
            sandbox.writes = {"notes.csv": "v2"}
            assert inputs["notes.csv"].read_text() == "v1"  # builds on its own pending proposal, not the original
        return await original_run(code, inputs, timeout, aliases)

    sandbox.run = second_writes_v2
    await runner.chat(agent, reply["id"])
    changes = env[3].for_messages([reply["id"]])[reply["id"]]
    assert len(changes) == 1 and changes[0]["status"] == "pending"
    assert (work / "notes.csv").read_text() == "v0"
    env[3].apply(changes[0]["id"])
    assert (work / "notes.csv").read_text() == "v2"


async def test_history_tells_the_model_what_happened_to_its_changes(env, tmp_path):
    store, events, docs, changes, config, agent, work = env
    sandbox = FakeSandbox(tmp_path, {"PO Tracker.xlsx": "edited"})
    edit = call("run_python", description="d", code="c", input_files=["PO Tracker.xlsx"])
    client = ScriptedClient(
        [ChatResult(tool_calls=[edit]), ChatResult(content="Proposed."), ChatResult(content="Revised plan.")]
    )
    runner = make_runner(env, client, sandbox)
    first = await ask(env, runner, "update it")
    change = changes.for_messages([first["id"]])[first["id"]][0]
    changes.discard(change["id"])
    await ask(env, runner, "I discarded your proposed change. Use Late instead.")
    history = client.requests[-1]["messages"]
    earlier_reply = next(m for m in history if m["role"] == "assistant" and "Proposed." in m["content"])
    assert "[File changes from this reply]" in earlier_reply["content"]
    assert "PO Tracker.xlsx: discarded by the user" in earlier_reply["content"]
