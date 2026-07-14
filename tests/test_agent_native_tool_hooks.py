import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from conductor_extras.runtime.agent_lifecycle_hooks import build_agent_lifecycle_hook_input
from conductor_extras.runtime.agent_tool_policy import effective_command_policy
from conductor_extras.runtime.agent_native_tool_hooks import (
    AGENT_NATIVE_TOOL_INPUT_SCHEMA,
    agent_native_tool_bridge_path,
    agent_native_tool_config_path,
    agent_native_tool_gate_path,
    agent_native_tool_input_json,
    bridge_program_sha256,
    build_agent_native_tool_config,
    build_agent_native_tool_gate,
    build_agent_native_tool_input,
    list_agent_native_tool_receipts,
    load_agent_native_tool_gate,
    settle_agent_native_tool_gate,
    snapshot_agent_native_tool_program,
    validate_agent_native_tool_input,
    write_agent_native_tool_bridge,
    write_agent_native_tool_config,
    write_agent_native_tool_gate,
)
from conductor_extras.runtime.artifacts import RunArtifacts, utc_now
from conductor_extras.runtime.dashboard import collect_run_detail, write_dashboard
from conductor_runtime.errors import PolicyError, ValidationError
from conductor_extras.runtime.runner import ProcessResult, WorkflowRunner
from conductor_extras.runtime.schemas import get_schema
from conductor_extras.runtime.security import RuntimePolicy
from conductor_extras.runtime.workflow import validate_workflow


def workflow_with_hook(**hook_overrides):
    hook = {
        "id": "guard",
        "event": "agent_pre_tool",
        "command": ["true"],
        "tools": ["Bash", "apply_patch"],
        "steps": ["work"],
        "scopes": ["codex_exec"],
        "on_failure": "block",
    }
    hook.update(hook_overrides)
    return {
        "schema": "conductor.workflow.v1",
        "name": "native-tool-hook",
        "mode": "read_only",
        "hooks": [hook],
        "steps": [
            {
                "id": "work",
                "kind": "codex_exec",
                "prompt": "Inspect.",
                "capture": "result.md",
            }
        ],
    }


def start_input():
    return build_agent_lifecycle_hook_input(
        event="agent_start",
        workflow_fingerprint="a" * 64,
        step_id="work",
        step_kind="codex_exec",
        scope="codex_exec",
        prompt_sha256="b" * 64,
        attempt=1,
        sandbox="read-only",
        session_mode="new",
        model="gpt-test",
        effort="high",
        max_tokens=1000,
    )


class NativeToolFixture:
    def __init__(
        self,
        root: Path,
        verifier_exit=0,
        verifier_log=None,
        verifier_body=None,
        timeout_seconds=5,
        command_policy=None,
    ):
        self.root = root
        self.run = RunArtifacts.create(
            {
                "schema": "conductor.workflow.v1",
                "name": "native-tool-fixture",
                "steps": [{"id": "note", "kind": "write_artifact", "content": "x"}],
            },
            root / "runs",
            run_id="fixture",
        )
        verifier = root / "bin" / "verifier"
        verifier.parent.mkdir()
        log_line = ""
        if verifier_log is not None:
            log_line = "with open(%r, 'a', encoding='utf-8') as stream: stream.write('called\\n')\n" % str(verifier_log)
        body = verifier_body or (
            "import json, sys\n"
            "payload=json.load(sys.stdin)\n"
            "assert payload['schema']==%r\n" % AGENT_NATIVE_TOOL_INPUT_SCHEMA
            + log_line
            + "raise SystemExit(%d)\n" % verifier_exit
        )
        verifier.write_text(
            "#!/usr/bin/env python3\n" + body,
            encoding="utf-8",
        )
        verifier.chmod(0o700)
        snapshot = snapshot_agent_native_tool_program(self.run, verifier)
        self.start = start_input()
        self.config = build_agent_native_tool_config(
            start_input=self.start,
            run_dir=self.run.run_dir,
            hooks=[
                {
                    "id": "guard",
                    "on_failure": "block",
                    "tools": ["Bash"],
                    "timeout_seconds": timeout_seconds,
                    **snapshot,
                }
            ],
            command_policy=command_policy,
        )
        self.bridge = write_agent_native_tool_bridge(self.run)
        write_agent_native_tool_config(self.run, self.config)
        self.gate = build_agent_native_tool_gate(
            start_input=self.start,
            config=self.config,
            bridge_sha256=bridge_program_sha256(),
            created_at_utc=utc_now(),
        )
        write_agent_native_tool_gate(self.run, self.gate)

    def invoke(self, event=None):
        event = event or {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_use_id": "tool-1",
            "tool_input": {"command": "pwd"},
            "session_id": "ignored",
        }
        env = os.environ.copy()
        env["CONDUCTOR_AGENT_PRE_TOOL_CONFIG"] = str(
            agent_native_tool_config_path(self.run, self.start["invocation_sha256"])
        )
        env["CONDUCTOR_AGENT_PRE_TOOL_CONFIG_SHA256"] = self.config[
            "config_sha256"
        ]
        return subprocess.run(
            [sys.executable, "-I", str(self.bridge), bridge_program_sha256()],
            input=json.dumps(event),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(self.root),
            timeout=10,
            check=False,
        )


