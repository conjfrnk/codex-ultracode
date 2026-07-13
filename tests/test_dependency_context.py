import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conductor_runtime.dashboard import (
    _step_agent_budget_text,
    _summarize_steps,
    collect_run_detail,
)
from conductor_runtime.diagnostics import diagnose_run_detail
from conductor_runtime.errors import StepExecutionError, ValidationError
from conductor_runtime.goal_loop import (
    authoritative_workflow_completion_step,
    load_satisfied_workflow_completion,
)
from conductor_runtime.model_verdict import parse_model_verdict
from conductor_runtime.planner import build_workflow_plan
from conductor_runtime.runner import (
    CODEX_CONTEXT_BEGIN,
    CODEX_CONTEXT_END,
    MAX_CODEX_CONTEXT_BYTES,
    ProcessResult,
    WorkflowRunner,
)
from conductor_runtime.schemas import get_schema
from conductor_runtime.security import RuntimePolicy
from conductor_runtime.workflow import (
    MAX_CODEX_CONTEXT_SOURCES,
    validate_workflow,
    workflow_fingerprint,
)


def workflow_with_steps(steps):
    return {
        "schema": "conductor.workflow.v1",
        "name": "dependency-context-test",
        "mode": "read_only",
        "max_workers": 1,
        "steps": steps,
    }


class RecordingCodexRunner(WorkflowRunner):
    def __init__(self, *args, completion_verdict_output=None, **kwargs):
        self.prompts = []
        self.completion_verdict_output = completion_verdict_output
        super().__init__(*args, **kwargs)

    def _codex_command(self, step, output_path, *, workspace=None):
        del step, workspace
        return ["fake-codex", str(output_path)]

    def _run_process(
        self,
        argv,
        cwd,
        timeout,
        input_text=None,
        output_limit_bytes=1024 * 1024,
        stdout_line_callback=None,
        interrupt_check=None,
    ):
        if argv[0] != "fake-codex":
            return super()._run_process(
                argv,
                cwd=cwd,
                timeout=timeout,
                input_text=input_text,
                output_limit_bytes=output_limit_bytes,
                stdout_line_callback=stdout_line_callback,
                interrupt_check=interrupt_check,
            )
        del cwd, timeout, output_limit_bytes, interrupt_check
        prompt = input_text or ""
        self.prompts.append(prompt)
        output_path = Path(argv[-1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if "fail-closed completion review using contract strict-v1" in prompt:
            verdict = self.completion_verdict_output
            if verdict is None:
                verdict = {
                    "satisfied": True,
                    "feedback": "",
                    "evidence": ["deterministic test evidence"],
                    "confidence": "high",
                }
            output_text = json.dumps(verdict) if isinstance(verdict, dict) else str(verdict)
        else:
            output_text = "FAKE_RESULT_%d\n%s" % (len(self.prompts), prompt)
        output_path.write_text(output_text, encoding="utf-8")
        session_id = "00000000-0000-4000-8000-%012d" % len(self.prompts)
        lines = [
            json.dumps({"type": "thread.started", "thread_id": session_id}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "total_tokens": 10,
                    },
                }
            ),
        ]
        if stdout_line_callback is not None:
            for line in lines:
                stdout_line_callback(line)
        return ProcessResult(
            returncode=0,
            stdout="\n".join(lines) + "\n",
            stderr="",
        )


class TamperingMapRunner(RecordingCodexRunner):
    def _agent_map(self, step):
        super()._agent_map(step)
        output = sorted(
            path
            for path in (self.run.artifacts_dir / step["capture_dir"]).glob("*.md")
            if path.is_file()
        )[0]
        output.write_text("changed after producer completion\n", encoding="utf-8")


class SymlinkArtifactRunner(RecordingCodexRunner):
    def _write_artifact(self, step):
        super()._write_artifact(step)
        if step["id"] != "source":
            return
        output = self.run.resolve_artifact_path(step["output"])
        outside = self.run.run_dir / "outside.txt"
        outside.write_text("replacement\n", encoding="utf-8")
        output.unlink()
        output.symlink_to(outside)


class MissingReceiptRunner(RecordingCodexRunner):
    def _write_artifact(self, step):
        super()._write_artifact(step)
        if step["id"] != "source":
            return
        state = self.run.read_state()
        state["steps"]["source"].pop("context_output_sha256", None)
        state["steps"]["source"].pop("context_output_bytes", None)
        self.run.save_state(state)


