import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from conductor_runtime.agent_lifecycle_hooks import (
    AGENT_LIFECYCLE_HOOK_INPUT_FIELDS,
    AGENT_LIFECYCLE_HOOK_INPUT_SCHEMA,
    agent_lifecycle_hook_input_json,
    build_agent_lifecycle_hook_input,
    build_agent_lifecycle_stop_input,
    validate_agent_lifecycle_hook_input,
)
from conductor_runtime.agent_lifecycle_stop_gate import (
    AGENT_LIFECYCLE_STOP_GATE_SCHEMA,
    bind_agent_lifecycle_stop_gate_result,
    build_agent_lifecycle_stop_gate,
    claim_agent_lifecycle_stop_gate_hook,
    complete_agent_lifecycle_stop_gate_hook,
    load_agent_lifecycle_stop_gate,
)
from conductor_runtime.direct_workspace_transaction import (
    DIRECT_WORKSPACE_TRANSACTION_SCHEMA,
    bind_direct_workspace_transaction_merge,
    build_direct_workspace_transaction,
    load_direct_workspace_transaction,
    mark_direct_workspace_transaction_merged,
    validate_direct_workspace_transaction,
)
from conductor_runtime.agent_lifecycle_context import (
    AGENT_LIFECYCLE_CONTEXT_BEGIN,
    AGENT_LIFECYCLE_CONTEXT_END,
    AGENT_LIFECYCLE_CONTEXT_SCHEMA,
    load_agent_lifecycle_context_receipt,
    verify_agent_lifecycle_context,
)
from conductor_runtime.errors import StepExecutionError, ValidationError
from conductor_runtime.runner import ProcessResult, WorkflowRunner
from conductor_runtime.schemas import get_schema
from conductor_runtime.security import RuntimePolicy
from conductor_runtime.staged_workspace import (
    plan_workspace_delta_merge,
    snapshot_workspace,
    workspace_snapshot_manifest,
)
from conductor_runtime.workflow import validate_workflow


WORKFLOW_SHA256 = "a" * 64
PROMPT_SHA256 = "b" * 64


def lifecycle_input(event="agent_start", scope="codex_exec", **overrides):
    values = {
        "event": event,
        "workflow_fingerprint": WORKFLOW_SHA256,
        "step_id": "agent",
        "step_kind": "codex_exec",
        "scope": scope,
        "prompt_sha256": PROMPT_SHA256,
        "attempt": 1,
        "sandbox": "read-only",
        "session_mode": "new",
    }
    values.update(overrides)
    return build_agent_lifecycle_hook_input(**values)


def codex_workflow(hooks):
    return {
        "schema": "conductor.workflow.v1",
        "name": "agent-lifecycle-test",
        "mode": "read_only",
        "max_workers": 1,
        "steps": [
            {
                "id": "agent",
                "kind": "codex_exec",
                "risk": "low",
                "sandbox": "read-only",
                "prompt": "Perform the bounded task.",
                "capture": "agent.md",
            }
        ],
        "hooks": hooks,
    }


def codex_write_workflow(hooks):
    workflow = codex_workflow(hooks)
    workflow["name"] = "agent-lifecycle-write-test"
    workflow["mode"] = "workspace_write"
    workflow["steps"][0]["sandbox"] = "workspace-write"
    return workflow


class FakeLifecycleRunner(WorkflowRunner):
    def __init__(self, *args, **kwargs):
        self.provider_calls = 0
        self.provider_prompts = []
        self.context_calls = 0
        self.raise_provider_error = False
        super().__init__(*args, **kwargs)

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
        del cwd, timeout, output_limit_bytes, stdout_line_callback, interrupt_check
        if argv[0] == "cat":
            return ProcessResult(returncode=0, stdout=input_text or "", stderr="")
        if argv[0] == "false":
            return ProcessResult(returncode=1, stdout="", stderr="rejected")
        if argv[0] == "true":
            self.context_calls += 1
            return ProcessResult(returncode=0, stdout="", stderr="")
        if argv[0] == "printf":
            self.context_calls += 1
            output = argv[1]
            if output.startswith("repeat:"):
                output = "x" * int(output.split(":", 1)[1])
            return ProcessResult(
                returncode=0,
                stdout=output,
                stderr="",
                stdout_truncated=len(argv) > 2 and argv[2] == "truncated",
            )
        if argv[0] != "provider":
            raise AssertionError("unexpected command %r" % (argv,))
        self.provider_calls += 1
        self.provider_prompts.append(input_text or "")
        if self.raise_provider_error:
            raise RuntimeError("provider transport failed")
        Path(argv[1]).write_text("provider result\n", encoding="utf-8")
        return ProcessResult(
            returncode=0,
            stdout=(
                '{"type":"thread.started","thread_id":"00000000-0000-4000-8000-%012d"}\n'
                '{"type":"turn.completed"}\n' % self.provider_calls
            ),
            stderr="",
        )


class CommandProviderRunner(WorkflowRunner):
    def _codex_command(self, step, output_path, *, workspace=None):
        del step, workspace
        code = (
            "import pathlib,sys; "
            "pathlib.Path(sys.argv[1]).write_text(sys.stdin.read(),encoding='utf-8')"
        )
        return [sys.executable, "-c", code, str(output_path)]


class FakeCommandLifecycleRunner(FakeLifecycleRunner):
    def _codex_command(self, step, output_path, *, workspace=None):
        del step, workspace
        return ["provider", str(output_path)]


class CrashAfterCodexTerminalRunner(FakeCommandLifecycleRunner):
    def __init__(self, *args, **kwargs):
        self.crash_after_terminal = True
        super().__init__(*args, **kwargs)

    def _codex_step_terminal_checkpoint(self, phase, terminal):
        del terminal
        if phase == "written" and self.crash_after_terminal:
            self.crash_after_terminal = False
            raise RuntimeError("simulated crash after codex terminal")


class CrashAfterCompletedCodexTerminalRunner(FakeCommandLifecycleRunner):
    def __init__(self, *args, **kwargs):
        self.crash_after_terminal = True
        super().__init__(*args, **kwargs)

    def _codex_step_terminal_checkpoint(self, phase, terminal):
        del terminal
        if phase == "step-completed" and self.crash_after_terminal:
            self.crash_after_terminal = False
            raise RuntimeError("simulated crash after completed codex terminal")


class CrashAfterMapTerminalRunner(FakeCommandLifecycleRunner):
    def __init__(self, *args, **kwargs):
        self.crash_after_terminal = True
        super().__init__(*args, **kwargs)

    def _agent_map_packet_terminal_checkpoint(self, phase, terminal):
        del terminal
        if phase == "written" and self.crash_after_terminal:
            self.crash_after_terminal = False
            raise RuntimeError("simulated crash after map terminal")


class SimulatedLifecycleHardCrash(BaseException):
    pass


class CrashSafeStopGateRunner(FakeCommandLifecycleRunner):
    def __init__(
        self,
        *args,
        stop_gate_crash_phase=None,
        crash_after_stop_hook=False,
        **kwargs,
    ):
        self.stop_gate_crash_phase = stop_gate_crash_phase
        self.crash_after_stop_hook = crash_after_stop_hook
        super().__init__(*args, **kwargs)

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
        if argv[0] != "provider":
            return super()._run_process(
                argv,
                cwd,
                timeout,
                input_text=input_text,
                output_limit_bytes=output_limit_bytes,
                stdout_line_callback=stdout_line_callback,
                interrupt_check=interrupt_check,
            )
        del cwd, timeout, output_limit_bytes, interrupt_check
        self.provider_calls += 1
        self.provider_prompts.append(input_text or "")
        Path(argv[1]).write_text("provider result\n", encoding="utf-8")
        stdout = (
            '{"type":"thread.started","thread_id":"00000000-0000-4000-8000-%012d"}\n'
            '{"type":"turn.completed"}\n' % self.provider_calls
        )
        if stdout_line_callback is not None:
            for line in stdout.splitlines():
                stdout_line_callback(line)
        return ProcessResult(returncode=0, stdout=stdout, stderr="")

    def _agent_lifecycle_stop_gate_checkpoint(self, phase, gate):
        del gate
        if phase == self.stop_gate_crash_phase:
            self.stop_gate_crash_phase = None
            raise SimulatedLifecycleHardCrash(
                "simulated hard crash at stop gate %s" % phase
            )

    def _execute_hook(self, hook, event, *args, **kwargs):
        result = super()._execute_hook(hook, event, *args, **kwargs)
        if event == "agent_stop" and self.crash_after_stop_hook:
            self.crash_after_stop_hook = False
            raise SimulatedLifecycleHardCrash(
                "simulated hard crash after stop hook execution"
            )
        return result