class FakeNativeToolRunner(WorkflowRunner):
    def __init__(self, *args, **kwargs):
        self.provider_calls = 0
        self.provider_commands = []
        self.provider_envs = []
        self.tool_allowed = None
        super().__init__(*args, **kwargs)

    def _codex_command(self, step, output_path, *, workspace=None):
        del step
        bound_workspace = Path(workspace or self.workspace)
        return [
            "provider",
            "exec",
            "--cd",
            str(bound_workspace),
            "--output-last-message",
            str(output_path),
            "-",
        ]

    def _run_process(
        self,
        argv,
        cwd,
        timeout,
        input_text=None,
        output_limit_bytes=1024 * 1024,
        env=None,
        stdout_line_callback=None,
        interrupt_check=None,
    ):
        del timeout, input_text, output_limit_bytes, interrupt_check
        self.provider_calls += 1
        self.provider_commands.append(list(argv))
        self.provider_envs.append(dict(env or {}))
        self.assert_native_command(argv, cwd)
        bridge = agent_native_tool_bridge_path(self.run)
        hook_result = subprocess.run(
            [sys.executable, "-I", str(bridge), bridge_program_sha256()],
            input=json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_use_id": "provider-tool-1",
                    "tool_input": {"command": "touch danger.txt"},
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(cwd),
            timeout=10,
            check=False,
        )
        decision = json.loads(hook_result.stdout).get("hookSpecificOutput", {}).get(
            "permissionDecision"
        )
        self.tool_allowed = decision != "deny"
        if self.tool_allowed:
            (Path(cwd) / "danger.txt").write_text("tool ran\n", encoding="utf-8")
        output_index = argv.index("--output-last-message") + 1
        output_path = Path(argv[output_index])
        thread = (
            '{"type":"thread.started","thread_id":"00000000-0000-4000-8000-000000000001"}'
        )
        terminal = (
            '{"type":"turn.completed","usage":{"input_tokens":10,'
            '"cached_input_tokens":0,"output_tokens":2,"reasoning_output_tokens":0}}'
        )
        if stdout_line_callback is not None:
            stdout_line_callback(thread)
        output_path.write_text("provider result\n", encoding="utf-8")
        if stdout_line_callback is not None:
            stdout_line_callback(terminal)
        return ProcessResult(
            returncode=0,
            stdout=thread + "\n" + terminal + "\n",
            stderr="",
        )

    def assert_native_command(self, argv, cwd):
        if argv[0] != "/opt/codex":
            raise AssertionError("native tool preflight did not pin Codex")
        if str(cwd) != argv[argv.index("--cd") + 1]:
            raise AssertionError("native tool command workspace changed")
        if not any(
            isinstance(value, str) and value.startswith("hooks.PreToolUse=")
            for value in argv
        ):
            raise AssertionError("native PreToolUse hook was not injected")


class CrashAfterNativeToolTerminalRunner(FakeNativeToolRunner):
    def __init__(self, *args, **kwargs):
        self.crash_after_terminal = True
        super().__init__(*args, **kwargs)

    def _codex_step_terminal_checkpoint(self, phase, terminal):
        del terminal
        if phase == "written" and self.crash_after_terminal:
            self.crash_after_terminal = False
            raise RuntimeError("simulated crash after native-tool terminal")