class DriftingMapRunner(RecordingCodexRunner):
    def _agent_map(self, step):
        super()._agent_map(step)
        (self.workspace / "a.txt").write_text("changed source\n", encoding="utf-8")


class RunPathArtifactRunner(RecordingCodexRunner):
    def _write_artifact(self, step):
        effective = dict(step)
        effective["content"] = "private run path: %s\n" % self.run.run_dir
        super()._write_artifact(effective)


class DependencyContextTests(unittest.TestCase):
    def test_omitted_context_from_adds_no_producer_receipt(self):
        workflow = workflow_with_steps(
            [
                {
                    "id": "source",
                    "kind": "write_artifact",
                    "output": "source.txt",
                    "content": "ordinary artifact",
                }
            ]
        )
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = WorkflowRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(),
            ).execute()
            source_state = run.read_state()["steps"]["source"]
        self.assertNotIn("context_output_sha256", source_state)
        self.assertNotIn("context_output_bytes", source_state)

    def test_context_from_validation_is_direct_bounded_and_artifact_only(self):
        valid = workflow_with_steps(
            [
                {
                    "id": "source",
                    "kind": "write_artifact",
                    "output": "source.txt",
                    "content": "evidence",
                },
                {
                    "id": "sink",
                    "kind": "codex_exec",
                    "depends_on": ["source"],
                    "context_from": ["source"],
                    "prompt": "Use the evidence.",
                },
            ]
        )
        validate_workflow(valid)

        duplicate = copy.deepcopy(valid)
        duplicate["steps"][1]["context_from"] = ["source", "source"]
        with self.assertRaisesRegex(ValidationError, "unique step ids"):
            validate_workflow(duplicate)

        indirect = copy.deepcopy(valid)
        indirect["steps"][1]["depends_on"] = []
        with self.assertRaisesRegex(ValidationError, "direct dependency"):
            validate_workflow(indirect)

        wrong_target = copy.deepcopy(valid)
        wrong_target["steps"][0]["context_from"] = ["source"]
        with self.assertRaisesRegex(ValidationError, "only with codex_exec"):
            validate_workflow(wrong_target)

        unsupported = workflow_with_steps(
            [
                {"id": "gate", "kind": "manual_gate"},
                {
                    "id": "sink",
                    "kind": "codex_exec",
                    "depends_on": ["gate"],
                    "context_from": ["gate"],
                    "prompt": "Review.",
                },
            ]
        )
        with self.assertRaisesRegex(ValidationError, "unsupported kind manual_gate"):
            validate_workflow(unsupported)

        uncaptured_shell = workflow_with_steps(
            [
                {"id": "probe", "kind": "shell", "command": ["true"]},
                {
                    "id": "sink",
                    "kind": "codex_exec",
                    "depends_on": ["probe"],
                    "context_from": ["probe"],
                    "prompt": "Review.",
                },
            ]
        )
        with self.assertRaisesRegex(ValidationError, "must capture output"):
            validate_workflow(uncaptured_shell)

        too_many = copy.deepcopy(valid)
        too_many["steps"][1]["context_from"] = [
            "source-%d" % index for index in range(MAX_CODEX_CONTEXT_SOURCES + 1)
        ]
        with self.assertRaisesRegex(ValidationError, "must contain 1 to"):
            validate_workflow(too_many)

        unbounded_map = workflow_with_steps(
            [
                {
                    "id": "packet-review",
                    "kind": "agent_map",
                    "items_artifact": "items.txt",
                    "prompt_template": "Review {item}",
                },
                {
                    "id": "sink",
                    "kind": "codex_exec",
                    "depends_on": ["packet-review"],
                    "context_from": ["packet-review"],
                    "prompt": "Synthesize.",
                },
            ]
        )
        with self.assertRaisesRegex(ValidationError, "must set max_packets"):
            validate_workflow(unbounded_map)

        oversized_map = workflow_with_steps(
            [
                {
                    "id": "packet-review",
                    "kind": "agent_map",
                    "items": ["item-%02d" % index for index in range(33)],
                    "prompt_template": "Review {item}",
                },
                {
                    "id": "sink",
                    "kind": "codex_exec",
                    "depends_on": ["packet-review"],
                    "context_from": ["packet-review"],
                    "prompt": "Synthesize.",
                },
            ]
        )
        with self.assertRaisesRegex(ValidationError, "at most 32 artifacts"):
            validate_workflow(oversized_map)

    def test_schema_exports_context_from_bounds(self):
        schema = get_schema("workflow")
        variants = schema["properties"]["steps"]["items"]["oneOf"]
        codex = next(
            variant
            for variant in variants
            if variant["properties"]["kind"].get("const") == "codex_exec"
        )
        context = codex["properties"]["context_from"]
        self.assertEqual(context["maxItems"], MAX_CODEX_CONTEXT_SOURCES)
        self.assertTrue(context["uniqueItems"])
        self.assertEqual(
            codex["properties"]["completion_verdict"]["const"],
            "strict-v1",
        )
        feedback = get_schema("completion-verdict-feedback")
        self.assertEqual(
            feedback["properties"]["schema"]["const"],
            "conductor.completion_verdict_feedback.v1",
        )
        self.assertEqual(
            feedback["properties"]["contract"]["const"],
            "strict-v1",
        )
        self.assertEqual(feedback["properties"]["feedback_present"]["const"], True)
        self.assertNotIn("feedback", feedback["properties"])

    def test_completion_verdict_validation_is_codex_only_and_versioned(self):
        workflow = workflow_with_steps(
            [
                {
                    "id": "review",
                    "kind": "codex_exec",
                    "prompt": "Review completion.",
                    "completion_verdict": "strict-v1",
                }
            ]
        )
        validate_workflow(workflow)

        invalid_version = copy.deepcopy(workflow)
        invalid_version["steps"][0]["completion_verdict"] = "strict-v2"
        with self.assertRaisesRegex(ValidationError, "must be strict-v1"):
            validate_workflow(invalid_version)

        write_review = copy.deepcopy(workflow)
        write_review["steps"][0]["sandbox"] = "workspace-write"
        with self.assertRaisesRegex(ValidationError, "requires a read-only sandbox"):
            validate_workflow(write_review)

        wrong_kind = workflow_with_steps(
            [
                {
                    "id": "artifact",
                    "kind": "write_artifact",
                    "output": "artifact.txt",
                    "content": "data",
                    "completion_verdict": "strict-v1",
                }
            ]
        )
        with self.assertRaisesRegex(ValidationError, "only with codex_exec"):
            validate_workflow(wrong_kind)

    def test_satisfied_completion_verdict_completes_with_hash_only_state(self):
        workflow = workflow_with_steps(
            [
                {
                    "id": "review",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "prompt": "Review completion.",
                    "completion_verdict": "strict-v1",
                    "capture": "review.json",
                }
            ]
        )
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = RecordingCodexRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            run = runner.execute()
            state = run.read_state()["steps"]["review"]
            detail = collect_run_detail(root / "runs", run.run_dir.name)
            (run.artifacts_dir / "review.json").write_text(
                json.dumps(
                    {
                        "satisfied": False,
                        "feedback": "tampered",
                        "evidence": ["changed"],
                        "confidence": "low",
                    }
                ),
                encoding="utf-8",
            )
            tampered_detail = collect_run_detail(root / "runs", run.run_dir.name)

        self.assertEqual(state["status"], "completed")
        self.assertTrue(state["completion_verdict_valid"])
        self.assertTrue(state["completion_verdict_satisfied"])
        self.assertEqual(state["completion_verdict_confidence"], "high")
        self.assertEqual(state["completion_verdict_evidence_count"], 1)
        self.assertFalse(state["completion_verdict_feedback_present"])
        self.assertEqual(len(state["completion_verdict_output_sha256"]), 64)
        self.assertNotIn("deterministic test evidence", json.dumps(state))
        self.assertIn("Return exactly one JSON object", runner.prompts[0])
        review_step = detail["steps"][0]
        self.assertEqual(review_step["completion_verdict_status"], "satisfied")
        review_artifact = next(
            artifact for artifact in detail["artifacts"]["files"]
            if artifact["name"] == "review.json"
        )
        self.assertFalse(review_artifact["excerpt"]["present"])
        self.assertNotIn("deterministic test evidence", json.dumps(detail))
        self.assertEqual(
            tampered_detail["steps"][0]["completion_verdict_status"],
            "invalid",
        )
        tampered_diagnostics = diagnose_run_detail(tampered_detail)
        self.assertIn(
            "completion_verdict_invalid",
            {issue["code"] for issue in tampered_diagnostics["issues"]},
        )

    def test_unsatisfied_completion_verdict_fails_and_blocks_downstream_provider(self):
        workflow = workflow_with_steps(
            [
                {
                    "id": "review",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "prompt": "Review completion.",
                    "completion_verdict": "strict-v1",
                    "capture": "review.json",
                },
                {
                    "id": "downstream",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "depends_on": ["review"],
                    "prompt": "Must not launch.",
                },
            ]
        )
        validate_workflow(workflow)
        feedback = "Fix the failing verification before completion."
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = RecordingCodexRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                completion_verdict_output={
                    "satisfied": False,
                    "feedback": feedback,
                    "evidence": ["verification failed"],
                    "confidence": "high",
                },
            )
            with self.assertRaisesRegex(StepExecutionError, "verdict is unsatisfied"):
                runner.execute()
            state = runner.run.read_state()["steps"]["review"]
            detail = collect_run_detail(root / "runs", runner.run.run_dir.name)

        self.assertEqual(len(runner.prompts), 1)
        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["completion_verdict_valid"])
        self.assertFalse(state["completion_verdict_satisfied"])
        self.assertTrue(state["completion_verdict_feedback_present"])
        self.assertNotIn(feedback, json.dumps(state))
        self.assertNotIn(feedback, json.dumps(detail))
        self.assertEqual(
            detail["steps"][0]["completion_verdict_status"],
            "unsatisfied",
        )
        diagnostics = diagnose_run_detail(detail)
        self.assertIn(
            "completion_verdict_unsatisfied",
            {issue["code"] for issue in diagnostics["issues"]},
        )

    def test_completion_verdict_hash_and_parse_share_one_file_snapshot(self):
        step = {
            "id": "review",
            "kind": "codex_exec",
            "risk": "medium",
            "sandbox": "read-only",
            "prompt": "Review completion.",
            "capture": "review.json",
            "completion_verdict": "strict-v1",
        }
        workflow = workflow_with_steps([step])
        validate_workflow(workflow)
        payload = json.dumps(
            {
                "satisfied": False,
                "feedback": "One blocker remains.",
                "evidence": ["The required check is absent."],
                "confidence": "high",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = RecordingCodexRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            output_path = runner.run.resolve_artifact_path("review.json")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(payload, encoding="utf-8")
            with patch(
                "conductor_runtime.runner._file_hash_record",
                side_effect=AssertionError("completion verdict reopened for hashing"),
            ):
                result = runner._completion_verdict_result(step, output_path)

        self.assertFalse(result["accepted"])
        self.assertEqual(
            result["metrics"]["completion_verdict_output_sha256"],
            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            result["metrics"]["completion_verdict_output_bytes"],
            len(payload.encode("utf-8")),
        )

    def test_satisfied_workflow_completion_revalidates_private_output(self):
        workflow = workflow_with_steps(
            [
                {
                    "id": "review",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "sandbox": "read-only",
                    "prompt": "Review completion.",
                    "capture": "review.json",
                    "completion_verdict": "strict-v1",
                }
            ]
        )
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = RecordingCodexRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            run = runner.execute()
            evidence = load_satisfied_workflow_completion(
                run.run_dir,
                expected_workflow_fingerprint=workflow_fingerprint(workflow),
                expected_step_id="review",
            )
            (run.artifacts_dir / "review.json").write_text(
                json.dumps(
                    {
                        "satisfied": True,
                        "feedback": "",
                        "evidence": ["changed evidence"],
                        "confidence": "low",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "has drifted"):
                load_satisfied_workflow_completion(
                    run.run_dir,
                    expected_workflow_fingerprint=workflow_fingerprint(workflow),
                    expected_step_id="review",
                )

        self.assertTrue(evidence["satisfied"])
        self.assertEqual(evidence["confidence"], "high")
        self.assertEqual(evidence["evidence_count"], 1)
        self.assertNotIn("evidence", evidence)

    def test_malformed_and_duplicate_key_completion_verdicts_fail_closed(self):
        workflow = workflow_with_steps(
            [
                {
                    "id": "review",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "prompt": "Review completion.",
                    "completion_verdict": "strict-v1",
                }
            ]
        )
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = RecordingCodexRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                completion_verdict_output="not-json",
            )
            with self.assertRaisesRegex(StepExecutionError, "verdict output is invalid"):
                runner.execute()
            state = runner.run.read_state()["steps"]["review"]
            detail = collect_run_detail(root / "runs", runner.run.run_dir.name)
        self.assertFalse(state["completion_verdict_valid"])
        self.assertEqual(len(state["completion_verdict_output_sha256"]), 64)
        self.assertEqual(detail["steps"][0]["completion_verdict_status"], "invalid")
        diagnostics = diagnose_run_detail(detail)
        self.assertIn(
            "completion_verdict_invalid",
            {issue["code"] for issue in diagnostics["issues"]},
        )

        duplicate = (
            '{"satisfied":true,"satisfied":false,"feedback":"fix",'
            '"evidence":["x"],"confidence":"high"}'
        )
        with self.assertRaisesRegex(ValidationError, "duplicate key satisfied"):
            parse_model_verdict(duplicate)

    def test_ultracode_planner_wires_raw_reproduction_evidence(self):
        workflow = build_workflow_plan(
            task="Diagnose the failing check.",
            profile="ultracode",
            failing_commands=["true"],
            check_commands=["true"],
            max_workers=1,
        )
        validate_workflow(workflow)
        steps = {step["id"]: step for step in workflow["steps"]}
        self.assertEqual(
            steps["context-map"]["context_from"],
            ["collect-files", "reproduce-1"],
        )
        self.assertEqual(
            steps["analyze-task"]["context_from"],
            ["context-map", "reproduce-1"],
        )
        self.assertEqual(
            steps["synthesize-findings"]["context_from"],
            ["packet-review", "analyze-task"],
        )
        self.assertEqual(
            steps["final-review"]["context_from"],
            ["synthesize-findings", "verify-1"],
        )

    def test_generated_ultracode_graph_requires_satisfied_final_verdict(self):
        workflow = build_workflow_plan(
            task="Review and verify one file.",
            profile="ultracode",
            write_capable=True,
            packet_items=["a.txt"],
            check_commands=["true"],
            max_workers=1,
        )
        validate_workflow(workflow)
        self.assertEqual(
            authoritative_workflow_completion_step(workflow)["id"],
            "final-review",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("bounded source\n", encoding="utf-8")
            runner = RecordingCodexRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(
                    allow_agent=True,
                    allow_writes=True,
                    approvals={"implement-generated-workflow"},
                ),
            )
            run = runner.execute()
            state = run.read_state()

        final = state["steps"]["final-review"]
        self.assertEqual(state["status"], "completed")
        self.assertEqual(final["status"], "completed")
        self.assertTrue(final["completion_verdict_satisfied"])
        self.assertEqual(final["agent_context_source_count"], 3)
        final_prompt = runner.prompts[-1]
        self.assertLess(
            final_prompt.index("step=synthesize-findings"),
            final_prompt.index("step=implement-with-codex"),
        )
        self.assertLess(
            final_prompt.index("step=implement-with-codex"),
            final_prompt.index("step=verify-1"),
        )
        self.assertIn("fail-closed completion review", final_prompt)

    def test_dashboard_exposes_only_hash_only_context_summary(self):
        digest = "a" * 64
        steps = _summarize_steps(
            {
                "sink": {
                    "kind": "codex_exec",
                    "status": "completed",
                    "agent_context_contract": "direct-artifact-v1",
                    "agent_context_source_count": 2,
                    "agent_context_artifact_count": 3,
                    "agent_context_bytes": 4096,
                    "agent_context_sha256": digest,
                }
            },
            [{"id": "sink", "kind": "codex_exec"}],
        )
        self.assertEqual(steps[0]["agent_context_artifact_count"], 3)
        self.assertEqual(steps[0]["agent_context_sha256"], digest)
        text = _step_agent_budget_text(steps[0])
        self.assertIn("dependency evidence 2 source(s)/3 artifact(s)/4096 bytes", text)
        self.assertIn("aaaaaaaaaaaa", text)
        self.assertNotIn("artifacts/", json.dumps(steps[0]))

    def test_agent_map_evidence_reaches_synthesis_and_implementation(self):
        workflow = workflow_with_steps(
            [
                {
                    "id": "packet-review",
                    "kind": "agent_map",
                    "risk": "medium",
                    "items": ["a.txt", "b.txt"],
                    "prompt_template": "PACKET_TASK {index} {item}",
                    "capture_dir": "packets",
                    "max_workers": 1,
                    "max_packets": 2,
                },
                {
                    "id": "synthesize",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "depends_on": ["packet-review"],
                    "context_from": ["packet-review"],
                    "prompt": "SYNTHESIZE_TASK",
                    "capture": "synthesis.md",
                },
                {
                    "id": "implement",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "depends_on": ["synthesize"],
                    "context_from": ["synthesize"],
                    "prompt": "IMPLEMENT_TASK",
                    "capture": "implementation.md",
                },
            ]
        )
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("alpha evidence\n", encoding="utf-8")
            (root / "b.txt").write_text("beta evidence\n", encoding="utf-8")
            runner = RecordingCodexRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            run = runner.execute()
            state = run.read_state()

        self.assertEqual(len(runner.prompts), 4)
        synthesis_prompt = runner.prompts[2]
        self.assertIn("step=packet-review", synthesis_prompt)
        self.assertIn("PACKET_TASK 1 a.txt", synthesis_prompt)
        self.assertIn("PACKET_TASK 2 b.txt", synthesis_prompt)
        implementation_prompt = runner.prompts[3]
        self.assertIn("step=synthesize", implementation_prompt)
        self.assertIn("SYNTHESIZE_TASK", implementation_prompt)
        synthesis_state = state["steps"]["synthesize"]
        self.assertEqual(synthesis_state["agent_context_source_count"], 1)
        self.assertEqual(synthesis_state["agent_context_artifact_count"], 2)
        self.assertEqual(len(synthesis_state["agent_context_sha256"]), 64)
        self.assertNotIn("PACKET_TASK", json.dumps(synthesis_state))

    def test_captured_shell_output_reaches_codex_with_a_verified_receipt(self):
        workflow = workflow_with_steps(
            [
                {
                    "id": "probe",
                    "kind": "shell",
                    "command": ["printf", "verified-shell-evidence"],
                    "capture": "probe.txt",
                    "capture_mode": "stdout",
                },
                {
                    "id": "sink",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "depends_on": ["probe"],
                    "context_from": ["probe"],
                    "prompt": "Review shell evidence.",
                },
            ]
        )
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = RecordingCodexRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            run = runner.execute()
            state = run.read_state()

        self.assertIn("verified-shell-evidence", runner.prompts[0])
        self.assertEqual(len(state["steps"]["probe"]["context_output_sha256"]), 64)
        self.assertEqual(state["steps"]["sink"]["agent_context_artifact_count"], 1)

    def test_tampered_map_output_blocks_synthesis_before_provider_launch(self):
        workflow = workflow_with_steps(
            [
                {
                    "id": "packet-review",
                    "kind": "agent_map",
                    "risk": "medium",
                    "items": ["a.txt"],
                    "prompt_template": "Review {item}",
                    "capture_dir": "packets",
                    "max_workers": 1,
                },
                {
                    "id": "synthesize",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "depends_on": ["packet-review"],
                    "context_from": ["packet-review"],
                    "prompt": "Synthesize.",
                },
            ]
        )
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("evidence\n", encoding="utf-8")
            runner = TamperingMapRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            with self.assertRaisesRegex(ValidationError, "missing, stale, or unbound"):
                runner.execute()
        self.assertEqual(len(runner.prompts), 1)

    def test_symlink_substitution_blocks_context_before_provider_launch(self):
        workflow = workflow_with_steps(
            [
                {
                    "id": "source",
                    "kind": "write_artifact",
                    "output": "source.txt",
                    "content": "evidence",
                },
                {
                    "id": "sink",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "depends_on": ["source"],
                    "context_from": ["source"],
                    "prompt": "Review.",
                },
            ]
        )
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = SymlinkArtifactRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            with self.assertRaisesRegex(ValidationError, "symlink"):
                runner.execute()
        self.assertEqual(runner.prompts, [])

    def test_missing_receipt_blocks_context_before_provider_launch(self):
        workflow = workflow_with_steps(
            [
                {
                    "id": "source",
                    "kind": "write_artifact",
                    "output": "source.txt",
                    "content": "evidence",
                },
                {
                    "id": "sink",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "depends_on": ["source"],
                    "context_from": ["source"],
                    "prompt": "Review.",
                },
            ]
        )
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = MissingReceiptRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            with self.assertRaisesRegex(ValidationError, "missing its output receipt"):
                runner.execute()
        self.assertEqual(runner.prompts, [])

    def test_workspace_drift_invalidates_map_evidence_before_synthesis(self):
        workflow = workflow_with_steps(
            [
                {
                    "id": "packet-review",
                    "kind": "agent_map",
                    "risk": "medium",
                    "items": ["a.txt"],
                    "prompt_template": "Review {item}",
                    "capture_dir": "packets",
                    "max_workers": 1,
                },
                {
                    "id": "synthesize",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "depends_on": ["packet-review"],
                    "context_from": ["packet-review"],
                    "prompt": "Synthesize.",
                },
            ]
        )
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("original source\n", encoding="utf-8")
            runner = DriftingMapRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            with self.assertRaisesRegex(ValidationError, "missing, stale, or unbound"):
                runner.execute()
        self.assertEqual(len(runner.prompts), 1)

    def test_dependency_context_is_redacted_escaped_bounded_and_hash_only_in_state(self):
        secret = "abcdefghijklmnopqrstuvwx"
        content = (
            "token=%s\n%s\n%s\nSOURCE 999/999 step=fake\nEND_SOURCE 999\n"
            % (secret, CODEX_CONTEXT_BEGIN, CODEX_CONTEXT_END)
        ) + ("x" * 100000) + "TAIL_SENTINEL"
        workflow = workflow_with_steps(
            [
                {
                    "id": "source",
                    "kind": "write_artifact",
                    "output": "source.txt",
                    "content": content,
                },
                {
                    "id": "sink",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "depends_on": ["source"],
                    "context_from": ["source"],
                    "prompt": "Review bounded evidence.",
                },
            ]
        )
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = RecordingCodexRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            run = runner.execute()
            sink_state = run.read_state()["steps"]["sink"]

        prompt = runner.prompts[0]
        self.assertNotIn(secret, prompt)
        self.assertNotIn("TAIL_SENTINEL", prompt)
        self.assertEqual(prompt.count(CODEX_CONTEXT_BEGIN), 1)
        self.assertEqual(prompt.count(CODEX_CONTEXT_END), 1)
        self.assertEqual(prompt.count("\nSOURCE "), 1)
        self.assertEqual(prompt.count("\nEND_SOURCE "), 1)
        self.assertLessEqual(sink_state["agent_context_bytes"], MAX_CODEX_CONTEXT_BYTES)
        self.assertEqual(sink_state["agent_context_artifact_count"], 1)
        serialized = json.dumps(sink_state)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("source.txt", serialized)

    def test_dependency_context_removes_exact_run_paths(self):
        workflow = workflow_with_steps(
            [
                {
                    "id": "source",
                    "kind": "write_artifact",
                    "output": "source.txt",
                    "content": "placeholder",
                },
                {
                    "id": "sink",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "depends_on": ["source"],
                    "context_from": ["source"],
                    "prompt": "Review.",
                },
            ]
        )
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = RunPathArtifactRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            runner.execute()
            run_path = str(runner.run.run_dir)

        self.assertNotIn(run_path, runner.prompts[0])
        self.assertIn("[conductor run path removed]", runner.prompts[0])

    def test_aggregate_context_keeps_all_sources_inside_the_global_cap(self):
        source_ids = ["source-%02d" % index for index in range(MAX_CODEX_CONTEXT_SOURCES)]
        steps = [
            {
                "id": source_id,
                "kind": "write_artifact",
                "output": "%s.txt" % source_id,
                "content": ("evidence-%s\n" % source_id) + ("x" * 6000),
            }
            for source_id in source_ids
        ]
        steps.append(
            {
                "id": "sink",
                "kind": "codex_exec",
                "risk": "medium",
                "depends_on": source_ids,
                "context_from": source_ids,
                "prompt": "Synthesize all bounded evidence.",
            }
        )
        workflow = workflow_with_steps(steps)
        validate_workflow(workflow)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = RecordingCodexRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            run = runner.execute()
            sink_state = run.read_state()["steps"]["sink"]

        prompt = runner.prompts[0]
        self.assertIn("step=source-00", prompt)
        self.assertIn("step=source-31", prompt)
        self.assertEqual(sink_state["agent_context_artifact_count"], MAX_CODEX_CONTEXT_SOURCES)
        self.assertLessEqual(sink_state["agent_context_bytes"], MAX_CODEX_CONTEXT_BYTES)


if __name__ == "__main__":
    unittest.main()
