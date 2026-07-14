import json
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from conductor_runtime.cli import main
from conductor_runtime.core.auto import build_direct_workflow, run_direct
from conductor_runtime.core.codex import analyze_stream, validate_completion_verdict
from conductor_runtime.core.goal import run_goal
from conductor_runtime.core.policy import RuntimePolicy, shell_approval
from conductor_runtime.core.planner import run_planned_workflow
from conductor_runtime.core.runner import WorkflowRunner
from conductor_runtime.core.safe import (
    canonical_json_bytes,
    strict_json_bytes,
    write_new_bytes,
)
from conductor_runtime.core.staged import apply_verified_stage
from conductor_runtime.core.state import RunState
from conductor_runtime.core.workflow import validate_workflow
from conductor_runtime.errors import PolicyError, StepExecutionError, ValidationError


FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
output.parent.mkdir(parents=True, exist_ok=True)
prompt = sys.stdin.read()
sandbox = args[args.index("--sandbox") + 1]
if sandbox == "workspace-write":
    mode = os.environ.get("FAKE_CODEX_WRITE", "add")
    if mode == "add":
        pathlib.Path("made.txt").write_text("staged\n", encoding="utf-8")
    elif mode == "delete":
        pathlib.Path("remove.txt").unlink()
if output.name == "planned-workflow.json":
    value = {
        "schema": "conductor.workflow.v1",
        "name": "planned-map",
        "mode": "read_only",
        "max_workers": 2,
        "agent_max_tokens": 200,
        "agent_map_max_total_tokens": 400,
        "result_artifact": "synthesis.md",
        "steps": [
            {
                "id": "map",
                "kind": "agent_map",
                "items": ["one", "two"],
                "item_semantics": "opaque",
                "prompt_template": "Inspect {item}.",
                "capture_dir": "map",
                "max_workers": 2,
            },
            {
                "id": "collect",
                "kind": "collect_results",
                "source_step": "map",
                "depends_on": ["map"],
                "output": "collected.json",
            },
            {
                "id": "synthesize",
                "kind": "codex_exec",
                "depends_on": ["collect"],
                "context_from": ["collect"],
                "prompt": "Synthesize.",
                "capture": "synthesis.md",
            },
        ],
    }
    output.write_text(json.dumps(value), encoding="utf-8")
elif output.name == "verdict.json":
    once = os.environ.get("FAKE_CODEX_VERDICT_ONCE_PATH")
    if once and not pathlib.Path(once).exists():
        pathlib.Path(once).write_text("failed-once", encoding="utf-8")
        satisfied = False
    else:
        satisfied = os.environ.get("FAKE_CODEX_VERDICT", "pass") == "pass"
    value = {
        "satisfied": satisfied,
        "feedback": "" if satisfied else "verification failed",
        "evidence": ["fake check"] if satisfied else [],
        "confidence": 1.0,
    }
    output.write_text(json.dumps(value), encoding="utf-8")
elif "/map/" in output.as_posix():
    output.write_text("packet:" + prompt[-80:], encoding="utf-8")
elif output.name == "synthesis.md":
    output.write_text("synthesized", encoding="utf-8")
else:
    output.write_text("implemented" if sandbox == "workspace-write" else "inspected", encoding="utf-8")
session = "fake-" + output.stem
print(json.dumps({"type": "thread.started", "thread_id": session}))
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


def read_workflow(name="test"):
    return {
        "schema": "conductor.workflow.v1",
        "name": name,
        "mode": "read_only",
        "agent_max_tokens": 200,
        "steps": [
            {
                "id": "inspect",
                "kind": "codex_exec",
                "prompt": "Inspect the workspace.",
                "capture": "result.md",
            }
        ],
        "result_artifact": "result.md",
    }


class CoreRuntimeTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="conductor-core-")
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home = self.root / "state"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        codex = self.bin / "codex"
        codex.write_text(FAKE_CODEX, encoding="utf-8")
        codex.chmod(0o755)
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

    def test_direct_plan_has_no_provider_call(self):
        (self.bin / "codex").unlink()
        result = run_direct(
            "Inspect this workspace.",
            workspace=self.workspace,
            policy=RuntimePolicy(allow_agent=True),
            dry_run=True,
        )
        self.assertEqual(result.status, "planned")
        self.assertEqual(RunState.inspect(result.run_dir).state["steps"]["work"]["attempt"], 0)

    def test_direct_readonly_executes_one_model_step(self):
        result = run_direct(
            "Inspect this workspace.",
            workspace=self.workspace,
            policy=RuntimePolicy(allow_agent=True),
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.result_path.read_text(encoding="utf-8"), "inspected")
        self.assertIsNone(result.stage_evidence_path)

    def test_staged_write_verifies_then_applies_idempotently(self):
        policy = RuntimePolicy(allow_agent=True, allow_writes=True)
        result = run_direct("Create made.txt.", workspace=self.workspace, policy=policy)
        self.assertEqual(result.status, "completed")
        self.assertFalse((self.workspace / "made.txt").exists())
        apply_policy = RuntimePolicy(
            allow_writes=True,
            approvals={"verified-stage-apply"},
        )
        merged = apply_verified_stage(result.stage_evidence_path, self.workspace, policy=apply_policy)
        self.assertFalse(merged["already_applied"])
        self.assertEqual((self.workspace / "made.txt").read_text(encoding="utf-8"), "staged\n")
        repeated = apply_verified_stage(result.stage_evidence_path, self.workspace, policy=apply_policy)
        self.assertTrue(repeated["already_applied"])

    def test_staged_apply_rejects_source_drift(self):
        result = run_direct(
            "Create made.txt.",
            workspace=self.workspace,
            policy=RuntimePolicy(allow_agent=True, allow_writes=True),
        )
        (self.workspace / "drift.txt").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "workspace changed"):
            apply_verified_stage(
                result.stage_evidence_path,
                self.workspace,
                policy=RuntimePolicy(allow_writes=True, approvals={"verified-stage-apply"}),
            )

    def test_staged_delete_requires_separate_approval(self):
        (self.workspace / "remove.txt").write_text("remove", encoding="utf-8")
        with patch.dict(os.environ, {"FAKE_CODEX_WRITE": "delete"}):
            result = run_direct(
                "Delete remove.txt.",
                workspace=self.workspace,
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
            )
        with self.assertRaises(PolicyError):
            apply_verified_stage(
                result.stage_evidence_path,
                self.workspace,
                policy=RuntimePolicy(allow_writes=True, approvals={"verified-stage-apply"}),
            )
        merged = apply_verified_stage(
            result.stage_evidence_path,
            self.workspace,
            policy=RuntimePolicy(
                allow_writes=True,
                allow_destructive=True,
                approvals={"verified-stage-apply", "verified-stage-delete"},
            ),
        )
        self.assertEqual(merged["status"], "merged")
        self.assertFalse((self.workspace / "remove.txt").exists())

    def test_unsatisfied_model_verifier_never_seals_stage(self):
        with patch.dict(os.environ, {"FAKE_CODEX_VERDICT": "fail"}):
            result = run_direct(
                "Create made.txt.",
                workspace=self.workspace,
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
            )
        self.assertEqual(result.status, "failed")
        evidence = strict_json_bytes(result.stage_evidence_path.read_bytes(), "evidence")
        self.assertEqual(evidence["verification_status"], "pending")

    def test_verifier_cannot_modify_staged_workspace(self):
        command = [sys.executable, "-c", "from pathlib import Path; Path('verifier.txt').write_text('bad')"]
        workflow = {
            "schema": "conductor.workflow.v1",
            "name": "mutating-verifier",
            "agent_max_tokens": 200,
            "steps": [
                {
                    "id": "work",
                    "kind": "codex_exec",
                    "prompt": "Create made.txt.",
                    "sandbox": "workspace-write",
                },
                {
                    "id": "check",
                    "kind": "shell",
                    "depends_on": ["work"],
                    "command": command,
                    "writes": False,
                    "destructive": False,
                    "network": False,
                },
            ],
        }
        run = WorkflowRunner(
            workflow,
            self.workspace,
            None,
            RuntimePolicy(
                allow_agent=True,
                allow_writes=True,
                approvals={shell_approval(command)},
            ),
        ).execute()
        self.assertEqual(run.state["status"], "failed")
        self.assertIn("modified its isolated workspace", run.state["steps"]["check"]["detail"])
        self.assertFalse((self.workspace / "verifier.txt").exists())
        evidence = strict_json_bytes(run.read_artifact("stages/work.json"), "stage evidence")
        stage = run.run_dir / evidence["stage_subdir"]
        self.assertFalse((stage / "verifier.txt").exists())

    def test_goal_retries_only_verifier_failure_and_keeps_external_state(self):
        marker = self.root / "verifier-once"
        with patch.dict(os.environ, {"FAKE_CODEX_VERDICT_ONCE_PATH": str(marker)}):
            result = run_goal(
                "Create made.txt.",
                workspace=self.workspace,
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                max_iterations=2,
                goal_id="retry-once",
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.iterations, 2)
        self.assertFalse(result.goal_path.is_relative_to(self.workspace))
        state = strict_json_bytes(result.goal_path.read_bytes(), "goal")
        self.assertEqual([item["status"] for item in state["attempts"]], ["failed", "completed"])
        with self.assertRaisesRegex(ValidationError, "task changed"):
            run_goal(
                "A different task.",
                workspace=self.workspace,
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                max_iterations=2,
                resume_goal=result.goal_path,
            )

    def test_run_resume_binds_iteration_context(self):
        workflow = {
            "schema": "conductor.workflow.v1",
            "name": "context-resume",
            "steps": [
                {
                    "id": "check",
                    "kind": "shell",
                    "command": ["false"],
                    "writes": False,
                    "destructive": False,
                    "network": False,
                }
            ],
        }
        first = WorkflowRunner(
            workflow,
            self.workspace,
            None,
            RuntimePolicy(),
            iteration_context="first feedback",
        ).execute()
        with self.assertRaisesRegex(ValidationError, "iteration context"):
            WorkflowRunner(
                workflow,
                self.workspace,
                None,
                RuntimePolicy(),
                resume_dir=first.run_dir,
                iteration_context="changed feedback",
            )

    def test_map_collect_synthesis_is_bounded(self):
        workflow = {
            "schema": "conductor.workflow.v1",
            "name": "map-synthesis",
            "mode": "read_only",
            "max_workers": 2,
            "agent_max_tokens": 200,
            "agent_map_max_total_tokens": 400,
            "result_artifact": "synthesis.md",
            "steps": [
                {
                    "id": "map",
                    "kind": "agent_map",
                    "items": ["one", "two", "one"],
                    "item_semantics": "opaque",
                    "prompt_template": "Inspect {item} as packet {index}.",
                    "capture_dir": "map",
                    "max_workers": 2,
                },
                {
                    "id": "collect",
                    "kind": "collect_results",
                    "source_step": "map",
                    "depends_on": ["map"],
                    "output": "collected.json",
                },
                {
                    "id": "synthesize",
                    "kind": "codex_exec",
                    "depends_on": ["collect"],
                    "context_from": ["collect"],
                    "prompt": "Synthesize the bounded evidence.",
                    "capture": "synthesis.md",
                },
            ],
        }
        run = WorkflowRunner(
            workflow,
            self.workspace,
            None,
            RuntimePolicy(allow_agent=True, allow_parallel=True),
        ).execute()
        self.assertEqual(run.state["status"], "completed")
        self.assertEqual(run.read_artifact("synthesis.md"), b"synthesized")
        self.assertEqual(run.state["steps"]["map"]["metrics"]["packets"], 2)
        self.assertLessEqual(run.state["steps"]["map"]["metrics"]["charged_tokens"], 400)

    def test_opt_in_planner_revalidates_and_runs_map_synthesis(self):
        result = run_planned_workflow(
            "Inspect two independent areas and synthesize.",
            workspace=self.workspace,
            policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
            max_workers=2,
            max_items=2,
            execution_max_tokens=1000,
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.result_path.read_text(encoding="utf-8"), "synthesized")
        receipt = strict_json_bytes(result.receipt_path.read_bytes(), "planner receipt")
        self.assertEqual(receipt["provider_planning_calls"], 1)

    def test_parallel_map_requires_permission(self):
        workflow = self._map_workflow()
        run = WorkflowRunner(
            workflow,
            self.workspace,
            None,
            RuntimePolicy(allow_agent=True),
        ).execute()
        self.assertEqual(run.state["status"], "blocked")

    def test_non_inert_shell_requires_command_bound_approval(self):
        command = [sys.executable, "-c", "print('checked')"]
        workflow = {
            "schema": "conductor.workflow.v1",
            "name": "approved-shell",
            "steps": [
                {
                    "id": "check",
                    "kind": "shell",
                    "command": command,
                    "writes": False,
                    "destructive": False,
                    "network": False,
                }
            ],
        }
        blocked = WorkflowRunner(workflow, self.workspace, None, RuntimePolicy()).execute()
        self.assertEqual(blocked.state["status"], "blocked")
        completed = WorkflowRunner(
            workflow,
            self.workspace,
            None,
            RuntimePolicy(approvals={shell_approval(command)}),
        ).execute()
        self.assertEqual(completed.state["status"], "completed")

    def test_cli_prints_command_bound_approval_for_blocked_run(self):
        command = [sys.executable, "-c", "print('checked')"]
        workflow = {
            "schema": "conductor.workflow.v1",
            "name": "approval-output",
            "steps": [
                {
                    "id": "check",
                    "kind": "shell",
                    "command": command,
                    "writes": False,
                    "destructive": False,
                    "network": False,
                }
            ],
        }
        workflow_path = self.root / "workflow.json"
        workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["run", str(workflow_path), "--workspace", str(self.workspace)])
        self.assertEqual(status, 1)
        self.assertIn("Status: blocked", output.getvalue())
        self.assertIn("--approve %s" % shell_approval(command), output.getvalue())

    def test_failed_shell_run_resumes_with_exact_bindings(self):
        workflow = {
            "schema": "conductor.workflow.v1",
            "name": "resume-shell",
            "steps": [
                {
                    "id": "check",
                    "kind": "shell",
                    "command": ["test", "-f", "ready"],
                    "capture": "check.txt",
                    "writes": False,
                    "destructive": False,
                    "network": False,
                }
            ],
        }
        policy = RuntimePolicy()
        first = WorkflowRunner(workflow, self.workspace, None, policy).execute()
        self.assertEqual(first.state["status"], "failed")
        (self.workspace / "ready").write_text("yes", encoding="utf-8")
        resumed = WorkflowRunner(
            workflow,
            self.workspace,
            None,
            policy,
            resume_dir=first.run_dir,
        ).execute()
        self.assertEqual(resumed.state["status"], "completed")
        self.assertEqual(resumed.state["steps"]["check"]["attempt"], 2)

    def test_resume_rejects_policy_and_artifact_changes(self):
        workflow = {
            "schema": "conductor.workflow.v1",
            "name": "artifact-resume",
            "steps": [{"id": "write", "kind": "write_artifact", "output": "value.txt", "content": "ok"}],
        }
        run = WorkflowRunner(workflow, self.workspace, None, RuntimePolicy()).execute()
        run.artifact_path("value.txt").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "changed"):
            WorkflowRunner(
                workflow,
                self.workspace,
                None,
                RuntimePolicy(),
                resume_dir=run.run_dir,
            )
        with self.assertRaisesRegex(ValidationError, "permissions"):
            WorkflowRunner(
                workflow,
                self.workspace,
                None,
                RuntimePolicy(allow_network=True),
                resume_dir=run.run_dir,
            )

    def test_inspect_rejects_rehashed_state_tampering(self):
        result = run_direct(
            "Inspect.",
            workspace=self.workspace,
            policy=RuntimePolicy(allow_agent=True),
        )
        state_path = result.run_dir / "state.json"
        value = json.loads(state_path.read_text(encoding="utf-8"))
        value["status"] = "failed"
        state_path.write_bytes(canonical_json_bytes(value))
        with self.assertRaisesRegex(ValidationError, "hash"):
            RunState.inspect(result.run_dir)

    @staticmethod
    def _map_workflow():
        return {
            "schema": "conductor.workflow.v1",
            "name": "parallel-map",
            "max_workers": 2,
            "agent_max_tokens": 200,
            "agent_map_max_total_tokens": 400,
            "steps": [
                {
                    "id": "map",
                    "kind": "agent_map",
                    "items": ["one", "two"],
                    "item_semantics": "opaque",
                    "prompt_template": "Inspect {item}.",
                    "max_workers": 2,
                },
                {
                    "id": "collect",
                    "kind": "collect_results",
                    "source_step": "map",
                    "depends_on": ["map"],
                    "output": "collected.json",
                },
                {
                    "id": "synthesize",
                    "kind": "codex_exec",
                    "depends_on": ["collect"],
                    "context_from": ["collect"],
                    "prompt": "Synthesize.",
                    "capture": "synthesis.md",
                },
            ],
        }


