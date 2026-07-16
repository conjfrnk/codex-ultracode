import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from conductor_runtime.core import staged
from conductor_runtime.core.codex import CodexInvocationError
from conductor_runtime.core.policy import RuntimePolicy, shell_approval
from conductor_runtime.core.runner import WorkflowRunner
from conductor_runtime.core.safe import canonical_json_bytes, strict_json_bytes
from conductor_runtime.core.staged import (
    apply_verified_stage,
    create_stage,
    finalize_stage,
    mark_stage_verified,
)
from conductor_runtime.core.workflow import SCHEMA
from conductor_runtime.errors import ValidationError


class CoreStageLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="conductor-stage-lifecycle-")
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home = self.root / "state"
        self.environment = patch.dict(os.environ, {"CODEX_CONDUCTOR_HOME": str(self.home)})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_resumable_writer_reuses_hash_bound_pending_stage_and_applies(self):
        command, workflow, policy = self._writer_workflow(
            "assert Path('partial.txt').read_text() == 'partial'; "
            "assert Path('final.txt').read_text() == 'final'"
        )
        calls = []

        def timeout(**kwargs):
            calls.append((kwargs["workspace"], kwargs["resume_session_id"]))
            (kwargs["workspace"] / "partial.txt").write_text("partial", encoding="utf-8")
            raise CodexInvocationError(
                "Codex step timed out",
                session_id="pending-session",
                resumable=True,
            )

        with patch("conductor_runtime.core.runner.invoke_codex", side_effect=timeout):
            first = WorkflowRunner(workflow, self.workspace, None, policy).execute()
        self.assertEqual(first.state["status"], "failed")
        first_record = first.state["steps"]["work"]
        self.assertTrue(first_record["resume_available"])
        pending_relative = first_record["pending_stage"]
        self.assertIsInstance(pending_relative, str)
        descriptor = strict_json_bytes(first.read_artifact(pending_relative), "pending descriptor")
        self.assertEqual(descriptor["run_id"], first.descriptor["run_id"])
        self.assertEqual(descriptor["step_id"], "work")
        stage_dir = first.run_dir / descriptor["stage_subdir"]
        self.assertEqual(stage_dir.resolve(), calls[0][0].resolve())

        def success(**kwargs):
            calls.append((kwargs["workspace"], kwargs["resume_session_id"]))
            self.assertEqual((kwargs["workspace"] / "partial.txt").read_text(), "partial")
            (kwargs["workspace"] / "final.txt").write_text("final", encoding="utf-8")
            return self._codex_result(kwargs)

        with patch("conductor_runtime.core.runner.invoke_codex", side_effect=success):
            resumed = WorkflowRunner(
                workflow,
                self.workspace,
                None,
                policy,
                resume_dir=first.run_dir,
            ).execute()
        self.assertEqual(resumed.state["status"], "completed")
        self.assertEqual(calls[1], (calls[0][0], "pending-session"))
        self.assertIsNone(resumed.state["steps"]["work"]["pending_stage"])
        evidence_path = resumed.artifact_path("stages/work.json")
        merged = apply_verified_stage(evidence_path, self.workspace, policy=self._apply_policy())
        self.assertEqual(merged["status"], "merged")
        self.assertEqual((self.workspace / "partial.txt").read_text(), "partial")
        self.assertEqual((self.workspace / "final.txt").read_text(), "final")
        self.assertIn(shell_approval(command), policy.approvals)

    def test_interrupted_running_writer_reuses_stage_after_process_crash_state(self):
        _, workflow, policy = self._writer_workflow(
            "assert Path('before-crash.txt').read_text() == 'kept'; "
            "assert Path('after-crash.txt').read_text() == 'finished'"
        )
        interrupted = WorkflowRunner(workflow, self.workspace, None, policy)
        interrupted.run.set_status("running")
        interrupted.run.transition_step("work", "running")
        stage_dir = interrupted._provider_workspace(workflow["steps"][0])
        (stage_dir / "before-crash.txt").write_text("kept", encoding="utf-8")
        run_dir = interrupted.run.run_dir

        calls = []

        def success(**kwargs):
            calls.append(kwargs["workspace"])
            self.assertEqual((kwargs["workspace"] / "before-crash.txt").read_text(), "kept")
            (kwargs["workspace"] / "after-crash.txt").write_text("finished", encoding="utf-8")
            return self._codex_result(kwargs)

        with patch("conductor_runtime.core.runner.invoke_codex", side_effect=success):
            resumed = WorkflowRunner(
                workflow,
                self.workspace,
                None,
                policy,
                resume_dir=run_dir,
            ).execute()
        self.assertEqual(resumed.state["status"], "completed")
        self.assertEqual(calls, [stage_dir])
        self.assertEqual(resumed.state["steps"]["work"]["attempt"], 2)

    def test_nonresumable_writer_failure_discards_stage_before_retry(self):
        _, workflow, policy = self._writer_workflow(
            "assert not Path('discarded.txt').exists(); "
            "assert Path('clean.txt').read_text() == 'clean'"
        )

        def fail(**kwargs):
            (kwargs["workspace"] / "discarded.txt").write_text("discard me", encoding="utf-8")
            raise CodexInvocationError("provider failed", session_id="ended", resumable=False)

        with patch("conductor_runtime.core.runner.invoke_codex", side_effect=fail):
            first = WorkflowRunner(workflow, self.workspace, None, policy).execute()
        self.assertEqual(first.state["status"], "failed")
        self.assertIsNone(first.state["steps"]["work"]["pending_stage"])
        self.assertFalse((first.run_dir / "stages" / "work").exists())

        def success(**kwargs):
            self.assertFalse((kwargs["workspace"] / "discarded.txt").exists())
            (kwargs["workspace"] / "clean.txt").write_text("clean", encoding="utf-8")
            return self._codex_result(kwargs)

        with patch("conductor_runtime.core.runner.invoke_codex", side_effect=success):
            resumed = WorkflowRunner(
                workflow,
                self.workspace,
                None,
                policy,
                resume_dir=first.run_dir,
            ).execute()
        self.assertEqual(resumed.state["status"], "completed")
        evidence = strict_json_bytes(resumed.read_artifact("stages/work.json"), "stage evidence")
        self.assertNotIn("discarded.txt", {item["path"] for item in evidence["after"]["files"]})

    def test_pending_descriptor_rejects_rehashed_canonical_path_substitution(self):
        _, workflow, policy = self._writer_workflow("assert True")
        interrupted = WorkflowRunner(workflow, self.workspace, None, policy)
        interrupted.run.set_status("running")
        interrupted.run.transition_step("work", "running")
        interrupted._provider_workspace(workflow["steps"][0])
        pending = interrupted.run.state["steps"]["work"]["pending_stage"]
        descriptor = strict_json_bytes(interrupted.run.read_artifact(pending), "pending descriptor")
        descriptor["stage_path_sha256"] = "0" * 64
        descriptor.pop("descriptor_sha256")
        descriptor["descriptor_sha256"] = staged.sha256_bytes(canonical_json_bytes(descriptor))
        interrupted.run.write_artifact(pending, canonical_json_bytes(descriptor), replace=True)
        with self.assertRaisesRegex(ValidationError, "canonical path changed"):
            WorkflowRunner(
                workflow,
                self.workspace,
                None,
                policy,
                resume_dir=interrupted.run.run_dir,
            )

    def test_finalization_rejects_new_and_source_existing_excluded_roots(self):
        (self.workspace / ".git").mkdir()
        (self.workspace / ".git" / "config").write_text("source", encoding="utf-8")
        run_dir = self.root / "excluded-run"
        run_dir.mkdir()
        stage = create_stage(run_dir, "work", self.workspace)
        (stage["stage_dir"] / ".git").mkdir()
        (stage["stage_dir"] / ".git" / "config").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "excluded directories"):
            self._finalize(run_dir, stage)

        second_run = self.root / "new-excluded-run"
        second_run.mkdir()
        second = create_stage(second_run, "work", self.workspace)
        (second["stage_dir"] / "node_modules").mkdir()
        with self.assertRaisesRegex(ValidationError, "excluded directories"):
            self._finalize(second_run, second)

    def test_ignored_tree_counts_toward_quota_and_is_removed_after_timeout(self):
        _, workflow, policy = self._writer_workflow("assert False")

        def oversized_timeout(**kwargs):
            ignored = kwargs["workspace"] / "node_modules"
            ignored.mkdir()
            (ignored / "payload.bin").write_bytes(b"12345")
            raise CodexInvocationError("timed out", session_id="resume-me", resumable=True)

        with (
            patch.object(staged, "MAX_STAGE_BYTES", 4),
            patch("conductor_runtime.core.runner.invoke_codex", side_effect=oversized_timeout),
        ):
            run = WorkflowRunner(workflow, self.workspace, None, policy).execute()
        self.assertEqual(run.state["status"], "failed")
        self.assertIn("total bytes", run.state["steps"]["work"]["detail"])
        self.assertIsNone(run.state["steps"]["work"]["pending_stage"])
        self.assertFalse((run.run_dir / "stages" / "work").exists())

        quota_run = self.root / "quota-run"
        quota_run.mkdir()
        stage = create_stage(quota_run, "work", self.workspace)
        ignored = stage["stage_dir"] / "node_modules"
        ignored.mkdir()
        (ignored / "payload.bin").write_bytes(b"12345")
        with patch.object(staged, "MAX_STAGE_BYTES", 4):
            with self.assertRaisesRegex(ValidationError, "total bytes"):
                self._finalize(quota_run, stage)

    def test_invalid_excluded_output_from_downstream_verifier_is_discarded(self):
        workflow = {
            "schema": SCHEMA,
            "name": "invalid-downstream-stage",
            "agent_max_tokens": 200,
            "steps": [
                {
                    "id": "work",
                    "kind": "codex_exec",
                    "prompt": "Implement the change.",
                    "capture": "result.md",
                    "sandbox": "workspace-write",
                },
                {
                    "id": "check",
                    "kind": "codex_exec",
                    "depends_on": ["work"],
                    "prompt": "Verify the change.",
                    "capture": "verdict.json",
                    "completion_verdict": "strict-v1",
                },
            ],
            "result_artifact": "result.md",
        }
        calls = 0

        def provider(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                (kwargs["workspace"] / "valid.txt").write_text("valid", encoding="utf-8")
            else:
                ignored = kwargs["workspace"] / "node_modules"
                ignored.mkdir()
                (ignored / "verifier.bin").write_bytes(b"unaccounted")
            return self._codex_result(kwargs)

        with patch("conductor_runtime.core.runner.invoke_codex", side_effect=provider):
            run = WorkflowRunner(
                workflow,
                self.workspace,
                None,
                RuntimePolicy(allow_agent=True, allow_writes=True),
            ).execute()
        self.assertEqual(run.state["status"], "failed")
        self.assertIn("excluded directories", run.state["detail"])
        self.assertFalse((run.run_dir / "stages" / "work").exists())

    @unittest.skipUnless(os.name == "posix", "interprocess file locks require POSIX")
    def test_concurrent_runs_serialize_apply_for_the_same_canonical_workspace(self):
        winner = self._verified_stage("winner-run", "winner.txt", "winner")
        loser = self._verified_stage("loser-run", "loser.txt", "loser")
        entered = self.root / "winner-entered"
        release = self.root / "release-winner"
        second_attempted = self.root / "second-attempted"
        second_blocked = self.root / "second-blocked"
        second_acquired = self.root / "second-acquired"
        first_result = self.root / "first-result.json"
        second_result = self.root / "second-result.json"

        first_script = self._apply_script(
            winner,
            first_result,
            prelude="""
original = staged._apply_changes
def paused(source, stage, changes):
    Path(%(entered)r).write_text("entered", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not Path(%(release)r).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("test release timed out")
        time.sleep(0.01)
    original(source, stage, changes)
staged._apply_changes = paused
"""
            % {"entered": str(entered), "release": str(release)},
        )
        second_script = self._apply_script(
            loser,
            second_result,
            prelude="""
from contextlib import contextmanager
original_lock = staged.workspace_apply_lock
@contextmanager
def observed_lock(workspace):
    Path(%(attempted)r).write_text("attempted", encoding="utf-8")
    watchdog = os.fork()
    if watchdog == 0:
        time.sleep(0.25)
        if not Path(%(acquired)r).exists():
            Path(%(blocked)r).write_text("blocked", encoding="utf-8")
        os._exit(0)
    try:
        with original_lock(workspace):
            Path(%(acquired)r).write_text("acquired", encoding="utf-8")
            yield
    finally:
        os.waitpid(watchdog, 0)
staged.workspace_apply_lock = observed_lock
"""
            % {
                "attempted": str(second_attempted),
                "blocked": str(second_blocked),
                "acquired": str(second_acquired),
            },
        )
        first = subprocess.Popen(
            [sys.executable, "-c", first_script],
            cwd=Path(__file__).resolve().parents[1],
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second = None
        try:
            self._wait_for(entered)
            second = subprocess.Popen(
                [sys.executable, "-c", second_script],
                cwd=Path(__file__).resolve().parents[1],
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self._wait_for(second_attempted)
            self._wait_for(second_blocked)
            self.assertFalse(second_acquired.exists())
            self.assertIsNone(second.poll())
            release.write_text("release", encoding="utf-8")
            first_stdout, first_stderr = first.communicate(timeout=10)
            second_stdout, second_stderr = second.communicate(timeout=10)
        finally:
            if first.poll() is None:
                first.kill()
                first.wait()
            if second is not None and second.poll() is None:
                second.kill()
                second.wait()
        self.assertEqual(first.returncode, 0, (first_stdout, first_stderr))
        self.assertEqual(second.returncode, 0, (second_stdout, second_stderr))
        self.assertEqual(json.loads(first_result.read_text())["status"], "merged")
        loser_result = json.loads(second_result.read_text())
        self.assertEqual(loser_result["error"], "ValidationError")
        self.assertIn("workspace changed", loser_result["detail"])
        self.assertTrue(second_acquired.exists())
        self.assertEqual((self.workspace / "winner.txt").read_text(), "winner")
        self.assertFalse((self.workspace / "loser.txt").exists())

    def _writer_workflow(self, check_source):
        command = [
            sys.executable,
            "-c",
            "from pathlib import Path; " + check_source,
        ]
        workflow = {
            "schema": SCHEMA,
            "name": "stage-resume",
            "agent_max_tokens": 200,
            "steps": [
                {
                    "id": "work",
                    "kind": "codex_exec",
                    "prompt": "Implement the change.",
                    "capture": "result.md",
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
            "result_artifact": "result.md",
        }
        policy = RuntimePolicy(
            allow_agent=True,
            allow_writes=True,
            approvals={shell_approval(command)},
        )
        return command, workflow, policy

    @staticmethod
    def _codex_result(kwargs):
        output = "implemented"
        kwargs["run"].write_artifact(kwargs["output_relative"], output, replace=True)
        receipt = ".receipts/fake-result.json"
        kwargs["run"].write_artifact(receipt, "{}", replace=True)
        return SimpleNamespace(
            output=output,
            output_sha256=staged.sha256_bytes(output.encode("utf-8")),
            session_id="completed-session",
            usage={"total_tokens": 1},
            process=SimpleNamespace(duration_ms=1),
            receipt_relative=receipt,
        )

    def _finalize(self, run_dir, stage):
        return finalize_stage(
            run_dir=run_dir,
            run_id="stage-run",
            step_id="work",
            workspace=self.workspace,
            stage_dir=stage["stage_dir"],
            before=stage["before"],
        )

    def _verified_stage(self, run_id, filename, content):
        workflow = {
            "schema": SCHEMA,
            "name": run_id,
            "steps": [
                {
                    "id": "work",
                    "kind": "write_artifact",
                    "output": "noop.txt",
                    "content": "noop",
                }
            ],
            "result_artifact": "noop.txt",
        }
        run = WorkflowRunner(
            workflow,
            self.workspace,
            self.root / "runs",
            RuntimePolicy(),
            run_id=run_id,
        ).execute()
        self.assertEqual(run.state["status"], "completed")
        stage = create_stage(run.run_dir, "work", self.workspace)
        (stage["stage_dir"] / filename).write_text(content, encoding="utf-8")
        evidence = finalize_stage(
            run_dir=run.run_dir,
            run_id=run.descriptor["run_id"],
            step_id="work",
            workspace=self.workspace,
            stage_dir=stage["stage_dir"],
            before=stage["before"],
        )
        evidence = mark_stage_verified(evidence, ["check"])
        relative = "stages/work.json"
        run.write_artifact(relative, canonical_json_bytes(evidence))
        run.update_step("work", stage_evidence=relative)
        return run.artifact_path(relative)

    def _apply_script(self, evidence, result, *, prelude):
        return """
import json
import os
import time
from pathlib import Path
from conductor_runtime.core import staged
from conductor_runtime.core.policy import RuntimePolicy

%(prelude)s
try:
    value = staged.apply_verified_stage(
        Path(%(evidence)r),
        Path(%(workspace)r),
        policy=RuntimePolicy(allow_writes=True, approvals={"verified-stage-apply"}),
    )
except Exception as exc:
    value = {"error": exc.__class__.__name__, "detail": str(exc)}
Path(%(result)r).write_text(json.dumps(value), encoding="utf-8")
""" % {
            "prelude": prelude,
            "evidence": str(evidence),
            "workspace": str(self.workspace),
            "result": str(result),
        }

    @staticmethod
    def _wait_for(path, timeout=5):
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("timed out waiting for %s" % path)
            time.sleep(0.01)

    @staticmethod
    def _apply_policy():
        return RuntimePolicy(allow_writes=True, approvals={"verified-stage-apply"})


if __name__ == "__main__":
    unittest.main()
