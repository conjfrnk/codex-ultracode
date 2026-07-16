import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from conductor_extras import cli as extras_cli


class ExtrasCliNamespaceTests(unittest.TestCase):
    def test_parser_identity_and_top_level_help_are_extras_specific(self):
        parser = extras_cli.build_parser()
        help_text = parser.format_help()

        self.assertEqual(parser.prog, "conductor-extras")
        self.assertNotIn("conductor-runtime", help_text)
        for group in extras_cli._COMMAND_GROUPS:
            self.assertIn(group, help_text)
        self.assertIn(
            extras_cli.FLAT_COMMAND_ALIASES_SUPPORTED_THROUGH,
            help_text,
        )

        stdout = StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as caught:
            extras_cli.main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertTrue(stdout.getvalue().startswith("conductor-extras "))

    def test_registry_gives_every_flat_command_one_grouped_route(self):
        parser = extras_cli.build_parser()
        flat_commands = set(
            extras_cli._command_subparsers(parser).choices
        )
        registry = extras_cli._grouped_command_registry(parser)
        grouped_flat_commands = [
            flat_command
            for commands in registry.values()
            for flat_command in commands.values()
        ]

        self.assertEqual(set(registry), set(extras_cli._COMMAND_GROUPS))
        self.assertEqual(set(grouped_flat_commands), flat_commands)
        self.assertEqual(len(grouped_flat_commands), len(flat_commands))

    def test_group_and_leaf_help_are_discoverable(self):
        group_help = StringIO()
        with redirect_stdout(group_help), self.assertRaises(SystemExit) as caught:
            extras_cli.main(["memory", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("conductor-extras memory", group_help.getvalue())
        self.assertIn("list", group_help.getvalue())

        leaf_help = StringIO()
        with redirect_stdout(leaf_help), self.assertRaises(SystemExit) as caught:
            extras_cli.main(["memory", "list", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("usage: conductor-extras memory list", leaf_help.getvalue())

    def test_representative_grouped_commands_reuse_flat_dispatch(self):
        cases = [
            (["workflow", "validate", "workflow.json"], "_validate", "validate"),
            (
                ["team", "list-inbox", "run-dir", "team-step"],
                "_list_team_inbox",
                "list-team-inbox",
            ),
            (["memory", "list", "--json"], "_list_agent_memory", "list-agent-memory"),
            (["benchmark", "run", "suite.json"], "_benchmark", "benchmark"),
            (["routine", "list", "--json"], "_list_routines", "list-routines"),
            (["cloud", "status", "task-123"], "_cloud_status", "cloud-status"),
        ]

        for index, (argv, handler_name, flat_command) in enumerate(cases):
            with self.subTest(group=argv[0]):
                stderr = StringIO()
                with patch.object(
                    extras_cli,
                    handler_name,
                    return_value=20 + index,
                ) as handler, redirect_stderr(stderr):
                    result = extras_cli.main(argv)
                self.assertEqual(result, 20 + index)
                self.assertEqual(handler.call_args.args[0].command, flat_command)
                self.assertEqual(stderr.getvalue(), "")

    def test_flat_aliases_dispatch_and_emit_bounded_deprecation(self):
        cases = [
            (
                ["list-agent-memory", "--json"],
                "_list_agent_memory",
                "memory list",
            ),
            (["benchmark", "suite.json"], "_benchmark", "benchmark run"),
        ]
        for argv, handler_name, grouped_command in cases:
            with self.subTest(flat_command=argv[0]):
                stderr = StringIO()
                with patch.object(
                    extras_cli,
                    handler_name,
                    return_value=31,
                ) as handler, patch.object(
                    extras_cli.sys,
                    "argv",
                    ["conductor-extras", *argv],
                ), redirect_stderr(stderr):
                    result = extras_cli.main()
                self.assertEqual(result, 31)
                handler.assert_called_once()
                warning = stderr.getvalue()
                self.assertIn("DEPRECATION", warning)
                self.assertIn(
                    "conductor-extras %s" % grouped_command,
                    warning,
                )
                self.assertIn(
                    extras_cli.FLAT_COMMAND_ALIASES_SUPPORTED_THROUGH,
                    warning,
                )

    def test_invalid_grouped_command_fails_under_group_identity(self):
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            extras_cli.main(["memory", "not-a-command"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("conductor-extras memory", stderr.getvalue())
        self.assertIn("invalid choice", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