class TransactionalWorkspaceRunner(CrashSafeStopGateRunner):
    def __init__(
        self,
        *args,
        transaction_crash_phase=None,
        terminal_crash_phase=None,
        partial_merge_crash=False,
        emit_terminal=True,
        mutate_source_during_stop=False,
        **kwargs,
    ):
        self.transaction_crash_phase = transaction_crash_phase
        self.terminal_crash_phase = terminal_crash_phase
        self.partial_merge_crash = partial_merge_crash
        self.emit_terminal = emit_terminal
        self.mutate_source_during_stop = mutate_source_during_stop
        self.hook_workspace_observations = []
        super().__init__(*args, **kwargs)

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
        if argv[0] == "provider":
            Path(cwd, "target.txt").write_text(
                "provider change\n",
                encoding="utf-8",
            )
            second = Path(cwd, "second.txt")
            if second.is_file():
                second.write_text("provider second change\n", encoding="utf-8")
            if not self.emit_terminal:
                self.provider_calls += 1
                self.provider_prompts.append(input_text or "")
                Path(argv[1]).write_text("provider result\n", encoding="utf-8")
                return ProcessResult(returncode=0, stdout="", stderr="")
        else:
            source_path = Path(self.workspace, "target.txt")
            stage_path = Path(cwd, "target.txt")
            self.hook_workspace_observations.append(
                {
                    "cwd": str(Path(cwd).resolve()),
                    "source": source_path.read_text(encoding="utf-8"),
                    "stage": stage_path.read_text(encoding="utf-8"),
                }
            )
            if self.mutate_source_during_stop:
                self.mutate_source_during_stop = False
                source_path.write_text("concurrent source change\n", encoding="utf-8")
        return super()._run_process(
            argv,
            cwd,
            timeout,
            input_text=input_text,
            output_limit_bytes=output_limit_bytes,
            stdout_line_callback=stdout_line_callback,
            interrupt_check=interrupt_check,
        )

    def _direct_workspace_transaction_checkpoint(self, phase, transaction):
        del transaction
        if phase == self.transaction_crash_phase:
            self.transaction_crash_phase = None
            raise SimulatedLifecycleHardCrash(
                "simulated hard crash at direct workspace %s" % phase
            )

    def _codex_step_terminal_checkpoint(self, phase, terminal):
        del terminal
        if phase == self.terminal_crash_phase:
            self.terminal_crash_phase = None
            raise SimulatedLifecycleHardCrash(
                "simulated hard crash at Codex terminal %s" % phase
            )

    def _apply_direct_workspace_transaction(
        self,
        stage,
        stage_result,
        plan,
        source_before,
    ):
        if self.partial_merge_crash:
            self.partial_merge_crash = False
            path = plan["apply_files"][0]
            record = stage_result.entries.get(path)
            target = Path(self.workspace, path)
            if record is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(Path(stage, path).read_bytes())
                target.chmod(0o755 if record.executable else 0o644)
            raise SimulatedLifecycleHardCrash(
                "simulated hard crash during partial direct workspace merge"
            )
        return super()._apply_direct_workspace_transaction(
            stage,
            stage_result,
            plan,
            source_before,
        )


class AgentLifecycleHookContractTests(unittest.TestCase):
    def test_direct_workspace_transaction_state_machine_is_hash_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            stage = root / "stage"
            source.mkdir()
            stage.mkdir()
            (source / "target.txt").write_text("base\n", encoding="utf-8")
            (stage / "target.txt").write_text("result\n", encoding="utf-8")
            base = snapshot_workspace(source)
            result = snapshot_workspace(stage)
            source_before = snapshot_workspace(source)
            transaction = build_direct_workspace_transaction(
                workflow_fingerprint=WORKFLOW_SHA256,
                step_id="agent",
                attempt=1,
                source_workspace_sha256="c" * 64,
                base_manifest=workspace_snapshot_manifest(base),
                created_at_utc="2026-07-12T00:00:00.000Z",
            )
            self.assertEqual(
                transaction["schema"],
                DIRECT_WORKSPACE_TRANSACTION_SCHEMA,
            )
            self.assertEqual(transaction["status"], "prepared")
            plan = plan_workspace_delta_merge(base, source_before, result)
            transaction = bind_direct_workspace_transaction_merge(
                transaction,
                result_manifest=workspace_snapshot_manifest(result),
                source_before_manifest=workspace_snapshot_manifest(source_before),
                merge_plan=plan,
                invocation_sha256="d" * 64,
                terminal_sha256="e" * 64,
                stop_gate_sha256="f" * 64,
                updated_at_utc="2026-07-12T00:00:01.000Z",
            )
            self.assertEqual(transaction["status"], "merging")
            transaction = mark_direct_workspace_transaction_merged(
                transaction,
                updated_at_utc="2026-07-12T00:00:02.000Z",
            )
            validate_direct_workspace_transaction(transaction)
            self.assertEqual(transaction["status"], "merged")
            schema = get_schema("direct-workspace-transaction")
            self.assertEqual(
                schema["properties"]["schema"]["const"],
                DIRECT_WORKSPACE_TRANSACTION_SCHEMA,
            )
            self.assertEqual(
                set(schema["required"]),
                set(transaction),
            )
            self.assertFalse(schema["additionalProperties"])
            changed = json.loads(json.dumps(transaction))
            changed["merge_plan"]["changed_files"] = []
            with self.assertRaises(ValidationError):
                validate_direct_workspace_transaction(changed)

    def test_strict_content_free_contract_and_schema(self):
        start = lifecycle_input(
            model="gpt-test",
            effort="high",
            max_tokens=12000,
            session_id="private-session",
        )
        self.assertEqual(set(start), AGENT_LIFECYCLE_HOOK_INPUT_FIELDS)
        self.assertEqual(start["schema"], AGENT_LIFECYCLE_HOOK_INPUT_SCHEMA)
        self.assertEqual(start["status"], "starting")
        self.assertEqual(
            start["session_id_sha256"],
            hashlib.sha256(b"private-session").hexdigest(),
        )
        rendered = agent_lifecycle_hook_input_json(start)
        self.assertNotIn("gpt-test", rendered)
        self.assertNotIn("private-session", rendered)
        self.assertNotIn("Perform the bounded task", rendered)
        schema = get_schema("agent-lifecycle-hook-input")
        self.assertEqual(
            schema["properties"]["schema"]["const"],
            AGENT_LIFECYCLE_HOOK_INPUT_SCHEMA,
        )
        self.assertFalse(schema["additionalProperties"])

    def test_every_scope_has_strict_identity_requirements(self):
        payloads = [
            lifecycle_input(),
            lifecycle_input(
                scope="agent_map_packet",
                step_kind="agent_map",
                packet_index=2,
                packet_generation=3,
            ),
            lifecycle_input(
                scope="agent_team_plan",
                step_kind="agent_team",
                member_id="lead",
                task_id="task",
                generation=1,
                round_number=0,
                plan_revision=1,
            ),
            lifecycle_input(
                scope="agent_team_review",
                step_kind="agent_team",
                member_id="reviewer",
                task_id="task",
                generation=1,
                round_number=0,
                plan_revision=1,
            ),
            lifecycle_input(
                scope="agent_team_turn",
                step_kind="agent_team",
                member_id="worker",
                task_id="task",
                generation=1,
                round_number=2,
            ),
            lifecycle_input(
                scope="agent_team_quality_retry",
                step_kind="agent_team",
                member_id="worker",
                task_id="task",
                generation=1,
                round_number=2,
                quality_retry_index=1,
                session_id="same-session",
                session_mode="resume",
            ),
        ]
        for payload in payloads:
            validate_agent_lifecycle_hook_input(payload)
            stop = dict(payload)
            stop.update(
                {
                    "event": "agent_stop",
                    "status": "returned",
                    "returncode": 0,
                    "provider_stdout_sha256": "c" * 64,
                    "provider_stderr_sha256": "d" * 64,
                }
            )
            validate_agent_lifecycle_hook_input(stop)

        bad = dict(payloads[-1])
        bad["quality_retry_index"] = None
        with self.assertRaises(ValidationError):
            validate_agent_lifecycle_hook_input(bad)
        bad = dict(payloads[1])
        bad["step_kind"] = "codex_exec"
        with self.assertRaises(ValidationError):
            validate_agent_lifecycle_hook_input(bad)

    def test_stop_gate_state_machine_is_hash_bound_and_ordered(self):
        start = lifecycle_input()
        hooks = [
            {"id": "advisory", "on_failure": "warn"},
            {"id": "required", "on_failure": "block"},
        ]
        gate = build_agent_lifecycle_stop_gate(
            start,
            hooks,
            created_at_utc="2026-07-12T00:00:00.000Z",
        )
        self.assertEqual(gate["schema"], AGENT_LIFECYCLE_STOP_GATE_SCHEMA)
        self.assertEqual(
            get_schema("agent-lifecycle-stop-gate")["properties"]["schema"]["const"],
            AGENT_LIFECYCLE_STOP_GATE_SCHEMA,
        )
        self.assertEqual(gate["status"], "pending")
        stop = build_agent_lifecycle_stop_input(
            start,
            status="returned",
            returncode=0,
            output_sha256="c" * 64,
        )
        gate = bind_agent_lifecycle_stop_gate_result(
            gate,
            stop,
            updated_at_utc="2026-07-12T00:00:01.000Z",
        )
        gate = claim_agent_lifecycle_stop_gate_hook(
            gate,
            updated_at_utc="2026-07-12T00:00:02.000Z",
        )
        gate = complete_agent_lifecycle_stop_gate_hook(
            gate,
            {
                "hook": hooks[0],
                "status": "failed",
                "stdout": "bounded warning",
                "stdout_truncated": False,
            },
            updated_at_utc="2026-07-12T00:00:03.000Z",
        )
        self.assertEqual(gate["status"], "ready")
        self.assertEqual(gate["warning_count"], 1)
        gate = claim_agent_lifecycle_stop_gate_hook(gate)
        gate = complete_agent_lifecycle_stop_gate_hook(
            gate,
            {
                "hook": hooks[1],
                "status": "completed",
                "stdout": "",
                "stdout_truncated": False,
            },
        )
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["next_hook_index"], 2)

        changed = dict(gate)
        changed["warning_count"] = 0
        with self.assertRaises(ValidationError):
            complete_agent_lifecycle_stop_gate_hook(changed, {})


