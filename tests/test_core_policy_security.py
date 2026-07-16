import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conductor_runtime.core.policy import (
    CAPABILITY_FIELDS,
    RuntimePolicy,
    enforce_shell,
    policy_fingerprint,
    prepare_shell_launch,
    shell_approval,
)
from conductor_runtime.core.process import run_process, sanitized_subprocess_environment
from conductor_runtime.core import runner as core_runner
from conductor_runtime.core.runner import WorkflowRunner
from conductor_runtime.core.workflow import SCHEMA
from conductor_runtime.errors import ValidationError


FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
output.parent.mkdir(parents=True, exist_ok=True)
retained = os.environ.get("OPENAI_API_KEY", "<missing-retained>")
ambient = os.environ.get("CORE_ARBITRARY_SENTINEL", "<missing-ambient>")
output.write_text("retained=%s\nambient=%s\n" % (retained, ambient), encoding="utf-8")
print(json.dumps({"type": "thread.started", "thread_id": "policy-security-session"}))
print(json.dumps({"type": "item.completed", "retained": retained, "ambient": ambient}))
print(json.dumps({
    "type": "turn.completed",
    "usage": {
        "input_tokens": 1,
        "cached_input_tokens": 0,
        "output_tokens": 1,
        "reasoning_output_tokens": 0,
    },
}))
'''


class CorePolicySecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="conductor-core-policy-")
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state = self.root / "state"
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_path_shadowed_inert_names_cannot_launch_without_exact_approval(self):
        for executable in ("true", "false", "test", "["):
            with self.subTest(executable=executable):
                marker = self.root / ("launched-" + executable.replace("[", "bracket"))
                shadow = self.bin / executable
                shadow.write_text("#!/bin/sh\nprintf launched > \"$1\"\n", encoding="utf-8")
                shadow.chmod(0o755)
                command = [executable, str(marker)]
                workflow = {
                    "schema": SCHEMA,
                    "name": "path-shadow-" + executable.replace("[", "bracket"),
                    "steps": [
                        {
                            "id": "probe",
                            "kind": "shell",
                            "command": command,
                            "writes": False,
                            "destructive": False,
                            "network": False,
                        }
                    ],
                }
                with patch.dict(
                    os.environ,
                    {
                        "CODEX_CONDUCTOR_HOME": str(self.state),
                        "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    },
                ):
                    expected_approval = shell_approval(
                        command,
                        cwd=self.workspace,
                        workspace=self.workspace,
                    )
                    run = WorkflowRunner(
                        workflow, self.workspace, None, RuntimePolicy()
                    ).execute()
                self.assertEqual(run.state["status"], "blocked")
                self.assertIn(expected_approval, run.state["detail"])
                self.assertFalse(marker.exists())

    def test_approved_bare_command_is_path_and_content_bound_before_launch(self):
        first_bin = self.root / "first-bin"
        second_bin = self.root / "second-bin"
        first_bin.mkdir()
        second_bin.mkdir()
        first = self._write_echo_executable(first_bin, "identity-probe", "first")
        second = self._write_echo_executable(second_bin, "identity-probe", "second")
        first_environment = self._shell_environment(first_bin)
        second_environment = self._shell_environment(second_bin)
        command = ["identity-probe"]
        first_approval = shell_approval(
            command,
            cwd=self.workspace,
            workspace=self.workspace,
            environment=first_environment,
        )
        second_approval = shell_approval(
            command,
            cwd=self.workspace,
            workspace=self.workspace,
            environment=second_environment,
        )
        self.assertNotEqual(first_approval, second_approval)
        assessment = enforce_shell(
            {
                "id": "probe",
                "command": command,
                "writes": False,
                "destructive": False,
                "network": False,
            },
            RuntimePolicy(approvals={first_approval}),
            cwd=self.workspace,
            workspace=self.workspace,
            environment=first_environment,
        )
        launch_argv = prepare_shell_launch(
            assessment,
            isolated_workspace=self.workspace,
        )
        self.assertEqual(Path(launch_argv[0]), first.resolve())

        with patch.dict(os.environ, {"PATH": str(second_bin)}):
            result = run_process(
                launch_argv,
                cwd=self.workspace,
                env=second_environment,
                timeout_seconds=10,
                output_limit_bytes=1024,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "first")
        self.assertNotEqual(Path(launch_argv[0]), second.resolve())

    def test_workspace_relative_and_absolute_executables_launch_from_isolated_copy(self):
        checks = self.workspace / "checks"
        checks.mkdir()
        executable = self._write_self_path_executable(checks, "show-path")
        cases = (
            ("relative", ["./show-path" if os.name != "nt" else ".\\show-path"]),
            ("absolute", [str(executable)]),
        )
        for label, command in cases:
            with self.subTest(kind=label):
                approval = shell_approval(
                    command,
                    cwd=checks,
                    workspace=self.workspace,
                )
                workflow = {
                    "schema": SCHEMA,
                    "name": "isolated-executable-" + label,
                    "steps": [
                        {
                            "id": "probe",
                            "kind": "shell",
                            "cwd": "checks",
                            "command": command,
                            "capture": "launched-path.txt",
                            "capture_mode": "stdout",
                            "writes": False,
                            "destructive": False,
                            "network": False,
                        }
                    ],
                }
                run = WorkflowRunner(
                    workflow,
                    self.workspace,
                    None,
                    RuntimePolicy(approvals={approval}),
                ).execute()
                self.assertEqual(run.state["status"], "completed", run.state.get("detail"))
                launched = Path(run.read_artifact("launched-path.txt").decode().strip())
                self.assertEqual(launched.name, executable.name)
                self.assertFalse(launched.is_relative_to(self.workspace))
                self.assertNotEqual(launched, executable)

        active_workspace = self.root / "active-workspace"
        active_checks = active_workspace / "checks"
        active_checks.mkdir(parents=True)
        active_executable = self._write_self_path_executable(active_checks, "show-path")
        isolated_workspace = self.root / "nested-isolation"
        isolated_checks = isolated_workspace / "checks"
        isolated_checks.mkdir(parents=True)
        isolated_executable = self._write_self_path_executable(isolated_checks, "show-path")
        absolute_command = [str(executable)]
        approval = shell_approval(
            absolute_command,
            cwd=checks,
            workspace=self.workspace,
        )
        assessment = enforce_shell(
            {
                "id": "probe",
                "command": absolute_command,
                "writes": False,
                "destructive": False,
                "network": False,
            },
            RuntimePolicy(approvals={approval}),
            cwd=active_checks,
            workspace=active_workspace,
            workspace_alias=self.workspace,
        )
        self.assertEqual(Path(assessment.argv[0]), active_executable.resolve())
        launch_argv = prepare_shell_launch(
            assessment,
            isolated_workspace=isolated_workspace,
        )
        self.assertEqual(Path(launch_argv[0]), isolated_executable.resolve())

    def test_workspace_executable_mutation_after_binding_cannot_switch_launched_bytes(self):
        checks = self.workspace / "checks"
        checks.mkdir()
        executable = self._write_echo_executable(checks, "mutable-probe", "approved")
        command = ["./mutable-probe" if os.name != "nt" else ".\\mutable-probe"]
        approval = shell_approval(
            command,
            cwd=checks,
            workspace=self.workspace,
        )
        workflow = {
            "schema": SCHEMA,
            "name": "mutated-executable",
            "steps": [
                {
                    "id": "probe",
                    "kind": "shell",
                    "cwd": "checks",
                    "command": command,
                    "writes": False,
                    "destructive": False,
                    "network": False,
                }
            ],
        }
        create_stage = core_runner.create_stage

        def switch_before_copy(*args, **kwargs):
            self._replace_echo_executable(executable, "unapproved")
            return create_stage(*args, **kwargs)

        with (
            patch.object(core_runner, "create_stage", side_effect=switch_before_copy),
            patch.object(core_runner, "run_process") as launched,
        ):
            run = WorkflowRunner(
                workflow,
                self.workspace,
                None,
                RuntimePolicy(approvals={approval}),
            ).execute()
        self.assertEqual(run.state["status"], "failed")
        self.assertIn("executable changed after approval", run.state["steps"]["probe"]["detail"])
        launched.assert_not_called()

    def test_policy_copies_approvals_and_rejects_non_boolean_capabilities(self):
        caller_approvals = {"initial"}
        policy = RuntimePolicy(allow_agent=True, approvals=caller_approvals)
        fingerprint = policy_fingerprint(policy)

        caller_approvals.add("late")

        self.assertIsInstance(policy.approvals, frozenset)
        self.assertFalse(policy.has_approval("late"))
        self.assertEqual(policy_fingerprint(policy), fingerprint)
        with self.assertRaises(AttributeError):
            policy.approvals.add("later")
        for capability in CAPABILITY_FIELDS:
            with self.subTest(capability=capability), self.assertRaisesRegex(
                ValidationError, "must be a boolean"
            ):
                RuntimePolicy(**{capability: 1})

    def test_provider_environment_is_allowlisted_and_retained_secret_is_exactly_redacted(self):
        codex = self.bin / "codex"
        codex.write_text(FAKE_CODEX, encoding="utf-8")
        codex.chmod(0o755)
        retained_secret = "arbitrary-retained-value-9b11c75d"
        ambient_secret = "arbitrary-ambient-value-4b9e0a2f"
        workflow = {
            "schema": SCHEMA,
            "name": "provider-environment",
            "result_artifact": "result.md",
            "steps": [
                {
                    "id": "work",
                    "kind": "codex_exec",
                    "prompt": "Inspect the workspace.",
                    "capture": "result.md",
                    "max_tokens": 100,
                }
            ],
        }
        with patch.dict(
            os.environ,
            {
                "CODEX_CONDUCTOR_HOME": str(self.state),
                "CORE_ARBITRARY_SENTINEL": ambient_secret,
                "OPENAI_API_KEY": retained_secret,
                "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            },
        ):
            run = WorkflowRunner(
                workflow,
                self.workspace,
                None,
                RuntimePolicy(allow_agent=True),
            ).execute()

        self.assertEqual(run.state["status"], "completed")
        output = run.read_artifact("result.md").decode("utf-8")
        self.assertIn("retained=<redacted-secret>", output)
        self.assertIn("ambient=<missing-ambient>", output)
        for path in run.run_dir.rglob("*"):
            if path.is_file():
                payload = path.read_bytes()
                self.assertNotIn(retained_secret.encode("utf-8"), payload)
                self.assertNotIn(ambient_secret.encode("utf-8"), payload)

    def _shell_environment(self, executable_directory: Path):
        source = dict(os.environ)
        source["PATH"] = str(executable_directory)
        if os.name == "nt":
            source["PATHEXT"] = ".CMD"
        return sanitized_subprocess_environment(source=source)

    def _write_echo_executable(self, directory: Path, name: str, value: str) -> Path:
        path = directory / ((name + ".cmd") if os.name == "nt" else name)
        self._replace_echo_executable(path, value)
        return path

    def _replace_echo_executable(self, path: Path, value: str) -> None:
        if os.name == "nt":
            path.write_text("@echo off\r\necho %s\r\n" % value, encoding="utf-8")
        else:
            path.write_text("#!/bin/sh\nprintf '%s\\n' '%s'\n" % ("%s", value), encoding="utf-8")
        path.chmod(0o755)

    def _write_self_path_executable(self, directory: Path, name: str) -> Path:
        path = directory / ((name + ".cmd") if os.name == "nt" else name)
        if os.name == "nt":
            path.write_text("@echo off\r\necho %~f0\r\n", encoding="utf-8")
        else:
            path.write_text("#!/bin/sh\nprintf '%s\\n' \"$0\"\n" % "%s", encoding="utf-8")
        path.chmod(0o755)
        return path


if __name__ == "__main__":
    unittest.main()
