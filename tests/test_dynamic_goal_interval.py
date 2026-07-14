import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conductor_extras.runtime.background_goal import _build_payload, _encode_payload, _read_payload
from conductor_extras.runtime.dashboard import _summarize_goal
from conductor_runtime.errors import PolicyError, ValidationError
from conductor_extras.runtime.goal_loop import load_goal_state
from conductor_extras.runtime.model_goal_loop import (
    DYNAMIC_GOAL_INTERVAL_MODE,
    run_model_goal_loop,
    validate_model_goal_loop_request,
)
from conductor_extras.runtime.model_orchestrator import MODEL_WORKFLOW_EXECUTE_APPROVAL
from conductor_extras.runtime.model_verdict import parse_dynamic_model_verdict
from conductor_extras.runtime.runner import ProcessResult
from conductor_extras.runtime.security import RuntimePolicy


class DynamicGoalIntervalTests(unittest.TestCase):
    def setUp(self):
        self.policy = RuntimePolicy(
            allow_agent=True,
            approvals={MODEL_WORKFLOW_EXECUTE_APPROVAL},
        )

    def _planner(self):
        calls = []

        def fake(argv, cwd, timeout, input_text=None, output_limit_bytes=None):
            del cwd, timeout, output_limit_bytes
            calls.append(input_text)
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "dynamic-interval-%d" % len(calls),
                "description": "Dynamic interval fixture.",
                "mode": "review",
                "max_workers": 1,
                "steps": [
                    {
                        "id": "record",
                        "kind": "write_artifact",
                        "output": "dynamic.md",
                        "content": "dynamic iteration %d\n" % len(calls),
                    }
                ],
            }
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps(workflow), encoding="utf-8")
            return ProcessResult(0, "", "")

        return calls, fake

    def _verifier(self, verdicts):
        prompts = []

        def fake(argv, cwd, timeout, input_text=None, output_limit_bytes=None):
            del cwd, timeout, output_limit_bytes
            prompts.append(input_text)
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps(verdicts.pop(0)), encoding="utf-8")
            return ProcessResult(0, "", "")

        return prompts, fake

    def _run(self, root, **overrides):
        values = {
            "task": "Check an external result until the evidence is complete.",
            "workspace": root,
            "runs_dir": root / "runs",
            "goals_dir": root / "goals",
            "policy": self.policy,
            "check_command": None,
            "check_prompt": "The workspace proves the external result is complete.",
            "max_iterations": 2,
            "dynamic_interval": True,
            "min_delay_minutes": 2,
            "max_delay_minutes": 9,
            "goal_id": "dynamic-interval-goal",
            "max_items": 10,
            "max_steps": 4,
        }
        values.update(overrides)
        return run_model_goal_loop(**values)

    def test_dynamic_verdict_contract_is_strict_and_bound_aware(self):
        verdict = parse_dynamic_model_verdict(
            json.dumps(
                {
                    "satisfied": False,
                    "feedback": "The deployment is still active.",
                    "evidence": ["The status remains running."],
                    "confidence": "high",
                    "next_delay_minutes": 7,
                    "next_delay_reason": "The status is changing slowly.",
                }
            ),
            min_delay_minutes=2,
            max_delay_minutes=9,
        )
        self.assertEqual(verdict["next_delay_minutes"], 7)
        with self.assertRaisesRegex(ValidationError, "integer delay from 2 to 9"):
            parse_dynamic_model_verdict(
                json.dumps(dict(verdict, next_delay_minutes=10)),
                min_delay_minutes=2,
                max_delay_minutes=9,
            )
        with self.assertRaisesRegex(ValidationError, "must not schedule"):
            parse_dynamic_model_verdict(
                json.dumps(
                    dict(
                        verdict,
                        satisfied=True,
                        feedback="",
                        next_delay_minutes=2,
                    )
                ),
                min_delay_minutes=2,
                max_delay_minutes=9,
            )
        with self.assertRaisesRegex(ValidationError, "exactly"):
            parse_dynamic_model_verdict(
                json.dumps(dict(verdict, extra=True)),
                min_delay_minutes=2,
                max_delay_minutes=9,
            )

    def test_dynamic_interval_waits_once_without_an_extra_provider_call(self):
        planner_calls, planner = self._planner()
        verifier_prompts, verifier = self._verifier(
            [
                {
                    "satisfied": False,
                    "feedback": "The deployment is still active.",
                    "evidence": ["The status remains running."],
                    "confidence": "high",
                    "next_delay_minutes": 7,
                    "next_delay_reason": "The deployment is progressing slowly.",
                },
                {
                    "satisfied": True,
                    "feedback": "",
                    "evidence": ["The final status is complete."],
                    "confidence": "high",
                    "next_delay_minutes": None,
                    "next_delay_reason": "",
                },
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("conductor_extras.runtime.model_planner.run_process", side_effect=planner):
                with patch("conductor_extras.runtime.goal_loop.run_process", side_effect=verifier):
                    with patch("conductor_extras.runtime.model_goal_loop.time.sleep") as sleeper:
                        result = self._run(root)
            state = load_goal_state(result.goal_path)
            dashboard = _summarize_goal(
                result.goal_path,
                result.goal_path.stat().st_mtime,
            )

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(planner_calls), 2)
        self.assertEqual(len(verifier_prompts), 2)
        self.assertEqual(sleeper.call_count, 1)
        self.assertGreaterEqual(sleeper.call_args.args[0], 419.0)
        self.assertLessEqual(sleeper.call_args.args[0], 420.0)
        self.assertIn('"next_delay_minutes": integer|null', verifier_prompts[0])
        self.assertIn("from 2 through 9", verifier_prompts[0])
        self.assertEqual(
            state["dynamic_interval"],
            {
                "mode": DYNAMIC_GOAL_INTERVAL_MODE,
                "min_delay_minutes": 2,
                "max_delay_minutes": 9,
                "additional_provider_calls_per_selection": 0,
            },
        )
        wait = state["iterations"][0]["dynamic_wait"]
        self.assertEqual(wait["status"], "elapsed")
        self.assertEqual(wait["delay_minutes"], 7)
        self.assertEqual(
            wait["reason_sha256"],
            hashlib.sha256(
                b"The deployment is progressing slowly."
            ).hexdigest(),
        )
        self.assertEqual(
            [item["status"] for item in state["iterations"]],
            ["goal_not_satisfied", "goal_satisfied"],
        )
        self.assertEqual(dashboard["dynamic_interval"]["min_delay_minutes"], 2)
        self.assertEqual(dashboard["next_wake"]["delay_minutes"], 7)
        self.assertEqual(dashboard["next_wake"]["status"], "elapsed")

    def test_dynamic_wait_survives_interruption_and_resume_without_reselection(self):
        planner_calls, planner = self._planner()
        _prompts, verifier = self._verifier(
            [
                {
                    "satisfied": False,
                    "feedback": "The check is not complete.",
                    "evidence": ["The current status is pending."],
                    "confidence": "medium",
                    "next_delay_minutes": 3,
                    "next_delay_reason": "The external check needs a few minutes.",
                },
                {
                    "satisfied": True,
                    "feedback": "",
                    "evidence": ["The current status is complete."],
                    "confidence": "high",
                    "next_delay_minutes": None,
                    "next_delay_reason": "",
                },
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("conductor_extras.runtime.model_planner.run_process", side_effect=planner):
                with patch("conductor_extras.runtime.goal_loop.run_process", side_effect=verifier):
                    with patch(
                        "conductor_extras.runtime.model_goal_loop.time.sleep",
                        side_effect=KeyboardInterrupt,
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            self._run(root)
                    goal_path = root / "goals" / "dynamic-interval-goal.json"
                    interrupted = load_goal_state(goal_path)
                    scheduled_for = interrupted["iterations"][0]["dynamic_wait"][
                        "scheduled_for_utc"
                    ]
                    self.assertEqual(interrupted["status"], "waiting")
                    self.assertEqual(
                        interrupted["iterations"][0]["dynamic_wait"]["status"],
                        "waiting",
                    )
                    with patch(
                        "conductor_extras.runtime.model_goal_loop.time.sleep"
                    ) as resumed_sleep:
                        result = self._run(
                            root,
                            goal_id=None,
                            resume_goal=goal_path,
                        )
            final = load_goal_state(result.goal_path)

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(planner_calls), 2)
        self.assertEqual(resumed_sleep.call_count, 1)
        wait = final["iterations"][0]["dynamic_wait"]
        self.assertEqual(wait["scheduled_for_utc"], scheduled_for)
        self.assertEqual(wait["status"], "elapsed")
        self.assertEqual(len(final["resumes"]), 1)

    def test_dynamic_interval_rejects_non_model_verifiers_and_malformed_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValidationError, "model-only verifier"):
                validate_model_goal_loop_request(
                    task="Check the build.",
                    workspace=root,
                    policy=self.policy,
                    check_command=["true"],
                    check_prompt=None,
                    check_model=None,
                    planner_model=None,
                    planner_effort="high",
                    name=None,
                    write_capable=False,
                    max_iterations=2,
                    dynamic_interval=True,
                    min_delay_minutes=None,
                    max_delay_minutes=None,
                    max_workers=1,
                    max_items=10,
                    max_steps=4,
                    planning_timeout_seconds=30,
                    planning_output_limit_bytes=65536,
                    check_timeout_seconds=30,
                    check_output_limit_bytes=65536,
                    foreground=True,
                    reuse_planner_session=True,
                )

            planner_calls, planner = self._planner()
            _prompts, verifier = self._verifier(
                [
                    {
                        "satisfied": False,
                        "feedback": "Still pending.",
                        "evidence": ["Pending state observed."],
                        "confidence": "high",
                        "next_delay_minutes": 60,
                        "next_delay_reason": "Wait.",
                    }
                ]
            )
            with patch("conductor_extras.runtime.model_planner.run_process", side_effect=planner):
                with patch("conductor_extras.runtime.goal_loop.run_process", side_effect=verifier):
                    with patch("conductor_extras.runtime.model_goal_loop.time.sleep") as sleeper:
                        result = self._run(root)
            state = load_goal_state(result.goal_path)

        self.assertEqual(result.status, "verifier_failed")
        self.assertEqual(len(planner_calls), 1)
        sleeper.assert_not_called()
        self.assertIn("integer delay from 2 to 9", state["iterations"][0]["check"]["error"])

    def test_monitor_event_wakes_early_and_becomes_untrusted_planner_feedback(self):
        planner_calls, planner = self._planner()
        verifier_prompts, verifier = self._verifier(
            [
                {
                    "satisfied": False,
                    "feedback": "The deployment is still active.",
                    "evidence": ["The status remains running."],
                    "confidence": "high",
                    "next_delay_minutes": 7,
                    "next_delay_reason": "Wait for the deployment event.",
                },
                {
                    "satisfied": True,
                    "feedback": "",
                    "evidence": ["The final status is complete."],
                    "confidence": "high",
                    "next_delay_minutes": None,
                    "next_delay_reason": "",
                },
            ]
        )
        monitor_calls = []

        def fake_monitor(
            argv,
            cwd,
            timeout,
            input_text=None,
            output_limit_bytes=None,
            stdout_line_callback=None,
            interrupt_check=None,
        ):
            del input_text
            monitor_calls.append((argv, cwd, timeout, output_limit_bytes))
            self.assertIsNotNone(stdout_line_callback)
            self.assertIsNotNone(interrupt_check)
            stdout_line_callback("deployment complete\n")
            self.assertTrue(interrupt_check())
            return ProcessResult(
                -9,
                "deployment complete\n",
                "",
                interrupted=True,
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("conductor_extras.runtime.model_planner.run_process", side_effect=planner):
                with patch("conductor_extras.runtime.goal_loop.run_process", side_effect=verifier):
                    with patch(
                        "conductor_extras.runtime.model_goal_loop.run_process",
                        side_effect=fake_monitor,
                    ):
                        with patch("conductor_extras.runtime.model_goal_loop.time.sleep") as sleeper:
                            result = self._run(
                                root,
                                monitor_command=["tail", "-f", "status.log"],
                            )
            state = load_goal_state(result.goal_path)
            dashboard = _summarize_goal(
                result.goal_path,
                result.goal_path.stat().st_mtime,
            )

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(planner_calls), 2)
        self.assertEqual(len(verifier_prompts), 2)
        self.assertEqual(len(monitor_calls), 1)
        self.assertGreaterEqual(monitor_calls[0][2], 419.0)
        self.assertLessEqual(monitor_calls[0][2], 420.0)
        self.assertEqual(monitor_calls[0][3], 64 * 1024)
        sleeper.assert_not_called()
        self.assertIn(
            "Event monitor emitted this untrusted observation: deployment complete",
            planner_calls[1],
        )
        self.assertEqual(state["monitor"]["mode"], "stdout-event-v1")
        self.assertFalse(state["monitor"]["raw_argv_persisted"])
        self.assertNotIn("status.log", json.dumps(state, sort_keys=True))
        monitor = state["iterations"][0]["dynamic_wait"]["monitor"]
        self.assertEqual(monitor["status"], "event")
        self.assertTrue(monitor["event_present"])
        self.assertEqual(monitor["event_chars"], len("deployment complete"))
        self.assertEqual(
            monitor["event_sha256"],
            hashlib.sha256(b"deployment complete").hexdigest(),
        )
        self.assertEqual(dashboard["monitor"]["mode"], "stdout-event-v1")
        self.assertEqual(dashboard["next_wake"]["monitor"]["status"], "event")
        self.assertEqual(
            dashboard["next_wake"]["monitor"]["event_sha256"],
            monitor["event_sha256"],
        )

    def test_monitor_timeout_uses_the_selected_deadline_without_extra_sleep(self):
        planner_calls, planner = self._planner()
        _verifier_prompts, verifier = self._verifier(
            [
                {
                    "satisfied": False,
                    "feedback": "The deployment is pending.",
                    "evidence": ["The status is pending."],
                    "confidence": "medium",
                    "next_delay_minutes": 3,
                    "next_delay_reason": "Use the selected deadline.",
                },
                {
                    "satisfied": True,
                    "feedback": "",
                    "evidence": ["The status is complete."],
                    "confidence": "high",
                    "next_delay_minutes": None,
                    "next_delay_reason": "",
                },
            ]
        )

        def fake_monitor(*args, **kwargs):
            del args, kwargs
            return ProcessResult(-9, "", "", timed_out=True)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("conductor_extras.runtime.model_planner.run_process", side_effect=planner):
                with patch("conductor_extras.runtime.goal_loop.run_process", side_effect=verifier):
                    with patch(
                        "conductor_extras.runtime.model_goal_loop.run_process",
                        side_effect=fake_monitor,
                    ):
                        with patch("conductor_extras.runtime.model_goal_loop.time.sleep") as sleeper:
                            result = self._run(
                                root,
                                monitor_command=["tail", "-f", "status.log"],
                            )
            state = load_goal_state(result.goal_path)

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(planner_calls), 2)
        sleeper.assert_not_called()
        self.assertNotIn("Event monitor emitted", planner_calls[1])
        monitor = state["iterations"][0]["dynamic_wait"]["monitor"]
        self.assertEqual(monitor["status"], "deadline")
        self.assertTrue(monitor["timed_out"])
        self.assertFalse(monitor["event_present"])

    def test_monitor_real_stdout_process_wakes_the_goal(self):
        _planner_calls, planner = self._planner()
        _verifier_prompts, verifier = self._verifier(
            [
                {
                    "satisfied": False,
                    "feedback": "The deployment is pending.",
                    "evidence": ["The status is pending."],
                    "confidence": "medium",
                    "next_delay_minutes": 3,
                    "next_delay_reason": "Listen for the completion event.",
                },
                {
                    "satisfied": True,
                    "feedback": "",
                    "evidence": ["The status is complete."],
                    "confidence": "high",
                    "next_delay_minutes": None,
                    "next_delay_reason": "",
                },
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("conductor_extras.runtime.model_planner.run_process", side_effect=planner):
                with patch("conductor_extras.runtime.goal_loop.run_process", side_effect=verifier):
                    result = self._run(
                        root,
                        monitor_command=["printf", "deployment complete\n"],
                    )
            state = load_goal_state(result.goal_path)

        self.assertEqual(result.status, "completed")
        monitor = state["iterations"][0]["dynamic_wait"]["monitor"]
        self.assertEqual(monitor["status"], "event")
        self.assertEqual(monitor["event_excerpt"], "deployment complete")
        self.assertGreater(monitor["stdout_bytes"], 0)

    def test_monitor_failure_fails_closed_with_persisted_evidence(self):
        _planner_calls, planner = self._planner()
        _verifier_prompts, verifier = self._verifier(
            [
                {
                    "satisfied": False,
                    "feedback": "Still pending.",
                    "evidence": ["Pending state observed."],
                    "confidence": "high",
                    "next_delay_minutes": 2,
                    "next_delay_reason": "Listen for an event.",
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("conductor_extras.runtime.model_planner.run_process", side_effect=planner):
                with patch("conductor_extras.runtime.goal_loop.run_process", side_effect=verifier):
                    with patch(
                        "conductor_extras.runtime.model_goal_loop.run_process",
                        return_value=ProcessResult(2, "", "monitor failed\n"),
                    ):
                        with self.assertRaisesRegex(
                            ValidationError,
                            "monitor command failed",
                        ):
                            self._run(
                                root,
                                monitor_command=["tail", "-f", "status.log"],
                            )
            state = load_goal_state(
                root / "goals" / "dynamic-interval-goal.json"
            )

        self.assertEqual(state["status"], "workflow_failed")
        self.assertEqual(state["error_class"], "DynamicIntervalStateInvalid")
        monitor = state["iterations"][0]["dynamic_wait"]["monitor"]
        self.assertEqual(monitor["status"], "failed")
        self.assertEqual(monitor["returncode"], 2)
        self.assertEqual(monitor["error_class"], "MonitorCommandFailed")

    def test_active_monitor_is_not_replayed_after_interruption(self):
        planner_calls, planner = self._planner()
        _verifier_prompts, verifier = self._verifier(
            [
                {
                    "satisfied": False,
                    "feedback": "The check is not complete.",
                    "evidence": ["The current status is pending."],
                    "confidence": "medium",
                    "next_delay_minutes": 3,
                    "next_delay_reason": "Listen until the deadline.",
                },
                {
                    "satisfied": True,
                    "feedback": "",
                    "evidence": ["The current status is complete."],
                    "confidence": "high",
                    "next_delay_minutes": None,
                    "next_delay_reason": "",
                },
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("conductor_extras.runtime.model_planner.run_process", side_effect=planner):
                with patch("conductor_extras.runtime.goal_loop.run_process", side_effect=verifier):
                    with patch(
                        "conductor_extras.runtime.model_goal_loop.run_process",
                        side_effect=KeyboardInterrupt,
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            self._run(
                                root,
                                monitor_command=["tail", "-f", "status.log"],
                            )
                    goal_path = root / "goals" / "dynamic-interval-goal.json"
                    interrupted = load_goal_state(goal_path)
                    self.assertEqual(interrupted["status"], "monitoring")
                    self.assertEqual(
                        interrupted["iterations"][0]["dynamic_wait"]["monitor"]["status"],
                        "active",
                    )
                    with patch(
                        "conductor_extras.runtime.model_goal_loop.run_process"
                    ) as resumed_monitor:
                        with patch(
                            "conductor_extras.runtime.model_goal_loop.time.sleep"
                        ) as resumed_sleep:
                            result = self._run(
                                root,
                                goal_id=None,
                                resume_goal=goal_path,
                                monitor_command=["tail", "-f", "status.log"],
                            )
            final = load_goal_state(result.goal_path)

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(planner_calls), 2)
        resumed_monitor.assert_not_called()
        self.assertEqual(resumed_sleep.call_count, 1)
        monitor = final["iterations"][0]["dynamic_wait"]["monitor"]
        self.assertEqual(monitor["status"], "uncertain")
        self.assertEqual(monitor["error_class"], "PriorMonitorOutcomeUncertain")
        self.assertEqual(final["iterations"][0]["dynamic_wait"]["status"], "elapsed")

    def test_active_monitor_outcome_tampering_blocks_resume(self):
        _planner_calls, planner = self._planner()
        _verifier_prompts, verifier = self._verifier(
            [
                {
                    "satisfied": False,
                    "feedback": "The check is not complete.",
                    "evidence": ["The current status is pending."],
                    "confidence": "medium",
                    "next_delay_minutes": 3,
                    "next_delay_reason": "Listen until the deadline.",
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("conductor_extras.runtime.model_planner.run_process", side_effect=planner):
                with patch("conductor_extras.runtime.goal_loop.run_process", side_effect=verifier):
                    with patch(
                        "conductor_extras.runtime.model_goal_loop.run_process",
                        side_effect=KeyboardInterrupt,
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            self._run(
                                root,
                                monitor_command=["tail", "-f", "status.log"],
                            )
            goal_path = root / "goals" / "dynamic-interval-goal.json"
            state = load_goal_state(goal_path)
            state["iterations"][0]["dynamic_wait"]["monitor"]["returncode"] = 0
            goal_path.write_text(
                json.dumps(state, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValidationError,
                "unfinished dynamic goal monitor outcome is invalid",
            ):
                self._run(
                    root,
                    goal_id=None,
                    resume_goal=goal_path,
                    monitor_command=["tail", "-f", "status.log"],
                )

    def test_monitor_requires_dynamic_read_only_secret_safe_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = {
                "task": "Check a remote status.",
                "workspace": root,
                "policy": self.policy,
                "check_command": None,
                "check_prompt": "The remote status is complete.",
                "check_model": None,
                "planner_model": None,
                "planner_effort": "high",
                "name": None,
                "write_capable": False,
                "max_iterations": 2,
                "dynamic_interval": True,
                "min_delay_minutes": 2,
                "max_delay_minutes": 9,
                "max_workers": 1,
                "max_items": 10,
                "max_steps": 4,
                "planning_timeout_seconds": 30,
                "planning_output_limit_bytes": 65536,
                "check_timeout_seconds": 30,
                "check_output_limit_bytes": 65536,
                "foreground": True,
                "reuse_planner_session": True,
            }
            with self.assertRaisesRegex(ValidationError, "requires dynamic_interval"):
                validate_model_goal_loop_request(
                    **dict(
                        base,
                        dynamic_interval=False,
                        min_delay_minutes=None,
                        max_delay_minutes=None,
                        monitor_command=["tail", "-f", "status.log"],
                    )
                )
            with self.assertRaisesRegex(ValidationError, "read-only"):
                validate_model_goal_loop_request(
                    **dict(base, monitor_command=["touch", "status.log"])
                )
            with self.assertRaisesRegex(ValidationError, "secret-like"):
                validate_model_goal_loop_request(
                    **dict(
                        base,
                        monitor_command=[
                            "tail",
                            "-f",
                            "OPENAI_API_KEY=sk-proj-" + "A" * 40,
                        ],
                    )
                )
            with self.assertRaises(PolicyError):
                validate_model_goal_loop_request(
                    **dict(
                        base,
                        monitor_command=["curl", "https://example.com/events"],
                    )
                )

    def test_background_payload_binds_dynamic_interval_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            prepared = validate_model_goal_loop_request(
                task="Check a remote status.",
                workspace=root,
                policy=self.policy,
                check_command=None,
                check_prompt="The remote status is complete.",
                check_model=None,
                planner_model=None,
                planner_effort="high",
                name=None,
                write_capable=False,
                max_iterations=2,
                dynamic_interval=True,
                min_delay_minutes=4,
                max_delay_minutes=12,
                monitor_command=["tail", "-f", "status.log"],
                max_workers=1,
                max_items=10,
                max_steps=4,
                planning_timeout_seconds=30,
                planning_output_limit_bytes=65536,
                check_timeout_seconds=30,
                check_output_limit_bytes=65536,
                foreground=False,
                reuse_planner_session=True,
            )
            payload = _build_payload(
                prepared=prepared,
                workspace=root,
                runs_dir=root / "runs",
                goals_dir=root / "goals",
                goal_id="background-dynamic-goal",
                resume_goal=None,
                policy=self.policy,
                check_model=None,
                write_capable=False,
                max_iterations=2,
                max_workers=1,
                max_items=10,
                max_steps=4,
                planning_timeout_seconds=30,
                planning_output_limit_bytes=65536,
                check_timeout_seconds=30,
                check_output_limit_bytes=65536,
                direct_plan=False,
                progressive_plan=False,
            )
            loaded = _read_payload(io.BytesIO(_encode_payload(payload)))
            self.assertEqual(
                loaded["config"]["dynamic_interval"],
                prepared["dynamic_interval"],
            )
            self.assertEqual(loaded["config"]["monitor"], prepared["monitor"])
            self.assertEqual(
                loaded["config"]["monitor_command"],
                ["tail", "-f", "status.log"],
            )
            payload["config"]["monitor_command"][-1] = "other.log"
            with self.assertRaisesRegex(PolicyError, "monitor changed during handoff"):
                _read_payload(io.BytesIO(_encode_payload(payload)))
            payload["config"]["monitor_command"][-1] = "status.log"
            payload["config"]["dynamic_interval"][
                "additional_provider_calls_per_selection"
            ] = 1
            with self.assertRaisesRegex(PolicyError, "changed during handoff"):
                _read_payload(io.BytesIO(_encode_payload(payload)))


if __name__ == "__main__":
    unittest.main()