class AgentLifecycleWorkflowTests(unittest.TestCase):
    def test_validation_accepts_filters_and_rejects_unsafe_shapes(self):
        workflow = codex_workflow(
            [
                {
                    "id": "start",
                    "event": "agent_start",
                    "command": ["cat"],
                    "steps": ["agent"],
                    "scopes": ["codex_exec"],
                }
            ]
        )
        validate_workflow(workflow)

        for field in ("writes", "destructive", "network", "external_path"):
            bad = json.loads(json.dumps(workflow))
            bad["hooks"][0][field] = True
            with self.assertRaises(ValidationError, msg=field):
                validate_workflow(bad)
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][0]["capture"] = "shared.txt"
        with self.assertRaises(ValidationError):
            validate_workflow(bad)

    def test_agent_stop_block_is_explicit_and_retry_remains_invalid(self):
        blocking = codex_workflow(
            [
                {
                    "id": "required-stop",
                    "event": "agent_stop",
                    "command": ["cat"],
                    "on_failure": "block",
                }
            ]
        )
        validate_workflow(blocking)
        retrying = json.loads(json.dumps(blocking))
        retrying["hooks"][0]["on_failure"] = "retry"
        with self.assertRaises(ValidationError):
            validate_workflow(retrying)

    def test_context_injection_validation_is_explicit_and_fail_closed(self):
        workflow = codex_workflow(
            [
                {
                    "id": "context",
                    "event": "agent_start",
                    "command": ["cat"],
                    "scopes": ["codex_exec"],
                    "inject_context": True,
                    "context_limit_bytes": 4096,
                }
            ]
        )
        validate_workflow(workflow)

        mutations = [
            {"event": "agent_stop", "on_failure": "warn"},
            {"on_failure": "warn"},
            {"scopes": ["agent_team_turn"]},
            {"members": ["worker"]},
            {"context_limit_bytes": 65537},
        ]
        for mutation in mutations:
            bad = json.loads(json.dumps(workflow))
            bad["hooks"][0].update(mutation)
            with self.assertRaises(ValidationError, msg=str(mutation)):
                validate_workflow(bad)

        missing_scope = json.loads(json.dumps(workflow))
        missing_scope["hooks"][0].pop("scopes")
        with self.assertRaises(ValidationError):
            validate_workflow(missing_scope)

        no_injection = json.loads(json.dumps(workflow))
        no_injection["hooks"][0]["inject_context"] = False
        with self.assertRaises(ValidationError):
            validate_workflow(no_injection)
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][0]["on_failure"] = "retry"
        with self.assertRaises(ValidationError):
            validate_workflow(bad)
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][0]["scopes"] = ["agent_map_packet"]
        with self.assertRaises(ValidationError):
            validate_workflow(bad)
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][0]["members"] = ["worker"]
        with self.assertRaises(ValidationError):
            validate_workflow(bad)
        bad = json.loads(json.dumps(workflow))
        bad["hooks"][0].update(
            {"event": "agent_stop", "on_failure": "block"}
        )
        with self.assertRaises(ValidationError):
            validate_workflow(bad)


