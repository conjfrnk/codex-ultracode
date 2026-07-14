import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from conductor_runtime.cli import main
from conductor_runtime.core.auto import run_direct
from conductor_runtime.core.policy import RuntimePolicy


FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import pathlib
import sys

if sys.argv[1:] == ["--version"]:
    print("fake-codex 1.0")
    raise SystemExit(0)
args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
output.parent.mkdir(parents=True, exist_ok=True)
sandbox = args[args.index("--sandbox") + 1]
if sandbox == "workspace-write":
    pathlib.Path("made.txt").write_text("made\n")
if output.name == "verdict.json":
    output.write_text(json.dumps({
        "satisfied": True,
        "feedback": "",
        "evidence": ["checked"],
        "confidence": 1.0,
    }))
else:
    output.write_text("model-result")
print(json.dumps({"type": "thread.started", "thread_id": "cli-session"}))
print(json.dumps({
    "type": "turn.completed",
    "usage": {
        "input_tokens": 10,
        "cached_input_tokens": 0,
        "output_tokens": 5,
        "reasoning_output_tokens": 0,
    },
}))
'''


class CoreCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="conductor-cli-")
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home = self.root / "state"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.codex = self.bin / "codex"
        self.codex.write_text(FAKE_CODEX, encoding="utf-8")
        self.codex.chmod(0o755)
        self.environment = patch.dict(
            os.environ,
            {
                "CODEX_CONDUCTOR_HOME": str(self.home),
                "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_help_and_version_are_available_without_runtime_initialization(self):
        status, output, error = self._main([])
        self.assertEqual(status, 0)
        self.assertIn("COMMAND", output)
        self.assertEqual(error, "")
        status, output, error = self._main(["--version"])
        self.assertEqual(status, 0)
        self.assertRegex(output, r"conductor-runtime \d+\.\d+\.\d+")
        self.assertEqual(error, "")

    def test_validate_and_list_report_validity_and_sanitize_paths(self):
        valid = self._write_workflow("valid.json")
        status, output, error = self._main(["validate", str(valid)])
        self.assertEqual(status, 0)
        self.assertIn("OK:", output)
        self.assertEqual(error, "")

        secret = "ghp_" + "a" * 36
        invalid = self.root / ("\x1b[31m" + secret + ".json")
        value = self._workflow()
        value["unsupported"] = True
        invalid.write_text(json.dumps(value), encoding="utf-8")
        status, _, error = self._main(["validate", str(invalid)])
        self.assertEqual(status, 2)
        self.assertNotIn(secret, error)
        self.assertNotIn("\x1b", error)

        status, output, error = self._main(["list", str(self.root)])
        self.assertEqual(status, 0)
        self.assertIn("OK\t", output)
        self.assertIn("INVALID\t", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("\x1b", output)
        self.assertEqual(error, "")

    def test_run_print_result_and_status_verify_the_external_record(self):
        workflow = self._write_workflow("run.json")
        status, output, error = self._main(["run", str(workflow), "--workspace", str(self.workspace), "--print-result"])
        self.assertEqual(status, 0)
        self.assertIn("Status: completed", output)
        self.assertIn("Result:\nvalue", output)
        self.assertEqual(error, "")
        run_dir = Path(next(line.split(": ", 1)[1] for line in output.splitlines() if line.startswith("Run directory:")))
        self.assertFalse(run_dir.is_relative_to(self.workspace))

        status, output, error = self._main(["status", str(run_dir)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)["status"], "completed")
        self.assertEqual(error, "")

        status, _, error = self._main(
            ["run", str(workflow), "--workspace", str(self.workspace), "--dry-run", "--print-result"]
        )
        self.assertEqual(status, 2)
        self.assertIn("requires real execution", error)

    def test_auto_plan_has_no_provider_dependency_and_direct_execution_uses_one(self):
        self.codex.unlink()
        with patch.dict(os.environ, {"PATH": str(self.bin)}):
            status, output, error = self._main(
                [
                    "auto",
                    "--task",
                    "Inspect.",
                    "--strategy",
                    "direct",
                    "--workspace",
                    str(self.workspace),
                    "--allow-agent",
                    "--plan-only",
                ]
            )
        self.assertEqual(status, 0)
        self.assertIn("Route: direct", output)
        self.assertIn("Status: planned", output)
        self.assertEqual(error, "")

        self.codex.write_text(FAKE_CODEX, encoding="utf-8")
        self.codex.chmod(0o755)
        requested = self.workspace / "answer.md"
        status, output, error = self._main(
            [
                "auto",
                "--task",
                "Inspect.",
                "--workspace",
                str(self.workspace),
                "--allow-agent",
                "--output",
                str(requested),
            ]
        )
        self.assertEqual(status, 0)
        self.assertIn("Status: completed", output)
        self.assertEqual(requested.read_text(), "model-result")
        self.assertEqual(error, "")

    def test_apply_command_is_verified_and_idempotent(self):
        result = run_direct(
            "Create made.txt.",
            workspace=self.workspace,
            policy=RuntimePolicy(allow_agent=True, allow_writes=True),
        )
        arguments = [
            "apply",
            str(result.stage_evidence_path),
            "--workspace",
            str(self.workspace),
            "--allow-writes",
            "--approve",
            "verified-stage-apply",
        ]
        status, output, error = self._main(arguments)
        self.assertEqual(status, 0)
        self.assertIn("Already applied: no", output)
        self.assertEqual((self.workspace / "made.txt").read_text(), "made\n")
        self.assertEqual(error, "")
        status, output, error = self._main(arguments)
        self.assertEqual(status, 0)
        self.assertIn("Already applied: yes", output)
        self.assertEqual(error, "")

    def test_doctor_reports_ready_and_missing_states(self):
        status, output, error = self._main(["doctor", "--json"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)["status"], "ready")
        self.assertEqual(error, "")
        with patch.dict(os.environ, {"PATH": str(self.root / "missing-bin")}):
            status, output, error = self._main(["doctor"])
        self.assertEqual(status, 1)
        self.assertIn("status: missing", output)
        self.assertEqual(error, "")

    def _write_workflow(self, name):
        path = self.root / name
        path.write_text(json.dumps(self._workflow()), encoding="utf-8")
        return path

    @staticmethod
    def _workflow():
        return {
            "schema": "conductor.workflow.v1",
            "name": "cli-workflow",
            "result_artifact": "result.txt",
            "steps": [
                {
                    "id": "write",
                    "kind": "write_artifact",
                    "output": "result.txt",
                    "content": "value",
                }
            ],
        }

    @staticmethod
    def _main(arguments):
        output = io.StringIO()
        error = io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            status = main(arguments)
        return status, output.getvalue(), error.getvalue()


if __name__ == "__main__":
    unittest.main()