class AgentNativeToolHookTests(unittest.TestCase):
    def test_omitted_hooks_preserve_the_direct_provider_fast_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            step = {
                "id": "work",
                "kind": "codex_exec",
                "prompt": "Inspect.",
                "capture": "result.md",
            }
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "native-tool-fast-path",
                "steps": [step],
            }
            runner = WorkflowRunner(
                workflow=workflow,
                workspace=workspace,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            command = ["codex", "exec", "--cd", str(workspace), "-"]
            expected = ProcessResult(returncode=0, stdout="", stderr="")
            with mock.patch.object(
                runner,
                "_run_process",
                return_value=expected,
            ) as run_process, mock.patch(
                "conductor_extras.runtime.runner.write_agent_native_tool_bridge"
            ) as write_bridge, mock.patch(
                "conductor_extras.runtime.runner.prepare_pre_tool_hook_state"
            ) as preflight:
                observed = runner._run_codex_provider(
                    workflow_step=step,
                    provider_step=step,
                    scope="codex_exec",
                    command=command,
                    prompt="Inspect.",
                    cwd=workspace,
                    timeout=60,
                    output_limit_bytes=1024,
                    output_path=runner.run.resolve_artifact_path("result.md"),
                )
            self.assertIs(observed, expected)
            run_process.assert_called_once_with(
                command,
                input_text="Inspect.",
                cwd=workspace,
                timeout=60,
                output_limit_bytes=1024,
            )
            write_bridge.assert_not_called()
            preflight.assert_not_called()

    def test_public_schemas_are_closed_and_workflow_exposes_tool_filters(self):
        expected = {
            "agent-native-tool-hook-input": (
                "conductor.agent_native_tool_hook_input.v1",
                28,
            ),
            "agent-native-tool-gate": (
                "conductor.agent_native_tool_gate.v1",
                26,
            ),
            "agent-native-tool-receipt": (
                "conductor.agent_native_tool_receipt.v1",
                20,
            ),
        }
        for name, (schema_id, required_count) in expected.items():
            with self.subTest(name=name):
                schema = get_schema(name)
                self.assertEqual(schema["properties"]["schema"]["const"], schema_id)
                self.assertEqual(len(schema["required"]), required_count)
                self.assertFalse(schema["additionalProperties"])
        tools = get_schema("workflow")["properties"]["hooks"]["items"][
            "properties"
        ]["tools"]
        self.assertEqual(tools["maxItems"], 32)

    def test_workflow_contract_is_opt_in_bounded_and_provider_filtered(self):
        validate_workflow(workflow_with_hook())
        invalid = [
            ({"tools": None}, "tools"),
            ({"tools": ["Bash", "Bash"]}, "unique"),
            ({"command": ["python3", "guard.py"]}, "one executable"),
            ({"command": ["./guard"]}, "on PATH"),
            ({"on_failure": "retry"}, "retry mode"),
            ({"timeout_seconds": 31}, "at most 30"),
            ({"capture": "gate.log"}, "does not support capture"),
            ({"writes": True}, "cannot declare"),
        ]
        for override, message in invalid:
            with self.subTest(override=override), self.assertRaisesRegex(
                ValidationError, message
            ):
                validate_workflow(workflow_with_hook(**override))
        custom = workflow_with_hook(
            command=["custom-native-verifier"],
            risk="high",
        )
        validate_workflow(custom)
        custom["hooks"][0]["risk"] = "low"
        with self.assertRaisesRegex(ValidationError, "require risk high"):
            validate_workflow(custom)

    def test_strict_user_input_contains_raw_proposal_but_hashes_identity(self):
        payload = build_agent_native_tool_input(
            start_input(),
            tool_name="Bash",
            tool_use_id="tool-secret-id",
            tool_input={"command": "git status --short"},
        )
        validate_agent_native_tool_input(payload)
        self.assertEqual(payload["event"], "agent_pre_tool")
        self.assertEqual(payload["tool_input"], {"command": "git status --short"})
        self.assertNotIn("tool-secret-id", agent_native_tool_input_json(payload))
        changed = json.loads(json.dumps(payload))
        changed["tool_input"]["command"] = "whoami"
        with self.assertRaisesRegex(ValidationError, "input hash"):
            validate_agent_native_tool_input(changed)

    def test_bridge_passes_and_settles_hash_only_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = NativeToolFixture(Path(tmp))
            result = fixture.invoke()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {})
            receipts = list_agent_native_tool_receipts(
                fixture.run, fixture.start["invocation_sha256"]
            )
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["status"], "passed")
            serialized = json.dumps(receipts[0])
            self.assertNotIn("pwd", serialized)
            settled = settle_agent_native_tool_gate(
                fixture.run,
                fixture.gate,
                updated_at_utc=utc_now(),
            )
            self.assertEqual(settled["status"], "settled")
            self.assertEqual(settled["passed_count"], 1)
            self.assertEqual(settled["denied_count"], 0)

    def test_bridge_denies_failed_blocking_verifier_before_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = NativeToolFixture(Path(tmp), verifier_exit=2)
            result = fixture.invoke()
            decision = json.loads(result.stdout)["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")
            receipts = list_agent_native_tool_receipts(
                fixture.run, fixture.start["invocation_sha256"]
            )
            self.assertEqual(receipts[0]["status"], "denied")
            settled = settle_agent_native_tool_gate(
                fixture.run,
                fixture.gate,
                updated_at_utc=utc_now(),
            )
            self.assertEqual(settled["denied_count"], 1)

    def test_bridge_rejects_duplicate_and_nonstandard_hook_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = NativeToolFixture(Path(tmp))
            env = os.environ.copy()
            env["CONDUCTOR_AGENT_PRE_TOOL_CONFIG"] = str(
                agent_native_tool_config_path(
                    fixture.run,
                    fixture.start["invocation_sha256"],
                )
            )
            env["CONDUCTOR_AGENT_PRE_TOOL_CONFIG_SHA256"] = fixture.config[
                "config_sha256"
            ]
            malformed = [
                '{"hook_event_name":"PreToolUse","tool_name":"Bash",'
                '"tool_name":"apply_patch","tool_use_id":"tool-1",'
                '"tool_input":{"command":"pwd"}}',
                '{"hook_event_name":"PreToolUse","tool_name":"Bash",'
                '"tool_use_id":"tool-1","tool_input":NaN}',
            ]
            for raw in malformed:
                with self.subTest(raw=raw):
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            str(fixture.bridge),
                            bridge_program_sha256(),
                        ],
                        input=raw,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                        cwd=str(fixture.root),
                        timeout=10,
                        check=False,
                    )
                    self.assertEqual(
                        json.loads(result.stdout)["hookSpecificOutput"][
                            "permissionDecision"
                        ],
                        "deny",
                    )
            self.assertEqual(
                list_agent_native_tool_receipts(
                    fixture.run,
                    fixture.start["invocation_sha256"],
                ),
                [],
            )

    def test_restricted_command_policy_and_native_verifier_compose_in_one_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "calls.log"
            policy = effective_command_policy(
                {
                    "command_allowlist": [],
                    "allow_apply_patch": False,
                },
                additional_rules=[
                    {
                        "argv": [
                            ".agents/skills/boundary-review/scripts/verify-boundary.sh"
                        ]
                    }
                ],
                additional_tools=["mcp__docs__search"],
            )
            fixture = NativeToolFixture(
                root,
                verifier_log=log,
                command_policy=policy,
            )
            mcp_allowed = fixture.invoke(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "mcp__docs__search",
                    "tool_use_id": "allowed-mcp-tool",
                    "tool_input": {"query": "bounded"},
                }
            )
            self.assertEqual(json.loads(mcp_allowed.stdout), {})
            self.assertFalse(log.exists())
            mcp_denied = fixture.invoke(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "mcp__docs__delete",
                    "tool_use_id": "denied-mcp-tool",
                    "tool_input": {},
                }
            )
            self.assertEqual(
                json.loads(mcp_denied.stdout)["hookSpecificOutput"]["permissionDecision"],
                "deny",
            )
            denied = fixture.invoke(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_use_id": "denied-tool",
                    "tool_input": {"command": "whoami"},
                }
            )
            self.assertEqual(
                json.loads(denied.stdout)["hookSpecificOutput"][
                    "permissionDecision"
                ],
                "deny",
            )
            self.assertFalse(log.exists())
            allowed = fixture.invoke(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_use_id": "allowed-tool",
                    "tool_input": {
                        "command": ".agents/skills/boundary-review/scripts/verify-boundary.sh"
                    },
                }
            )
            self.assertEqual(json.loads(allowed.stdout), {})
            self.assertEqual(log.read_text(encoding="utf-8"), "called\n")
            settled = settle_agent_native_tool_gate(
                fixture.run,
                fixture.gate,
                updated_at_utc=utc_now(),
            )
            self.assertEqual(
                settled["command_policy_sha256"],
                fixture.config["command_policy_sha256"],
            )
            self.assertEqual(settled["receipt_count"], 1)

    def test_bridge_timeout_denies_and_indeterminate_claim_rejects_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            timeout_fixture = NativeToolFixture(
                Path(tmp),
                verifier_body="import time\ntime.sleep(2)\n",
                timeout_seconds=1,
            )
            timed_out = timeout_fixture.invoke()
            self.assertEqual(
                json.loads(timed_out.stdout)["hookSpecificOutput"][
                    "permissionDecision"
                ],
                "deny",
            )
            receipt = list_agent_native_tool_receipts(
                timeout_fixture.run,
                timeout_fixture.start["invocation_sha256"],
            )[0]
            self.assertEqual(receipt["hook_results"][0]["status"], "timed_out")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = NativeToolFixture(
                root,
                verifier_body="import time\ntime.sleep(1)\n",
                timeout_seconds=5,
            )
            env = os.environ.copy()
            env["CONDUCTOR_AGENT_PRE_TOOL_CONFIG"] = str(
                agent_native_tool_config_path(
                    fixture.run, fixture.start["invocation_sha256"]
                )
            )
            env["CONDUCTOR_AGENT_PRE_TOOL_CONFIG_SHA256"] = fixture.config[
                "config_sha256"
            ]
            process = subprocess.Popen(
                [sys.executable, "-I", str(fixture.bridge), bridge_program_sha256()],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(root),
                env=env,
            )
            event = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_use_id": "running-tool",
                "tool_input": {"command": "pwd"},
            }
            process.stdin.write(json.dumps(event))
            process.stdin.close()
            receipt_dir = (
                fixture.run.artifacts_dir
                / ".agent-native-tool-hooks"
                / "receipts"
                / fixture.start["invocation_sha256"]
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not list(receipt_dir.glob("*.json")):
                time.sleep(0.01)
            self.assertTrue(list(receipt_dir.glob("*.json")))
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
            with self.assertRaisesRegex(ValidationError, "ReceiptIndeterminate"):
                settle_agent_native_tool_gate(
                    fixture.run,
                    fixture.gate,
                    updated_at_utc=utc_now(),
                )
            time.sleep(1.05)

    def test_bridge_replay_is_exactly_once_and_unmatched_tools_are_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "calls.log"
            fixture = NativeToolFixture(root, verifier_log=log)
            first = fixture.invoke()
            second = fixture.invoke()
            self.assertEqual(json.loads(first.stdout), {})
            self.assertEqual(json.loads(second.stdout), {})
            self.assertEqual(log.read_text(encoding="utf-8"), "called\n")
            unmatched = fixture.invoke(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "apply_patch",
                    "tool_use_id": "tool-2",
                    "tool_input": {"command": "patch"},
                }
            )
            self.assertEqual(json.loads(unmatched.stdout), {})
            self.assertEqual(
                len(
                    list_agent_native_tool_receipts(
                        fixture.run, fixture.start["invocation_sha256"]
                    )
                ),
                1,
            )

    def test_bridge_and_parent_fail_closed_on_tamper_or_running_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = NativeToolFixture(Path(tmp))
            fixture.invoke()
            receipt_dir = (
                fixture.run.artifacts_dir
                / ".agent-native-tool-hooks"
                / "receipts"
                / fixture.start["invocation_sha256"]
            )
            receipt_path = next(receipt_dir.glob("*.json"))
            value = json.loads(receipt_path.read_text(encoding="utf-8"))
            value["tool_name"] = "apply_patch"
            receipt_path.write_text(json.dumps(value), encoding="utf-8")
            denied = fixture.invoke()
            self.assertEqual(
                json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"],
                "deny",
            )
            with self.assertRaisesRegex(ValidationError, "receipt hash"):
                settle_agent_native_tool_gate(
                    fixture.run,
                    fixture.gate,
                    updated_at_utc=utc_now(),
                )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = NativeToolFixture(Path(tmp))
            fixture.invoke()
            receipt_path = next(
                (
                    fixture.run.artifacts_dir
                    / ".agent-native-tool-hooks"
                    / "receipts"
                    / fixture.start["invocation_sha256"]
                ).glob("*.json")
            )
            value = json.loads(receipt_path.read_text(encoding="utf-8"))
            value["tool_name"] = "apply_patch"
            value["receipt_sha256"] = ""
            value["receipt_sha256"] = hashlib.sha256(
                json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            receipt_path.write_text(
                json.dumps(value, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            denied = fixture.invoke()
            self.assertEqual(
                json.loads(denied.stdout)["hookSpecificOutput"][
                    "permissionDecision"
                ],
                "deny",
            )

    def test_gate_config_and_bridge_are_strictly_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = NativeToolFixture(Path(tmp))
            gate = load_agent_native_tool_gate(
                agent_native_tool_gate_path(
                    fixture.run, fixture.start["invocation_sha256"]
                )
            )
            self.assertEqual(gate["status"], "prepared")
            bridge = agent_native_tool_bridge_path(fixture.run)
            bridge.chmod(stat.S_IRUSR | stat.S_IWUSR)
            bridge.write_text("raise SystemExit(0)\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "BridgeChanged"):
                settle_agent_native_tool_gate(
                    fixture.run,
                    gate,
                    updated_at_utc=utc_now(),
                )
            failed = load_agent_native_tool_gate(
                agent_native_tool_gate_path(
                    fixture.run, fixture.start["invocation_sha256"]
                )
            )
            self.assertEqual(failed["status"], "failed")

    def test_runner_denies_before_tool_and_settles_before_terminal_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            verifier = bin_dir / "false"
            verifier.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
            verifier.chmod(0o700)
            workflow = workflow_with_hook(command=[verifier.name])
            preflight = {
                "codex_path": "/opt/codex",
                "codex_version": "codex-cli 0.144.1",
                "hook_hash": "sha256:" + "c" * 64,
                "hook_state_config": 'hooks.state={"own"={enabled=true}}',
                "disabled_unmanaged_hooks": 2,
                "managed_hooks": 1,
            }
            path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            with mock.patch.dict(os.environ, {"PATH": path}), mock.patch(
                "conductor_extras.runtime.runner.restricted_hook_python",
                return_value=sys.executable,
            ), mock.patch(
                "conductor_extras.runtime.runner.prepare_pre_tool_hook_state",
                return_value=preflight,
            ) as prepare:
                runner = FakeNativeToolRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True, approvals={"guard"}),
                )
                run = runner.execute()
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual(runner.provider_calls, 1)
            self.assertFalse(runner.tool_allowed)
            self.assertFalse((workspace / "danger.txt").exists())
            self.assertEqual(run.read_state()["status"], "completed")
            gates = list(
                (run.artifacts_dir / ".agent-native-tool-hooks" / "gates").glob(
                    "*.json"
                )
            )
            self.assertEqual(len(gates), 1)
            gate = load_agent_native_tool_gate(gates[0])
            self.assertEqual(gate["status"], "settled")
            self.assertEqual(gate["denied_count"], 1)
            summary = run.read_state()["agent_native_tool_hooks"]
            self.assertEqual(summary["status"], "settled")
            self.assertEqual(summary["gate_count"], 1)
            self.assertEqual(summary["receipt_count"], 1)
            self.assertEqual(summary["denied_count"], 1)
            self.assertEqual(summary["command_policy_gate_count"], 0)
            self.assertNotIn("touch danger", json.dumps(summary))
            detail = collect_run_detail(root / "runs", run.run_dir.name)
            self.assertEqual(
                detail["agent_native_tool_hooks"]["denied_count"],
                1,
            )
            self.assertIn(
                "agent_native_tool_calls_denied",
                {issue["code"] for issue in detail["diagnostics"]["issues"]},
            )
            dashboard = root / "dashboard.html"
            write_dashboard(root / "runs", root / "benchmarks", dashboard)
            dashboard_text = dashboard.read_text(encoding="utf-8")
            self.assertIn("Native Tool Gates", dashboard_text)
            self.assertIn("1 denied", dashboard_text)
            self.assertNotIn("touch danger", dashboard_text)
            self.assertTrue(
                runner.provider_envs[0]["CONDUCTOR_AGENT_PRE_TOOL_CONFIG"].startswith(
                    str(run.run_dir)
                )
            )
            command = runner.provider_commands[0]
            exclude = next(
                value
                for value in command
                if isinstance(value, str)
                and value.startswith("shell_environment_policy.exclude=")
            )
            self.assertIn("CONDUCTOR_AGENT_PRE_TOOL_CONFIG", exclude)

    def test_runner_warn_mode_allows_tool_and_records_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            verifier = bin_dir / "false"
            verifier.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            verifier.chmod(0o700)
            workflow = workflow_with_hook(
                command=[verifier.name],
                on_failure="warn",
            )
            preflight = {
                "codex_path": "/opt/codex",
                "codex_version": "codex-cli 0.144.1",
                "hook_hash": "sha256:" + "d" * 64,
                "hook_state_config": 'hooks.state={"own"={enabled=true}}',
                "disabled_unmanaged_hooks": 0,
                "managed_hooks": 0,
            }
            path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            with mock.patch.dict(os.environ, {"PATH": path}), mock.patch(
                "conductor_extras.runtime.runner.restricted_hook_python",
                return_value=sys.executable,
            ), mock.patch(
                "conductor_extras.runtime.runner.prepare_pre_tool_hook_state",
                return_value=preflight,
            ):
                runner = FakeNativeToolRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True, approvals={"guard"}),
                )
                run = runner.execute()
            self.assertTrue(runner.tool_allowed)
            self.assertEqual(
                (workspace / "danger.txt").read_text(encoding="utf-8"),
                "tool ran\n",
            )
            gate_path = next(
                (run.artifacts_dir / ".agent-native-tool-hooks" / "gates").glob(
                    "*.json"
                )
            )
            gate = load_agent_native_tool_gate(gate_path)
            self.assertEqual(gate["passed_count"], 1)
            self.assertEqual(gate["warning_count"], 1)

    def test_native_session_timeout_covers_the_full_verifier_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            verifier = bin_dir / "chain-verifier"
            verifier.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            verifier.chmod(0o700)
            hooks = []
            for hook_id in ("guard-a", "guard-b"):
                hooks.append(
                    {
                        "id": hook_id,
                        "event": "agent_pre_tool",
                        "command": [verifier.name],
                        "tools": ["Bash"],
                        "steps": ["work"],
                        "scopes": ["codex_exec"],
                        "timeout_seconds": 30,
                        "risk": "high",
                    }
                )
            workflow = workflow_with_hook()
            workflow["hooks"] = hooks
            preflight = {
                "codex_path": "/opt/codex",
                "codex_version": "codex-cli 0.144.1",
                "hook_hash": "sha256:" + "e" * 64,
                "hook_state_config": 'hooks.state={"own"={enabled=true}}',
                "disabled_unmanaged_hooks": 0,
                "managed_hooks": 0,
            }
            path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            with mock.patch.dict(os.environ, {"PATH": path}), mock.patch(
                "conductor_extras.runtime.runner.restricted_hook_python",
                return_value=sys.executable,
            ), mock.patch(
                "conductor_extras.runtime.runner.prepare_pre_tool_hook_state",
                return_value=preflight,
            ) as prepare:
                runner = FakeNativeToolRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(
                        allow_agent=True,
                        approvals={"guard-a", "guard-b"},
                    ),
                )
                runner.execute()
            self.assertEqual(prepare.call_args.kwargs["timeout_seconds"], 65)
            definition = next(
                value
                for value in runner.provider_commands[0]
                if isinstance(value, str) and value.startswith("hooks.PreToolUse=")
            )
            self.assertIn("timeout=65", definition)

    def test_custom_verifier_requires_explicit_approval_before_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            verifier = bin_dir / "custom-native-verifier"
            verifier.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            verifier.chmod(0o700)
            workflow = workflow_with_hook(
                command=[verifier.name],
                risk="high",
            )
            path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            with mock.patch.dict(os.environ, {"PATH": path}), mock.patch(
                "conductor_extras.runtime.runner.prepare_pre_tool_hook_state"
            ) as prepare:
                runner = FakeNativeToolRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                )
                with self.assertRaisesRegex(
                    PolicyError,
                    "requires --approve guard",
                ):
                    runner.execute()
            self.assertEqual(runner.provider_calls, 0)
            prepare.assert_not_called()

    def test_repo_local_native_tool_state_fails_before_preflight_or_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            workflow = workflow_with_hook(command=["true"])
            with mock.patch(
                "conductor_extras.runtime.runner.prepare_pre_tool_hook_state"
            ) as prepare:
                runner = FakeNativeToolRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=workspace / ".runs",
                    policy=RuntimePolicy(allow_agent=True),
                )
                with self.assertRaisesRegex(
                    ValidationError,
                    "outside the source workspace",
                ):
                    runner.execute()
            self.assertEqual(runner.provider_calls, 0)
            prepare.assert_not_called()

    def test_parallel_map_uses_one_preflight_and_one_gate_per_invocation(self):
        class DeferredWorkerSummaryRunner(FakeNativeToolRunner):
            def __init__(self, *args, **kwargs):
                self.worker_summary_calls = 0
                self.main_summary_calls = 0
                super().__init__(*args, **kwargs)

            def _publish_agent_native_tool_summary(self):
                if threading.current_thread() is not threading.main_thread():
                    self.worker_summary_calls += 1
                    return {}
                self.main_summary_calls += 1
                return super()._publish_agent_native_tool_summary()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            verifier = bin_dir / "true"
            verifier.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            verifier.chmod(0o700)
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "native-tool-map",
                "mode": "read_only",
                "max_workers": 2,
                "hooks": [
                    {
                        "id": "guard",
                        "event": "agent_pre_tool",
                        "command": [verifier.name],
                        "tools": ["Bash"],
                        "steps": ["map"],
                        "scopes": ["agent_map_packet"],
                    }
                ],
                "steps": [
                    {
                        "id": "map",
                        "kind": "agent_map",
                        "items": ["one", "two"],
                        "prompt_template": "Inspect {item}.",
                        "capture_dir": "reviews",
                        "max_workers": 2,
                    }
                ],
            }
            preflight = {
                "codex_path": "/opt/codex",
                "codex_version": "codex-cli 0.144.1",
                "hook_hash": "sha256:" + "f" * 64,
                "hook_state_config": 'hooks.state={"own"={enabled=true}}',
                "disabled_unmanaged_hooks": 0,
                "managed_hooks": 0,
            }
            path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            with mock.patch.dict(os.environ, {"PATH": path}), mock.patch(
                "conductor_extras.runtime.runner.restricted_hook_python",
                return_value=sys.executable,
            ), mock.patch(
                "conductor_extras.runtime.runner.prepare_pre_tool_hook_state",
                return_value=preflight,
            ) as prepare:
                runner = DeferredWorkerSummaryRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(
                        allow_agent=True,
                        allow_parallel=True,
                        approvals={"guard"},
                    ),
                )
                run = runner.execute()
            self.assertEqual(runner.provider_calls, 2)
            self.assertEqual(runner.worker_summary_calls, 2)
            self.assertEqual(runner.main_summary_calls, 1)
            self.assertEqual(prepare.call_count, 1)
            gates = [
                load_agent_native_tool_gate(path)
                for path in sorted(
                    (
                        run.artifacts_dir
                        / ".agent-native-tool-hooks"
                        / "gates"
                    ).glob("*.json")
                )
            ]
            self.assertEqual(len(gates), 2)
            self.assertTrue(all(gate["status"] == "settled" for gate in gates))
            self.assertEqual(
                {gate["start_input"]["packet_index"] for gate in gates},
                {1, 2},
            )
            summary = run.read_state()["agent_native_tool_hooks"]
            self.assertEqual(summary["gate_count"], 2)
            self.assertEqual(summary["settled_gate_count"], 2)
            self.assertEqual(summary["receipt_count"], 2)

    def test_team_turn_scope_binds_member_task_and_generation(self):
        from tests.test_agent_team import FakeTeamRunner, continuation_workflow

        class NativeToolTeamRunner(FakeTeamRunner):
            def _run_process(
                self,
                argv,
                cwd,
                timeout,
                input_text=None,
                output_limit_bytes=1024 * 1024,
                env=None,
                process_event_callback=None,
                stdout_line_callback=None,
                interrupt_check=None,
            ):
                if "--output-last-message" in argv and env is not None:
                    bridge = agent_native_tool_bridge_path(self.run)
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            str(bridge),
                            bridge_program_sha256(),
                        ],
                        input=json.dumps(
                            {
                                "hook_event_name": "PreToolUse",
                                "tool_name": "Bash",
                                "tool_use_id": "team-tool-%d" % (len(self.calls) + 1),
                                "tool_input": {"command": "pwd"},
                            }
                        ),
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                        cwd=str(cwd),
                        timeout=10,
                        check=False,
                    )
                    if json.loads(result.stdout).get("hookSpecificOutput"):
                        raise AssertionError("team pre-tool verifier unexpectedly denied")
                return super()._run_process(
                    argv,
                    cwd,
                    timeout,
                    input_text=input_text,
                    output_limit_bytes=output_limit_bytes,
                    env=env,
                    process_event_callback=process_event_callback,
                    stdout_line_callback=stdout_line_callback,
                    interrupt_check=interrupt_check,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            verifier = bin_dir / "true"
            verifier.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            verifier.chmod(0o700)
            workflow = continuation_workflow()
            workflow["hooks"] = [
                {
                    "id": "team-guard",
                    "event": "agent_pre_tool",
                    "command": [verifier.name],
                    "tools": ["Bash"],
                    "steps": ["team-debug"],
                    "scopes": ["agent_team_turn"],
                    "members": ["worker"],
                    "tasks": ["debug"],
                }
            ]
            preflight = {
                "codex_path": "/opt/codex",
                "codex_version": "codex-cli 0.144.1",
                "hook_hash": "sha256:" + "1" * 64,
                "hook_state_config": 'hooks.state={"own"={enabled=true}}',
                "disabled_unmanaged_hooks": 0,
                "managed_hooks": 0,
            }
            path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            with mock.patch.dict(os.environ, {"PATH": path}), mock.patch(
                "conductor_extras.runtime.runner.restricted_hook_python",
                return_value=sys.executable,
            ), mock.patch(
                "conductor_extras.runtime.runner.prepare_pre_tool_hook_state",
                return_value=preflight,
            ):
                runner = NativeToolTeamRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(
                        allow_agent=True,
                        approvals={"team-guard"},
                    ),
                )
                run = runner.execute()
            gates = [
                load_agent_native_tool_gate(path)
                for path in (
                    run.artifacts_dir / ".agent-native-tool-hooks" / "gates"
                ).glob("*.json")
            ]
            self.assertEqual(len(gates), 1)
            start = gates[0]["start_input"]
            self.assertEqual(start["scope"], "agent_team_turn")
            self.assertEqual(start["member_id"], "worker")
            self.assertEqual(start["task_id"], "debug")
            self.assertEqual(start["generation"], 0)
            self.assertEqual(start["round"], 1)
            self.assertEqual(gates[0]["status"], "settled")

    def test_terminal_recovery_requires_settled_gate_without_replay(self):
        for mutation in (None, "tamper"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "workspace"
                workspace.mkdir()
                bin_dir = root / "bin"
                bin_dir.mkdir()
                verifier = bin_dir / "true"
                verifier.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                verifier.chmod(0o700)
                workflow = workflow_with_hook(command=[verifier.name])
                preflight = {
                    "codex_path": "/opt/codex",
                    "codex_version": "codex-cli 0.144.1",
                    "hook_hash": "sha256:" + "e" * 64,
                    "hook_state_config": 'hooks.state={"own"={enabled=true}}',
                    "disabled_unmanaged_hooks": 0,
                    "managed_hooks": 0,
                }
                path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
                with mock.patch.dict(os.environ, {"PATH": path}), mock.patch(
                    "conductor_extras.runtime.runner.restricted_hook_python",
                    return_value=sys.executable,
                ), mock.patch(
                    "conductor_extras.runtime.runner.prepare_pre_tool_hook_state",
                    return_value=preflight,
                ):
                    runner = CrashAfterNativeToolTerminalRunner(
                        workflow=workflow,
                        workspace=workspace,
                        base_run_dir=root / "runs",
                        policy=RuntimePolicy(
                            allow_agent=True,
                            approvals={"guard"},
                        ),
                    )
                    with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                        runner.execute()
                    self.assertEqual(runner.provider_calls, 1)
                    gate_path = next(
                        (
                            runner.run.artifacts_dir
                            / ".agent-native-tool-hooks"
                            / "gates"
                        ).glob("*.json")
                    )
                    self.assertEqual(
                        load_agent_native_tool_gate(gate_path)["status"],
                        "settled",
                    )
                    if mutation == "tamper":
                        gate = json.loads(gate_path.read_text(encoding="utf-8"))
                        gate["denied_count"] = 1
                        gate_path.write_text(json.dumps(gate), encoding="utf-8")
                    step = runner.workflow["steps"][0]
                    runner.run.mark_step_started(step["id"], step["kind"])
                    if mutation is None:
                        runner._codex_exec(step)
                        self.assertEqual(runner.provider_calls, 1)
                        self.assertEqual(
                            runner.run.read_state()["steps"][step["id"]]["status"],
                            "completed",
                        )
                    else:
                        with self.assertRaisesRegex(ValidationError, "gate"):
                            runner._codex_exec(step)
                        self.assertEqual(runner.provider_calls, 1)


if __name__ == "__main__":
    unittest.main()