class AgentLifecycleRunnerTests(unittest.TestCase):
    def _runner(self, root, hooks):
        return FakeLifecycleRunner(
            workflow=codex_workflow(hooks),
            workspace=root,
            base_run_dir=root / "runs",
            policy=RuntimePolicy(),
        )

    def _invoke(self, runner, cleanup=None):
        step = runner.workflow["steps"][0]
        runner.run.mark_step_started(step["id"], step["kind"])
        output_path = runner.run.resolve_artifact_path("agent.md")
        result = runner._run_codex_provider(
            workflow_step=step,
            provider_step=step,
            scope="codex_exec",
            command=["provider", str(output_path)],
            prompt="private prompt text",
            cwd=runner.workspace,
            timeout=10,
            output_limit_bytes=4096,
            output_path=output_path,
            reject_cleanup=cleanup,
            expected_session_id="private-session-id",
        )
        return result, output_path

    def test_blocking_direct_write_is_isolated_until_stop_gate_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "target.txt").write_text("base\n", encoding="utf-8")
            runner = TransactionalWorkspaceRunner(
                workflow=codex_write_workflow(
                    [
                        {
                            "id": "advisory",
                            "event": "agent_stop",
                            "command": ["false"],
                            "on_failure": "warn",
                        },
                        {
                            "id": "required",
                            "event": "agent_stop",
                            "command": ["cat"],
                            "on_failure": "block",
                        }
                    ]
                ),
                workspace=source,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
            )
            runner.execute()
            self.assertEqual(
                (source / "target.txt").read_text(encoding="utf-8"),
                "provider change\n",
            )
            self.assertEqual(runner.provider_calls, 1)
            self.assertEqual(len(runner.hook_workspace_observations), 2)
            for observed in runner.hook_workspace_observations:
                self.assertEqual(observed["source"], "base\n")
                self.assertEqual(observed["stage"], "provider change\n")
                self.assertNotEqual(observed["cwd"], str(source.resolve()))
            transaction_path = next(
                (runner.run.artifacts_dir / ".direct-workspace-transactions").glob("*.json")
            )
            transaction = load_direct_workspace_transaction(transaction_path)
            self.assertEqual(transaction["status"], "merged")
            self.assertFalse((runner.run.run_dir / transaction["stage_relative"]).exists())
            metrics = runner.run.read_state()["steps"]["agent"]
            self.assertTrue(metrics["direct_workspace_transactional"])
            self.assertEqual(metrics["direct_workspace_changed_files"], 1)

    def test_blocking_direct_write_failure_never_mutates_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "target.txt").write_text("base\n", encoding="utf-8")
            runner = TransactionalWorkspaceRunner(
                workflow=codex_write_workflow(
                    [
                        {
                            "id": "required",
                            "event": "agent_stop",
                            "command": ["false"],
                            "on_failure": "block",
                        }
                    ]
                ),
                workspace=source,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
            )
            with self.assertRaises(StepExecutionError):
                runner.execute()
            self.assertEqual(
                (source / "target.txt").read_text(encoding="utf-8"),
                "base\n",
            )
            transaction_path = next(
                (runner.run.artifacts_dir / ".direct-workspace-transactions").glob("*.json")
            )
            transaction = load_direct_workspace_transaction(transaction_path)
            self.assertEqual(transaction["status"], "rejected")
            self.assertFalse((runner.run.run_dir / transaction["stage_relative"]).exists())

    def test_completed_direct_write_can_start_a_new_explicit_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "target.txt").write_text("base\n", encoding="utf-8")
            runner = TransactionalWorkspaceRunner(
                workflow=codex_write_workflow(
                    [
                        {
                            "id": "required",
                            "event": "agent_stop",
                            "command": ["cat"],
                            "on_failure": "block",
                        }
                    ]
                ),
                workspace=source,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
            )
            runner.execute()
            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            runner._codex_exec(step)
            self.assertEqual(runner.provider_calls, 2)
            transaction_path = next(
                (runner.run.artifacts_dir / ".direct-workspace-transactions").glob("*.json")
            )
            transaction = load_direct_workspace_transaction(transaction_path)
            self.assertEqual(transaction["attempt"], 2)
            self.assertEqual(transaction["status"], "merged")

    def test_blocking_direct_write_without_terminal_never_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "target.txt").write_text("base\n", encoding="utf-8")
            runner = TransactionalWorkspaceRunner(
                workflow=codex_write_workflow(
                    [
                        {
                            "id": "required",
                            "event": "agent_stop",
                            "command": ["cat"],
                            "on_failure": "block",
                        }
                    ]
                ),
                workspace=source,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                emit_terminal=False,
            )
            with self.assertRaisesRegex(
                ValidationError,
                "requires exact local terminal evidence",
            ):
                runner.execute()
            self.assertEqual(runner.provider_calls, 1)
            self.assertEqual(
                (source / "target.txt").read_text(encoding="utf-8"),
                "base\n",
            )
            transaction_path = next(
                (runner.run.artifacts_dir / ".direct-workspace-transactions").glob("*.json")
            )
            transaction = load_direct_workspace_transaction(transaction_path)
            self.assertEqual(transaction["status"], "prepared")
            self.assertTrue((runner.run.run_dir / transaction["stage_relative"]).is_dir())

    def test_advisory_direct_write_keeps_zero_copy_fast_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "target.txt").write_text("base\n", encoding="utf-8")
            runner = TransactionalWorkspaceRunner(
                workflow=codex_write_workflow(
                    [
                        {
                            "id": "advisory",
                            "event": "agent_stop",
                            "command": ["cat"],
                            "on_failure": "warn",
                        }
                    ]
                ),
                workspace=source,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
            )
            runner.execute()
            self.assertEqual(
                runner.hook_workspace_observations[0]["cwd"],
                str(source.resolve()),
            )
            self.assertFalse(
                (runner.run.artifacts_dir / ".direct-workspace-transactions").exists()
            )
            self.assertFalse((runner.run.run_dir / ".direct-workspaces").exists())

    def test_direct_write_terminal_recovery_merges_without_provider_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "target.txt").write_text("base\n", encoding="utf-8")
            runner = TransactionalWorkspaceRunner(
                workflow=codex_write_workflow(
                    [
                        {
                            "id": "required",
                            "event": "agent_stop",
                            "command": ["cat"],
                            "on_failure": "block",
                        }
                    ]
                ),
                workspace=source,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                stop_gate_crash_phase="ready",
            )
            with self.assertRaises(SimulatedLifecycleHardCrash):
                runner.execute()
            self.assertEqual(
                (source / "target.txt").read_text(encoding="utf-8"),
                "base\n",
            )
            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            runner._codex_exec(step)
            self.assertEqual(runner.provider_calls, 1)
            self.assertEqual(
                (source / "target.txt").read_text(encoding="utf-8"),
                "provider change\n",
            )

    def test_direct_write_merge_recovers_before_and_after_source_apply(self):
        for phase, expected_before in (
            ("merging", "base\n"),
            ("source-merged", "provider change\n"),
            ("merged", "provider change\n"),
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source"
                source.mkdir()
                (source / "target.txt").write_text("base\n", encoding="utf-8")
                runner = TransactionalWorkspaceRunner(
                    workflow=codex_write_workflow(
                        [
                            {
                                "id": "required",
                                "event": "agent_stop",
                                "command": ["cat"],
                                "on_failure": "block",
                            }
                        ]
                    ),
                    workspace=source,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                    transaction_crash_phase=phase,
                )
                with self.assertRaises(SimulatedLifecycleHardCrash):
                    runner.execute()
                self.assertEqual(
                    (source / "target.txt").read_text(encoding="utf-8"),
                    expected_before,
                )
                step = runner.workflow["steps"][0]
                runner.run.mark_step_started(step["id"], step["kind"])
                runner._codex_exec(step)
                self.assertEqual(runner.provider_calls, 1)
                self.assertEqual(
                    (source / "target.txt").read_text(encoding="utf-8"),
                    "provider change\n",
                )
                transaction_path = next(
                    (runner.run.artifacts_dir / ".direct-workspace-transactions").glob("*.json")
                )
                self.assertEqual(
                    load_direct_workspace_transaction(transaction_path)["status"],
                    "merged",
                )

    def test_direct_write_partial_merge_recovers_remaining_files_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "target.txt").write_text("base\n", encoding="utf-8")
            (source / "second.txt").write_text("second base\n", encoding="utf-8")
            runner = TransactionalWorkspaceRunner(
                workflow=codex_write_workflow(
                    [
                        {
                            "id": "required",
                            "event": "agent_stop",
                            "command": ["cat"],
                            "on_failure": "block",
                        }
                    ]
                ),
                workspace=source,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                partial_merge_crash=True,
            )
            with self.assertRaises(SimulatedLifecycleHardCrash):
                runner.execute()
            values = {
                (source / "target.txt").read_text(encoding="utf-8"),
                (source / "second.txt").read_text(encoding="utf-8"),
            }
            self.assertIn("base\n", values)
            self.assertTrue(
                {"provider change\n", "provider second change\n"} & values
            )
            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            runner._codex_exec(step)
            self.assertEqual(runner.provider_calls, 1)
            self.assertEqual(
                (source / "target.txt").read_text(encoding="utf-8"),
                "provider change\n",
            )
            self.assertEqual(
                (source / "second.txt").read_text(encoding="utf-8"),
                "provider second change\n",
            )

    def test_completed_direct_write_terminal_reconciliation_cleans_stage(self):
        for crash_kwargs in (
            {"terminal_crash_phase": "step-completed"},
            {"transaction_crash_phase": "terminal-removed"},
        ):
            with self.subTest(crash_kwargs=crash_kwargs), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source"
                source.mkdir()
                (source / "target.txt").write_text("base\n", encoding="utf-8")
                runner = TransactionalWorkspaceRunner(
                    workflow=codex_write_workflow(
                        [
                            {
                                "id": "required",
                                "event": "agent_stop",
                                "command": ["cat"],
                                "on_failure": "block",
                            }
                        ]
                    ),
                    workspace=source,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                    **crash_kwargs,
                )
                with self.assertRaises(SimulatedLifecycleHardCrash):
                    runner.execute()
                transaction_path = next(
                    (runner.run.artifacts_dir / ".direct-workspace-transactions").glob("*.json")
                )
                transaction = load_direct_workspace_transaction(transaction_path)
                self.assertEqual(transaction["status"], "merged")
                self.assertTrue((runner.run.run_dir / transaction["stage_relative"]).is_dir())
                runner.execute()
                self.assertEqual(runner.provider_calls, 1)
                self.assertFalse((runner.run.run_dir / transaction["stage_relative"]).exists())

    def test_direct_write_transaction_tamper_and_symlink_fail_before_replay(self):
        for mutation in ("hash", "missing", "stage", "symlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source"
                source.mkdir()
                (source / "target.txt").write_text("base\n", encoding="utf-8")
                runner = TransactionalWorkspaceRunner(
                    workflow=codex_write_workflow(
                        [
                            {
                                "id": "required",
                                "event": "agent_stop",
                                "command": ["cat"],
                                "on_failure": "block",
                            }
                        ]
                    ),
                    workspace=source,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                    stop_gate_crash_phase="ready",
                )
                with self.assertRaises(SimulatedLifecycleHardCrash):
                    runner.execute()
                transaction_path = next(
                    (runner.run.artifacts_dir / ".direct-workspace-transactions").glob("*.json")
                )
                if mutation == "hash":
                    value = json.loads(transaction_path.read_text(encoding="utf-8"))
                    value["source_workspace_sha256"] = "0" * 64
                    transaction_path.write_text(
                        json.dumps(value, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                elif mutation == "missing":
                    transaction_path.unlink()
                elif mutation == "stage":
                    transaction = load_direct_workspace_transaction(transaction_path)
                    (runner.run.run_dir / transaction["stage_relative"] / "target.txt").write_text(
                        "tampered stage\n",
                        encoding="utf-8",
                    )
                else:
                    target = root / "transaction.json"
                    transaction_path.rename(target)
                    transaction_path.symlink_to(target)
                step = runner.workflow["steps"][0]
                runner.run.mark_step_started(step["id"], step["kind"])
                with self.assertRaises(ValidationError):
                    runner._codex_exec(step)
                self.assertEqual(runner.provider_calls, 1)
                self.assertEqual(
                    (source / "target.txt").read_text(encoding="utf-8"),
                    "base\n",
                )

    def test_blocking_direct_write_rejects_repo_local_stage_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / "target.txt").write_text("base\n", encoding="utf-8")
            runner = TransactionalWorkspaceRunner(
                workflow=codex_write_workflow(
                    [
                        {
                            "id": "required",
                            "event": "agent_stop",
                            "command": ["cat"],
                            "on_failure": "block",
                        }
                    ]
                ),
                workspace=source,
                base_run_dir=source / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
            )
            with self.assertRaisesRegex(ValidationError, "outside the source workspace"):
                runner.execute()
            self.assertEqual(runner.provider_calls, 0)
            self.assertEqual(
                (source / "target.txt").read_text(encoding="utf-8"),
                "base\n",
            )

    def test_direct_write_source_drift_records_conflict_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "target.txt").write_text("base\n", encoding="utf-8")
            runner = TransactionalWorkspaceRunner(
                workflow=codex_write_workflow(
                    [
                        {
                            "id": "required",
                            "event": "agent_stop",
                            "command": ["cat"],
                            "on_failure": "block",
                        }
                    ]
                ),
                workspace=source,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
                mutate_source_during_stop=True,
            )
            with self.assertRaisesRegex(StepExecutionError, "conflicts"):
                runner.execute()
            self.assertEqual(
                (source / "target.txt").read_text(encoding="utf-8"),
                "concurrent source change\n",
            )
            transaction_path = next(
                (runner.run.artifacts_dir / ".direct-workspace-transactions").glob("*.json")
            )
            transaction = load_direct_workspace_transaction(transaction_path)
            self.assertEqual(transaction["status"], "conflict")
            self.assertEqual(transaction["merge_plan"]["conflicting_files"], ["target.txt"])
            terminal_dir = runner.run.artifacts_dir / ".codex-step-terminals"
            self.assertFalse(terminal_dir.exists() and list(terminal_dir.glob("*.json")))
            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            runner._codex_exec(step)
            self.assertEqual(runner.provider_calls, 2)
            self.assertEqual(
                (source / "target.txt").read_text(encoding="utf-8"),
                "provider change\n",
            )

    def test_start_and_stop_hooks_receive_hash_only_payloads_and_unique_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = self._runner(
                root,
                [
                    {"id": "start", "event": "agent_start", "command": ["cat"]},
                    {"id": "stop", "event": "agent_stop", "command": ["cat"]},
                ],
            )
            result, output_path = self._invoke(runner)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(runner.provider_calls, 1)
            events = runner.run.read_state()["hook_events"]
            self.assertEqual([event["event"] for event in events], ["agent_start", "agent_stop"])
            self.assertEqual(events[0]["invocation_sha256"], events[1]["invocation_sha256"])
            self.assertEqual(events[0]["scope"], "codex_exec")
            self.assertEqual(events[0]["on_failure"], "block")
            self.assertEqual(events[1]["on_failure"], "warn")
            logs = sorted(runner.run.logs_dir.rglob("*.stdout.log"))
            self.assertEqual(len(logs), 2)
            payloads = [json.loads(path.read_text(encoding="utf-8")) for path in logs]
            by_event = {payload["event"]: payload for payload in payloads}
            self.assertEqual(by_event["agent_start"]["status"], "starting")
            self.assertEqual(by_event["agent_stop"]["status"], "returned")
            self.assertEqual(
                by_event["agent_stop"]["output_sha256"],
                hashlib.sha256(output_path.read_bytes()).hexdigest(),
            )
            serialized = "\n".join(path.read_text(encoding="utf-8") for path in logs)
            self.assertNotIn("private prompt text", serialized)
            self.assertNotIn("private-session-id", serialized)
            self.assertNotIn("provider result", serialized)

    def test_start_block_prevents_provider_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            start_runner = self._runner(
                root,
                [{"id": "start", "event": "agent_start", "command": ["false"]}],
            )
            with self.assertRaises(StepExecutionError):
                self._invoke(start_runner)
            self.assertEqual(start_runner.provider_calls, 0)

    def test_warn_and_runner_error_preserve_primary_provider_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            warn_runner = self._runner(
                root,
                [
                    {
                        "id": "stop",
                        "event": "agent_stop",
                        "command": ["false"],
                        "on_failure": "warn",
                    }
                ],
            )
            result, _ = self._invoke(warn_runner)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(warn_runner.run.read_state()["hook_events"][0]["status"], "failed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            error_runner = self._runner(
                root,
                [{"id": "stop", "event": "agent_stop", "command": ["cat"]}],
            )
            error_runner.raise_provider_error = True
            with self.assertRaisesRegex(RuntimeError, "provider transport failed"):
                self._invoke(error_runner)
            log = next(error_runner.run.logs_dir.rglob("*.stdout.log"))
            payload = json.loads(log.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "runner_error")
            self.assertEqual(payload["error_class"], "RuntimeError")

    def test_blocking_stop_gate_passes_in_order_and_advisory_path_stays_artifact_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = self._runner(
                root,
                [
                    {
                        "id": "advisory",
                        "event": "agent_stop",
                        "command": ["false"],
                        "on_failure": "warn",
                    },
                    {
                        "id": "required",
                        "event": "agent_stop",
                        "command": ["cat"],
                        "on_failure": "block",
                    },
                ],
            )
            result, _ = self._invoke(runner)
            self.assertEqual(result.returncode, 0)
            gate_path = next(
                (runner.run.artifacts_dir / ".agent-lifecycle-stop-gates").glob("*.json")
            )
            gate = load_agent_lifecycle_stop_gate(gate_path)
            self.assertEqual(gate["status"], "passed")
            self.assertEqual(gate["hook_ids"], ["advisory", "required"])
            self.assertEqual(gate["warning_count"], 1)
            self.assertIsNone(gate["stop_input"]["provider_stdout_sha256"])
            self.assertIsNone(gate["stop_input"]["provider_stderr_sha256"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = self._runner(
                root,
                [
                    {
                        "id": "advisory",
                        "event": "agent_stop",
                        "command": ["cat"],
                    }
                ],
            )
            self._invoke(runner)
            self.assertFalse(
                (runner.run.artifacts_dir / ".agent-lifecycle-stop-gates").exists()
            )

    def test_blocking_stop_failure_rejects_successful_provider_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = self._runner(
                root,
                [
                    {
                        "id": "required",
                        "event": "agent_stop",
                        "command": ["false"],
                        "on_failure": "block",
                    }
                ],
            )
            cleanup_calls = []
            with self.assertRaises(StepExecutionError):
                self._invoke(runner, cleanup=lambda: cleanup_calls.append(True))
            self.assertEqual(runner.provider_calls, 1)
            self.assertEqual(cleanup_calls, [True])
            gate_path = next(
                (runner.run.artifacts_dir / ".agent-lifecycle-stop-gates").glob("*.json")
            )
            self.assertEqual(load_agent_lifecycle_stop_gate(gate_path)["status"], "failed")

    def test_map_and_team_blocking_stop_failures_reject_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "blocking-stop-map-failure",
                "mode": "read_only",
                "max_workers": 1,
                "steps": [
                    {
                        "id": "map",
                        "kind": "agent_map",
                        "risk": "medium",
                        "items": ["one"],
                        "prompt_template": "Review {item} in packet {index}.",
                        "max_workers": 1,
                    }
                ],
                "hooks": [
                    {
                        "id": "required",
                        "event": "agent_stop",
                        "command": ["false"],
                        "on_failure": "block",
                        "scopes": ["agent_map_packet"],
                    }
                ],
            }
            runner = FakeCommandLifecycleRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            with self.assertRaises(StepExecutionError):
                runner.execute()
            self.assertEqual(runner.provider_calls, 1)
            gate_path = next(
                (runner.run.artifacts_dir / ".agent-lifecycle-stop-gates").glob("*.json")
            )
            self.assertEqual(load_agent_lifecycle_stop_gate(gate_path)["status"], "failed")

        from tests.test_agent_team import FakeTeamRunner, continuation_workflow

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["hooks"] = [
                {
                    "id": "required",
                    "event": "agent_stop",
                    "command": ["false"],
                    "on_failure": "block",
                    "scopes": ["agent_team_turn"],
                    "members": ["worker"],
                    "tasks": ["debug"],
                }
            ]
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            with self.assertRaises(StepExecutionError):
                runner.execute()
            self.assertEqual(len(runner.calls), 1)
            gate_path = next(
                (runner.run.artifacts_dir / ".agent-lifecycle-stop-gates").glob("*.json")
            )
            self.assertEqual(load_agent_lifecycle_stop_gate(gate_path)["status"], "failed")

    def test_terminal_recovery_finishes_unclaimed_blocking_stop_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = CrashSafeStopGateRunner(
                workflow=codex_workflow(
                    [
                        {
                            "id": "required",
                            "event": "agent_stop",
                            "command": ["cat"],
                            "on_failure": "block",
                        }
                    ]
                ),
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                stop_gate_crash_phase="ready",
            )
            with self.assertRaises(SimulatedLifecycleHardCrash):
                runner.execute()
            gate_path = next(
                (runner.run.artifacts_dir / ".agent-lifecycle-stop-gates").glob("*.json")
            )
            self.assertEqual(load_agent_lifecycle_stop_gate(gate_path)["status"], "ready")
            self.assertEqual(runner.provider_calls, 1)
            self.assertEqual(runner.run.read_state().get("hook_events", []), [])

            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            runner._codex_exec(step)
            self.assertEqual(runner.provider_calls, 1)
            stop_events = [
                event
                for event in runner.run.read_state()["hook_events"]
                if event["event"] == "agent_stop"
            ]
            self.assertEqual(len(stop_events), 1)
            self.assertEqual(load_agent_lifecycle_stop_gate(gate_path)["status"], "passed")

    def test_terminal_recovery_requires_untampered_blocking_stop_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = CrashSafeStopGateRunner(
                workflow=codex_workflow(
                    [
                        {
                            "id": "required",
                            "event": "agent_stop",
                            "command": ["cat"],
                            "on_failure": "block",
                        }
                    ]
                ),
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                stop_gate_crash_phase="ready",
            )
            with self.assertRaises(SimulatedLifecycleHardCrash):
                runner.execute()
            gate_path = next(
                (runner.run.artifacts_dir / ".agent-lifecycle-stop-gates").glob("*.json")
            )
            gate_path.unlink()
            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            with self.assertRaisesRegex(ValidationError, "gate is missing"):
                runner._codex_exec(step)
            self.assertEqual(runner.provider_calls, 1)

    def test_terminal_recovery_rejects_indeterminate_hook_without_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = CrashSafeStopGateRunner(
                workflow=codex_workflow(
                    [
                        {
                            "id": "required",
                            "event": "agent_stop",
                            "command": ["cat"],
                            "on_failure": "block",
                        }
                    ]
                ),
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                crash_after_stop_hook=True,
            )
            with self.assertRaises(SimulatedLifecycleHardCrash):
                runner.execute()
            gate_path = next(
                (runner.run.artifacts_dir / ".agent-lifecycle-stop-gates").glob("*.json")
            )
            self.assertEqual(load_agent_lifecycle_stop_gate(gate_path)["status"], "running")
            initial_events = list(runner.run.read_state()["hook_events"])
            self.assertEqual(len(initial_events), 1)

            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            with self.assertRaisesRegex(StepExecutionError, "indeterminate"):
                runner._codex_exec(step)
            self.assertEqual(runner.provider_calls, 1)
            self.assertEqual(runner.run.read_state()["hook_events"], initial_events)
            self.assertEqual(load_agent_lifecycle_stop_gate(gate_path)["status"], "uncertain")

    def test_terminal_recovery_resumes_after_last_durably_completed_stop_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = CrashSafeStopGateRunner(
                workflow=codex_workflow(
                    [
                        {
                            "id": "first",
                            "event": "agent_stop",
                            "command": ["cat"],
                            "on_failure": "warn",
                        },
                        {
                            "id": "second",
                            "event": "agent_stop",
                            "command": ["cat"],
                            "on_failure": "block",
                        },
                    ]
                ),
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                stop_gate_crash_phase="hook-completed",
            )
            with self.assertRaises(SimulatedLifecycleHardCrash):
                runner.execute()
            gate_path = next(
                (runner.run.artifacts_dir / ".agent-lifecycle-stop-gates").glob("*.json")
            )
            gate = load_agent_lifecycle_stop_gate(gate_path)
            self.assertEqual(gate["status"], "ready")
            self.assertEqual(gate["next_hook_index"], 1)
            self.assertEqual(
                [event["id"] for event in runner.run.read_state()["hook_events"]],
                ["first"],
            )

            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            runner._codex_exec(step)
            self.assertEqual(runner.provider_calls, 1)
            self.assertEqual(
                [event["id"] for event in runner.run.read_state()["hook_events"]],
                ["first", "second"],
            )
            self.assertEqual(load_agent_lifecycle_stop_gate(gate_path)["status"], "passed")

    def test_map_terminal_recovery_finishes_blocking_stop_without_provider_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "blocking-stop-map-recovery",
                "mode": "read_only",
                "max_workers": 1,
                "steps": [
                    {
                        "id": "map",
                        "kind": "agent_map",
                        "risk": "medium",
                        "items": ["one"],
                        "prompt_template": "Review {item} in packet {index}.",
                        "max_workers": 1,
                    }
                ],
                "hooks": [
                    {
                        "id": "required",
                        "event": "agent_stop",
                        "command": ["cat"],
                        "on_failure": "block",
                        "scopes": ["agent_map_packet"],
                    }
                ],
            }
            runner = CrashSafeStopGateRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                stop_gate_crash_phase="ready",
            )
            with self.assertRaises(SimulatedLifecycleHardCrash):
                runner.execute()
            self.assertEqual(runner.provider_calls, 1)
            gate_path = next(
                (runner.run.artifacts_dir / ".agent-lifecycle-stop-gates").glob("*.json")
            )
            self.assertEqual(load_agent_lifecycle_stop_gate(gate_path)["status"], "ready")

            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            runner._agent_map(step)
            self.assertEqual(runner.provider_calls, 1)
            self.assertEqual(load_agent_lifecycle_stop_gate(gate_path)["status"], "passed")
            self.assertEqual(
                sum(
                    event["event"] == "agent_stop"
                    for event in runner.run.read_state()["hook_events"]
                ),
                1,
            )

    def test_team_terminal_recovery_finishes_blocking_stop_without_provider_replay(self):
        from conductor_runtime.run_control import retry_step
        from tests.test_agent_team import (
            FakeTeamRunner,
            SimulatedHardCrash,
            continuation_workflow,
        )

        class StopGateTeamRunner(FakeTeamRunner):
            def __init__(self, *args, crash_ready=False, **kwargs):
                self.crash_ready = crash_ready
                super().__init__(*args, **kwargs)

            def _agent_lifecycle_stop_gate_checkpoint(self, phase, gate):
                if (
                    self.crash_ready
                    and phase == "ready"
                    and gate["scope"] == "agent_team_turn"
                ):
                    self.crash_ready = False
                    raise SimulatedHardCrash("blocking stop gate ready")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["hooks"] = [
                {
                    "id": "required",
                    "event": "agent_stop",
                    "command": ["cat"],
                    "on_failure": "block",
                    "scopes": ["agent_team_turn"],
                    "members": ["worker"],
                    "tasks": ["debug"],
                }
            ]
            policy = RuntimePolicy(allow_agent=True)
            crashed = StopGateTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                crash_ready=True,
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            self.assertEqual(len(crashed.calls), 1)
            gate_path = next(
                (crashed.run.artifacts_dir / ".agent-lifecycle-stop-gates").glob("*.json")
            )
            self.assertEqual(load_agent_lifecycle_stop_gate(gate_path)["status"], "ready")

            retry_step(
                crashed.run.run_dir,
                "team-debug",
                reason="recover blocking lifecycle stop gate",
            )
            resumed = StopGateTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])
            self.assertEqual(load_agent_lifecycle_stop_gate(gate_path)["status"], "passed")
            self.assertEqual(
                sum(
                    event["event"] == "agent_stop"
                    for event in resumed.run.read_state()["hook_events"]
                ),
                1,
            )

    def test_team_completion_only_recovery_requires_passed_stop_gate(self):
        from conductor_runtime.agent_team_turn_completion import (
            list_agent_team_turn_completion_paths,
        )
        from conductor_runtime.agent_team_turn_terminal import (
            list_agent_team_turn_terminal_paths,
        )
        from conductor_runtime.run_control import retry_step
        from tests.test_agent_team import (
            FakeTeamRunner,
            SimulatedHardCrash,
            continuation_workflow,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["hooks"] = [
                {
                    "id": "required",
                    "event": "agent_stop",
                    "command": ["cat"],
                    "on_failure": "block",
                    "scopes": ["agent_team_turn"],
                    "members": ["worker"],
                    "tasks": ["debug"],
                }
            ]
            policy = RuntimePolicy(allow_agent=True)
            crashed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                turn_completion_crash_phase="written",
            )
            with self.assertRaises(SimulatedHardCrash):
                crashed.execute()
            self.assertEqual(
                list_agent_team_turn_terminal_paths(
                    crashed.run,
                    workflow["steps"][0],
                ),
                [],
            )
            self.assertEqual(
                len(
                    list_agent_team_turn_completion_paths(
                        crashed.run,
                        workflow["steps"][0],
                    )
                ),
                1,
            )
            gate_path = next(
                (crashed.run.artifacts_dir / ".agent-lifecycle-stop-gates").glob("*.json")
            )
            self.assertEqual(load_agent_lifecycle_stop_gate(gate_path)["status"], "passed")

            retry_step(
                crashed.run.run_dir,
                "team-debug",
                reason="recover completion behind passed lifecycle stop gate",
            )
            resumed = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=policy,
                resume_dir=crashed.run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.calls, [])
            self.assertEqual(
                list_agent_team_turn_completion_paths(
                    resumed.run,
                    workflow["steps"][0],
                ),
                [],
            )

    def test_stop_gate_tamper_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = self._runner(
                root,
                [
                    {
                        "id": "required",
                        "event": "agent_stop",
                        "command": ["cat"],
                        "on_failure": "block",
                    }
                ],
            )
            self._invoke(runner)
            gate_path = next(
                (runner.run.artifacts_dir / ".agent-lifecycle-stop-gates").glob("*.json")
            )
            original = gate_path.read_text(encoding="utf-8")
            gate = json.loads(original)
            gate["status"] = "failed"
            gate_path.write_text(json.dumps(gate), encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_agent_lifecycle_stop_gate(gate_path)

            gate_path.write_text(original, encoding="utf-8")
            target = gate_path.with_name("target.json")
            gate_path.rename(target)
            gate_path.symlink_to(target)
            with self.assertRaisesRegex(ValidationError, "symlink"):
                load_agent_lifecycle_stop_gate(gate_path)

    def test_no_lifecycle_hooks_take_direct_provider_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = self._runner(root, [])
            result, _ = self._invoke(runner)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(runner.provider_calls, 1)
            self.assertNotIn("hook_events", runner.run.read_state())
            self.assertEqual(list(runner.run.logs_dir.rglob("hooks/*")), [])

    def test_context_is_redacted_framed_receipted_and_reused_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hook_output = (
                "api_key=super-secret-value\n"
                "Use repository-local UTC timestamps.\n"
                + AGENT_LIFECYCLE_CONTEXT_BEGIN
                + "\n"
                + AGENT_LIFECYCLE_CONTEXT_END
                + "\n"
            )
            runner = self._runner(
                root,
                [
                    {
                        "id": "context",
                        "event": "agent_start",
                        "command": ["printf", hook_output],
                        "scopes": ["codex_exec"],
                        "inject_context": True,
                        "context_limit_bytes": 2048,
                    },
                    {
                        "id": "context-two",
                        "event": "agent_start",
                        "command": ["printf", "Second ordered context source.\n"],
                        "scopes": ["codex_exec"],
                        "inject_context": True,
                    },
                    {
                        "id": "stop",
                        "event": "agent_stop",
                        "command": ["cat"],
                        "scopes": ["codex_exec"],
                    },
                ],
            )
            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            prepared = runner._prepare_agent_lifecycle_prompt(
                workflow_step=step,
                provider_step=step,
                scope="codex_exec",
                base_prompt="private base prompt",
                execution_workspace=root,
            )
            repeated = runner._prepare_agent_lifecycle_prompt(
                workflow_step=step,
                provider_step=step,
                scope="codex_exec",
                base_prompt="private base prompt",
                execution_workspace=root,
            )
            self.assertEqual(runner.context_calls, 2)
            self.assertEqual(prepared["effective_prompt"], repeated["effective_prompt"])
            effective = prepared["effective_prompt"]
            self.assertIn(AGENT_LIFECYCLE_CONTEXT_BEGIN, effective)
            self.assertIn(AGENT_LIFECYCLE_CONTEXT_END, effective)
            self.assertEqual(effective.count(AGENT_LIFECYCLE_CONTEXT_BEGIN), 1)
            self.assertEqual(effective.count(AGENT_LIFECYCLE_CONTEXT_END), 1)
            self.assertIn("[agent lifecycle context marker]", effective)
            self.assertIn("Use repository-local UTC timestamps.", effective)
            self.assertLess(
                effective.index("Use repository-local UTC timestamps."),
                effective.index("Second ordered context source."),
            )
            self.assertIn("api_key=<redacted>", effective)
            self.assertNotIn("super-secret-value", effective)

            receipt_paths = list(
                (runner.run.artifacts_dir / ".agent-lifecycle-context").glob("*.json")
            )
            self.assertEqual(len(receipt_paths), 1)
            receipt = load_agent_lifecycle_context_receipt(receipt_paths[0])
            self.assertEqual(receipt["schema"], AGENT_LIFECYCLE_CONTEXT_SCHEMA)
            self.assertEqual(receipt["context_hook_count"], 2)
            self.assertEqual(receipt["hook_count"], 2)
            context = verify_agent_lifecycle_context(runner.run, receipt)
            self.assertEqual("private base prompt" + context, effective)
            self.assertEqual(
                get_schema("agent-lifecycle-context")["properties"]["schema"]["const"],
                AGENT_LIFECYCLE_CONTEXT_SCHEMA,
            )

            output_path = runner.run.resolve_artifact_path("agent.md")
            runner._run_codex_provider(
                workflow_step=step,
                provider_step=step,
                scope="codex_exec",
                command=["provider", str(output_path)],
                prompt=effective,
                cwd=root,
                timeout=10,
                output_limit_bytes=4096,
                output_path=output_path,
                prepared_lifecycle=prepared,
            )
            self.assertEqual(runner.provider_calls, 1)
            self.assertEqual(runner.provider_prompts, [effective])
            events = runner.run.read_state()["hook_events"]
            self.assertEqual(len(events), 3)
            self.assertEqual(
                {event["invocation_sha256"] for event in events},
                {events[0]["invocation_sha256"]},
            )

    def test_context_injection_honors_step_filter_without_extra_hook_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "filtered-context",
                "mode": "read_only",
                "max_workers": 1,
                "steps": [
                    {
                        "id": "one",
                        "kind": "codex_exec",
                        "risk": "low",
                        "sandbox": "read-only",
                        "prompt": "First task.",
                        "capture": "one.md",
                    },
                    {
                        "id": "two",
                        "kind": "codex_exec",
                        "risk": "low",
                        "sandbox": "read-only",
                        "prompt": "Second task.",
                        "capture": "two.md",
                    },
                ],
                "hooks": [
                    {
                        "id": "context",
                        "event": "agent_start",
                        "command": ["printf", "Only the second task receives this context.\n"],
                        "steps": ["two"],
                        "scopes": ["codex_exec"],
                        "inject_context": True,
                    }
                ],
            }
            runner = FakeCommandLifecycleRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            runner.execute()
            self.assertEqual(runner.provider_calls, 2)
            self.assertEqual(runner.context_calls, 1)
            self.assertNotIn(AGENT_LIFECYCLE_CONTEXT_BEGIN, runner.provider_prompts[0])
            self.assertIn(AGENT_LIFECYCLE_CONTEXT_BEGIN, runner.provider_prompts[1])

    def test_context_injection_rejects_empty_truncated_and_over_limit_output(self):
        cases = [
            (["true"], 32, "empty output"),
            (["printf", "bounded", "truncated"], 32, "truncated"),
            (["printf", "ninebytes"], 8, "exceeded 8 bytes"),
        ]
        for command, limit, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runner = self._runner(
                    root,
                    [
                        {
                            "id": "context",
                            "event": "agent_start",
                            "command": command,
                            "scopes": ["codex_exec"],
                            "inject_context": True,
                            "context_limit_bytes": limit,
                        }
                    ],
                )
                step = runner.workflow["steps"][0]
                runner.run.mark_step_started(step["id"], step["kind"])
                with self.assertRaisesRegex(ValidationError, message):
                    runner._prepare_agent_lifecycle_prompt(
                        workflow_step=step,
                        provider_step=step,
                        scope="codex_exec",
                        base_prompt="base",
                        execution_workspace=root,
                    )
                self.assertEqual(runner.provider_calls, 0)
                receipt_dir = runner.run.artifacts_dir / ".agent-lifecycle-context"
                self.assertFalse(receipt_dir.exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = self._runner(
                root,
                [
                    {
                        "id": "context-one",
                        "event": "agent_start",
                        "command": ["printf", "repeat:40000"],
                        "scopes": ["codex_exec"],
                        "inject_context": True,
                        "context_limit_bytes": 50000,
                    },
                    {
                        "id": "context-two",
                        "event": "agent_start",
                        "command": ["printf", "repeat:40000"],
                        "scopes": ["codex_exec"],
                        "inject_context": True,
                        "context_limit_bytes": 50000,
                    },
                ],
            )
            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            with self.assertRaisesRegex(ValidationError, "exceeds 65536 bytes"):
                runner._prepare_agent_lifecycle_prompt(
                    workflow_step=step,
                    provider_step=step,
                    scope="codex_exec",
                    base_prompt="base",
                    execution_workspace=root,
                )
            self.assertEqual(runner.context_calls, 2)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = self._runner(
                root,
                [
                    {
                        "id": "context",
                        "event": "agent_start",
                        "command": ["printf", "bounded\n"],
                        "scopes": ["codex_exec"],
                        "inject_context": True,
                    }
                ],
            )
            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            with self.assertRaisesRegex(ValidationError, "effective prompt exceeds"):
                runner._prepare_agent_lifecycle_prompt(
                    workflow_step=step,
                    provider_step=step,
                    scope="codex_exec",
                    base_prompt="b" * (4 * 1024 * 1024),
                    execution_workspace=root,
                )

    def test_context_recovery_fails_closed_for_tamper_and_omission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = self._runner(
                root,
                [
                    {
                        "id": "context",
                        "event": "agent_start",
                        "command": ["printf", "trusted only as untrusted context\n"],
                        "scopes": ["codex_exec"],
                        "inject_context": True,
                    }
                ],
            )
            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            prepared = runner._prepare_agent_lifecycle_prompt(
                workflow_step=step,
                provider_step=step,
                scope="codex_exec",
                base_prompt="base",
                execution_workspace=root,
            )
            effective_sha256 = hashlib.sha256(
                prepared["effective_prompt"].encode("utf-8")
            ).hexdigest()
            resolved = runner._resolve_agent_lifecycle_prompt(
                workflow_step=step,
                provider_step=step,
                scope="codex_exec",
                base_prompt="base",
                effective_prompt_sha256=effective_sha256,
            )
            self.assertEqual(resolved, prepared["effective_prompt"])

            receipt_path = next(
                (runner.run.artifacts_dir / ".agent-lifecycle-context").glob("*.json")
            )
            receipt = load_agent_lifecycle_context_receipt(receipt_path)
            context_path = runner.run.logs_dir / receipt["context_log"]
            original_context = context_path.read_text(encoding="utf-8")
            context_path.write_text(original_context + "tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "context log"):
                runner._resolve_agent_lifecycle_prompt(
                    workflow_step=step,
                    provider_step=step,
                    scope="codex_exec",
                    base_prompt="base",
                    effective_prompt_sha256=effective_sha256,
                )
            context_path.write_text(original_context, encoding="utf-8")

            context_path.unlink()
            symlink_target = context_path.parent / "context-target.log"
            symlink_target.write_text(original_context, encoding="utf-8")
            context_path.symlink_to(symlink_target)
            with self.assertRaisesRegex(ValidationError, "symlink"):
                runner._resolve_agent_lifecycle_prompt(
                    workflow_step=step,
                    provider_step=step,
                    scope="codex_exec",
                    base_prompt="base",
                    effective_prompt_sha256=effective_sha256,
                )
            context_path.unlink()
            context_path.write_text(original_context, encoding="utf-8")

            renamed_receipt = receipt_path.with_name("c" * 64 + ".json")
            receipt_path.rename(renamed_receipt)
            with self.assertRaisesRegex(ValidationError, "filename binding"):
                runner._resolve_agent_lifecycle_prompt(
                    workflow_step=step,
                    provider_step=step,
                    scope="codex_exec",
                    base_prompt="base",
                    effective_prompt_sha256=effective_sha256,
                )
            renamed_receipt.rename(receipt_path)
            receipt_path.unlink()
            with self.assertRaisesRegex(ValidationError, "receipt is missing"):
                runner._resolve_agent_lifecycle_prompt(
                    workflow_step=step,
                    provider_step=step,
                    scope="codex_exec",
                    base_prompt="base",
                    effective_prompt_sha256=effective_sha256,
                )

    def test_codex_terminal_recovery_uses_receipt_without_provider_or_hook_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = codex_workflow(
                [
                    {
                        "id": "context",
                        "event": "agent_start",
                        "command": ["printf", "Use the recovered direct-call context.\n"],
                        "scopes": ["codex_exec"],
                        "inject_context": True,
                    }
                ]
            )
            runner = CrashAfterCodexTerminalRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                runner.execute()
            self.assertEqual(runner.context_calls, 1)
            self.assertEqual(runner.provider_calls, 1)
            self.assertIn(
                AGENT_LIFECYCLE_CONTEXT_BEGIN,
                runner.provider_prompts[0],
            )

            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            runner._codex_exec(step)
            self.assertEqual(runner.context_calls, 1)
            self.assertEqual(runner.provider_calls, 1)
            self.assertEqual(
                runner.run.read_state()["steps"][step["id"]]["status"],
                "completed",
            )

    def test_map_terminal_recovery_uses_receipt_without_provider_or_hook_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "terminal-context-map",
                "mode": "read_only",
                "max_workers": 1,
                "steps": [
                    {
                        "id": "map",
                        "kind": "agent_map",
                        "risk": "medium",
                        "items": ["one"],
                        "prompt_template": "Review {item} in packet {index}.",
                        "max_workers": 1,
                    }
                ],
                "hooks": [
                    {
                        "id": "context",
                        "event": "agent_start",
                        "command": ["printf", "Use the recovered packet context.\n"],
                        "scopes": ["agent_map_packet"],
                        "inject_context": True,
                    }
                ],
            }
            runner = CrashAfterMapTerminalRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            with self.assertRaises(StepExecutionError):
                runner.execute()
            self.assertEqual(runner.context_calls, 1)
            self.assertEqual(runner.provider_calls, 1)

            step = runner.workflow["steps"][0]
            runner.run.mark_step_started(step["id"], step["kind"])
            runner._agent_map(step)
            self.assertEqual(runner.context_calls, 1)
            self.assertEqual(runner.provider_calls, 1)
            self.assertEqual(
                runner.run.read_state()["steps"][step["id"]]["status"],
                "completed",
            )

    def test_completed_codex_terminal_reconciliation_requires_context_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = codex_workflow(
                [
                    {
                        "id": "context",
                        "event": "agent_start",
                        "command": ["printf", "Use completed-terminal context.\n"],
                        "scopes": ["codex_exec"],
                        "inject_context": True,
                    }
                ]
            )
            runner = CrashAfterCompletedCodexTerminalRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            with self.assertRaisesRegex(RuntimeError, "completed codex terminal"):
                runner.execute()
            step = runner.workflow["steps"][0]
            self.assertEqual(
                runner.run.read_state()["steps"][step["id"]]["status"],
                "completed",
            )
            receipt_path = next(
                (runner.run.artifacts_dir / ".agent-lifecycle-context").glob("*.json")
            )
            receipt_text = receipt_path.read_text(encoding="utf-8")
            receipt_path.unlink()
            with self.assertRaisesRegex(ValidationError, "receipt is missing"):
                runner._reconcile_completed_codex_step_terminal(step)
            receipt_path.write_text(receipt_text, encoding="utf-8")
            runner._reconcile_completed_codex_step_terminal(step)
            self.assertEqual(runner.context_calls, 1)
            self.assertEqual(runner.provider_calls, 1)

    def test_parallel_map_context_is_bound_to_cache_without_hook_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "parallel-context-map",
                "mode": "read_only",
                "max_workers": 4,
                "steps": [
                    {
                        "id": "map",
                        "kind": "agent_map",
                        "risk": "medium",
                        "items": ["one", "two", "three", "four"],
                        "prompt_template": "Review {item} in packet {index}.",
                        "max_workers": 4,
                    }
                ],
                "hooks": [
                    {
                        "id": "context",
                        "event": "agent_start",
                        "command": ["printf", "Apply the packet-specific review rubric.\n"],
                        "scopes": ["agent_map_packet"],
                        "inject_context": True,
                    }
                ],
            }
            runner = FakeCommandLifecycleRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
            )
            runner.execute()
            self.assertEqual(runner.context_calls, 4)
            self.assertEqual(runner.provider_calls, 4)
            self.assertEqual(len(runner.provider_prompts), 4)
            self.assertTrue(
                all(AGENT_LIFECYCLE_CONTEXT_BEGIN in prompt for prompt in runner.provider_prompts)
            )
            receipts = list(
                (runner.run.artifacts_dir / ".agent-lifecycle-context").glob("*.json")
            )
            self.assertEqual(len(receipts), 4)

            runner._agent_map(runner.workflow["steps"][0])
            self.assertEqual(runner.context_calls, 4)
            self.assertEqual(runner.provider_calls, 4)

            first_receipt = next(
                path
                for path in receipts
                if load_agent_lifecycle_context_receipt(path)["packet_index"] == 1
            )
            first_receipt.unlink()
            with self.assertRaisesRegex(ValidationError, "missing for cached"):
                runner._agent_map(runner.workflow["steps"][0])
            self.assertEqual(runner.context_calls, 4)
            self.assertEqual(runner.provider_calls, 4)

    def test_dry_run_preflights_without_hook_or_provider_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FakeLifecycleRunner(
                workflow=codex_workflow(
                    [
                        {"id": "start", "event": "agent_start", "command": ["cat"]},
                        {"id": "stop", "event": "agent_stop", "command": ["cat"]},
                    ]
                ),
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                dry_run=True,
            )
            runner.execute()
            self.assertEqual(runner.provider_calls, 0)
            events = runner.run.read_state()["hook_events"]
            self.assertEqual(len(events), 2)
            self.assertTrue(all(event["status"] == "planned" for event in events))

    def test_full_codex_and_parallel_map_paths_emit_balanced_lifecycle_events(self):
        hooks = [
            {"id": "start", "event": "agent_start", "command": ["cat"]},
            {
                "id": "stop",
                "event": "agent_stop",
                "command": ["cat"],
                "on_failure": "block",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = CommandProviderRunner(
                workflow=codex_workflow(hooks),
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            runner.execute()
            events = runner.run.read_state()["hook_events"]
            self.assertEqual([event["event"] for event in events], ["agent_start", "agent_stop"])
            self.assertTrue(all(event["scope"] == "codex_exec" for event in events))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "parallel-lifecycle-map",
                "mode": "read_only",
                "max_workers": 4,
                "steps": [
                    {
                        "id": "map",
                        "kind": "agent_map",
                        "risk": "medium",
                        "items": ["item-%02d" % index for index in range(8)],
                        "prompt_template": "Review {item} at packet {index}.",
                        "max_workers": 4,
                    }
                ],
                "hooks": [
                    dict(hook, scopes=["agent_map_packet"])
                    for hook in hooks
                ],
            }
            runner = CommandProviderRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
            )
            runner.execute()
            events = runner.run.read_state()["hook_events"]
            self.assertEqual(len(events), 16)
            self.assertEqual(sum(event["event"] == "agent_start" for event in events), 8)
            self.assertEqual(sum(event["event"] == "agent_stop" for event in events), 8)
            pairs = {}
            for event in events:
                pairs.setdefault(event["invocation_sha256"], set()).add(event["event"])
                self.assertEqual(event["scope"], "agent_map_packet")
                self.assertIn(event["packet_index"], range(1, 9))
            self.assertEqual(len(pairs), 8)
            self.assertTrue(all(value == {"agent_start", "agent_stop"} for value in pairs.values()))
            gates = [
                load_agent_lifecycle_stop_gate(path)
                for path in (
                    runner.run.artifacts_dir / ".agent-lifecycle-stop-gates"
                ).glob("*.json")
            ]
            self.assertEqual(len(gates), 8)
            self.assertEqual(
                {gate["start_input"]["packet_index"] for gate in gates},
                set(range(1, 9)),
            )
            self.assertTrue(all(gate["status"] == "passed" for gate in gates))

    def test_full_team_turn_and_plan_protocol_paths_emit_filtered_scopes(self):
        from tests.test_agent_team import (
            FakePlanApprovalRunner,
            FakeTeamRunner,
            coordinated_workflow,
            plan_approval_workflow,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = coordinated_workflow()
            workflow["hooks"] = [
                {
                    "id": "start",
                    "event": "agent_start",
                    "command": ["cat"],
                    "scopes": ["agent_team_turn"],
                },
                {
                    "id": "stop",
                    "event": "agent_stop",
                    "command": ["cat"],
                    "on_failure": "block",
                    "scopes": ["agent_team_turn"],
                },
            ]
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
            )
            runner.execute()
            events = runner.run.read_state()["hook_events"]
            self.assertEqual(len(events), 6)
            self.assertTrue(all(event["scope"] == "agent_team_turn" for event in events))
            self.assertEqual(
                {event["member_id"] for event in events},
                {"lead", "security", "correctness"},
            )
            self.assertEqual(
                len(
                    list(
                        (runner.run.artifacts_dir / ".agent-lifecycle-stop-gates").glob(
                            "*.json"
                        )
                    )
                ),
                3,
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "shared.txt").write_text("base\n", encoding="utf-8")
            workflow = plan_approval_workflow()
            workflow["hooks"] = [
                {
                    "id": "start",
                    "event": "agent_start",
                    "command": ["cat"],
                    "scopes": ["agent_team_plan", "agent_team_review"],
                },
                {
                    "id": "stop",
                    "event": "agent_stop",
                    "command": ["cat"],
                    "on_failure": "block",
                    "scopes": ["agent_team_plan", "agent_team_review"],
                },
            ]
            runner = FakePlanApprovalRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
            )
            runner.execute()
            events = runner.run.read_state()["hook_events"]
            self.assertEqual(len(events), 4)
            self.assertEqual(
                {event["scope"] for event in events},
                {"agent_team_plan", "agent_team_review"},
            )
            gates = [
                load_agent_lifecycle_stop_gate(path)
                for path in (
                    runner.run.artifacts_dir / ".agent-lifecycle-stop-gates"
                ).glob("*.json")
            ]
            self.assertEqual(len(gates), 2)
            self.assertTrue(all(gate["status"] == "passed" for gate in gates))

    def test_quality_retry_scope_uses_independent_blocking_stop_gates(self):
        from tests.test_agent_team import FakeTeamRunner, continuation_workflow

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            workflow = continuation_workflow()
            workflow["hooks"] = [
                {
                    "id": "required-stop",
                    "event": "agent_stop",
                    "command": ["cat"],
                    "on_failure": "block",
                    "scopes": ["agent_team_turn", "agent_team_quality_retry"],
                    "members": ["worker"],
                    "tasks": ["debug"],
                },
                {
                    "id": "quality",
                    "event": "team_task_completed",
                    "command": ["grep", "QUALITY_GATE_SIMULATED"],
                    "on_failure": "retry",
                    "max_retries": 1,
                    "retry_max_tokens": 1000,
                },
            ]
            runner = FakeTeamRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                quality_gate_failures=1,
            )
            runner.execute()
            self.assertEqual(len(runner.calls), 2)
            stop_events = [
                event
                for event in runner.run.read_state()["hook_events"]
                if event["event"] == "agent_stop"
            ]
            self.assertEqual(
                {event["scope"] for event in stop_events},
                {"agent_team_turn", "agent_team_quality_retry"},
            )
            gates = [
                load_agent_lifecycle_stop_gate(path)
                for path in (
                    runner.run.artifacts_dir / ".agent-lifecycle-stop-gates"
                ).glob("*.json")
            ]
            self.assertEqual(len(gates), 2)
            self.assertTrue(all(gate["status"] == "passed" for gate in gates))


if __name__ == "__main__":
    unittest.main()
