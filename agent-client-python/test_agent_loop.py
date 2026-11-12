"""Tests for main.py with a fake model and a fake MCP server. No API key, Redis or Go binary needed.

They check the parts that cost real money or hide real faults when they break: writes are blocked on a
stale feed, crashes land in the ledger, the turn cap holds, and the process exit codes are honest.
"""
import asyncio
import importlib
import json
import sys
import types

import pytest


class Blk:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def tool_use(call_id, name, args):
    return Blk(type="tool_use", id=call_id, name=name, input=args)


def reply(content, stop):
    return Blk(content=content, stop_reason=stop, usage=Blk(input_tokens=10, output_tokens=5))


FIX = {"device_id": "car_012", "parameter": "power_parameter", "value": "eco_mode"}


class World:
    """Everything the fakes share: the scripted model replies and what the fake server saw."""
    def __init__(self):
        self.script = []                # list of replies, or callables (messages) -> reply
        self.feed_status = "ok"
        self.scan_text = None           # raw scan_fleet reply; None = a normal reply built from feed_status
        self.server_calls = []          # (tool, args) that reached the server
        self.sent_messages = []         # the last messages list given to the model
        self.fail_on_initialize = None  # exception to raise when the MCP session starts
        self.fail_on_stdio = None       # exception to raise when the stdio transport is opened
        self.dotenv_calls = []          # keyword arguments each load_dotenv() call received


@pytest.fixture
def agent(monkeypatch, tmp_path):
    world = World()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class Messages:
        def __init__(self):
            self.i = 0

        def create(self, **kw):
            world.sent_messages = list(kw["messages"])
            item = world.script[min(self.i, len(world.script) - 1)]
            self.i += 1
            if isinstance(item, BaseException):
                raise item
            return item(kw["messages"]) if callable(item) else item

    class Session:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            if world.fail_on_initialize:
                raise world.fail_on_initialize

        async def list_tools(self):
            return Blk(tools=[Blk(name=n, description=n, input_schema={"type": "object"})
                              for n in ("scan_fleet", "send_ota_command")])

        async def call_tool(self, name, arguments=None):
            world.server_calls.append((name, arguments))
            if name == "scan_fleet":
                text = world.scan_text or json.dumps({"feed_status": world.feed_status, "flagged": []})
            else:
                text = "Success: Over-The-Air command deployed to car_012. Register 'power_parameter' updated to 'eco_mode'."
            return Blk(content=[Blk(text=text)], is_error=False)

    class Stdio:
        def __init__(self, params):
            pass

        async def __aenter__(self):
            if world.fail_on_stdio:
                raise world.fail_on_stdio
            return (1, 2)

        async def __aexit__(self, *exc):
            return False

    fakes = {
        "dotenv": types.SimpleNamespace(load_dotenv=lambda *a, **k: world.dotenv_calls.append(k)),
        "anthropic": types.SimpleNamespace(Anthropic=lambda: Blk(messages=Messages())),
        "mcp": types.SimpleNamespace(ClientSession=Session, StdioServerParameters=lambda **k: None),
        "mcp.client": types.ModuleType("mcp.client"),
        "mcp.client.stdio": types.SimpleNamespace(stdio_client=Stdio),
    }
    for name, module in fakes.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "main", raising=False)
    main = importlib.import_module("main")
    main.SESSION.update(started=False, status="not_started")
    world.main = main
    yield world
    sys.modules.pop("main", None)


def ledger_events(tmp_path):
    events = []
    for path in sorted((tmp_path / "logs").glob("fleet_agent_ledger_*.jsonl")):
        events += [json.loads(line) for line in path.read_text().splitlines()]
    return events


def kinds(tmp_path):
    return [e["event_type"] for e in ledger_events(tmp_path)]


def finish(text="done"):
    return reply([Blk(type="text", text=text)], "end_turn")


# ---- stale-feed guard in the client -------------------------------------------------------

def test_write_is_allowed_after_a_healthy_scan(agent, tmp_path):
    agent.script = [reply([tool_use("1", "scan_fleet", {})], "tool_use"),
                    reply([tool_use("2", "send_ota_command", FIX)], "tool_use"), finish()]
    assert agent.main.main() == 0
    assert [c[0] for c in agent.server_calls] == ["scan_fleet", "send_ota_command"]
    assert kinds(tmp_path)[-1] == "SESSION_COMPLETED"


@pytest.mark.parametrize("status", ["stale", "empty"])
def test_write_never_reaches_the_server_after_a_bad_scan(agent, tmp_path, status):
    agent.feed_status = status
    agent.script = [reply([tool_use("1", "scan_fleet", {})], "tool_use"),
                    reply([tool_use("2", "send_ota_command", FIX)], "tool_use"), finish("STALE TELEMETRY")]
    assert agent.main.main() == 0
    assert [c[0] for c in agent.server_calls] == ["scan_fleet"]            # the write was stopped here
    assert "GUARDRAIL_VIOLATION" in kinds(tmp_path)
    result = agent.sent_messages[-1]["content"][0]                          # what the model was told
    assert result["is_error"] is True and "STALE TELEMETRY" in result["content"]


