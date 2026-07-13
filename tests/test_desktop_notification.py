import json
import shlex
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from conductor_runtime.artifacts import RunArtifacts
import conductor_runtime.background_run as background_run
import conductor_runtime.background_goal as background_goal
import conductor_runtime.desktop_notification as desktop_notification
import conductor_runtime.routine_supervisor as routine_supervisor
from conductor_runtime.legacy_cli import main as cli_main
from conductor_runtime.desktop_notification import (
    DESKTOP_NOTIFICATION_FILENAME_TEMPLATE,
    desktop_notification_directory_for_artifact,
    desktop_notification_status_for_execution,
    load_desktop_notification_receipt,
    send_artifact_desktop_notification,
    send_run_desktop_notification,
    validate_desktop_notification_receipt,
)
from conductor_runtime.auto_orchestrator import run_auto_orchestration
from conductor_runtime.model_orchestrator import (
    MODEL_WORKFLOW_EXECUTE_APPROVAL,
    run_model_workflow,
)
from conductor_runtime.runner import ProcessResult
from conductor_runtime.routines import routine_list_record, validate_routine_manifest
from conductor_runtime.errors import ValidationError
from conductor_runtime.schemas import get_schema, schema_types
from conductor_runtime.security import RuntimePolicy, assess_command
from conductor_runtime.saved_workflows import write_saved_workflow_script


