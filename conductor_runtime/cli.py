"""Small canonical CLI for the default Conductor runtime."""

import argparse
import sys

from . import __version__


CANONICAL_COMMANDS = (
    "auto",
    "run",
    "validate",
    "status",
    "list",
    "apply",
    "doctor",
)
_LEGACY_ALIASES = {"apply": "apply-verified-stage"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conductor-runtime",
        description="Run secure, resumable Codex workflows.",
    )
    parser.add_argument("--version", action="version", version="conductor-runtime %s" % __version__)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    descriptions = {
        "auto": "Use the model-first automatic entry point.",
        "run": "Run or resume a validated workflow.",
        "validate": "Validate workflow JSON without execution.",
        "status": "Inspect one external run record.",
        "list": "List external run records.",
        "apply": "Apply a verified staged result.",
        "doctor": "Check the local Codex command host.",
    }
    for command in CANONICAL_COMMANDS:
        commands.add_parser(command, help=descriptions[command], add_help=False)
    commands.add_parser(
        "legacy",
        help="Run an optional compatibility or development command.",
        add_help=False,
    )
    return parser


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not args or args == ["-h"] or args == ["--help"]:
        parser.print_help()
        return 0
    if args == ["--version"]:
        print("conductor-runtime %s" % __version__)
        return 0

    command = args[0]
    if command == "legacy":
        if len(args) == 1:
            from .legacy_cli import build_parser as build_legacy_parser

            build_legacy_parser().print_help()
            return 0
        return _legacy_main(args[1:])
    if command in CANONICAL_COMMANDS:
        return _legacy_main([_LEGACY_ALIASES.get(command, command), *args[1:]])
    # Source checkouts retain compatibility/development commands. The default
    # packaged runtime omits this module and fails with the focused message.
    try:
        return _legacy_main(args)
    except ModuleNotFoundError as exc:
        if exc.name != "%s.legacy_cli" % __package__:
            raise
        parser.error("unknown command %r; install optional tooling or use a core command" % command)
        return 2


def _legacy_main(argv) -> int:
    from .legacy_cli import main as legacy_main

    return legacy_main(argv)
