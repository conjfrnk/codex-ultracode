import json
import hashlib
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from conductor_extras.cli import main as cli_main
from conductor_runtime.errors import ValidationError
from conductor_extras.runtime.routine_supervisor import (
    load_routine_controls,
    load_supervisor_state,
    routine_due_slot,
    run_due_routines,
)
from conductor_extras.runtime.routine_service import (
    ROUTINE_SERVICE_INSTALL_APPROVAL,
    build_routine_service_grant,
    install_routine_service,
    load_routine_service_state,
    routine_service_paths,
    run_routine_service_worker,
)
from conductor_extras.runtime.routines import (
    CRON_EXPRESSION_SHAPE_PATTERN,
    MAX_CRON_EXPRESSION_CHARS,
    build_schedule,
    load_routine_manifest,
    parse_cron_expression,
    routine_list_record,
    validate_schedule,
)
from conductor_extras.runtime.schemas import get_schema
from conductor_extras.runtime.security import RuntimePolicy


def _workflow():
    return {
        "schema": "conductor.workflow.v1",
        "name": "cron-test",
        "description": "Cron routine test workflow.",
        "mode": "read_only",
        "max_workers": 1,
        "steps": [
            {
                "id": "check",
                "kind": "shell",
                "risk": "low",
                "writes": False,
                "command": ["true"],
            }
        ],
    }


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _manifest(expression, *, created="2026-07-01T00:00:00.000Z", zone="UTC"):
    return {
        "created_at_utc": created,
        "schedule": build_schedule(cron=expression, timezone=zone),
    }


class CronExpressionTests(unittest.TestCase):
    def test_parser_canonicalizes_numeric_spelling_and_expands_fields(self):
        parsed = parse_cron_expression("  */15\t09-17/2 01,15 * 1-5  ")

        self.assertEqual(parsed["expression"], "*/15 9-17/2 1,15 * 1-5")
        minute, hour, day_of_month, month, day_of_week = parsed["fields"]
        self.assertEqual(minute, frozenset({0, 15, 30, 45}))
        self.assertEqual(hour, frozenset({9, 11, 13, 15, 17}))
        self.assertEqual(day_of_month, frozenset({1, 15}))
        self.assertEqual(month, frozenset(range(1, 13)))
        self.assertEqual(day_of_week, frozenset({1, 2, 3, 4, 5}))
        self.assertFalse(parsed["day_of_month_wildcard"])
        self.assertFalse(parsed["day_of_week_wildcard"])

    def test_numeric_start_steps_and_sunday_alias_are_supported(self):
        parsed = parse_cron_expression("05/20 * * * 7")

        self.assertEqual(parsed["expression"], "5/20 * * * 7")
        self.assertEqual(parsed["fields"][0], frozenset({5, 25, 45}))
        self.assertEqual(parsed["fields"][4], frozenset({0}))
        self.assertEqual(
            build_schedule(cron="05/20 * * * 7", timezone="America/Los_Angeles"),
            {
                "kind": "cron",
                "expression": "5/20 * * * 7",
                "timezone": "America/Los_Angeles",
            },
        )

    def test_parser_rejects_unsupported_or_malformed_expressions(self):
        invalid = [
            "",
            "* * * *",
            "* * * * * *",
            "* * * * MON",
            "? * * * *",
            "* * L * *",
            "* * 1W * *",
            "60 * * * *",
            "* 24 * * *",
            "* * 0 * *",
            "* * * 13 *",
            "* * * * 8",
            "10-5 * * * *",
            "1,,2 * * * *",
            "*/0 * * * *",
            "*/61 * * * *",
            "1/2/3 * * * *",
            "* * * * -",
            "\uff11 * * * *",
            "0" * (MAX_CRON_EXPRESSION_CHARS + 1),
        ]

        for expression in invalid:
            with self.subTest(expression=expression), self.assertRaises(ValidationError):
                parse_cron_expression(expression)

    def test_stored_schedule_must_be_exact_and_canonical(self):
        validate_schedule({"kind": "cron", "expression": "0 9 * * 1-5", "timezone": "UTC"})

        with self.assertRaisesRegex(ValidationError, "canonical"):
            validate_schedule({"kind": "cron", "expression": "00 09 * * 1-5", "timezone": "UTC"})
        with self.assertRaisesRegex(ValidationError, "must contain"):
            validate_schedule(
                {
                    "kind": "cron",
                    "expression": "0 9 * * 1-5",
                    "timezone": "UTC",
                    "unexpected": True,
                }
            )
        with self.assertRaisesRegex(ValidationError, "exactly one schedule"):
            build_schedule(manual=True, cron="* * * * *")

    def test_public_schema_has_strict_cron_shape(self):
        manifest_schema = get_schema("routine-manifest")
        schedule_schema = manifest_schema["properties"]["schedule"]
        cron = next(item for item in schedule_schema["oneOf"] if item["properties"]["kind"]["const"] == "cron")

        self.assertEqual(cron["required"], ["kind", "expression", "timezone"])
        self.assertFalse(cron["additionalProperties"])
        self.assertEqual(cron["properties"]["expression"]["maxLength"], MAX_CRON_EXPRESSION_CHARS)
        self.assertEqual(cron["properties"]["expression"]["pattern"], CRON_EXPRESSION_SHAPE_PATTERN)
        launch = manifest_schema["properties"]["launch"]
        self.assertIn("runtime_sha256", launch["properties"])
        self.assertNotIn("runtime_sha256", launch["required"])