def test_a_later_healthy_scan_unblocks_writes(agent, tmp_path):
    agent.feed_status = "stale"
    first = reply([tool_use("1", "scan_fleet", {})], "tool_use")

    def second(messages):
        agent.feed_status = "ok"
        return reply([tool_use("2", "scan_fleet", {})], "tool_use")

    agent.script = [first, second, reply([tool_use("3", "send_ota_command", FIX)], "tool_use"), finish()]
    assert agent.main.main() == 0
    assert ("send_ota_command", FIX) in agent.server_calls


def test_write_before_any_scan_is_blocked_and_the_model_is_told_to_scan(agent, tmp_path):
    agent.script = [reply([tool_use("1", "send_ota_command", FIX)], "tool_use"), finish()]
    assert agent.main.main() == 0
    assert agent.server_calls == []
    result = agent.sent_messages[-1]["content"][0]
    assert result["is_error"] is True and "scan_fleet" in result["content"]


def test_write_in_the_same_turn_as_the_first_scan_is_blocked(agent, tmp_path):
    # tool calls of one turn run in parallel, so the write cannot rely on a scan that has not returned yet
    agent.script = [reply([tool_use("1", "scan_fleet", {}), tool_use("2", "send_ota_command", FIX)], "tool_use"), finish()]
    assert agent.main.main() == 0
    assert [c[0] for c in agent.server_calls] == ["scan_fleet"]


def test_an_unreadable_scan_after_a_healthy_one_blocks_writes_again(agent, tmp_path):
    def second(messages):
        agent.scan_text = "not json"
        return reply([tool_use("2", "scan_fleet", {})], "tool_use")

    agent.script = [reply([tool_use("1", "scan_fleet", {})], "tool_use"), second,
                    reply([tool_use("3", "send_ota_command", FIX)], "tool_use"), finish()]
    assert agent.main.main() == 0
    assert [c[0] for c in agent.server_calls] == ["scan_fleet", "scan_fleet"]


# ---- crashes are visible and exit codes are honest -----------------------------------------

def test_startup_failure_is_logged_with_a_hint_and_exits_1(agent, tmp_path, capsys):
    class Group(Exception):
        exceptions = [Exception("Connection closed")]
    agent.fail_on_initialize = Group("unhandled errors in a TaskGroup")
    agent.script = [finish()]
    assert agent.main.main() == 1
    events = ledger_events(tmp_path)
    assert events[-1]["event_type"] == "STARTUP_FAILED" and "make up" in events[-1]["payload"]["error"]
    assert "SESSION_START" not in kinds(tmp_path)
    assert "make up" in capsys.readouterr().err


def test_missing_binary_hint(agent, tmp_path, capsys):
    agent.fail_on_stdio = FileNotFoundError("./mcp-server-go/mcp-server-go-binary")
    assert agent.main.main() == 1
    assert "make build" in capsys.readouterr().err


def test_crash_after_the_session_started_is_written_as_aborted(agent, tmp_path):
    agent.script = [reply([tool_use("1", "scan_fleet", {})], "tool_use"), RuntimeError("model call failed")]
    assert agent.main.main() == 1
    last = ledger_events(tmp_path)[-1]
    assert last["event_type"] == "SESSION_ABORTED" and last["payload"]["reason"] == "exception"
    assert "model call failed" in last["payload"]["error"]
    assert kinds(tmp_path).count("SESSION_ABORTED") == 1


def test_turn_cap_stops_the_run_and_exits_2(agent, tmp_path, monkeypatch):
    monkeypatch.setattr(agent.main, "MAX_TURNS", 3)
    agent.script = [reply([tool_use("1", "scan_fleet", {})], "tool_use")]    # never finishes
    assert agent.main.main() == 2
    last = ledger_events(tmp_path)[-1]
    assert last["event_type"] == "SESSION_ABORTED" and last["payload"]["reason"] == "max_turns"
    assert kinds(tmp_path).count("MODEL_TURN") == 3


def test_unexpected_stop_reason_aborts_with_exit_2(agent, tmp_path):
    agent.script = [reply([Blk(type="text", text="cut off")], "max_tokens")]
    assert agent.main.main() == 2
    assert ledger_events(tmp_path)[-1]["payload"]["reason"] == "stop_reason=max_tokens"


def test_ctrl_c_is_recorded_and_exits_130(agent, tmp_path):
    agent.script = [reply([tool_use("1", "scan_fleet", {})], "tool_use"), KeyboardInterrupt()]
    assert agent.main.main() == 130
    assert ledger_events(tmp_path)[-1]["payload"]["reason"] == "interrupted"


def test_missing_api_key_gives_a_clear_message(agent, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert agent.main.main() == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "logs").exists()


def test_turn_cap_is_fifteen(agent):
    assert agent.main.MAX_TURNS == 15


def test_env_file_key_overrides_a_stale_key_exported_in_the_shell(agent):
    assert agent.dotenv_calls and agent.dotenv_calls[0].get("override") is True
