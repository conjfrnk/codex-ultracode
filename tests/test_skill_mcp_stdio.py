import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from conductor_extras.runtime.agent_profiles import (
    AGENT_PROFILE_SCHEMA,
    _read_agent_profile_skill_record,
    agent_profile_summary,
    effective_agent_step,
)
from conductor_extras.runtime.agent_team import AGENT_TEAM_TURN_SCHEMA
from conductor_extras.runtime.agent_skill_mcp import codex_skill_mcp_config_arg
from conductor_extras.runtime.codex_checkpoint import (
    load_codex_step_checkpoint,
    write_codex_step_checkpoint,
)
from conductor_extras.runtime.codex_hook_preflight import (
    SESSION_HOOK_KEY,
    hooks_state_config_arg,
)
from conductor_runtime.errors import PolicyError, ValidationError
from conductor_extras.runtime.run_control import recover_run
from conductor_extras.runtime.runner import ProcessResult, WorkflowRunner
from conductor_extras.runtime.security import RuntimePolicy, enforce_agent_policy
from conductor_extras.runtime.skill_mcp_stdio import SkillMcpStdioLaunch


def _stdio_dependency(skill: str, args=None):
    return {
        "skill": skill,
        "name": "localDocs",
        "transport": "stdio",
        "script": "scripts/server.py",
        "args": list(args or []),
        "tools": ["search", "fetch"],
    }


def _profile(skill: str, dependency):
    return {
        "schema": AGENT_PROFILE_SCHEMA,
        "name": "local-docs",
        "instructions": "Use only the selected local documentation server.",
        "sandbox": "read-only",
        "tool_policy": "restricted",
        "command_allowlist": [],
        "allow_apply_patch": False,
        "skills": [skill],
        "skill_mcp_dependencies": [dependency],
    }


def _workflow(profile):
    return {
        "schema": "conductor.workflow.v1",
        "name": "stdio-skill-mcp",
        "mode": "read_only",
        "agent_profiles": [profile],
        "steps": [
            {
                "id": "review",
                "kind": "codex_exec",
                "agent_profile": profile["name"],
                "prompt": "Use the selected local server.",
                "capture": "review.md",
            }
        ],
    }


def _write_skill(root: Path, relative: str, script: str) -> Path:
    skill = root / relative
    (skill / "agents").mkdir(parents=True)
    (skill / "scripts").mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: local-docs\ndescription: Search local docs.\n---\n",
        encoding="utf-8",
    )
    (skill / "agents" / "openai.yaml").write_text(
        "dependencies:\n"
        "  tools:\n"
        "    - type: \"mcp\"\n"
        "      value: \"localDocs\"\n"
        "      transport: \"stdio\"\n"
        "      command: \"scripts/server.py\"\n",
        encoding="utf-8",
    )
    server = skill / "scripts" / "server.py"
    server.write_text(script, encoding="utf-8")
    server.chmod(0o755)
    return skill


def _discovery(root: Path, relative: str, command: str = "scripts/server.py"):
    return {
        "codex_path": "/opt/codex",
        "codex_version": "codex-cli 0.144.1",
        "skills": [
            {
                "name": "local-docs",
                "description": "Search local docs.",
                "dependencies": {
                    "tools": [
                        {
                            "type": "mcp",
                            "value": "localDocs",
                            "transport": "stdio",
                            "command": command,
                        }
                    ]
                },
                "path": str((root / relative / "SKILL.md").resolve()),
                "scope": "repo",
                "enabled": True,
            }
        ],
        "errors": [],
    }


class _CaptureRunner(WorkflowRunner):
    def __init__(self, *args, **kwargs):
        self.commands = []
        self._command_lock = threading.Lock()
        super().__init__(*args, **kwargs)

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
    ):
        del cwd, timeout, input_text, output_limit_bytes, env, process_event_callback
        with self._command_lock:
            self.commands.append(list(argv))
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        if "--" in output.stem:
            task_id = output.stem.split("--", 1)[1]
            output.write_text(
                json.dumps(
                    {
                        "schema": AGENT_TEAM_TURN_SCHEMA,
                        "task_id": task_id,
                        "status": "completed",
                        "summary": "bounded result",
                        "messages": [],
                        "claim_task_ids": [],
                        "task_proposals": [],
                    }
                ),
                encoding="utf-8",
            )
        else:
            output.write_text("bounded result\n", encoding="utf-8")
        started = json.dumps(
            {
                "type": "thread.started",
                "thread_id": "12345678-1234-4234-8234-123456789abc",
            }
        )
        completed = json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            }
        )
        if stdout_line_callback is not None:
            stdout_line_callback(started)
            stdout_line_callback(completed)
        return ProcessResult(
            returncode=0,
            stdout=started + "\n" + completed + "\n",
            stderr="",
        )