class CronDueSlotTests(unittest.TestCase):
    def test_latest_slot_replay_and_creation_boundary(self):
        manifest = _manifest("*/15 * * * *")
        now = datetime(2026, 7, 12, 12, 37, 59, tzinfo=timezone.utc)

        self.assertEqual(routine_due_slot(manifest, {}, now), datetime(2026, 7, 12, 12, 30, tzinfo=timezone.utc))
        self.assertIsNone(
            routine_due_slot(
                manifest,
                {"last_scheduled_for_utc": "2026-07-12T12:30:00.000Z"},
                now,
            )
        )
        self.assertEqual(
            routine_due_slot(
                manifest,
                {"last_scheduled_for_utc": "2026-07-12T12:30:00.000Z"},
                datetime(2026, 7, 12, 13, 1, tzinfo=timezone.utc),
            ),
            datetime(2026, 7, 12, 13, 0, tzinfo=timezone.utc),
        )

        created_after_slot = _manifest("*/15 * * * *", created="2026-07-12T12:31:00.000Z")
        self.assertIsNone(routine_due_slot(created_after_slot, {}, datetime(2026, 7, 12, 12, 40, tzinfo=timezone.utc)))

    def test_day_of_month_and_weekday_use_vixie_or_semantics(self):
        either = _manifest("0 9 15 * 1")
        self.assertEqual(
            routine_due_slot(either, {}, datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)),
            datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            routine_due_slot(either, {}, datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)),
            datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc),
        )

        monday_only = _manifest("0 9 * * 1")
        self.assertEqual(
            routine_due_slot(monday_only, {}, datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)),
            datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc),
        )
        fifteenth_only = _manifest("0 9 15 * *")
        self.assertEqual(
            routine_due_slot(fifteenth_only, {}, datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)),
            datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc),
        )

        stepped_wildcard = _manifest("0 9 */2 * 1")
        self.assertEqual(
            routine_due_slot(stepped_wildcard, {}, datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)),
            datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc),
        )

    def test_sunday_alias_timezone_and_catchup_bound(self):
        sunday = _manifest("0 9 * * 7")
        self.assertEqual(
            routine_due_slot(sunday, {}, datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc)),
            datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc),
        )

        los_angeles = _manifest("0 9 * * *", zone="America/Los_Angeles")
        self.assertEqual(
            routine_due_slot(los_angeles, {}, datetime(2026, 7, 12, 17, 0, tzinfo=timezone.utc)),
            datetime(2026, 7, 12, 16, 0, tzinfo=timezone.utc),
        )

        outside_catchup = _manifest("0 9 1 * *", created="2026-01-01T00:00:00.000Z")
        self.assertIsNone(
            routine_due_slot(outside_catchup, {}, datetime(2026, 7, 12, 17, 0, tzinfo=timezone.utc))
        )

    def test_dst_spring_gap_is_skipped_and_fall_duplicate_runs_twice(self):
        try:
            ZoneInfo("America/Los_Angeles")
        except ZoneInfoNotFoundError:
            self.skipTest("America/Los_Angeles timezone data is unavailable")

        spring = _manifest(
            "30 2 * * *",
            created="2026-03-01T00:00:00.000Z",
            zone="America/Los_Angeles",
        )
        self.assertEqual(
            routine_due_slot(spring, {}, datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc)),
            datetime(2026, 3, 7, 10, 30, tzinfo=timezone.utc),
        )

        fall = _manifest(
            "30 1 * * *",
            created="2026-10-25T00:00:00.000Z",
            zone="America/Los_Angeles",
        )
        self.assertEqual(
            routine_due_slot(fall, {}, datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)),
            datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            routine_due_slot(
                fall,
                {"last_scheduled_for_utc": "2026-11-01T08:30:00.000Z"},
                datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc),
            ),
            datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc),
        )