class CoreValidationTests(unittest.TestCase):
    def test_unknown_workflow_field_is_rejected(self):
        workflow = read_workflow()
        workflow["surprise"] = True
        with self.assertRaisesRegex(ValidationError, "unsupported fields"):
            validate_workflow(workflow)

    def test_codex_step_requires_effective_token_cap(self):
        workflow = read_workflow()
        workflow.pop("agent_max_tokens")
        with self.assertRaisesRegex(ValidationError, "max_tokens"):
            validate_workflow(workflow)

    def test_workspace_write_requires_downstream_verifier(self):
        workflow = read_workflow()
        workflow["steps"][0]["sandbox"] = "workspace-write"
        with self.assertRaisesRegex(ValidationError, "verifier"):
            validate_workflow(workflow)

    def test_agent_map_requires_collect_and_synthesis(self):
        workflow = {
            "schema": "conductor.workflow.v1",
            "name": "orphan-map",
            "agent_max_tokens": 200,
            "agent_map_max_total_tokens": 200,
            "steps": [
                {
                    "id": "map",
                    "kind": "agent_map",
                    "items": ["one"],
                    "item_semantics": "opaque",
                    "prompt_template": "Inspect {item}.",
                }
            ],
        }
        with self.assertRaisesRegex(ValidationError, "collector"):
            validate_workflow(workflow)

    def test_direct_workflow_defaults_to_one_model_call(self):
        workflow = build_direct_workflow("Inspect.", writes=False)
        self.assertEqual([step["kind"] for step in workflow["steps"]], ["codex_exec"])
        self.assertEqual(workflow["steps"][0]["max_tokens"], 20000)

    def test_strict_json_rejects_duplicate_keys(self):
        with self.assertRaises(ValidationError):
            strict_json_bytes(b'{"a": 1, "a": 2}', "value")

    def test_write_new_never_clobbers_or_follows_leaf_symlink(self):
        with tempfile.TemporaryDirectory(prefix="conductor-safe-") as tmp:
            root = Path(tmp).resolve()
            target = root / "target"
            write_new_bytes(target, b"one", "target")
            with self.assertRaises(ValidationError):
                write_new_bytes(target, b"two", "target")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaises(ValidationError):
                write_new_bytes(link, b"three", "link")
            self.assertEqual(target.read_bytes(), b"one")

    def test_stream_and_verdict_contracts_are_strict(self):
        stream = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "session"}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 2,
                            "cached_input_tokens": 1,
                            "output_tokens": 3,
                            "reasoning_output_tokens": 1,
                        },
                    }
                ),
            ]
        )
        self.assertEqual(analyze_stream(stream)["terminal_status"], "completed")
        verdict = json.dumps(
            {"satisfied": True, "feedback": "", "evidence": ["test"], "confidence": 0.9}
        )
        self.assertTrue(validate_completion_verdict(verdict)["satisfied"])
        with self.assertRaises(StepExecutionError):
            validate_completion_verdict('{"satisfied": true}')


if __name__ == "__main__":
    unittest.main()
