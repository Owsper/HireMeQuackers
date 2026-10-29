"""Interactive `quackers` session (what you get by running `quackers` alone).

- Lines starting with `/` run the normal CLI commands: `/scan`, `/jobs --new`,
  `/app set 12 shortlisted`, ... with tab completion and history.
- Anything else is sent to Gemini (see `quackers.agent`), which answers and
  calls the same commands as tools.
"""
from __future__ import annotations

import argparse
import io
import os
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import Dict, List, Tuple

from quackers import cli, store, term

BUILTINS: Dict[str, str] = {
    "help": "show commands and examples",
    "clear": "clear the screen and start a new AI conversation",
    "exit": "leave quackers",
}

CommandTree = Dict[str, Tuple[str, "CommandTree"]]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def split_args(text: str) -> List[str]:
    """Shell-like split that keeps Windows backslashes (C:\\cv.pdf) intact."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    return list(lexer)


def command_tree(parser: argparse.ArgumentParser) -> CommandTree:
    """{name: (help, {subname: (help, ...)})} for every (sub)command."""
    for action in parser._actions:  # noqa: SLF001 - argparse has no public API for this
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            helps = {a.dest: a.help or "" for a in action._choices_actions}  # noqa: SLF001
            return {name: (helps.get(name, ""), command_tree(sub))
                    for name, sub in action.choices.items()}
    return {}


class _Tee(io.TextIOBase):
    """Write to the terminal and keep a copy for the AI model."""

    def __init__(self, primary, copy: io.StringIO):
        self._primary, self._copy = primary, copy

    def write(self, s: str) -> int:
        self._primary.write(s)
        self._copy.write(s)
        return len(s)

    def flush(self) -> None:
        self._primary.flush()

    def isatty(self) -> bool:
        return self._primary.isatty()


def execute(argv: List[str], *, capture: bool = False) -> Tuple[bool, str]:
    """Run one CLI command in-process. Returns (ok, captured plain-text output)."""
    buf = io.StringIO()
    out = _Tee(sys.stdout, buf) if capture else sys.stdout
    err = _Tee(sys.stderr, buf) if capture else sys.stderr
    ok = True
    try:
        with redirect_stdout(out), redirect_stderr(err):
            args = cli.build_parser().parse_args(argv)
            args.func(args)
    except cli.CliError as exc:
        term.error(str(exc))
        buf.write(f"ERROR: {exc}\n")
        ok = False
    except SystemExit as exc:  # argparse usage errors and --help
        ok = exc.code in (0, None)
    except KeyboardInterrupt:
        term.error("Interrupted.")
        buf.write("ERROR: interrupted by the user\n")
        ok = False
    return ok, term.clean(buf.getvalue())


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _width() -> int:
    try:
        return max(40, min(os.get_terminal_size().columns, 100))
    except OSError:
        return 80


def _box(lines: List[str], width: int) -> None:
    inner = width - 4
    print(term.style("╭" + "─" * (width - 2) + "╮", "cyan"))
    for line in lines:
        visible = term._cell_width(term._ANSI_RE.sub("", line))  # noqa: SLF001
        pad = max(0, inner - visible)
        print(term.style("│ ", "cyan") + line + " " * pad + term.style(" │", "cyan"))
    print(term.style("╰" + "─" * (width - 2) + "╯", "cyan"))


def banner(agent) -> None:
    profile = store.load_active_profile()
    jobs = store.list_jobs_with_match(limit=10000)
    last = store.latest_scan_dict(limit=1)
    if profile:
        who = f"{term.clean(profile.name)} (v{profile.profile_version})"
    else:
        who = "none — /profile import <cv>"
    ai = (term.style(f"on ({agent.model})", "green") if agent.available
          else term.style(f"off — {agent.unavailable_reason}", "yellow"))
    lines = [
        term.style("🦆 Welcome to HireMeQuackers!", "bold"),
        "",
        term.style("  /help for commands · or just type what you want", "dim"),
        "",
        f"  profile: {who}",
        f"  jobs: {len(jobs)} · last scan: {last['started_at'] if last else 'never'}",
        f"  AI: {ai}",
    ]
    _box(lines, min(_width(), 64))


def print_help(tree: CommandTree, agent) -> None:
    term.heading("Commands")
    rows = [{"command": f"/{name}", "what": help_} for name, (help_, _) in tree.items()]
    rows += [{"command": f"/{name}", "what": help_} for name, help_ in BUILTINS.items()]
    term.table(rows, ["command", "what"], max_widths={"what": 70})
    term.info("\nAdd -h to any command for its options, e.g. /jobs -h")
    term.heading("Examples")
    term.bullets([
        "/scan --no-ai",
        "/jobs --new --sort score --why",
        "/job 12",
        "/app set 12 shortlisted",
        '/config set locations="Berlin, Munich" include_remote=true',
    ])
    if agent.available:
        term.heading("Or just ask")
        term.bullets([
            "find remote data internships and show the best 5",
            "write a confident cover letter for job 12",
            "what applications need a follow-up this week?",
        ])
    else:
        term.info(f"\nPlain-text questions need Gemini ({agent.unavailable_reason}).")


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def _make_reader(tree: CommandTree):
    """Return a `read() -> str` function; raises EOFError / KeyboardInterrupt."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return lambda: input("> ")
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import FileHistory
    except ImportError:
        return lambda: input("> ")

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            yield from (Completion(name, start_position=-len(prefix), display=shown,
                                   display_meta=help_)
                        for name, prefix, shown, help_ in slash_completions(
                            document.text_before_cursor, tree))

    from config.settings import get_config

    history_dir = get_config().data_dir
    history_dir.mkdir(parents=True, exist_ok=True)
    session = PromptSession(
        history=FileHistory(str(history_dir / "quackers_history")),
        completer=SlashCompleter(),
        complete_while_typing=True,
        bottom_toolbar=HTML(" <b>/help</b> commands · <b>Tab</b> complete · "
                            "<b>Ctrl+C</b> twice to exit"),
        placeholder=HTML('<style fg="ansibrightblack">Try "find remote internships" or /help</style>'),
    )
    return lambda: session.prompt(HTML("<b><ansicyan>&gt;</ansicyan></b> "))