class CronRoutineIntegrationTests(unittest.TestCase):
    def test_packaged_runtime_manifest_self_invokes_and_binds_launch_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_path = root / "workflow.json"
            runtime_path = root / "conductor-runtime.pyz"
            manifest_path = root / "routines" / "packaged-cron.json"
            _write_json(workflow_path, _workflow())
            runtime_path.write_bytes(b"#!/usr/bin/env python3\nPK\x03\x04packaged-runtime\n")

            with patch("conductor_extras.runtime.background_run.sys.argv", [str(runtime_path)]), redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "write-routine-manifest",
                            str(manifest_path),
                            "--name",
                            "packaged-cron",
                            "--workflow",
                            str(workflow_path),
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--cron",
                            "* * * * *",
                            "--allow-writes",
                            "--max-workers",
                            "2",
                        ]
                    ),
                    0,
                )

            manifest = load_routine_manifest(manifest_path)
            argv = manifest["launch"]["command_argv"]
            self.assertEqual(
                argv[:4],
                [sys.executable, str(runtime_path.resolve()), "run", str(workflow_path.absolute())],
            )
            self.assertIn("--allow-writes", argv)
            self.assertNotIn("--allow-destructive", argv)
            self.assertEqual(
                manifest["launch"]["runtime_sha256"],
                hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
            )

            cases = []
            tampered = json.loads(json.dumps(manifest))
            tampered["launch"]["command_argv"].append("--allow-destructive")
            cases.append((tampered, "allow_destructive must match launch policy"))

            tampered = json.loads(json.dumps(manifest))
            target_index = tampered["launch"]["command_argv"].index("run") + 1
            tampered["launch"]["command_argv"][target_index] = str(root / "different.json")
            cases.append((tampered, "workflow must match target.workflow_path"))

            tampered = json.loads(json.dumps(manifest))
            tampered["launch"]["command_argv"][1] = "relative-runtime.pyz"
            cases.append((tampered, "absolute packaged .pyz"))

            for index, (tampered, expected) in enumerate(cases):
                tampered["launch"]["command"] = shlex.join(tampered["launch"]["command_argv"])
                tampered["external_supervisor_contract"]["may_run_command_argv"] = list(
                    tampered["launch"]["command_argv"]
                )
                path = root / ("tampered-packaged-%d.json" % index)
                _write_json(path, tampered)
                with self.assertRaisesRegex(ValidationError, expected):
                    load_routine_manifest(path)

            runtime_path.write_bytes(runtime_path.read_bytes() + b"drift")
            with self.assertRaisesRegex(ValidationError, "packaged runtime changed"):
                load_routine_manifest(manifest_path)

    def test_cli_write_list_status_and_approved_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_path = root / "workflow.json"
            manifest_path = root / "routines" / "cron-check.json"
            _write_json(workflow_path, _workflow())

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "write-routine-manifest",
                            str(manifest_path),
                            "--name",
                            "cron-check",
                            "--workflow",
                            str(workflow_path),
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--cron",
                            "*/20 08-18 * * 1-5",
                            "--timezone",
                            "America/Los_Angeles",
                        ]
                    ),
                    0,
                )

            manifest = load_routine_manifest(manifest_path)
            self.assertEqual(
                manifest["schedule"],
                {
                    "kind": "cron",
                    "expression": "*/20 8-18 * * 1-5",
                    "timezone": "America/Los_Angeles",
                },
            )
            self.assertEqual(
                routine_list_record(manifest_path, manifest)["schedule"],
                "cron */20 8-18 * * 1-5 America/Los_Angeles",
            )

            status_stdout = StringIO()
            with redirect_stdout(status_stdout):
                self.assertEqual(cli_main(["status-routine", str(manifest_path)]), 0)
            status = json.loads(status_stdout.getvalue())
            self.assertEqual(status["manifest"]["schedule"], "cron */20 8-18 * * 1-5 America/Los_Angeles")

            stderr = StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(
                    cli_main(["update-routine-schedule", str(manifest_path), "--cron", "0 9 * * 1-5"]),
                    2,
                )
            self.assertIn("routine:cron-check", stderr.getvalue())

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "update-routine-schedule",
                            str(manifest_path),
                            "--cron",
                            "0 9 * * 1-5",
                            "--timezone",
                            "UTC",
                            "--approve",
                            "routine:cron-check",
                            "--reason",
                            "token=cronupdatesecret",
                        ]
                    ),
                    0,
                )
            self.assertEqual(
                load_routine_manifest(manifest_path)["schedule"],
                {"kind": "cron", "expression": "0 9 * * 1-5", "timezone": "UTC"},
            )
            controls = load_routine_controls(manifest_path.parent)
            self.assertEqual(controls["controls"]["cron-check"]["last_action"], "schedule-updated")
            self.assertNotIn("cronupdatesecret", json.dumps(controls))

    def test_supervisor_executes_each_due_cron_slot_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_path = root / "workflow.json"
            routines = root / "routines"
            manifest_path = routines / "cron-exec.json"
            _write_json(workflow_path, _workflow())
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "write-routine-manifest",
                            str(manifest_path),
                            "--name",
                            "cron-exec",
                            "--workflow",
                            str(workflow_path),
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--cron",
                            "* * * * *",
                        ]
                    ),
                    0,
                )

            created = datetime.fromisoformat(
                load_routine_manifest(manifest_path)["created_at_utc"].replace("Z", "+00:00")
            )
            now = created + timedelta(minutes=1)
            first = run_due_routines(
                routines,
                approvals={"routine:cron-exec"},
                now=now,
                in_process=True,
            )
            second = run_due_routines(
                routines,
                approvals={"routine:cron-exec"},
                now=now,
                in_process=True,
            )

            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["status"], "completed")
            self.assertEqual(first[0]["trigger"], "scheduled")
            self.assertEqual(second, [])
            state = load_supervisor_state(routines)["routines"]["cron-exec"]
            self.assertEqual(state["executions"], 1)
            self.assertEqual(state["last_scheduled_for_utc"], first[0]["scheduled_for_utc"])

    def test_durable_service_worker_executes_due_cron_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_path = root / "workflow.json"
            routines = root / "routines"
            manifest_path = routines / "cron-service.json"
            runtime_path = root / "conductor-runtime.pyz"
            _write_json(workflow_path, _workflow())
            runtime_path.write_bytes(b"#!/usr/bin/env python3\nPK\x03\x04cron-runtime\n")
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "write-routine-manifest",
                            str(manifest_path),
                            "--name",
                            "cron-service",
                            "--workflow",
                            str(workflow_path),
                            "--workspace",
                            str(root),
                            "--runs-dir",
                            str(root / "runs"),
                            "--cron",
                            "* * * * *",
                        ]
                    ),
                    0,
                )

            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["created_at_utc"] = (
                datetime.now(timezone.utc) - timedelta(minutes=2)
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            _write_json(manifest_path, document)
            load_routine_manifest(manifest_path)

            grant = build_routine_service_grant(
                routines_dir=routines,
                routine_manifests=[manifest_path],
                runtime_path=runtime_path,
                policy=RuntimePolicy(
                    approvals={ROUTINE_SERVICE_INSTALL_APPROVAL, "routine:cron-service"}
                ),
                allow_service_install=True,
                platform="launchd",
                poll_seconds=1,
                grant_days=1,
            )
            install_routine_service(
                grant,
                home=root / "home",
                start=False,
                manager_runner=lambda argv, ignore_missing: None,
            )
            paths = routine_service_paths(grant, home=root / "home")

            self.assertEqual(
                run_routine_service_worker(
                    paths["grant"],
                    running_runtime_path=runtime_path,
                    max_cycles=1,
                ),
                0,
            )
            service_state = load_routine_service_state(paths["state"])
            self.assertEqual(service_state["status"], "stopped")
            self.assertEqual(service_state["results_completed"], 1)
            execution = load_supervisor_state(routines)["routines"]["cron-service"]
            self.assertEqual(execution["last_status"], "completed")
            self.assertEqual(execution["last_trigger"], "scheduled")
            self.assertEqual(execution["executions"], 1)


if __name__ == "__main__":
    unittest.main()