def _preflight():
    return {
        "codex_path": "/opt/codex",
        "codex_version": "codex-cli 0.144.1",
        "hook_hash": "sha256:" + "d" * 64,
        "hook_state_config": hooks_state_config_arg(
            {
                SESSION_HOOK_KEY: {
                    "enabled": True,
                    "trusted_hash": "sha256:" + "d" * 64,
                }
            }
        ),
        "disabled_unmanaged_hooks": 0,
        "managed_hooks": 0,
    }


def _assert_proxy_commands(testcase, commands):
    testcase.assertTrue(commands)
    receipt_paths = []
    for command in commands:
        config = next(value for value in command if value.startswith("mcp_servers="))
        testcase.assertIn("_skill-mcp-stdio-proxy", config)
        testcase.assertNotIn("scripts/server.py", config)
        args_match = re.search(r"args=(\[[^\]]*\])", config)
        testcase.assertIsNotNone(args_match)
        args = json.loads(args_match.group(1))
        receipts = [Path(value) for value in args if value.endswith("/receipt.json")]
        testcase.assertEqual(len(receipts), 1)
        testcase.assertFalse(receipts[0].exists())
        receipt_paths.extend(receipts)
    testcase.assertEqual(len(set(receipt_paths)), len(receipt_paths))


class SkillMcpStdioTests(unittest.TestCase):
    def test_stdio_route_is_exact_approved_and_rendered_only_through_proxy(self):
        skill = ".agents/skills/local-docs"
        dependency = _stdio_dependency(skill)
        profile = _profile(skill, dependency)
        summary = agent_profile_summary(profile)
        self.assertEqual(summary["skill_mcp_dependency_count"], 1)
        self.assertEqual(summary["skill_mcp_http_dependency_count"], 0)
        self.assertEqual(summary["skill_mcp_stdio_dependency_count"], 1)

        effective = effective_agent_step(
            {"agent_profiles": [profile], "steps": []},
            {"id": "review", "kind": "codex_exec", "agent_profile": profile["name"]},
        )
        with self.assertRaisesRegex(PolicyError, "skill-mcp:localDocs"):
            enforce_agent_policy(effective, RuntimePolicy(allow_agent=True))
        with self.assertRaisesRegex(PolicyError, "skill-mcp-stdio:localDocs"):
            enforce_agent_policy(
                effective,
                RuntimePolicy(
                    allow_agent=True,
                    approvals={"skill-mcp:localDocs"},
                ),
            )
        enforce_agent_policy(
            effective,
            RuntimePolicy(
                allow_agent=True,
                approvals={"skill-mcp:localDocs", "skill-mcp-stdio:localDocs"},
            ),
        )

        inert = codex_skill_mcp_config_arg([dependency])
        self.assertIn('command="conductor-skill-mcp-stdio-proxy-unprepared"', inert)
        self.assertNotIn("scripts/server.py", inert)
        routed = codex_skill_mcp_config_arg(
            [dependency],
            command_overrides={"localDocs": ["/usr/bin/python3", "proxy.py", "receipt"]},
        )
        self.assertIn('command="/usr/bin/python3"', routed)
        self.assertIn('args=["proxy.py","receipt"]', routed)
        self.assertNotIn("scripts/server.py", routed)

    def test_bind_requires_exact_expansion_free_command_and_executable_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = ".agents/skills/local-docs"
            _write_skill(root, skill, "#!/usr/bin/env python3\n")
            dependency = _stdio_dependency(skill, ["--root", "docs folder"])
            profile = _profile(skill, dependency)
            with mock.patch(
                "conductor_extras.runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=_discovery(
                    root,
                    skill,
                    command="scripts/server.py --root 'docs folder'",
                ),
            ):
                runner = WorkflowRunner(
                    workflow=_workflow(profile),
                    workspace=root,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                )
            effective = effective_agent_step(runner.workflow, runner.workflow["steps"][0])
            self.assertEqual(effective["_agent_profile_skill_mcp_stdio_dependency_count"], 1)

            for command in ("scripts/other.py", "scripts/server.py && echo bad", "$SERVER"):
                with self.subTest(command=command), mock.patch(
                    "conductor_extras.runtime.agent_profiles.discover_codex_skill_metadata",
                    return_value=_discovery(root, skill, command=command),
                ), self.assertRaisesRegex(ValidationError, "stdio"):
                    WorkflowRunner(
                        workflow=_workflow(profile),
                        workspace=root,
                        base_run_dir=root / ("bad-" + hashlib.sha256(command.encode()).hexdigest()[:8]),
                        policy=RuntimePolicy(allow_agent=True),
                    )

            (root / skill / "scripts" / "server.py").chmod(0o644)
            with mock.patch(
                "conductor_extras.runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=_discovery(
                    root,
                    skill,
                    command="scripts/server.py --root 'docs folder'",
                ),
            ), self.assertRaisesRegex(ValidationError, "non-empty and executable"):
                WorkflowRunner(
                    workflow=_workflow(profile),
                    workspace=root,
                    base_run_dir=root / "not-executable",
                    policy=RuntimePolicy(allow_agent=True),
                )

    @mock.patch(
        "conductor_extras.runtime.skill_mcp_stdio._supported_sandbox",
        return_value=("linux-bwrap", "/usr/bin/bwrap"),
    )
    def test_direct_map_team_and_same_thread_recovery_receive_fresh_proxy_routes(
        self,
        supported_sandbox,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = ".agents/skills/local-docs"
            _write_skill(root, skill, "#!/usr/bin/env python3\n")
            dependency = _stdio_dependency(skill)
            profile = _profile(skill, dependency)
            discovery = _discovery(root, skill)
            base_policy = RuntimePolicy(
                allow_agent=True,
                approvals={"skill-mcp:localDocs", "skill-mcp-stdio:localDocs"},
            )
            patches = (
                mock.patch(
                    "conductor_extras.runtime.agent_profiles.discover_codex_skill_metadata",
                    return_value=discovery,
                ),
                mock.patch(
                    "conductor_extras.runtime.runner.restricted_hook_python",
                    return_value="/usr/bin/python3",
                ),
                mock.patch(
                    "conductor_extras.runtime.runner.prepare_restricted_hook_state",
                    return_value=_preflight(),
                ),
            )
            with patches[0], patches[1], patches[2]:
                direct = _CaptureRunner(
                    workflow=_workflow(profile),
                    workspace=root,
                    base_run_dir=root / "direct-runs",
                    policy=base_policy,
                )
                direct.execute()
            _assert_proxy_commands(self, direct.commands)
            state = direct.run.read_state()["steps"]["review"]
            self.assertEqual(state["agent_profile_skill_mcp_dependency_count"], 1)
            self.assertEqual(state["agent_profile_skill_mcp_tool_count"], 2)
            self.assertEqual(state["agent_profile_skill_mcp_http_dependency_count"], 0)
            self.assertEqual(state["agent_profile_skill_mcp_stdio_dependency_count"], 1)

            checkpoint = load_codex_step_checkpoint(direct.run, "review")
            checkpoint["status"] = "active"
            checkpoint["finished_at_utc"] = None
            checkpoint["output_sha256"] = None
            checkpoint["error_class"] = None
            write_codex_step_checkpoint(direct.run, checkpoint)
            run_state = direct.run.read_state()
            run_state["status"] = "running"
            run_state.pop("finished_at_utc", None)
            run_state.pop("duration_ms", None)
            run_state["steps"]["review"]["status"] = "running"
            run_state["steps"]["review"].pop("finished_at_utc", None)
            run_state["steps"]["review"].pop("duration_ms", None)
            direct.run.save_state(run_state)
            recovered = recover_run(
                direct.run.run_dir,
                reason="resume selected Skill stdio MCP thread",
                resume_codex=True,
            )
            self.assertEqual(recovered["resolution"], "resume-codex")
            with mock.patch(
                "conductor_extras.runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=discovery,
            ), mock.patch(
                "conductor_extras.runtime.runner.restricted_hook_python",
                return_value="/usr/bin/python3",
            ), mock.patch(
                "conductor_extras.runtime.runner.prepare_restricted_hook_state",
                return_value=_preflight(),
            ):
                resumed = _CaptureRunner(
                    workflow=_workflow(profile),
                    workspace=root,
                    base_run_dir=root / "direct-runs",
                    resume_dir=direct.run.run_dir,
                    policy=base_policy,
                )
                resumed.execute()
            _assert_proxy_commands(self, resumed.commands)
            self.assertIn("resume", resumed.commands[0])

            (root / "a.py").write_text("A = 1\n", encoding="utf-8")
            (root / "b.py").write_text("B = 2\n", encoding="utf-8")
            map_workflow = {
                "schema": "conductor.workflow.v1",
                "name": "stdio-map",
                "mode": "read_only",
                "max_workers": 2,
                "agent_profiles": [dict(profile, max_workers=2)],
                "steps": [
                    {
                        "id": "review",
                        "kind": "agent_map",
                        "agent_profile": profile["name"],
                        "items": ["a.py", "b.py"],
                        "prompt_template": "Inspect {item}.",
                        "capture_dir": "reviews",
                        "max_workers": 2,
                    }
                ],
            }
            with mock.patch(
                "conductor_extras.runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=discovery,
            ), mock.patch(
                "conductor_extras.runtime.runner.restricted_hook_python",
                return_value="/usr/bin/python3",
            ), mock.patch(
                "conductor_extras.runtime.runner.prepare_restricted_hook_state",
                return_value=_preflight(),
            ):
                mapped = _CaptureRunner(
                    workflow=map_workflow,
                    workspace=root,
                    base_run_dir=root / "map-runs",
                    policy=RuntimePolicy(
                        allow_agent=True,
                        allow_parallel=True,
                        approvals={"skill-mcp:localDocs", "skill-mcp-stdio:localDocs"},
                    ),
                )
                mapped.execute()
            self.assertEqual(len(mapped.commands), 2)
            _assert_proxy_commands(self, mapped.commands)

            team_workflow = {
                "schema": "conductor.workflow.v1",
                "name": "stdio-team",
                "mode": "read_only",
                "max_workers": 2,
                "agent_profiles": [profile],
                "steps": [
                    {
                        "id": "team",
                        "kind": "agent_team",
                        "risk": "medium",
                        "sandbox": "read-only",
                        "max_workers": 2,
                        "max_rounds": 1,
                        "max_tokens": 1000,
                        "max_total_tokens": 2000,
                        "capture_dir": "team",
                        "report": "team.md",
                        "members": [
                            {
                                "id": "lead",
                                "role": "Lead",
                                "instructions": "Lead the review.",
                                "agent_profile": profile["name"],
                                "lead": True,
                            },
                            {
                                "id": "peer",
                                "role": "Peer",
                                "instructions": "Check the review.",
                                "agent_profile": profile["name"],
                                "lead": False,
                            },
                        ],
                        "tasks": [
                            {
                                "id": "inspect",
                                "description": "Inspect one boundary.",
                                "assignee": "lead",
                                "depends_on": [],
                            }
                        ],
                    }
                ],
            }
            with mock.patch(
                "conductor_extras.runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=discovery,
            ), mock.patch(
                "conductor_extras.runtime.runner.restricted_hook_python",
                return_value="/usr/bin/python3",
            ), mock.patch(
                "conductor_extras.runtime.runner.prepare_restricted_hook_state",
                return_value=_preflight(),
            ):
                team = _CaptureRunner(
                    workflow=team_workflow,
                    workspace=root,
                    base_run_dir=root / "team-runs",
                    policy=RuntimePolicy(
                        allow_agent=True,
                        allow_parallel=True,
                        approvals={"skill-mcp:localDocs", "skill-mcp-stdio:localDocs"},
                    ),
                )
                team.execute()
            _assert_proxy_commands(self, team.commands)
            team_state = team.run.read_state()
            self.assertGreaterEqual(supported_sandbox.call_count, 4)
            self.assertEqual(team_state["status"], "completed")
            self.assertEqual(
                team_state["steps"]["team"][
                    "agent_team_profile_skill_mcp_stdio_dependency_count"
                ],
                1,
            )

    @unittest.skipUnless(
        platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").is_file(),
        "requires macOS Seatbelt",
    )
    def test_real_proxy_denies_workspace_home_network_and_ambient_environment(self):
        script = r'''#!/usr/bin/env python3
import json
import os
import socket
from pathlib import Path

workspace = Path(os.sys.argv[1])
real_home = Path(os.sys.argv[2])
result = {
    "ambient_secret": os.environ.get("CONDUCTOR_STDIO_TEST_SECRET"),
    "skill_read": Path("SKILL.md").is_file(),
}
try:
    result["workspace_read"] = (workspace / "private.txt").read_text()
except OSError:
    result["workspace_read"] = None
try:
    (workspace / "escaped.txt").write_text("bad")
    result["workspace_write"] = True
except OSError:
    result["workspace_write"] = False
try:
    result["home_list"] = len(os.listdir(real_home))
except OSError:
    result["home_list"] = None
try:
    socket.create_connection(("127.0.0.1", 9), timeout=0.2)
    result["network_errno"] = 0
except OSError as exc:
    result["network_errno"] = exc.errno
runtime_file = Path(os.environ["TMPDIR"]) / "ok.txt"
runtime_file.write_text("ok")
result["runtime_write"] = runtime_file.read_text()
print(json.dumps(result, sort_keys=True))
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = ".agents/skills/local-docs"
            _write_skill(root, skill, script)
            (root / "private.txt").write_text("workspace-secret", encoding="utf-8")
            dependency = _stdio_dependency(skill, [str(root), str(Path.home())])
            record = _read_agent_profile_skill_record(root, skill)
            environment = os.environ.copy()
            environment["CONDUCTOR_STDIO_TEST_SECRET"] = "must-not-cross"
            launch_root = None
            with SkillMcpStdioLaunch(
                [dependency],
                workspace=root,
                skill_records=[record],
            ) as launch:
                launch_root = launch.root
                command = launch.commands["localDocs"]
                self.assertIn("_skill-mcp-stdio-proxy", command)
                self.assertNotIn("scripts/server.py", command)
                completed = subprocess.run(
                    command,
                    cwd=Path(__file__).resolve().parents[1],
                    env=environment,
                    text=True,
                    input="",
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertIsNone(result["ambient_secret"])
                self.assertTrue(result["skill_read"])
                self.assertIsNone(result["workspace_read"])
                self.assertFalse(result["workspace_write"])
                self.assertIsNone(result["home_list"])
                self.assertNotEqual(result["network_errno"], 0)
                self.assertEqual(result["runtime_write"], "ok")
                self.assertFalse((root / "escaped.txt").exists())
            self.assertIsNotNone(launch_root)
            self.assertFalse(launch_root.exists())

    @unittest.skipUnless(
        platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").is_file(),
        "requires macOS Seatbelt",
    )
    def test_proxy_fails_closed_when_snapshot_is_tampered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = ".agents/skills/local-docs"
            _write_skill(root, skill, "#!/usr/bin/env python3\nprint('{}')\n")
            dependency = _stdio_dependency(skill)
            record = _read_agent_profile_skill_record(root, skill)
            with SkillMcpStdioLaunch(
                [dependency],
                workspace=root,
                skill_records=[record],
            ) as launch:
                snapshot_script = launch.root / "route-00" / "skill" / "scripts" / "server.py"
                snapshot_script.chmod(0o755)
                snapshot_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                completed = subprocess.run(
                    launch.commands["localDocs"],
                    cwd=Path(__file__).resolve().parents[1],
                    text=True,
                    input="",
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(completed.returncode, 126)
                self.assertEqual(
                    completed.stderr,
                    "Conductor rejected the selected Skill stdio MCP launch.\n",
                )

    @unittest.skipUnless(
        platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").is_file(),
        "requires macOS Seatbelt",
    )
    def test_parent_cleanup_kills_a_server_that_ignores_termination(self):
        script = r'''#!/usr/bin/env python3
import os
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(os.getpid(), flush=True)
while True:
    time.sleep(1)
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = ".agents/skills/local-docs"
            _write_skill(root, skill, script)
            dependency = _stdio_dependency(skill)
            record = _read_agent_profile_skill_record(root, skill)
            launch = SkillMcpStdioLaunch(
                [dependency],
                workspace=root,
                skill_records=[record],
            )
            launch.__enter__()
            launch_root = launch.root
            proxy = subprocess.Popen(
                launch.commands["localDocs"],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                self.assertIsNotNone(proxy.stdout)
                child_pid = int(proxy.stdout.readline().strip())
                process_record = launch.root / "route-00" / "process.json"
                deadline = time.monotonic() + 3
                while not process_record.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(process_record.exists())
                launch.close()
                self.assertEqual(proxy.wait(timeout=5), 137)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
                self.assertFalse(launch_root.exists())
            finally:
                if proxy.poll() is None:
                    proxy.kill()
                    proxy.wait(timeout=5)
                if proxy.stdout is not None:
                    proxy.stdout.close()
                if proxy.stderr is not None:
                    proxy.stderr.close()
                launch.close()


if __name__ == "__main__":
    unittest.main()