def _workflow(steps=None):
    return {
        "schema": "conductor.workflow.v1",
        "name": "notification-test",
        "description": "Notification contract test.",
        "mode": "read_only",
        "max_workers": 1,
        "steps": list(steps or []),
    }


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class DesktopNotificationTests(unittest.TestCase):
    def _create_run(self, root: Path, run_id: str = "notification-run") -> RunArtifacts:
        return RunArtifacts.create(_workflow(), root / "runs", run_id=run_id)

    def test_macos_command_is_fixed_content_no_shell_and_receipt_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._create_run(Path(tmp))
            calls = []

            def execute(command, **kwargs):
                calls.append((command, kwargs))
                return SimpleNamespace(returncode=0)

            with patch("conductor_runtime.desktop_notification._usable_executable", return_value=True):
                receipt = send_run_desktop_notification(
                    run,
                    "completed",
                    system_name="Darwin",
                    execute=execute,
                )

            self.assertEqual(receipt["status"], "delivered")
            self.assertEqual(receipt["backend"], "macos-osascript")
            self.assertEqual(len(calls), 1)
            command, kwargs = calls[0]
            self.assertEqual(
                command,
                [
                    "/usr/bin/osascript",
                    "-e",
                    'display notification "Workflow completed." with title "Codex Conductor"',
                ],
            )
            self.assertNotIn("shell", kwargs)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            serialized = (run.run_dir / "desktop-notification-completed.json").read_text(encoding="utf-8")
            self.assertNotIn(run.run_dir.name, serialized)
            self.assertNotIn("notification-test", serialized)

    def test_final_receipt_suppresses_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._create_run(Path(tmp))
            calls = []

            def execute(command, **kwargs):
                calls.append(command)
                return SimpleNamespace(returncode=0)

            with patch("conductor_runtime.desktop_notification._usable_executable", return_value=True):
                first = send_run_desktop_notification(run, "completed", system_name="Darwin", execute=execute)
                second = send_run_desktop_notification(run, "completed", system_name="Darwin", execute=execute)

            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)

    def test_each_terminal_status_has_an_independent_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._create_run(Path(tmp))
            calls = []

            def execute(command, **kwargs):
                calls.append(command)
                return SimpleNamespace(returncode=0)

            with patch("conductor_runtime.desktop_notification._usable_executable", return_value=True):
                blocked = send_run_desktop_notification(run, "blocked", system_name="Darwin", execute=execute)
                completed = send_run_desktop_notification(run, "completed", system_name="Darwin", execute=execute)

            self.assertEqual(blocked["run_status"], "blocked")
            self.assertEqual(completed["run_status"], "completed")
            self.assertEqual(len(calls), 2)
            self.assertTrue((run.run_dir / "desktop-notification-blocked.json").is_file())
            self.assertTrue((run.run_dir / "desktop-notification-completed.json").is_file())

    def test_unsupported_platform_is_unavailable_without_process_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._create_run(Path(tmp))

            def unexpected_execute(*args, **kwargs):
                self.fail("unsupported platforms must not launch a process")

            receipt = send_run_desktop_notification(
                run,
                "failed",
                system_name="Plan9",
                execute=unexpected_execute,
            )
            self.assertEqual(receipt["status"], "unavailable")
            self.assertEqual(receipt["backend"], "unavailable")

    def test_linux_backend_uses_only_fixed_trusted_system_path(self):
        with patch("conductor_runtime.desktop_notification._usable_executable", return_value=True) as usable:
            backend, command = desktop_notification._notification_command("Linux", "blocked")
        self.assertEqual(backend, "linux-notify-send")
        self.assertEqual(command[0], "/usr/bin/notify-send")
        self.assertEqual(command[-1], "Workflow needs approval.")
        usable.assert_called_once_with(Path("/usr/bin/notify-send"))

        with tempfile.TemporaryDirectory() as tmp:
            untrusted = Path(tmp) / "notify-send"
            untrusted.write_text("#!/bin/sh\n", encoding="utf-8")
            untrusted.chmod(0o775)
            self.assertFalse(desktop_notification._usable_executable(untrusted))

    def test_nonzero_and_timeout_are_fail_open_final_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failed_run = self._create_run(root, "failed-command")
            timeout_run = self._create_run(root, "timed-out-command")

            with patch("conductor_runtime.desktop_notification._usable_executable", return_value=True):
                failed = send_run_desktop_notification(
                    failed_run,
                    "failed",
                    system_name="Darwin",
                    execute=lambda *args, **kwargs: SimpleNamespace(returncode=9),
                )

                def time_out(command, **kwargs):
                    raise subprocess.TimeoutExpired(command, kwargs["timeout"])

                timed_out = send_run_desktop_notification(
                    timeout_run,
                    "failed",
                    system_name="Darwin",
                    execute=time_out,
                )

            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["returncode"], 9)
            self.assertEqual(failed["error_class"], "NotificationCommandFailed")
            self.assertEqual(timed_out["status"], "failed")
            self.assertIsNone(timed_out["returncode"])
            self.assertEqual(timed_out["error_class"], "NotificationTimeout")

    def test_interrupted_claim_becomes_uncertain_without_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._create_run(Path(tmp))
            path = run.run_dir / "desktop-notification-completed.json"
            _write_json(
                path,
                {
                    "schema": "conductor.desktop_notification.v1",
                    "status": "sending",
                    "run_status": "completed",
                    "backend": "macos-osascript",
                    "content_policy": "fixed-status-only-v1",
                    "requested_at_utc": "2026-07-11T12:00:00.000Z",
                    "finished_at_utc": None,
                    "returncode": None,
                    "error_class": "",
                },
            )

            def unexpected_execute(*args, **kwargs):
                self.fail("an interrupted claim must never be replayed")

            receipt = send_run_desktop_notification(
                run,
                "completed",
                system_name="Darwin",
                execute=unexpected_execute,
            )
            self.assertEqual(receipt["status"], "uncertain")
            self.assertEqual(receipt["error_class"], "InterruptedDelivery")

    def test_atomic_claim_allows_only_one_concurrent_process_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._create_run(Path(tmp))
            entered = threading.Event()
            release = threading.Event()
            calls = []
            result = {}

            def execute(command, **kwargs):
                calls.append(command)
                entered.set()
                self.assertTrue(release.wait(2))
                return SimpleNamespace(returncode=0)

            def first_sender():
                result["first"] = send_run_desktop_notification(
                    run,
                    "completed",
                    system_name="Darwin",
                    execute=execute,
                )

            with patch("conductor_runtime.desktop_notification._usable_executable", return_value=True):
                thread = threading.Thread(target=first_sender)
                thread.start()
                self.assertTrue(entered.wait(2))
                second = send_run_desktop_notification(
                    run,
                    "completed",
                    system_name="Darwin",
                    execute=execute,
                )
                release.set()
                thread.join(2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(calls), 1)
            self.assertEqual(second["status"], "uncertain")
            self.assertEqual(result["first"]["status"], "delivered")
            final = load_desktop_notification_receipt(
                run.run_dir / "desktop-notification-completed.json"
            )
            self.assertEqual(final["status"], "delivered")

    def test_tampered_or_symlink_receipts_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tampered_run = self._create_run(root, "tampered-receipt")
            path = tampered_run.run_dir / "desktop-notification-completed.json"
            path.write_text('{"schema":"conductor.desktop_notification.v1","schema":"duplicate"}\n', encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_desktop_notification_receipt(path)

            symlink_run = self._create_run(root, "symlink-receipt")
            external = root / "external.json"
            external.write_text("{}\n", encoding="utf-8")
            symlink = symlink_run.run_dir / "desktop-notification-completed.json"
            symlink.symlink_to(external)
            with self.assertRaises(ValidationError):
                send_run_desktop_notification(symlink_run, "completed", system_name="Plan9")

    def test_non_finite_and_deeply_nested_receipts_fail_as_validation_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            non_finite = root / "non-finite.json"
            non_finite.write_text('{"returncode":NaN}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "not strict JSON"):
                load_desktop_notification_receipt(non_finite)

            nested = root / "nested.json"
            nested.write_text("[" * 1500 + "]" * 1500, encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_desktop_notification_receipt(nested)

    def test_invalid_run_status_is_rejected_before_artifact_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._create_run(Path(tmp))
            with self.assertRaises(ValidationError):
                send_run_desktop_notification(run, "running", system_name="Plan9")
            self.assertFalse(
                any(run.run_dir.glob(DESKTOP_NOTIFICATION_FILENAME_TEMPLATE % "*"))
            )

    def test_cross_field_inconsistencies_are_rejected(self):
        valid = {
            "schema": "conductor.desktop_notification.v1",
            "status": "delivered",
            "run_status": "completed",
            "backend": "macos-osascript",
            "content_policy": "fixed-status-only-v1",
            "requested_at_utc": "2026-07-11T12:00:00.000Z",
            "finished_at_utc": "2026-07-11T12:00:01.000Z",
            "returncode": 0,
            "error_class": "",
        }
        validate_desktop_notification_receipt(valid)
        mutations = [
            {"backend": "unavailable"},
            {"returncode": 1},
            {"error_class": "UnexpectedError"},
            {"requested_at_utc": "not-a-timestamp"},
            {"requested_at_utc": "2026-07-11T12:00:00Z"},
            {"finished_at_utc": "2026-07-11T11:59:59.000Z"},
            {"returncode": 999},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                candidate = dict(valid)
                candidate.update(mutation)
                with self.assertRaises(ValidationError):
                    validate_desktop_notification_receipt(candidate)

    def test_schema_registry_exposes_notification_receipt(self):
        exported = get_schema("desktop-notification")
        self.assertEqual(
            exported["properties"]["schema"]["const"],
            "conductor.desktop_notification.v1",
        )
        self.assertEqual(exported["properties"]["content_policy"]["const"], "fixed-status-only-v1")
        self.assertIn(
            "desktop-notification",
            {entry["name"] for entry in schema_types()},
        )

    def test_artifact_sidecar_is_private_exactly_once_and_status_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "goal-secret-name.json"
            _write_json(artifact, {"status": "verifier_failed"})
            calls = []

            def execute(command, **kwargs):
                calls.append(command)
                return SimpleNamespace(returncode=0)

            with patch("conductor_runtime.desktop_notification._usable_executable", return_value=True):
                first = send_artifact_desktop_notification(
                    artifact,
                    "verifier_failed",
                    system_name="Darwin",
                    execute=execute,
                )
                second = send_artifact_desktop_notification(
                    artifact,
                    "verifier_failed",
                    system_name="Darwin",
                    execute=execute,
                )

            receipt_dir = desktop_notification_directory_for_artifact(artifact)
            receipt_path = receipt_dir / "desktop-notification-failed.json"
            self.assertEqual(first, second)
            self.assertEqual(first["run_status"], "failed")
            self.assertEqual(len(calls), 1)
            self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
            serialized = receipt_path.read_text(encoding="utf-8")
            self.assertNotIn(artifact.name, serialized)
            self.assertNotIn(str(artifact), serialized)

    def test_artifact_sidecar_rejects_symlinks_and_nonterminal_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "goal.json"
            _write_json(artifact, {"status": "completed"})
            external = root / "external"
            external.mkdir()
            sidecar = Path(str(artifact) + ".notifications")
            sidecar.symlink_to(external, target_is_directory=True)
            with self.assertRaises(ValidationError):
                send_artifact_desktop_notification(artifact, "completed", system_name="Plan9")

        for status in ("planned", "running", "active", "routing"):
            with self.subTest(status=status), self.assertRaises(ValidationError):
                desktop_notification_status_for_execution(status)
        self.assertEqual(desktop_notification_status_for_execution("workflow_blocked"), "blocked")
        self.assertEqual(desktop_notification_status_for_execution("terminated"), "stopped")
        self.assertEqual(desktop_notification_status_for_execution("max_iterations_exhausted"), "failed")


class DesktopNotificationCliTests(unittest.TestCase):
    def _workflow_path(self, root: Path, steps) -> Path:
        path = root / "workflow.json"
        _write_json(path, _workflow(steps))
        return path

    def test_run_opt_in_notifies_completed_and_omission_has_zero_backend_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = self._workflow_path(
                root,
                [{"id": "record", "kind": "write_artifact", "output": "done.txt", "content": "done"}],
            )
            receipt = {"status": "delivered", "backend": "macos-osascript"}
            with patch("conductor_runtime.legacy_cli.send_run_desktop_notification", return_value=receipt) as notify:
                stdout = StringIO()
                with redirect_stdout(stdout):
                    code = cli_main(
                        [
                            "run",
                            str(workflow),
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--run-id",
                            "cli-notify",
                            "--desktop-notify",
                        ]
                    )
                self.assertEqual(code, 0)
                self.assertEqual(notify.call_count, 1)
                self.assertEqual(notify.call_args.args[1], "completed")
                self.assertIn("Desktop notification: delivered (macos-osascript)", stdout.getvalue())

            with patch("conductor_runtime.legacy_cli.send_run_desktop_notification") as notify:
                with redirect_stdout(StringIO()):
                    code = cli_main(
                        [
                            "run",
                            str(workflow),
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--run-id",
                            "cli-no-notify",
                        ]
                    )
                self.assertEqual(code, 0)
                notify.assert_not_called()

    def test_run_preserves_manual_gate_failure_and_notifies_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = self._workflow_path(
                root,
                [
                    {
                        "id": "approval",
                        "kind": "manual_gate",
                        "approval_id": "notification-approval",
                        "prompt": "Approve the test.",
                    }
                ],
            )
            receipt = {"status": "delivered", "backend": "macos-osascript"}
            stderr = StringIO()
            with patch("conductor_runtime.legacy_cli.send_run_desktop_notification", return_value=receipt) as notify:
                with redirect_stdout(StringIO()), redirect_stderr(stderr):
                    code = cli_main(
                        [
                            "run",
                            str(workflow),
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--run-id",
                            "cli-blocked",
                            "--desktop-notify",
                        ]
                    )
            self.assertEqual(code, 2)
            self.assertIn("requires --approve notification-approval", stderr.getvalue())
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(notify.call_args.args[1], "blocked")

    def test_notification_exception_does_not_change_successful_run_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = self._workflow_path(
                root,
                [{"id": "record", "kind": "write_artifact", "output": "done.txt", "content": "done"}],
            )
            stderr = StringIO()
            with patch(
                "conductor_runtime.legacy_cli.send_run_desktop_notification",
                side_effect=RuntimeError("backend detail must not escape"),
            ):
                with redirect_stdout(StringIO()), redirect_stderr(stderr):
                    code = cli_main(
                        [
                            "run",
                            str(workflow),
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--run-id",
                            "cli-fail-open",
                            "--desktop-notify",
                        ]
                    )
            self.assertEqual(code, 0)
            self.assertIn("WARNING: desktop notification unavailable: RuntimeError", stderr.getvalue())
            self.assertNotIn("backend detail", stderr.getvalue())

    def test_dry_run_rejects_desktop_notification_before_creating_a_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = self._workflow_path(
                root,
                [
                    {
                        "id": "record",
                        "kind": "write_artifact",
                        "output": "done.txt",
                        "content": "done",
                    }
                ],
            )
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = cli_main(
                    [
                        "run",
                        str(workflow),
                        "--workspace",
                        str(root),
                        "--runs-dir",
                        str(root / "runs"),
                        "--dry-run",
                        "--desktop-notify",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("requires real workflow execution", stderr.getvalue())
            self.assertFalse((root / "runs").exists())

    def test_validate_desktop_notification_cli_accepts_receipt_and_rejects_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = RunArtifacts.create(_workflow(), root / "runs", run_id="cli-validate")
            receipt = send_run_desktop_notification(run, "completed", system_name="Plan9")
            self.assertEqual(receipt["status"], "unavailable")
            path = run.run_dir / "desktop-notification-completed.json"
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = cli_main(["validate-desktop-notification", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("OK:", stdout.getvalue())

            tampered = json.loads(path.read_text(encoding="utf-8"))
            tampered["content_policy"] = "include-task-text"
            _write_json(path, tampered)
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = cli_main(["validate-desktop-notification", str(path)])
            self.assertEqual(code, 2)
            self.assertIn("content policy is invalid", stderr.getvalue())

    def test_validate_desktop_notification_is_classified_read_only(self):
        assessment = assess_command(
            [
                "python3",
                "-B",
                "-m",
                "conductor_runtime",
                "validate-desktop-notification",
                "receipt.json",
            ]
        )
        self.assertFalse(assessment.writes)
        self.assertFalse(assessment.network)
        self.assertFalse(assessment.destructive)

    def test_start_background_run_propagates_explicit_opt_in_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = self._workflow_path(
                root,
                [{"id": "record", "kind": "write_artifact", "output": "done.txt", "content": "done"}],
            )
            captured = []

            def fake_start(**kwargs):
                captured.append(kwargs)
                return SimpleNamespace(
                    run_dir=root / "runs" / "background-run",
                    pid=12345,
                    metadata={"status": "active"},
                )

            with patch("conductor_runtime.legacy_cli.start_background_run", side_effect=fake_start):
                with redirect_stdout(StringIO()):
                    enabled = cli_main(
                        [
                            "start-background-run",
                            str(workflow),
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--desktop-notify",
                            "--approve",
                            "ephemeral-test-approval",
                        ]
                    )
                    omitted = cli_main(
                        [
                            "start-background-run",
                            str(workflow),
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                        ]
                    )
            self.assertEqual(enabled, 0)
            self.assertEqual(omitted, 0)
            self.assertIs(captured[0]["desktop_notify"], True)
            self.assertIs(captured[1]["desktop_notify"], False)
            self.assertNotIn("ephemeral-test-approval", str(captured[0].get("self_command", "")))

    def test_saved_workflow_opt_in_notifies_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".claude" / "workflows" / "notify.js"
            write_saved_workflow_script(
                _workflow(
                    [
                        {
                            "id": "record",
                            "kind": "write_artifact",
                            "output": "done.txt",
                            "content": "done",
                        }
                    ]
                ),
                script,
                "notify",
            )
            receipt = {"status": "delivered", "backend": "macos-osascript"}
            with patch("conductor_runtime.legacy_cli.send_run_desktop_notification", return_value=receipt) as notify:
                with redirect_stdout(StringIO()):
                    code = cli_main(
                        [
                            "run-saved-workflow",
                            "notify",
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--run-id",
                            "saved-notify",
                            "--desktop-notify",
                        ]
                    )
            self.assertEqual(code, 0)
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(notify.call_args.args[1], "completed")

    def test_goal_and_model_goal_foreground_notify_from_goal_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = self._workflow_path(
                root,
                [
                    {
                        "id": "record",
                        "kind": "write_artifact",
                        "output": "done.txt",
                        "content": "done",
                    }
                ],
            )
            goal_path = root / "goals" / "goal-notify.json"
            goal_path.parent.mkdir()
            _write_json(goal_path, {"status": "completed"})
            receipt = {"status": "delivered", "backend": "macos-osascript"}
            fixed_result = SimpleNamespace(
                goal_id="goal-notify",
                status="completed",
                iterations=1,
                goal_path=goal_path,
                run_dirs=[],
            )
            with patch("conductor_runtime.legacy_cli.run_goal_loop", return_value=fixed_result), patch(
                "conductor_runtime.legacy_cli.send_artifact_desktop_notification",
                return_value=receipt,
            ) as notify:
                with redirect_stdout(StringIO()):
                    code = cli_main(
                        [
                            "run-goal",
                            str(workflow),
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--goals-dir",
                            str(root / "goals"),
                            "--check-command-json",
                            '["true"]',
                            "--desktop-notify",
                        ]
                    )
            self.assertEqual(code, 0)
            notify.assert_called_once_with(goal_path, "completed")

            model_result = SimpleNamespace(
                goal_id="goal-notify",
                status="verifier_failed",
                iterations=1,
                goal_path=goal_path,
                run_dirs=[],
            )
            with patch("conductor_runtime.legacy_cli.run_model_goal_loop", return_value=model_result), patch(
                "conductor_runtime.legacy_cli.send_artifact_desktop_notification",
                return_value=receipt,
            ) as notify:
                with redirect_stdout(StringIO()):
                    code = cli_main(
                        [
                            "run-model-goal",
                            "--task",
                            "bounded task",
                            "--check-command-json",
                            '["true"]',
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--goals-dir",
                            str(root / "goals"),
                            "--desktop-notify",
                        ]
                    )
            self.assertEqual(code, 1)
            notify.assert_called_once_with(goal_path, "verifier_failed")

    def test_model_workflow_propagates_notification_and_dry_run_rejects_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = RunArtifacts.create(_workflow(), root / "runs", run_id="model-wrapper")
            run.mark_run_status("completed")
            run_receipt = root / "model.run-receipt.json"
            _write_json(run_receipt, {"status": "completed"})
            result = SimpleNamespace(
                draft=None,
                receipt_path=run_receipt,
                run_dir=run.run_dir,
                background_pid=None,
                status="completed",
                error_class="",
            )
            receipt = {"status": "delivered", "backend": "macos-osascript"}
            with patch("conductor_runtime.legacy_cli.run_model_workflow", return_value=result) as execute, patch(
                "conductor_runtime.legacy_cli.send_run_desktop_notification",
                return_value=receipt,
            ) as notify:
                with redirect_stdout(StringIO()):
                    code = cli_main(
                        [
                            "run-model-workflow",
                            "--task",
                            "bounded task",
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--desktop-notify",
                        ]
                    )
            self.assertEqual(code, 0)
            self.assertIs(execute.call_args.kwargs["desktop_notify"], True)
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(notify.call_args.args[1], "completed")

            with patch("conductor_runtime.legacy_cli.run_model_workflow") as execute:
                stderr = StringIO()
                with redirect_stderr(stderr):
                    code = cli_main(
                        [
                            "run-model-workflow",
                            "--task",
                            "bounded task",
                            "--dry-run",
                            "--desktop-notify",
                        ]
                    )
            self.assertEqual(code, 2)
            execute.assert_not_called()
            self.assertIn("requires real model workflow execution", stderr.getvalue())

    def test_model_workflow_background_handoff_receives_only_boolean_opt_in(self):
        generated = _workflow(
            [
                {
                    "id": "record",
                    "kind": "write_artifact",
                    "output": "done.txt",
                    "content": "done",
                }
            ]
        )

        def fake_planner(argv, cwd, timeout, input_text=None, output_limit_bytes=None):
            output_path = Path(argv[argv.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(generated), encoding="utf-8")
            return ProcessResult(0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(
                "conductor_runtime.model_planner.run_process",
                side_effect=fake_planner,
            ), patch(
                "conductor_runtime.model_orchestrator.start_prepared_background_run",
                return_value=SimpleNamespace(pid=12345),
            ) as start:
                result = run_model_workflow(
                    task="bounded task",
                    workspace=root,
                    output_path=root / "generated.json",
                    planning_receipt_path=None,
                    run_receipt_path=None,
                    runs_dir=root / "runs",
                    policy=RuntimePolicy(
                        allow_agent=True,
                        approvals={MODEL_WORKFLOW_EXECUTE_APPROVAL},
                    ),
                    background=True,
                    desktop_notify=True,
                )
            self.assertEqual(result.status, "running")
            self.assertIs(start.call_args.kwargs["desktop_notify"], True)
            persisted = json.dumps(result.receipt, sort_keys=True)
            self.assertNotIn("Workflow completed.", persisted)

    def test_reviewed_model_workflow_cli_propagates_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = RunArtifacts.create(_workflow(), root / "runs", run_id="reviewed-wrapper")
            run.mark_run_status("completed")
            draft = SimpleNamespace(
                output_path=root / "draft.json",
                receipt_path=root / "planning.json",
                receipt={"workspace": {"mode": "bound"}},
            )
            result = SimpleNamespace(
                draft=draft,
                receipt_path=root / "reviewed-run.json",
                run_dir=run.run_dir,
                background_pid=None,
                status="completed",
                error_class="",
            )
            receipt = {"status": "delivered", "backend": "macos-osascript"}
            with patch(
                "conductor_runtime.legacy_cli.run_reviewed_model_workflow",
                return_value=result,
            ) as execute, patch(
                "conductor_runtime.legacy_cli.send_run_desktop_notification",
                return_value=receipt,
            ) as notify:
                with redirect_stdout(StringIO()):
                    code = cli_main(
                        [
                            "run-reviewed-model-workflow",
                            str(draft.output_path),
                            str(draft.receipt_path),
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--desktop-notify",
                        ]
                    )
            self.assertEqual(code, 0)
            self.assertIs(execute.call_args.kwargs["desktop_notify"], True)
            self.assertEqual(notify.call_count, 1)

    def test_background_model_goal_and_auto_propagate_boolean_without_parent_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goal_path = root / "goals" / "background-notify.json"
            metadata_path = root / "goals" / "background-workers" / "worker.json"
            background_result = SimpleNamespace(
                goal_id="background-notify",
                goal_path=goal_path,
                metadata_path=metadata_path,
                metadata={"status": "active"},
                pid=12345,
            )
            with patch(
                "conductor_runtime.legacy_cli.start_background_model_goal",
                return_value=background_result,
            ) as start, patch("conductor_runtime.legacy_cli.send_artifact_desktop_notification") as notify:
                with redirect_stdout(StringIO()):
                    code = cli_main(
                        [
                            "run-model-goal",
                            "--task",
                            "bounded task",
                            "--check-command-json",
                            '["true"]',
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--goals-dir",
                            str(root / "goals"),
                            "--background",
                            "--desktop-notify",
                        ]
                    )
            self.assertEqual(code, 0)
            self.assertIs(start.call_args.kwargs["desktop_notify"], True)
            notify.assert_not_called()

            goal_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(goal_path, {"status": "completed"})
            auto_receipt = root / "auto.json"
            _write_json(auto_receipt, {"status": "completed"})
            auto_result = SimpleNamespace(
                route="adaptive-direct-goal",
                execution_mode="foreground",
                status="completed",
                receipt_path=auto_receipt,
                artifact_paths=(goal_path,),
                run_dir=None,
                goal_path=goal_path,
                background_pid=None,
                error_class="",
                error_message="",
            )
            receipt = {"status": "delivered", "backend": "macos-osascript"}
            with patch("conductor_runtime.legacy_cli.run_auto_orchestration", return_value=auto_result) as execute, patch(
                "conductor_runtime.legacy_cli.send_artifact_desktop_notification",
                return_value=receipt,
            ) as notify:
                with redirect_stdout(StringIO()):
                    code = cli_main(
                        [
                            "auto",
                            "--task",
                            "bounded task",
                            "--check-command-json",
                            '["true"]',
                            "--desktop-notify",
                        ]
                    )
            self.assertEqual(code, 0)
            self.assertIs(execute.call_args.kwargs["desktop_notify"], True)
            notify.assert_called_once_with(goal_path, "completed")

    def test_auto_plan_only_notification_is_rejected_before_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValidationError, "require real automatic execution"):
                run_auto_orchestration(
                    task="bounded task",
                    workspace=root,
                    runs_dir=root / "runs",
                    goals_dir=root / "goals",
                    policy=RuntimePolicy(),
                    strategy="direct",
                    plan_only=True,
                    desktop_notify=True,
                )
            self.assertFalse((root / "runs").exists())
            self.assertFalse((root / "goals").exists())

    def test_auto_background_adaptive_route_propagates_notification_to_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_result = {
                "status": "running",
                "artifacts": [],
                "artifact_paths": [],
                "execution_started": True,
                "iterations": 0,
                "run_count": 0,
                "run_dir": None,
                "goal_path": root / "goals" / "auto-goal.json",
                "background_pid": 12345,
                "error_class": "",
            }
            with patch(
                "conductor_runtime.auto_orchestrator._run_auto_goal",
                return_value=child_result,
            ) as run_goal:
                result = run_auto_orchestration(
                    task="bounded task",
                    workspace=root,
                    runs_dir=root / "runs",
                    goals_dir=root / "goals",
                    policy=RuntimePolicy(
                        allow_agent=True,
                        approvals={MODEL_WORKFLOW_EXECUTE_APPROVAL},
                    ),
                    strategy="direct",
                    background=True,
                    check_command=["true"],
                    desktop_notify=True,
                )
            self.assertEqual(result.status, "running")
            self.assertIs(run_goal.call_args.kwargs["desktop_notify"], True)

    def test_routine_manifest_persists_only_flag_and_service_path_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = self._workflow_path(
                root,
                [
                    {
                        "id": "record",
                        "kind": "write_artifact",
                        "output": "done.txt",
                        "content": "done",
                    }
                ],
            )
            manifest_path = root / "routine.json"
            with redirect_stdout(StringIO()):
                code = cli_main(
                    [
                        "write-routine-manifest",
                        str(manifest_path),
                        "--name",
                        "notify-routine",
                        "--workflow",
                        str(workflow),
                        "--workspace",
                        str(root),
                        "--runs-dir",
                        str(root / "runs"),
                        "--manual",
                        "--desktop-notify",
                    ]
                )
            self.assertEqual(code, 0)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            argv = manifest["launch"]["command_argv"]
            self.assertEqual(argv.count("--desktop-notify"), 1)
            self.assertNotIn("--approve", argv)
            self.assertNotIn("Workflow completed.", json.dumps(manifest))
            self.assertIs(routine_list_record(manifest_path, manifest)["desktop_notify"], True)

            for replacement in (
                ["--desktop-notify", "--desktop-notify"],
                ["--desktop-notify=true"],
            ):
                with self.subTest(replacement=replacement):
                    tampered = json.loads(json.dumps(manifest))
                    command = [
                        value
                        for value in tampered["launch"]["command_argv"]
                        if value != "--desktop-notify"
                    ] + replacement
                    tampered["launch"]["command_argv"] = command
                    tampered["launch"]["command"] = shlex.join(command)
                    tampered["external_supervisor_contract"]["may_run_command_argv"] = command
                    with self.assertRaisesRegex(ValidationError, "invalid desktop notification flag"):
                        validate_routine_manifest(tampered)

            class FakeRunner:
                def __init__(self, *, base_run_dir, **kwargs):
                    self.run = RunArtifacts.create(
                        _workflow(),
                        base_run_dir,
                        run_id="routine-service-notify",
                    )

                def execute(self):
                    self.run.mark_run_status("completed")
                    return self.run

            with patch("conductor_runtime.routine_supervisor.WorkflowRunner", FakeRunner), patch(
                "conductor_runtime.routine_supervisor.send_run_desktop_notification",
                return_value={"status": "delivered", "backend": "macos-osascript"},
            ) as notify:
                result = routine_supervisor._execute_routine_in_process(
                    manifest=manifest,
                    workflow=_workflow(),
                    workflow_approvals=[],
                    timeout=10,
                )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(notify.call_args.args[1], "completed")


class DesktopNotificationBackgroundWorkerTests(unittest.TestCase):
    def test_model_goal_worker_argv_allows_only_one_final_notification_flag(self):
        template = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "benchmark-suites"
                / "background-model-goal-template.json"
            ).read_text(encoding="utf-8")
        )
        enabled = json.loads(json.dumps(template))
        enabled["worker"]["argv"].append("--desktop-notify")
        background_goal.validate_background_model_goal(enabled)

        misplaced = json.loads(json.dumps(enabled))
        misplaced["worker"]["argv"].append("unexpected")
        with self.assertRaisesRegex(ValidationError, "invalid desktop notification flag"):
            background_goal.validate_background_model_goal(misplaced)

        duplicate = json.loads(json.dumps(enabled))
        duplicate["worker"]["argv"].append("--desktop-notify")
        with self.assertRaisesRegex(ValidationError, "invalid desktop notification flag"):
            background_goal.validate_background_model_goal(duplicate)

        equals_form = json.loads(json.dumps(template))
        equals_form["worker"]["argv"].append("--desktop-notify=true")
        with self.assertRaisesRegex(ValidationError, "invalid desktop notification flag"):
            background_goal.validate_background_model_goal(equals_form)

    def test_internal_model_goal_worker_dispatch_propagates_flag(self):
        with patch("conductor_runtime.legacy_cli.run_background_model_goal_worker", return_value=0) as worker:
            code = cli_main(
                [
                    "_background-model-goal-worker",
                    "/external/goals/worker.json",
                    "--desktop-notify",
                ]
            )
        self.assertEqual(code, 0)
        worker.assert_called_once_with(
            Path("/external/goals/worker.json"),
            desktop_notify=True,
        )

    def test_model_goal_worker_notifies_after_terminal_metadata_without_changing_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata_path = root / "goals" / "background-workers" / "worker.json"
            metadata_path.parent.mkdir(parents=True)
            _write_json(metadata_path, {"placeholder": True})
            metadata = {
                "status": "active",
                "goal": {"artifact": "goal.json"},
                "worker": {"argv": ["runtime", "--desktop-notify"], "ready": False},
            }
            payload = {
                "task": "bounded task",
                "workspace": str(root),
                "runs_dir": str(root / "runs"),
                "goals_dir": str(root / "goals"),
                "goal_id": "goal",
                "resume_goal": None,
                "verifier": {"command": ["true"], "prompt": None},
                "config": {
                    "check_model": None,
                    "planner_model": None,
                    "planner_effort": "high",
                    "name": None,
                    "write_capable": False,
                    "max_iterations": 1,
                    "max_workers": 1,
                    "max_items": 1,
                    "max_steps": 1,
                    "planning_timeout_seconds": 10,
                    "planning_output_limit_bytes": 1024,
                    "check_timeout_seconds": 10,
                    "check_output_limit_bytes": 1024,
                    "reuse_planner_session": True,
                    "direct_plan": True,
                    "progressive_plan": False,
                },
            }
            result = SimpleNamespace(status="completed", iterations=1)
            receipt = {"status": "delivered", "backend": "macos-osascript"}
            with ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "conductor_runtime.background_goal.load_background_model_goal",
                        return_value=metadata,
                    )
                )
                stack.enter_context(
                    patch("conductor_runtime.background_goal._read_payload", return_value=payload)
                )
                stack.enter_context(patch("conductor_runtime.background_goal._validate_worker_identity"))
                stack.enter_context(patch("conductor_runtime.background_goal._validate_payload_matches_metadata"))
                stack.enter_context(
                    patch(
                        "conductor_runtime.background_goal._policy_from_payload",
                        return_value=RuntimePolicy(),
                    )
                )
                stack.enter_context(
                    patch("conductor_runtime.background_goal._heartbeat_loop", return_value=None)
                )
                stack.enter_context(
                    patch(
                        "conductor_runtime.background_goal.run_model_goal_loop",
                        return_value=result,
                    )
                )
                stack.enter_context(patch("conductor_runtime.background_goal._replace_metadata"))
                notify = stack.enter_context(
                    patch(
                        "conductor_runtime.background_goal.send_artifact_desktop_notification",
                        return_value=receipt,
                    )
                )
                stdout = StringIO()
                with redirect_stdout(stdout):
                    code = background_goal.run_background_model_goal_worker(
                        metadata_path,
                        BytesIO(),
                        desktop_notify=True,
                    )
            self.assertEqual(code, 0)
            notify.assert_called_once_with(metadata_path, "completed")
            self.assertIn("Desktop notification: delivered (macos-osascript)", stdout.getvalue())

    def test_worker_command_records_flag_without_approval_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "worker-run"
            base = ["/usr/bin/python3", "/opt/conductor-runtime.pyz"]
            enabled = background_run._background_worker_command(run_dir, base, True)
            omitted = background_run._background_worker_command(run_dir, base, False)
            self.assertEqual(enabled[-1], "--desktop-notify")
            self.assertNotIn("--desktop-notify", omitted)
            self.assertNotIn("--approve", enabled)
            self.assertNotIn("approval-secret", enabled)
            with self.assertRaises(ValidationError):
                background_run._background_worker_command(run_dir, base, "yes")
            with self.assertRaisesRegex(ValidationError, "must not persist approval values"):
                background_run._background_worker_command(
                    run_dir,
                    ["/usr/bin/python3", "--approve=approval-secret"],
                    True,
                )

    def test_background_metadata_rejects_equals_form_approval_persistence(self):
        template = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "benchmark-suites"
                / "background-run-template"
                / "background.json"
            ).read_text(encoding="utf-8")
        )
        template["worker"]["argv"].append("--approve=approval-secret")
        with self.assertRaisesRegex(ValidationError, "must not persist approval values"):
            background_run.validate_background_run(template)

    def _run_worker(
        self,
        root: Path,
        *,
        requested: bool,
        notification_side_effect=None,
        completion_receipt=None,
        completion_side_effect=None,
    ):
        run = RunArtifacts.create(_workflow(), root / "runs", run_id="worker-finalize")
        payload = {
            "workspace": str(root),
            "runs_dir": str(root / "runs"),
            "max_workers": 1,
            "completion_receipt": completion_receipt,
        }
        metadata = {}

        class FakeRunner:
            def __init__(self, *, resume_dir, **kwargs):
                self.run = RunArtifacts.resume(resume_dir)

            def execute(self):
                self.run.mark_run_status("completed")
                return self.run

        with ExitStack() as stack:
            stack.enter_context(patch("conductor_runtime.background_run.load_background_run", return_value=metadata))
            stack.enter_context(patch("conductor_runtime.background_run._read_launch_payload", return_value=payload))
            stack.enter_context(patch("conductor_runtime.background_run._validate_worker_identity"))
            stack.enter_context(patch("conductor_runtime.background_run._validate_payload_matches_metadata"))
            stack.enter_context(
                patch("conductor_runtime.background_run._policy_from_payload", return_value=RuntimePolicy())
            )
            stack.enter_context(patch("conductor_runtime.background_run.load_workflow", return_value=_workflow()))
            stack.enter_context(patch("conductor_runtime.background_run.WorkflowRunner", FakeRunner))
            stack.enter_context(patch("conductor_runtime.background_run._record_background_state"))
            stack.enter_context(patch("conductor_runtime.background_run._write_background_metadata"))
            stack.enter_context(
                patch(
                    "conductor_runtime.background_run._finalize_completion_receipt",
                    side_effect=completion_side_effect,
                )
            )
            notifier = stack.enter_context(
                patch(
                    "conductor_runtime.background_run.send_run_desktop_notification",
                    side_effect=notification_side_effect,
                    return_value={"status": "delivered", "backend": "macos-osascript"},
                )
            )
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = background_run.run_background_worker(
                    run.run_dir,
                    BytesIO(),
                    desktop_notify=requested,
                )
        return code, metadata, notifier, stdout.getvalue(), stderr.getvalue()

    def test_worker_notifies_completed_once_and_omission_has_no_backend_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, metadata, notifier, stdout, _stderr = self._run_worker(root, requested=True)
            self.assertEqual(code, 0)
            self.assertEqual(metadata["run_status"], "completed")
            self.assertEqual(metadata["exit_code"], 0)
            self.assertEqual(notifier.call_count, 1)
            self.assertEqual(notifier.call_args.args[1], "completed")
            self.assertIn("Desktop notification: delivered (macos-osascript)", stdout)

        with tempfile.TemporaryDirectory() as tmp:
            code, metadata, notifier, _stdout, _stderr = self._run_worker(Path(tmp), requested=False)
            self.assertEqual(code, 0)
            self.assertEqual(metadata["run_status"], "completed")
            notifier.assert_not_called()

    def test_worker_notification_exception_preserves_completed_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, metadata, notifier, _stdout, stderr = self._run_worker(
                Path(tmp),
                requested=True,
                notification_side_effect=RuntimeError("private backend detail"),
            )
            self.assertEqual(code, 0)
            self.assertEqual(metadata["run_status"], "completed")
            self.assertEqual(metadata["exit_code"], 0)
            self.assertEqual(notifier.call_count, 1)
            self.assertIn("WARNING: desktop notification unavailable: RuntimeError", stderr)
            self.assertNotIn("private backend detail", stderr)

    def test_worker_wrapper_finalization_failure_notifies_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, metadata, notifier, _stdout, stderr = self._run_worker(
                Path(tmp),
                requested=True,
                completion_receipt={"kind": "test-receipt"},
                completion_side_effect=ValidationError("finalization failed"),
            )
            self.assertEqual(code, 1)
            self.assertEqual(metadata["run_status"], "completed")
            self.assertEqual(metadata["exit_code"], 1)
            self.assertEqual(metadata["error_class"], "CompletionReceiptFinalizationError")
            self.assertEqual(notifier.call_args.args[1], "failed")
            self.assertIn("completion receipt finalization failed: ValidationError", stderr)


if __name__ == "__main__":
    unittest.main()
