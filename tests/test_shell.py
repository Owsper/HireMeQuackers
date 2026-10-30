"""Tests for the interactive `quackers` session and its AI agent."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from quackers import agent as agent_mod
from quackers import cli, shell
from services import llm


# ---- shell helpers --------------------------------------------------------


def test_split_args_keeps_windows_paths_and_quotes():
    assert shell.split_args(r'profile import C:\Users\me\cv.pdf --yes') == [
        "profile", "import", r"C:\Users\me\cv.pdf", "--yes"]
    assert shell.split_args('config set locations="Berlin, Munich"') == [
        "config", "set", "locations=Berlin, Munich"]


def test_command_tree_lists_commands_and_subcommands():
    tree = shell.command_tree(cli.build_parser())
    assert {"scan", "jobs", "job", "app", "docs", "config"} <= set(tree)
    assert "cover-letter" in tree["docs"][1]


def test_slash_completions():
    tree = shell.command_tree(cli.build_parser())
    names = [c[0] for c in shell.slash_completions("/sc", tree)]
    assert names == ["scan"]
    assert "help" in [c[0] for c in shell.slash_completions("/he", tree)]
    subs = [c[0] for c in shell.slash_completions("/docs co", tree)]
    assert subs == ["cover-letter"]
    assert list(shell.slash_completions("hello", tree)) == []


def test_execute_captures_output_and_usage_errors():
    ok, out = shell.execute(["jobs", "--limit", "nope"], capture=True)
    assert not ok and "invalid int value" in out
    ok, _ = shell.execute(["jobs", "-h"], capture=True)
    assert ok


class _OffAgent:
    available = False
    unavailable_reason = "no key"
    model = "x"

    def reset(self):
        pass


def test_handle_line_routes_exit_unknown_and_ai_off(capsys):
    tree = shell.command_tree(cli.build_parser())
    assert shell.handle_line("/exit", tree, _OffAgent()) is False
    assert shell.handle_line("/bogus", tree, _OffAgent()) is True
    assert "Unknown command" in capsys.readouterr().err
    assert shell.handle_line("find me jobs", tree, _OffAgent()) is True
    assert "AI mode is off" in capsys.readouterr().err


# ---- agent tool mapping ---------------------------------------------------


_FORBIDDEN = {("reset",), ("open",), ("config", "reset"), ("config", "load"),
              ("config", "edit"), ("profile", "import"), ("profile", "edit"),
              ("docs", "export"), ("sources", "enable"), ("sources", "disable")}


def test_destructive_commands_are_not_tools():
    samples = {"job_id": 1, "status": "SHORTLISTED", "text": "x", "date": "2026-01-01",
               "settings": ["locations=Berlin"]}
    for name, (_, schema, _) in agent_mod.TOOLS.items():
        args = {k: samples[k] for k in schema.get("required", [])}
        argv = agent_mod.build_argv(name, args)
        assert (argv[0],) not in _FORBIDDEN, name
        assert tuple(argv[:2]) not in _FORBIDDEN, name


def test_applied_never_auto_confirms():
    argv = agent_mod.build_argv("set_application_status", {"job_id": 5, "status": "applied"})
    assert argv == ["app", "set", "5", "APPLIED"]
    assert "--confirm" not in argv


@pytest.mark.parametrize("name, args, expected", [
    ("list_jobs", {"new": True, "sort_by_score": True, "limit": 5},
     ["jobs", "--limit", "5", "--new", "--sort", "score"]),
    ("scan_for_jobs", {"no_ai": True, "sources": ["lever"]}, ["scan", "--no-ai", "--source", "lever"]),
    ("update_search_config", {"settings": ["locations=Berlin, Munich"]},
     ["config", "set", "locations=Berlin, Munich"]),
    ("generate_cover_letter", {"job_id": 3, "tone": "weird"},
     ["docs", "cover-letter", "3", "--tone", "professional", "--show"]),
])
def test_build_argv(name, args, expected):
    assert agent_mod.build_argv(name, args) == expected


@pytest.mark.parametrize("name, args", [
    ("show_job", {"job_id": "abc"}),
    ("list_jobs", {"limit": 500}),
    ("set_application_status", {"job_id": 1, "status": "HIRED"}),
    ("delete_everything", {}),
])
def test_build_argv_rejects_bad_input(name, args):
    with pytest.raises(ValueError):
        agent_mod.build_argv(name, args)


def test_every_tool_argv_parses_with_the_cli():
    samples = {"job_id": 1, "status": "SHORTLISTED", "text": "hello", "date": "2026-01-01",
               "settings": ["locations=Berlin"]}
    parser = cli.build_parser()
    for name, (_, schema, _) in agent_mod.TOOLS.items():
        args = {k: samples[k] for k in schema.get("required", [])}
        parsed = parser.parse_args(agent_mod.build_argv(name, args))
        assert callable(parsed.func), name


# ---- agent loop with a fake provider chat ------------------------------------


class _FakeChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return self.replies.pop(0)

    def send_results(self, results):
        self.sent.append(results)
        return self.replies.pop(0)


def _call(name, **args):
    return llm.Call(f"call-{name}", name, args)


def _reply(text="", *calls):
    return llm.Reply(text, list(calls))


def _fake_agent(execute, replies):
    agent = agent_mod.Agent.__new__(agent_mod.Agent)
    agent._execute = execute
    agent._ai = SimpleNamespace(available=True)
    agent.unavailable_reason = None
    agent.model = "fake"
    agent._chat = _FakeChat(replies)
    return agent


def test_agent_runs_tool_calls_then_answers():
    executed = []

    def execute(argv):
        executed.append(argv)
        return True, "id  title\n12  Data Intern"

    agent = _fake_agent(execute, [
        _reply("", _call("list_jobs", new=True, limit=5)),
        _reply("Best match: #12 Data Intern"),
    ])
    shown = []
    answer = agent.ask("show new jobs", shown.append)
    assert answer == "Best match: #12 Data Intern"
    assert executed == [["jobs", "--limit", "5", "--new"]]
    assert shown == ["list_jobs(new=True, limit=5)"]


def test_agent_reports_bad_tool_arguments_to_model():
    agent = _fake_agent(lambda argv: pytest.fail("must not execute invalid calls"), [
        _reply("", _call("show_job", job_id="oops")),
        _reply("Which job do you mean?"),
    ])
    assert agent.ask("show job oops", lambda _: None) == "Which job do you mean?"
    call, result = agent._chat.sent[1][0]
    assert call.id == "call-show_job" and result["ok"] is False


def test_agent_stops_after_too_many_rounds():
    loop = _reply("", _call("get_status"))
    agent = _fake_agent(lambda argv: (True, "ok"), [loop] * (agent_mod.MAX_TOOL_ROUNDS + 1))
    assert "too many tool calls" in agent.ask("loop", lambda _: None)


def test_agent_unavailable_without_key(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    from config import settings

    if hasattr(settings.get_config, "cache_clear"):
        settings.get_config.cache_clear()
    agent = agent_mod.Agent(lambda argv: (True, ""))
    assert not agent.available and "GEMINI_API_KEY" in agent.unavailable_reason


def test_handle_line_ignores_byte_order_mark():
    tree = shell.command_tree(cli.build_parser())
    assert shell.handle_line("\ufeff/exit", tree, _OffAgent()) is False


def test_scan_tool_passes_titles_and_locations():
    argv = agent_mod.build_argv("scan_for_jobs", {"titles": ["Data Analyst", "BI Intern"],
                                                  "locations": ["Berlin"]})
    assert argv == ["scan", "--titles", "Data Analyst, BI Intern", "--location", "Berlin"]
    assert cli.build_parser().parse_args(argv).titles == "Data Analyst, BI Intern"


def test_scan_tool_passes_workplace_types():
    argv = agent_mod.build_argv("scan_for_jobs", {"titles": ["Dev"], "workplace_types": ["remote"]})
    assert argv == ["scan", "--titles", "Dev", "--type", "remote"]


def test_agent_drops_chat_after_too_many_rounds():
    loop = _reply("", _call("get_status"))
    agent = _fake_agent(lambda argv: (True, "ok"), [loop] * (agent_mod.MAX_TOOL_ROUNDS + 1))
    agent.ask("loop", lambda _: None)
    assert agent._chat is None  # the unanswered tool call must not poison the next message


def test_agent_reports_command_crash_to_model():
    def execute(argv):
        raise RuntimeError("database is locked")

    agent = _fake_agent(execute, [_reply("", _call("get_status")), _reply("The database is busy.")])
    assert agent.ask("status", lambda _: None) == "The database is busy."
    call, result = agent._chat.sent[1][0]
    assert result == {"ok": False, "output": "Command failed: database is locked"}


def test_agent_drops_chat_when_provider_fails():
    class _Broken:
        def send(self, text):
            raise RuntimeError("503")

    agent = _fake_agent(lambda argv: (True, ""), [])
    agent._chat = _Broken()
    with pytest.raises(RuntimeError):
        agent.ask("hi", lambda _: None)
    assert agent._chat is None