def slash_completions(text: str, tree: CommandTree):
    """Yield (name, typed_prefix, display, help) for a partially typed /command."""
    if not text.startswith("/"):
        return
    parts = text[1:].split(" ")
    if len(parts) == 1:
        options = {**{k: v[0] for k, v in tree.items()}, **BUILTINS}
    elif len(parts) == 2 and parts[0] in tree and tree[parts[0]][1]:
        options = {k: v[0] for k, v in tree[parts[0]][1].items()}
    else:
        return
    prefix = parts[-1]
    for name, help_ in options.items():
        if name.startswith(prefix):
            yield name, prefix, (f"/{name}" if len(parts) == 1 else name), help_


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def handle_line(line: str, tree: CommandTree, agent) -> bool:
    """Process one input line. Returns False when the user wants to exit."""
    line = line.replace("﻿", "").strip()  # PowerShell pipes prepend a BOM
    if not line:
        return True
    if line.startswith("/"):
        try:
            argv = split_args(line[1:])
        except ValueError as exc:
            term.error(f"Could not parse that line: {exc}")
            return True
        if not argv:
            print_help(tree, agent)
            return True
        name = argv[0].lower()
        if name in {"exit", "quit", "q"}:
            return False
        if name == "help":
            print_help(tree, agent)
        elif name == "clear":
            os.system("cls" if os.name == "nt" else "clear")
            agent.reset()
            banner(agent)
        elif name in tree:
            execute([name, *argv[1:]])
        else:
            term.error(f"Unknown command /{argv[0]}. Type /help.")
        return True

    if not agent.available:
        term.warn(f"AI mode is off ({agent.unavailable_reason}). "
                  "Use commands instead, e.g. /jobs --new. Type /help for the list.")
        return True

    def on_tool(call: str) -> None:
        print(term.style("● ", "cyan") + term.style(term.clean(call), "dim"))

    print(term.style("  thinking…", "dim"))
    try:
        answer = agent.ask(line, on_tool)
    except KeyboardInterrupt:
        term.error("Interrupted.")
        return True
    except Exception as exc:  # noqa: BLE001 - network / quota / SDK errors
        term.error(f"Gemini error: {exc}")
        return True
    if answer:
        print()
        term.paragraph(answer, indent=2)
    return True


def run() -> int:
    from quackers.agent import Agent

    tree = command_tree(cli.build_parser())
    agent = Agent(lambda argv: execute(argv, capture=True))
    banner(agent)
    read = _make_reader(tree)
    interrupted = False
    while True:
        print()
        try:
            line = read()
        except KeyboardInterrupt:
            if interrupted:
                break
            interrupted = True
            term.info(term.style("(press Ctrl+C again or type /exit to quit)", "dim"))
            continue
        except EOFError:
            break
        interrupted = False
        if not handle_line(line, tree, agent):
            break
    term.info("Bye! 🦆")
    return 0
