import json
import shlex
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from conductor_runtime.agent_profiles import (
    AGENT_PROFILE_SCHEMA,
    MAX_AGENT_PROFILE_SKILL_SCRIPT_RULES,
    agent_profile_summary,
    effective_agent_step,
    validate_agent_profile,
)
from conductor_runtime.agent_skill_mcp import (
    MAX_AGENT_PROFILE_SKILL_MCP_AUTH_HEADERS,
    MAX_AGENT_PROFILE_SKILL_MCP_DEPENDENCIES,
    MAX_AGENT_PROFILE_SKILL_MCP_TOOLS,
    codex_skill_mcp_config_arg,
    resolve_skill_mcp_auth_secrets,
)
from conductor_runtime.agent_team import AGENT_TEAM_TURN_SCHEMA, initial_agent_team_state
from conductor_runtime.agent_tool_policy import (
    AGENT_COMMAND_POLICY_SCHEMA,
    AGENT_COMMAND_POLICY_SCHEMA_V2,
    build_pre_tool_hook_command,
    command_policy_sha256,
    effective_command_policy,
    evaluate_pre_tool_use,
    parse_simple_shell_command,
)
from conductor_runtime.codex_hook_preflight import (
    SESSION_HOOK_KEY,
    hooks_state_config_arg,
    prepare_restricted_hook_state,
    restricted_codex_executable,
)
from conductor_runtime.codex_checkpoint import (
    load_codex_step_checkpoint,
    write_codex_step_checkpoint,
)
from conductor_runtime.cli import main as cli_main
from conductor_runtime.errors import PolicyError, ValidationError
from conductor_runtime.dashboard import collect_run_detail
from conductor_runtime.runner import ProcessResult, WorkflowRunner
from conductor_runtime.run_control import recover_run
from conductor_runtime.routine_supervisor import _validate_current_target
from conductor_runtime.routines import load_routine_manifest
from conductor_runtime.schemas import get_schema
from conductor_runtime.security import RuntimePolicy, enforce_agent_policy


def restricted_profile(**overrides):
    profile = {
        "schema": AGENT_PROFILE_SCHEMA,
        "name": "bounded-reviewer",
        "instructions": "Inspect only the bounded scope.",
        "sandbox": "read-only",
        "tool_policy": "restricted",
        "tool_output_token_limit": 1200,
        "command_allowlist": [
            {"argv": ["git", "status", "--short"]},
            {"argv_prefix": ["rg"]},
        ],
        "allow_apply_patch": False,
    }
    profile.update(overrides)
    return profile


def hook_event(tool_name, command):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
    }


def write_routed_skill(
    root: Path,
    relative: str = ".agents/skills/boundary-review",
    *,
    script_text: str = "#!/bin/sh\nprintf checked\\n\n",
    executable: bool = True,
) -> Path:
    skill = root / relative
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: boundary-review\ndescription: Verify one boundary.\n---\n\n"
        "Run scripts/verify-boundary.sh when verification is required.\n",
        encoding="utf-8",
    )
    script = skill / "scripts/verify-boundary.sh"
    script.write_text(script_text, encoding="utf-8")
    script.chmod(0o755 if executable else 0o644)
    return script


def write_mcp_skill(
    root: Path,
    relative: str = ".agents/skills/docs",
    *,
    name: str = "openaiDeveloperDocs",
    url: str = "https://developers.openai.com/mcp",
) -> Path:
    skill = root / relative
    (skill / "agents").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: docs\ndescription: Search official documentation.\n---\n",
        encoding="utf-8",
    )
    (skill / "agents" / "openai.yaml").write_text(
        "dependencies:\n"
        "  tools:\n"
        "    - type: \"mcp\"\n"
        "      value: \"%s\"\n"
        "      transport: \"streamable_http\"\n"
        "      url: \"%s\"\n" % (name, url),
        encoding="utf-8",
    )
    return skill / "SKILL.md"


def discovered_mcp_skill(
    root: Path,
    relative: str = ".agents/skills/docs",
    *,
    name: str = "openaiDeveloperDocs",
    url: str = "https://developers.openai.com/mcp",
    command=None,
):
    dependency = {
        "type": "mcp",
        "value": name,
        "transport": "streamable_http",
        "url": url,
    }
    if command is not None:
        dependency["command"] = command
    return {
        "codex_path": "/opt/codex",
        "codex_version": "codex-cli 0.144.1",
        "skills": [
            {
                "name": "docs",
                "description": "Search official documentation.",
                "dependencies": {"tools": [dependency]},
                "path": str((root / relative / "SKILL.md").resolve()),
                "scope": "repo",
                "enabled": True,
            }
        ],
        "errors": [],
    }


class FakeRestrictedRunner(WorkflowRunner):
    def __init__(self, *args, **kwargs):
        self.commands = []
        self.provider_envs = []
        self._commands_lock = threading.Lock()
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
        del cwd, timeout, input_text, output_limit_bytes, process_event_callback
        with self._commands_lock:
            self.commands.append(list(argv))
            self.provider_envs.append(dict(env or {}))
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
        return ProcessResult(returncode=0, stdout=started + "\n" + completed + "\n", stderr="")


class ReflectingRestrictedRunner(FakeRestrictedRunner):
    def __init__(self, *args, reflected_secret, **kwargs):
        self.reflected_secret = reflected_secret
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
        del cwd, timeout, input_text, output_limit_bytes, process_event_callback
        self.provider_envs.append(dict(env or {}))
        with self._commands_lock:
            self.commands.append(list(argv))
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            "reflected credential: %s\n" % self.reflected_secret,
            encoding="utf-8",
        )
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
                "reflected": self.reflected_secret,
            }
        )
        if stdout_line_callback is not None:
            stdout_line_callback(started)
            stdout_line_callback(completed)
        return ProcessResult(
            returncode=0,
            stdout=started + "\n" + completed + "\n",
            stderr="server reflected %s\n" % self.reflected_secret,
        )


class AgentToolPolicyTests(unittest.TestCase):
    def test_selected_skill_mcp_dependency_is_parsed_pinned_and_policy_routed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = ".agents/skills/docs"
            name = "openaiDeveloperDocs"
            url = "https://developers.openai.com/mcp"
            tools = ["search_openai_docs", "fetch_openai_doc"]
            write_mcp_skill(root, skill, name=name, url=url)
            profile = restricted_profile(
                command_allowlist=[],
                skills=[skill],
                skill_mcp_dependencies=[
                    {"skill": skill, "name": name, "url": url, "tools": tools}
                ],
            )
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "skill-mcp-routing",
                "mode": "read_only",
                "agent_profiles": [profile],
                "steps": [
                    {
                        "id": "review",
                        "kind": "codex_exec",
                        "agent_profile": profile["name"],
                        "prompt": "Search the selected documentation.",
                        "capture": "review.md",
                    }
                ],
            }
            with mock.patch(
                "conductor_runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=discovered_mcp_skill(root, skill, name=name, url=url),
            ) as discover:
                runner = WorkflowRunner(
                    workflow=workflow,
                    workspace=root,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(
                        allow_agent=True,
                        allow_network=True,
                        approvals={"skill-mcp:%s" % name},
                    ),
                )
            discover.assert_called_once_with(root.resolve())
            effective = effective_agent_step(runner.workflow, runner.workflow["steps"][0])
            policy = effective["_agent_command_policy"]
            self.assertEqual(policy["schema"], AGENT_COMMAND_POLICY_SCHEMA_V2)
            self.assertEqual(
                policy["tool_allowlist"],
                ["mcp__%s__%s" % (name, tool) for tool in tools],
            )
            self.assertTrue(
                evaluate_pre_tool_use(
                    policy,
                    hook_event("mcp__%s__search_openai_docs" % name, "ignored"),
                )[0]
            )
            self.assertFalse(
                evaluate_pre_tool_use(
                    policy,
                    hook_event("mcp__%s__delete_everything" % name, "ignored"),
                )[0]
            )
            prompt = runner._agent_prompt(effective, "Search.")
            self.assertIn("Selected Skill MCP dependencies: 1 exposing 2 exact tools", prompt)
            summary = agent_profile_summary(profile)
            self.assertEqual(summary["skill_mcp_dependency_count"], 1)
            self.assertEqual(summary["skill_mcp_tool_count"], 2)
            self.assertIsNotNone(summary["skill_mcp_dependencies_sha256"])

            preflight = {
                "codex_path": "/opt/codex",
                "codex_version": "codex-cli 0.144.1",
                "hook_hash": "sha256:" + "b" * 64,
                "hook_state_config": hooks_state_config_arg(
                    {
                        SESSION_HOOK_KEY: {
                            "enabled": True,
                            "trusted_hash": "sha256:" + "b" * 64,
                        }
                    }
                ),
                "disabled_unmanaged_hooks": 0,
                "managed_hooks": 0,
            }
            with mock.patch(
                "conductor_runtime.runner.restricted_hook_python",
                return_value="/usr/bin/python3",
            ), mock.patch(
                "conductor_runtime.runner.prepare_restricted_hook_state",
                return_value=preflight,
            ):
                prepared = runner._prepare_restricted_agent_step(effective)
            command = runner._codex_command(
                prepared,
                runner.run.artifacts_dir / "review.md",
            )
            mcp_config = next(value for value in command if value.startswith("mcp_servers="))
            self.assertIn('openaiDeveloperDocs={url="https://developers.openai.com/mcp"', mcp_config)
            self.assertIn('enabled_tools=["search_openai_docs","fetch_openai_doc"]', mcp_config)
            self.assertIn('default_tools_approval_mode="approve"', mcp_config)
            self.assertIn("--ignore-user-config", command)
            developer_config = next(
                value for value in command if value.startswith("developer_instructions=")
            )
            self.assertIn("only authorized network tools", developer_config)
            self.assertNotIn("do not request broader permissions, use network tools", developer_config)
            self.assertIn(["--disable", "skill_mcp_dependency_install"], [command[index:index + 2] for index in range(len(command) - 1)])

    def test_skill_mcp_requires_network_and_dependency_specific_approval(self):
        step = {
            "id": "docs",
            "kind": "codex_exec",
            "risk": "low",
            "sandbox": "read-only",
            "_agent_skill_mcp_dependencies": [
                {
                    "skill": ".agents/skills/docs",
                    "name": "docs",
                    "url": "https://example.com/mcp",
                    "tools": ["search"],
                }
            ],
        }
        with self.assertRaisesRegex(PolicyError, "--allow-network"):
            enforce_agent_policy(step, RuntimePolicy(allow_agent=True))
        with self.assertRaisesRegex(PolicyError, "--approve skill-mcp:docs"):
            enforce_agent_policy(
                step,
                RuntimePolicy(allow_agent=True, allow_network=True),
            )
        enforce_agent_policy(
            step,
            RuntimePolicy(
                allow_agent=True,
                allow_network=True,
                approvals={"skill-mcp:docs"},
            ),
        )

    def test_authenticated_skill_mcp_requires_separate_credential_approval(self):
        step = {
            "id": "private-docs",
            "kind": "codex_exec",
            "risk": "low",
            "sandbox": "read-only",
            "_agent_skill_mcp_dependencies": [
                {
                    "skill": ".agents/skills/docs",
                    "name": "docs",
                    "url": "https://example.com/mcp",
                    "tools": ["search"],
                    "auth": {"bearer_token_env_var": "PRIVATE_DOCS_TOKEN"},
                }
            ],
        }
        base = RuntimePolicy(
            allow_agent=True,
            allow_network=True,
            approvals={"skill-mcp:docs"},
        )
        with self.assertRaisesRegex(PolicyError, "--approve skill-mcp-auth:docs"):
            enforce_agent_policy(step, base)
        enforce_agent_policy(
            step,
            RuntimePolicy(
                allow_agent=True,
                allow_network=True,
                approvals={"skill-mcp:docs", "skill-mcp-auth:docs"},
            ),
        )

    def test_authenticated_skill_mcp_is_strictly_env_backed_and_renders_names_only(self):
        skill = ".agents/skills/docs"
        secret = "private-token-123456"
        dependency = {
            "skill": skill,
            "name": "privateDocs",
            "url": "https://example.com/mcp",
            "tools": ["search"],
            "auth": {
                "bearer_token_env_var": "PRIVATE_DOCS_TOKEN",
                "env_http_headers": {
                    "X-API-Key": "PRIVATE_DOCS_API_KEY",
                    "X-Tenant": "PRIVATE_DOCS_TENANT",
                },
            },
        }
        profile = restricted_profile(skills=[skill], skill_mcp_dependencies=[dependency])
        validate_agent_profile(profile)
        summary = agent_profile_summary(profile)
        self.assertEqual(summary["skill_mcp_auth_dependency_count"], 1)
        self.assertEqual(summary["skill_mcp_auth_env_var_count"], 3)
        self.assertEqual(summary["skill_mcp_auth_header_count"], 2)
        config = codex_skill_mcp_config_arg([dependency])
        self.assertIn('bearer_token_env_var="PRIVATE_DOCS_TOKEN"', config)
        self.assertIn('"X-API-Key"="PRIVATE_DOCS_API_KEY"', config)
        self.assertIn('"X-Tenant"="PRIVATE_DOCS_TENANT"', config)
        self.assertNotIn(secret, config)
        self.assertEqual(
            resolve_skill_mcp_auth_secrets(
                [dependency],
                {
                    "PRIVATE_DOCS_TOKEN": secret,
                    "PRIVATE_DOCS_API_KEY": "private-api-key-123",
                    "PRIVATE_DOCS_TENANT": "tenant-acme",
                },
            ),
            (secret, "private-api-key-123", "tenant-acme"),
        )
        invalid = [
            (dict(dependency, auth={}), "must contain only"),
            (dict(dependency, auth={"http_headers": {"X-Key": secret}}), "must contain only"),
            (dict(dependency, auth={"bearer_token_env_var": "BAD-NAME"}), "safe environment"),
            (dict(dependency, auth={"env_http_headers": {}}), "non-empty object"),
            (
                dict(
                    dependency,
                    auth={"env_http_headers": {"Authorization": "AUTH_TOKEN"}},
                ),
                "reserved HTTP header",
            ),
            (
                dict(dependency, auth={"env_http_headers": {"Bad:Header": "AUTH_TOKEN"}}),
                "unsafe HTTP header",
            ),
            (
                dict(
                    dependency,
                    auth={
                        "env_http_headers": {
                            "X-Key": "AUTH_TOKEN",
                            "x-key": "AUTH_TOKEN_2",
                        }
                    },
                ),
                "case-insensitive duplicate",
            ),
            (
                dict(
                    dependency,
                    auth={
                        "env_http_headers": {
                            "X-Key-%d" % index: "AUTH_TOKEN_%d" % index
                            for index in range(MAX_AGENT_PROFILE_SKILL_MCP_AUTH_HEADERS + 1)
                        }
                    },
                ),
                "at most 16",
            ),
        ]
        for candidate, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValidationError, message
            ):
                validate_agent_profile(
                    restricted_profile(
                        skills=[skill],
                        skill_mcp_dependencies=[candidate],
                    )
                )
        for environment, message in [
            ({}, "requires environment variable PRIVATE_DOCS_TOKEN"),
            (
                {
                    "PRIVATE_DOCS_TOKEN": "short",
                    "PRIVATE_DOCS_API_KEY": "private-api-key-123",
                    "PRIVATE_DOCS_TENANT": "tenant-acme",
                },
                "bounded safe bearer",
            ),
            (
                {
                    "PRIVATE_DOCS_TOKEN": secret,
                    "PRIVATE_DOCS_API_KEY": "bad\nheader",
                    "PRIVATE_DOCS_TENANT": "tenant-acme",
                },
                "bounded safe HTTP header",
            ),
        ]:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValidationError, message
            ):
                resolve_skill_mcp_auth_secrets([dependency], environment)

    def test_authenticated_skill_mcp_reflections_are_redacted_from_run_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = ".agents/skills/docs"
            secret = "reflection-secret-123456"
            name = "privateDocs"
            url = "https://example.com/mcp"
            write_mcp_skill(root, skill, name=name, url=url)
            dependency = {
                "skill": skill,
                "name": name,
                "url": url,
                "tools": ["search"],
                "auth": {"bearer_token_env_var": "PRIVATE_DOCS_TOKEN"},
            }
            profile = restricted_profile(
                command_allowlist=[],
                skills=[skill],
                skill_mcp_dependencies=[dependency],
            )
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "authenticated-skill-mcp-redaction",
                "mode": "read_only",
                "agent_profiles": [profile],
                "steps": [
                    {
                        "id": "review",
                        "kind": "codex_exec",
                        "agent_profile": profile["name"],
                        "prompt": "Search private documentation.",
                        "capture": "review.md",
                    }
                ],
            }
            preflight = {
                "codex_path": "/opt/codex",
                "codex_version": "codex-cli 0.144.1",
                "hook_hash": "sha256:" + "a" * 64,
                "hook_state_config": hooks_state_config_arg(
                    {
                        SESSION_HOOK_KEY: {
                            "enabled": True,
                            "trusted_hash": "sha256:" + "a" * 64,
                        }
                    }
                ),
                "disabled_unmanaged_hooks": 0,
                "managed_hooks": 0,
            }
            policy = RuntimePolicy(
                allow_agent=True,
                allow_network=True,
                approvals={
                    "skill-mcp:%s" % name,
                    "skill-mcp-auth:%s" % name,
                },
            )
            with mock.patch.dict(
                "os.environ",
                {
                    "PRIVATE_DOCS_TOKEN": secret,
                    "PRIVATE_DOCS_TOKEN_ALIAS": "prefix-%s-suffix" % secret,
                },
            ), mock.patch(
                "conductor_runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=discovered_mcp_skill(root, skill, name=name, url=url),
            ), mock.patch(
                "conductor_runtime.runner.restricted_hook_python",
                return_value="/usr/bin/python3",
            ), mock.patch(
                "conductor_runtime.runner.prepare_restricted_hook_state",
                return_value=preflight,
            ):
                runner = ReflectingRestrictedRunner(
                    workflow=workflow,
                    workspace=root,
                    base_run_dir=root / "runs",
                    policy=policy,
                    reflected_secret=secret,
                )
                runner.execute()
            self.assertEqual(len(runner.commands), 1)
            command_text = "\n".join(runner.commands[0])
            self.assertNotIn(secret, command_text)
            self.assertNotIn("PRIVATE_DOCS_TOKEN", command_text)
            self.assertNotIn("--ephemeral", runner.commands[0])
            self.assertNotIn('mcp_oauth_credentials_store="file"', runner.commands[0])
            self.assertIn("http://127.0.0.1:", command_text)
            self.assertNotIn(url, command_text)
            self.assertEqual(len(runner.provider_envs), 1)
            self.assertNotIn("PRIVATE_DOCS_TOKEN", runner.provider_envs[0])
            self.assertNotIn("PRIVATE_DOCS_TOKEN_ALIAS", runner.provider_envs[0])
            evidence = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in runner.run.run_dir.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(secret, evidence)
            self.assertIn("<redacted-secret>", evidence)

            checkpoint = load_codex_step_checkpoint(runner.run, "review")
            checkpoint["status"] = "active"
            checkpoint["finished_at_utc"] = None
            checkpoint["output_sha256"] = None
            checkpoint["error_class"] = None
            write_codex_step_checkpoint(runner.run, checkpoint)
            state = runner.run.read_state()
            state["status"] = "running"
            state.pop("finished_at_utc", None)
            state.pop("duration_ms", None)
            state["steps"]["review"]["status"] = "running"
            state["steps"]["review"].pop("finished_at_utc", None)
            state["steps"]["review"].pop("duration_ms", None)
            runner.run.save_state(state)
            recovered = recover_run(
                runner.run.run_dir,
                reason="resume authenticated MCP thread",
                resume_codex=True,
            )
            self.assertEqual(recovered["resolution"], "resume-codex")
            with mock.patch.dict("os.environ", {"PRIVATE_DOCS_TOKEN": secret}), mock.patch(
                "conductor_runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=discovered_mcp_skill(root, skill, name=name, url=url),
            ), mock.patch(
                "conductor_runtime.runner.restricted_hook_python",
                return_value="/usr/bin/python3",
            ), mock.patch(
                "conductor_runtime.runner.prepare_restricted_hook_state",
                return_value=preflight,
            ):
                resumed_runner = FakeRestrictedRunner(
                    workflow=workflow,
                    workspace=root,
                    base_run_dir=root / "runs",
                    resume_dir=runner.run.run_dir,
                    policy=policy,
                )
                resumed_runner.execute()
            self.assertEqual(len(resumed_runner.commands), 1)
            resumed_command_text = "\n".join(resumed_runner.commands[0])
            self.assertIn("resume", resumed_runner.commands[0])
            self.assertIn("http://127.0.0.1:", resumed_command_text)
            self.assertNotIn(secret, resumed_command_text)
            self.assertNotIn("PRIVATE_DOCS_TOKEN", resumed_command_text)
            self.assertNotIn("PRIVATE_DOCS_TOKEN", resumed_runner.provider_envs[0])
            team_workflow = {
                "schema": "conductor.workflow.v1",
                "name": "authenticated-skill-mcp-team",
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
                                "description": "Inspect private documentation.",
                                "assignee": "lead",
                                "depends_on": [],
                            }
                        ],
                    }
                ],
            }
            with mock.patch.dict("os.environ", {"PRIVATE_DOCS_TOKEN": secret}), mock.patch(
                "conductor_runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=discovered_mcp_skill(root, skill, name=name, url=url),
            ), mock.patch(
                "conductor_runtime.runner.restricted_hook_python",
                return_value="/usr/bin/python3",
            ), mock.patch(
                "conductor_runtime.runner.prepare_restricted_hook_state",
                return_value=preflight,
            ):
                team_runner = FakeRestrictedRunner(
                    workflow=team_workflow,
                    workspace=root,
                    base_run_dir=root / "team-runs",
                    policy=RuntimePolicy(
                        allow_agent=True,
                        allow_network=True,
                        allow_parallel=True,
                        approvals={
                            "skill-mcp:%s" % name,
                            "skill-mcp-auth:%s" % name,
                        },
                    ),
                )
                team_runner.execute()
            self.assertEqual(len(team_runner.commands), 1)
            team_command_text = "\n".join(team_runner.commands[0])
            self.assertIn("http://127.0.0.1:", team_command_text)
            self.assertNotIn(secret, team_command_text)
            self.assertNotIn("PRIVATE_DOCS_TOKEN", team_command_text)
            self.assertNotIn("--ephemeral", team_runner.commands[0])
            self.assertNotIn("PRIVATE_DOCS_TOKEN", team_runner.provider_envs[0])
            self.assertEqual(team_runner.run.read_state()["status"], "completed")

    def test_skill_mcp_rejects_unselected_unsafe_or_mismatched_authority(self):
        skill = ".agents/skills/docs"
        valid = {
            "skill": skill,
            "name": "docs",
            "url": "https://example.com/mcp",
            "tools": ["search"],
        }
        invalid = [
            (dict(valid, skill=".agents/skills/other"), "already selected"),
            (dict(valid, name="bad.name"), "safe MCP server"),
            (dict(valid, url="http://example.com/mcp"), "HTTPS URL without embedded"),
            (dict(valid, url="https://user:pass@example.com/mcp"), "HTTPS URL without embedded"),
            (dict(valid, url="https://example.com/mcp?token=x"), "HTTPS URL without embedded"),
            (dict(valid, tools=[]), "non-empty array"),
            (dict(valid, tools=["bad/tool"]), "safe MCP tool"),
        ]
        for dependency, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(ValidationError, message):
                validate_agent_profile(
                    restricted_profile(
                        skills=[skill],
                        skill_mcp_dependencies=[dependency],
                    )
                )
        with self.assertRaisesRegex(ValidationError, "at most 8"):
            validate_agent_profile(
                restricted_profile(
                    skills=[skill],
                    skill_mcp_dependencies=[
                        dict(valid, name="docs%d" % index)
                        for index in range(MAX_AGENT_PROFILE_SKILL_MCP_DEPENDENCIES + 1)
                    ],
                )
            )
        with self.assertRaisesRegex(ValidationError, "at most 32"):
            validate_agent_profile(
                restricted_profile(
                    skills=[skill],
                    skill_mcp_dependencies=[
                        dict(
                            valid,
                            tools=["tool%d" % index for index in range(MAX_AGENT_PROFILE_SKILL_MCP_TOOLS + 1)],
                        )
                    ],
                )
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_mcp_skill(root, skill, name="docs", url=valid["url"])
            profile = restricted_profile(skills=[skill], skill_mcp_dependencies=[valid])
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "bad-mcp-binding",
                "mode": "read_only",
                "agent_profiles": [profile],
                "steps": [{"id": "x", "kind": "codex_exec", "agent_profile": profile["name"], "prompt": "x", "capture": "x.md"}],
            }
            for metadata, message in [
                (discovered_mcp_skill(root, skill, name="docs", url="https://other.example/mcp"), "exactly match"),
                (discovered_mcp_skill(root, skill, name="docs", url=valid["url"], command="npx server"), "exactly match"),
            ]:
                with self.subTest(message=message), mock.patch(
                    "conductor_runtime.agent_profiles.discover_codex_skill_metadata",
                    return_value=metadata,
                ), self.assertRaisesRegex(ValidationError, message):
                    WorkflowRunner(
                        workflow=workflow,
                        workspace=root,
                        base_run_dir=root / ("runs-" + str(len(message))),
                        policy=RuntimePolicy(allow_agent=True),
                    )

    def test_profile_without_skill_mcp_keeps_v1_and_does_not_discover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = ".agents/skills/boundary-review"
            write_routed_skill(root, skill)
            profile = restricted_profile(skills=[skill])
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "no-mcp-overhead",
                "mode": "read_only",
                "agent_profiles": [profile],
                "steps": [{"id": "x", "kind": "codex_exec", "agent_profile": profile["name"], "prompt": "x", "capture": "x.md"}],
            }
            with mock.patch(
                "conductor_runtime.agent_profiles.discover_codex_skill_metadata"
            ) as discover:
                runner = WorkflowRunner(
                    workflow=workflow,
                    workspace=root,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                )
            discover.assert_not_called()
            effective = effective_agent_step(runner.workflow, runner.workflow["steps"][0])
            self.assertEqual(effective["_agent_command_policy"]["schema"], AGENT_COMMAND_POLICY_SCHEMA)
            self.assertNotIn("tool_allowlist", effective["_agent_command_policy"])

    def test_selected_skill_script_routes_are_bound_and_enforced_by_existing_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = ".agents/skills/boundary-review"
            script = "scripts/verify-boundary.sh"
            write_routed_skill(root, skill)
            profile = restricted_profile(
                command_allowlist=[],
                skills=[skill],
                skill_scripts=[
                    {"skill": skill, "script": script, "args": ["--check"]},
                    {"skill": skill, "script": script, "args_prefix": ["--scan"]},
                ],
            )
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "skill-script-routing",
                "mode": "read_only",
                "agent_profiles": [profile],
                "steps": [
                    {
                        "id": "review",
                        "kind": "codex_exec",
                        "agent_profile": profile["name"],
                        "prompt": "Verify the boundary.",
                        "capture": "review.md",
                    }
                ],
            }
            runner = WorkflowRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            effective = effective_agent_step(runner.workflow, runner.workflow["steps"][0])
            command = skill + "/" + script
            self.assertEqual(effective["_agent_profile_skill_script_count"], 2)
            self.assertEqual(
                effective["_agent_command_policy"]["command_allowlist"],
                [
                    {"argv": [command, "--check"]},
                    {"argv_prefix": [command, "--scan"]},
                ],
            )
            policy = effective["_agent_command_policy"]
            self.assertTrue(
                evaluate_pre_tool_use(policy, hook_event("Bash", command + " --check"))[0]
            )
            self.assertTrue(
                evaluate_pre_tool_use(
                    policy,
                    hook_event("Bash", command + " --scan src/module.py"),
                )[0]
            )
            for denied in [
                command + " --check extra",
                command + " --other",
                skill + "/scripts/other.sh --check",
                command + " --check; whoami",
            ]:
                with self.subTest(denied=denied):
                    self.assertFalse(
                        evaluate_pre_tool_use(policy, hook_event("Bash", denied))[0]
                    )
            prompt = runner._agent_prompt(effective, "Verify the boundary.")
            self.assertIn("Selected Skill script routes: 2", prompt)
            self.assertIn(json.dumps([command, "--check"]), prompt)
            self.assertIn("no other Skill resource becomes executable", prompt)
            summary = agent_profile_summary(profile)
            self.assertEqual(summary["skill_script_rule_count"], 2)
            self.assertEqual(summary["command_rule_count"], 2)

            changed_profile = json.loads(json.dumps(profile))
            changed_profile["skill_scripts"][0]["args"] = ["--changed"]
            changed_workflow = json.loads(json.dumps(workflow))
            changed_workflow["agent_profiles"] = [changed_profile]
            changed_runner = WorkflowRunner(
                workflow=changed_workflow,
                workspace=root,
                base_run_dir=root / "changed-runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            changed_effective = effective_agent_step(
                changed_runner.workflow,
                changed_runner.workflow["steps"][0],
            )
            self.assertNotEqual(
                effective["_agent_command_policy_sha256"],
                changed_effective["_agent_command_policy_sha256"],
            )

    def test_skill_script_routes_reject_invalid_authority_and_resources(self):
        skill = ".agents/skills/boundary-review"
        script = "scripts/verify-boundary.sh"
        valid = {"skill": skill, "script": script, "args": []}
        invalid_profiles = [
            (
                restricted_profile(skills=[skill], skill_scripts=[dict(valid, args_prefix=[])]),
                "exactly one",
            ),
            (
                restricted_profile(skills=[skill], skill_scripts=[dict(valid, script="references/check.md")]),
                "scripts/ directory",
            ),
            (
                restricted_profile(skills=[skill], skill_scripts=[dict(valid, skill=".agents/skills/other")]),
                "already selected",
            ),
            (
                restricted_profile(
                    skills=[skill],
                    skill_scripts=[valid] * (MAX_AGENT_PROFILE_SKILL_SCRIPT_RULES + 1),
                ),
                "at most 32",
            ),
            (
                restricted_profile(
                    command_allowlist=[{"argv": ["tool-%d" % index]} for index in range(64)],
                    skills=[skill],
                    skill_scripts=[valid],
                ),
                "at most 64",
            ),
        ]
        for profile, message in invalid_profiles:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValidationError, message
            ):
                validate_agent_profile(profile)

        def expect_resource_failure(script_text: str, executable: bool, message: str):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_routed_skill(
                    root,
                    skill,
                    script_text=script_text,
                    executable=executable,
                )
                profile = restricted_profile(
                    command_allowlist=[],
                    skills=[skill],
                    skill_scripts=[valid],
                )
                workflow = {
                    "schema": "conductor.workflow.v1",
                    "name": "invalid-skill-script",
                    "mode": "read_only",
                    "agent_profiles": [profile],
                    "steps": [
                        {
                            "id": "review",
                            "kind": "codex_exec",
                            "agent_profile": profile["name"],
                            "prompt": "Review.",
                            "capture": "review.md",
                        }
                    ],
                }
                with self.assertRaisesRegex(ValidationError, message):
                    WorkflowRunner(
                        workflow=workflow,
                        workspace=root,
                        base_run_dir=root / "runs",
                        policy=RuntimePolicy(allow_agent=True),
                    )

        expect_resource_failure("", True, "non-empty")
        expect_resource_failure("#!/bin/sh\n", False, "executable mode")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / skill
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("safe\n", encoding="utf-8")
            profile = restricted_profile(
                command_allowlist=[],
                skills=[skill],
                skill_scripts=[valid],
            )
            workflow = {
                "schema": "conductor.workflow.v1",
                "name": "missing-skill-script",
                "mode": "read_only",
                "agent_profiles": [profile],
                "steps": [
                    {
                        "id": "review",
                        "kind": "codex_exec",
                        "agent_profile": profile["name"],
                        "prompt": "Review.",
                        "capture": "review.md",
                    }
                ],
            }
            with self.assertRaisesRegex(ValidationError, "not a bound selected Skill resource"):
                WorkflowRunner(
                    workflow=workflow,
                    workspace=root,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                )

    def test_restricted_codex_version_range_includes_verified_0144_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            executable = root / "codex"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)

            def probe(version):
                return subprocess.CompletedProcess(
                    [str(executable), "--version"],
                    0,
                    stdout="codex-cli %s\n" % version,
                    stderr="",
                )

            for version in ["0.143.0", "0.144.0", "0.144.1", "0.144.999"]:
                with self.subTest(version=version), mock.patch(
                    "conductor_runtime.codex_hook_preflight.shutil.which",
                    return_value=str(executable),
                ), mock.patch(
                    "conductor_runtime.codex_hook_preflight.subprocess.run",
                    return_value=probe(version),
                ):
                    path, observed = restricted_codex_executable(workspace)
                self.assertEqual(path, str(executable.resolve()))
                self.assertEqual(observed, "codex-cli %s" % version)

            for version in ["0.142.999", "0.145.0"]:
                with self.subTest(version=version), mock.patch(
                    "conductor_runtime.codex_hook_preflight.shutil.which",
                    return_value=str(executable),
                ), mock.patch(
                    "conductor_runtime.codex_hook_preflight.subprocess.run",
                    return_value=probe(version),
                ), self.assertRaisesRegex(
                    ValidationError,
                    r"Codex CLI >= 0\.143\.0 and < 0\.145\.0",
                ):
                    restricted_codex_executable(workspace)

    def test_profile_policy_is_strict_bounded_and_summarized(self):
        profile = restricted_profile()
        validate_agent_profile(profile)
        summary = agent_profile_summary(profile)
        self.assertEqual(summary["tool_policy"], "restricted")
        self.assertEqual(summary["command_rule_count"], 2)
        self.assertFalse(summary["allow_apply_patch"])
        self.assertRegex(summary["command_policy_sha256"], r"^[0-9a-f]{64}$")

        for changed, message in [
            ({"command_allowlist": [{"argv": []}]}, "non-empty"),
            ({"command_allowlist": [{"argv": ["rg"], "argv_prefix": ["rg"]}]}, "exactly one"),
            ({"command_allowlist": [{"argv": ["rg"]}, {"argv": ["rg"]}]}, "duplicate"),
            ({"allow_apply_patch": "yes"}, "boolean"),
        ]:
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValidationError, message):
                    validate_agent_profile(restricted_profile(**changed))

        isolated = restricted_profile(tool_policy="isolated")
        with self.assertRaisesRegex(ValidationError, "require tool_policy restricted"):
            validate_agent_profile(isolated)

    def test_simple_command_parser_rejects_shell_expansion_and_compounds(self):
        self.assertEqual(parse_simple_shell_command("rg 'a;b' src"), ["rg", "a;b", "src"])
        self.assertEqual(parse_simple_shell_command(r"rg a\;b src"), ["rg", "a;b", "src"])
        self.assertEqual(parse_simple_shell_command("git status --short"), ["git", "status", "--short"])
        for command in [
            "rg x .; touch pwned",
            "rg x . && echo pwned",
            "rg $(cat secret) .",
            'rg "$HOME" .',
            "rg x *.py",
            "FOO=bar rg x .",
            "rg x .\nwhoami",
            "rg x . > out",
            "rg x . # comment",
            "rg 'unterminated",
        ]:
            with self.subTest(command=command):
                with self.assertRaises(ValidationError):
                    parse_simple_shell_command(command)

    def test_policy_evaluator_matches_exact_prefix_patch_and_unknown_tools(self):
        policy = {
            "schema": AGENT_COMMAND_POLICY_SCHEMA,
            "command_allowlist": [
                {"argv": ["git", "status", "--short"]},
                {"argv_prefix": ["rg"]},
            ],
            "allow_apply_patch": False,
        }
        self.assertTrue(evaluate_pre_tool_use(policy, hook_event("Bash", "git status --short"))[0])
        self.assertTrue(evaluate_pre_tool_use(policy, hook_event("Bash", "rg TODO src"))[0])
        self.assertFalse(evaluate_pre_tool_use(policy, hook_event("Bash", "git status"))[0])
        self.assertFalse(evaluate_pre_tool_use(policy, hook_event("Bash", "rg x .; whoami"))[0])
        self.assertFalse(evaluate_pre_tool_use(policy, hook_event("apply_patch", "*** Begin Patch"))[0])
        self.assertFalse(evaluate_pre_tool_use(policy, hook_event("mcp__fs__read", "ignored"))[0])

        allowed_patch = dict(policy, allow_apply_patch=True)
        self.assertTrue(
            evaluate_pre_tool_use(allowed_patch, hook_event("apply_patch", "*** Begin Patch"))[0]
        )

    def test_embedded_hook_program_fails_closed_and_matches_policy(self):
        policy = {
            "schema": AGENT_COMMAND_POLICY_SCHEMA,
            "command_allowlist": [{"argv": ["pwd"]}, {"argv_prefix": ["rg"]}],
            "allow_apply_patch": False,
        }
        command = shlex.split(build_pre_tool_hook_command(policy, "/usr/bin/python3"))
        cases = [
            (hook_event("Bash", "pwd"), None),
            (hook_event("Bash", "rg TODO ."), None),
            (hook_event("Bash", "pwd; whoami"), "deny"),
            (hook_event("Bash", "echo nope"), "deny"),
            (hook_event("apply_patch", "patch"), "deny"),
            ({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}}, "deny"),
        ]
        for event, expected in cases:
            with self.subTest(event=event):
                result = subprocess.run(
                    command,
                    input=json.dumps(event),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, 0)
                payload = json.loads(result.stdout)
                decision = payload.get("hookSpecificOutput", {}).get("permissionDecision")
                self.assertEqual(decision, expected)
        mcp_policy = effective_command_policy(
            {"command_allowlist": [], "allow_apply_patch": False},
            additional_tools=["mcp__docs__search"],
        )
        mcp_command = shlex.split(
            build_pre_tool_hook_command(mcp_policy, "/usr/bin/python3")
        )
        for tool, expected in [
            ("mcp__docs__search", None),
            ("mcp__docs__delete", "deny"),
            ("mcp__other__search", "deny"),
        ]:
            with self.subTest(tool=tool):
                result = subprocess.run(
                    mcp_command,
                    input=json.dumps(hook_event(tool, "ignored")),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=5,
                    check=False,
                )
                payload = json.loads(result.stdout)
                self.assertEqual(
                    payload.get("hookSpecificOutput", {}).get("permissionDecision"),
                    expected,
                )

    def test_two_pass_preflight_trusts_only_own_hook_and_disables_external(self):
        own = {
            "key": SESSION_HOOK_KEY,
            "eventName": "preToolUse",
            "handlerType": "command",
            "matcher": ".*",
            "command": "/usr/bin/true",
            "timeoutSec": 5,
            "statusMessage": "Enforcing Conductor command policy",
            "source": "sessionFlags",
            "isManaged": False,
            "currentHash": "sha256:" + "a" * 64,
            "enabled": True,
            "trustStatus": "untrusted",
        }
        external = {
            "key": "/tmp/hooks.json:pre_tool_use:0:0",
            "isManaged": False,
            "enabled": True,
        }
        managed = {
            "key": "/etc/codex/requirements.toml:pre_tool_use:0:0",
            "isManaged": True,
            "enabled": True,
        }
        verified_own = dict(own, trustStatus="trusted")
        verified_external = dict(external, enabled=False)
        with mock.patch(
            "conductor_runtime.codex_hook_preflight.restricted_codex_executable",
            return_value=("/usr/bin/true", "codex-cli 0.143.0"),
        ), mock.patch(
            "conductor_runtime.codex_hook_preflight._discover_hooks",
            side_effect=[[own, external, managed], [verified_own, verified_external, managed]],
        ) as discover:
            result = prepare_restricted_hook_state(
                workspace=Path("/tmp"),
                hook_command="/usr/bin/true",
                discovery_args=["--enable", "hooks"],
            )
        self.assertEqual(discover.call_count, 2)
        self.assertEqual(result["disabled_unmanaged_hooks"], 1)
        self.assertEqual(result["managed_hooks"], 1)
        self.assertIn("trusted_hash", result["hook_state_config"])
        self.assertIn("enabled=false", result["hook_state_config"])

        with self.assertRaisesRegex(ValidationError, "too large"):
            hooks_state_config_arg(
                {
                    "/tmp/%03d/%s" % (index, "x" * 4080): {"enabled": False}
                    for index in range(40)
                }
            )

    def test_runner_builds_restricted_command_without_global_trust_bypass(self):
        profile = restricted_profile(command_allowlist=[{"argv": ["pwd"]}])
        workflow = {
            "schema": "conductor.workflow.v1",
            "name": "restricted",
            "mode": "read_only",
            "agent_profiles": [profile],
            "steps": [
                {
                    "id": "review",
                    "kind": "codex_exec",
                    "agent_profile": profile["name"],
                    "prompt": "Inspect.",
                    "capture": "review.md",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = WorkflowRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            effective = effective_agent_step(runner.workflow, runner.workflow["steps"][0])
            preflight = {
                "codex_path": "/opt/codex",
                "codex_version": "codex-cli 0.143.0",
                "hook_hash": "sha256:" + "b" * 64,
                "hook_state_config": hooks_state_config_arg(
                    {
                        SESSION_HOOK_KEY: {
                            "enabled": True,
                            "trusted_hash": "sha256:" + "b" * 64,
                        }
                    }
                ),
                "disabled_unmanaged_hooks": 2,
                "managed_hooks": 1,
            }
            with mock.patch(
                "conductor_runtime.runner.restricted_hook_python",
                return_value="/usr/bin/python3",
            ), mock.patch(
                "conductor_runtime.runner.prepare_restricted_hook_state",
                return_value=preflight,
            ):
                prepared = runner._prepare_restricted_agent_step(effective)
            command = runner._codex_command(prepared, runner.run.artifacts_dir / "review.md")
            pairs = [command[index : index + 2] for index in range(len(command) - 1)]
            self.assertEqual(command[0], "/opt/codex")
            self.assertIn(["--disable", "unified_exec"], pairs)
            self.assertIn(["--enable", "hooks"], pairs)
            self.assertIn(["--config", "allow_login_shell=false"], pairs)
            self.assertNotIn(
                ["--config", "shell_environment_policy.allow_login_shell=false"],
                pairs,
            )
            shell_environment = next(
                value for value in command if value.startswith("shell_environment_policy.set=")
            )
            self.assertIn('HOME="/dev/null"', shell_environment)
            self.assertIn('ZDOTDIR="/dev/null"', shell_environment)
            self.assertNotIn("--dangerously-bypass-hook-trust", command)
            self.assertTrue(any(value.startswith("hooks.PreToolUse=") for value in command))
            self.assertTrue(any(value.startswith("hooks.state=") for value in command))

    def test_execution_and_fanout_preflight_once_and_publish_policy_evidence(self):
        preflight = {
            "codex_path": "/opt/codex",
            "codex_version": "codex-cli 0.143.0",
            "hook_hash": "sha256:" + "c" * 64,
            "hook_state_config": hooks_state_config_arg(
                {
                    SESSION_HOOK_KEY: {
                        "enabled": True,
                        "trusted_hash": "sha256:" + "c" * 64,
                    }
                }
            ),
            "disabled_unmanaged_hooks": 3,
            "managed_hooks": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = restricted_profile(command_allowlist=[{"argv_prefix": ["rg"]}])
            single_workflow = {
                "schema": "conductor.workflow.v1",
                "name": "restricted-single",
                "mode": "read_only",
                "agent_profiles": [profile],
                "steps": [
                    {
                        "id": "review",
                        "kind": "codex_exec",
                        "agent_profile": profile["name"],
                        "prompt": "Inspect.",
                        "capture": "review.md",
                    }
                ],
            }
            with mock.patch(
                "conductor_runtime.runner.restricted_hook_python",
                return_value="/usr/bin/python3",
            ), mock.patch(
                "conductor_runtime.runner.prepare_restricted_hook_state",
                return_value=preflight,
            ) as prepare:
                runner = FakeRestrictedRunner(
                    workflow=single_workflow,
                    workspace=root,
                    base_run_dir=root / "single-runs",
                    policy=RuntimePolicy(allow_agent=True),
                )
                run = runner.execute()
            self.assertEqual(prepare.call_count, 1)
            state = run.read_state()["steps"]["review"]
            self.assertEqual(state["agent_tool_policy"], "restricted")
            self.assertRegex(state["agent_command_policy_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(state["agent_restricted_hook_hash"], preflight["hook_hash"])
            self.assertEqual(state["agent_restricted_external_hooks_disabled"], 3)
            detail = collect_run_detail(run.run_dir.parent, run.run_dir.name)
            detail_step = detail["steps"][0]
            self.assertEqual(detail_step["agent_restricted_codex_version"], "codex-cli 0.143.0")
            self.assertEqual(detail_step["agent_restricted_managed_hooks"], 1)
            with mock.patch(
                "conductor_runtime.runner.prepare_restricted_hook_state"
            ) as resume_prepare:
                resumed = FakeRestrictedRunner(
                    workflow=single_workflow,
                    workspace=root,
                    base_run_dir=root / "single-runs",
                    policy=RuntimePolicy(allow_agent=True),
                    resume_dir=run.run_dir,
                )
                resumed.execute()
            resume_prepare.assert_not_called()
            self.assertEqual(resumed.commands, [])

            (root / "a.py").write_text("A = 1\n", encoding="utf-8")
            (root / "b.py").write_text("B = 2\n", encoding="utf-8")
            map_workflow = {
                "schema": "conductor.workflow.v1",
                "name": "restricted-map",
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
                "conductor_runtime.runner.restricted_hook_python",
                return_value="/usr/bin/python3",
            ), mock.patch(
                "conductor_runtime.runner.prepare_restricted_hook_state",
                return_value=preflight,
            ) as prepare_map:
                map_runner = FakeRestrictedRunner(
                    workflow=map_workflow,
                    workspace=root,
                    base_run_dir=root / "map-runs",
                    policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
                )
                map_run = map_runner.execute()
            self.assertEqual(prepare_map.call_count, 1)
            self.assertEqual(len(map_runner.commands), 2)
            map_state = map_run.read_state()["steps"]["review"]
            self.assertEqual(map_state["agent_restricted_hook_hash"], preflight["hook_hash"])
            traces = [
                json.loads(line)
                for line in (
                    map_run.artifacts_dir / "reviews" / ".agent-map-trace.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(traces), 2)
            self.assertTrue(
                all(trace["agent_restricted_hook_hash"] == preflight["hook_hash"] for trace in traces)
            )

    def test_skill_script_routes_publish_execution_resume_map_and_drift_evidence(self):
        preflight = {
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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = ".agents/skills/boundary-review"
            script_resource = "scripts/verify-boundary.sh"
            script_path = write_routed_skill(root, skill)
            profile = restricted_profile(
                command_allowlist=[],
                skills=[skill],
                skill_scripts=[
                    {"skill": skill, "script": script_resource, "args": []}
                ],
            )
            direct_workflow = {
                "schema": "conductor.workflow.v1",
                "name": "skill-script-direct",
                "mode": "read_only",
                "agent_profiles": [profile],
                "steps": [
                    {
                        "id": "review",
                        "kind": "codex_exec",
                        "agent_profile": profile["name"],
                        "prompt": "Run the selected verification route.",
                        "capture": "review.md",
                    }
                ],
            }
            with mock.patch(
                "conductor_runtime.runner.restricted_hook_python",
                return_value="/usr/bin/python3",
            ), mock.patch(
                "conductor_runtime.runner.prepare_restricted_hook_state",
                return_value=preflight,
            ) as prepare:
                runner = FakeRestrictedRunner(
                    workflow=direct_workflow,
                    workspace=root,
                    base_run_dir=root / "direct-runs",
                    policy=RuntimePolicy(allow_agent=True),
                )
                run = runner.execute()
            self.assertEqual(prepare.call_count, 1)
            state = run.read_state()["steps"]["review"]
            self.assertEqual(state["agent_profile_skill_script_count"], 1)
            detail = collect_run_detail(run.run_dir.parent, run.run_dir.name)
            self.assertEqual(
                detail["steps"][0]["agent_profile_skill_script_count"], 1
            )
            with mock.patch(
                "conductor_runtime.runner.prepare_restricted_hook_state"
            ) as resume_prepare:
                resumed = FakeRestrictedRunner(
                    workflow=direct_workflow,
                    workspace=root,
                    base_run_dir=root / "direct-runs",
                    policy=RuntimePolicy(allow_agent=True),
                    resume_dir=run.run_dir,
                )
                resumed.execute()
            resume_prepare.assert_not_called()
            self.assertEqual(resumed.commands, [])

            (root / "a.py").write_text("A = 1\n", encoding="utf-8")
            (root / "b.py").write_text("B = 2\n", encoding="utf-8")
            map_profile = dict(profile, max_workers=2)
            map_workflow = {
                "schema": "conductor.workflow.v1",
                "name": "skill-script-map",
                "mode": "read_only",
                "max_workers": 2,
                "agent_profiles": [map_profile],
                "steps": [
                    {
                        "id": "review",
                        "kind": "agent_map",
                        "agent_profile": profile["name"],
                        "items": ["a.py", "b.py"],
                        "prompt_template": "Verify {item}.",
                        "capture_dir": "reviews",
                        "max_workers": 2,
                    }
                ],
            }
            with mock.patch(
                "conductor_runtime.runner.restricted_hook_python",
                return_value="/usr/bin/python3",
            ), mock.patch(
                "conductor_runtime.runner.prepare_restricted_hook_state",
                return_value=preflight,
            ):
                mapped = FakeRestrictedRunner(
                    workflow=map_workflow,
                    workspace=root,
                    base_run_dir=root / "map-runs",
                    policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
                )
                map_run = mapped.execute()
            trace = [
                json.loads(line)
                for line in (
                    map_run.artifacts_dir / "reviews" / ".agent-map-trace.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(trace), 2)
            self.assertTrue(
                all(row["agent_profile_skill_script_count"] == 1 for row in trace)
            )

            team_workflow = {
                "schema": "conductor.workflow.v1",
                "name": "skill-script-team",
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
                                "instructions": "Lead verification.",
                                "agent_profile": profile["name"],
                                "lead": True,
                            },
                            {
                                "id": "peer",
                                "role": "Peer",
                                "instructions": "Peer verification.",
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
            team_runner = FakeRestrictedRunner(
                workflow=team_workflow,
                workspace=root,
                base_run_dir=root / "team-runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
            )
            team_step = team_runner.workflow["steps"][0]
            team_metrics = team_runner._agent_team_metrics(
                team_step,
                initial_agent_team_state(team_step, "0" * 64),
            )
            self.assertEqual(team_metrics["agent_team_profile_skill_profiles"], 1)
            self.assertEqual(team_metrics["agent_team_profile_skill_script_count"], 1)

            workflow_path = root / "skill-script-workflow.json"
            manifest_path = root / "skill-script-routine.json"
            workflow_path.write_text(json.dumps(direct_workflow), encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "write-routine-manifest",
                            str(manifest_path),
                            "--name",
                            "skill-script-routine",
                            "--workflow",
                            str(workflow_path),
                            "--workspace",
                            str(root),
                            "--manual",
                            "--allow-agent",
                        ]
                    ),
                    0,
                )
            routine_target = _validate_current_target(
                load_routine_manifest(manifest_path)
            )
            routine_effective = effective_agent_step(
                routine_target,
                routine_target["steps"][0],
            )
            self.assertEqual(routine_effective["_agent_profile_skill_script_count"], 1)

            drifting = FakeRestrictedRunner(
                workflow=direct_workflow,
                workspace=root,
                base_run_dir=root / "drift-runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            script_path.chmod(0o700)
            with self.assertRaisesRegex(ValidationError, "routine target changed"):
                _validate_current_target(load_routine_manifest(manifest_path))
            with self.assertRaisesRegex(ValidationError, "changed after run binding"):
                drifting.execute()
            self.assertEqual(drifting.commands, [])

    def test_skill_mcp_routes_publish_direct_resume_map_team_and_routine_evidence(self):
        preflight = {
            "codex_path": "/opt/codex",
            "codex_version": "codex-cli 0.144.1",
            "hook_hash": "sha256:" + "e" * 64,
            "hook_state_config": hooks_state_config_arg(
                {
                    SESSION_HOOK_KEY: {
                        "enabled": True,
                        "trusted_hash": "sha256:" + "e" * 64,
                    }
                }
            ),
            "disabled_unmanaged_hooks": 0,
            "managed_hooks": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = ".agents/skills/docs"
            name = "docs"
            url = "https://example.com/mcp"
            write_mcp_skill(root, skill, name=name, url=url)
            profile = restricted_profile(
                command_allowlist=[],
                skills=[skill],
                skill_mcp_dependencies=[
                    {
                        "skill": skill,
                        "name": name,
                        "url": url,
                        "tools": ["search", "fetch"],
                    }
                ],
            )
            direct_workflow = {
                "schema": "conductor.workflow.v1",
                "name": "skill-mcp-direct",
                "mode": "read_only",
                "agent_profiles": [profile],
                "steps": [
                    {
                        "id": "review",
                        "kind": "codex_exec",
                        "agent_profile": profile["name"],
                        "prompt": "Use the selected documentation dependency.",
                        "capture": "review.md",
                    }
                ],
            }
            runtime_policy = RuntimePolicy(
                allow_agent=True,
                allow_network=True,
                approvals={"skill-mcp:docs"},
            )
            discovery = discovered_mcp_skill(root, skill, name=name, url=url)
            with mock.patch(
                "conductor_runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=discovery,
            ), mock.patch(
                "conductor_runtime.runner.restricted_hook_python",
                return_value="/usr/bin/python3",
            ), mock.patch(
                "conductor_runtime.runner.prepare_restricted_hook_state",
                return_value=preflight,
            ):
                runner = FakeRestrictedRunner(
                    workflow=direct_workflow,
                    workspace=root,
                    base_run_dir=root / "direct-runs",
                    policy=runtime_policy,
                )
                run = runner.execute()
            state = run.read_state()["steps"]["review"]
            self.assertEqual(state["agent_profile_skill_mcp_dependency_count"], 1)
            self.assertEqual(state["agent_profile_skill_mcp_tool_count"], 2)
            self.assertRegex(state["agent_profile_skill_mcp_sha256"], r"^[0-9a-f]{64}$")
            detail = collect_run_detail(run.run_dir.parent, run.run_dir.name)
            self.assertEqual(
                detail["steps"][0]["agent_profile_skill_mcp_dependency_count"],
                1,
            )
            with mock.patch(
                "conductor_runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=discovery,
            ) as resume_discover, mock.patch(
                "conductor_runtime.runner.prepare_restricted_hook_state"
            ) as resume_prepare:
                resumed = FakeRestrictedRunner(
                    workflow=direct_workflow,
                    workspace=root,
                    base_run_dir=root / "direct-runs",
                    policy=runtime_policy,
                    resume_dir=run.run_dir,
                )
                resumed.execute()
            resume_discover.assert_called_once()
            resume_prepare.assert_not_called()
            self.assertEqual(resumed.commands, [])

            (root / "a.py").write_text("A = 1\n", encoding="utf-8")
            (root / "b.py").write_text("B = 2\n", encoding="utf-8")
            map_workflow = {
                "schema": "conductor.workflow.v1",
                "name": "skill-mcp-map",
                "mode": "read_only",
                "max_workers": 2,
                "agent_profiles": [dict(profile, max_workers=2)],
                "steps": [
                    {
                        "id": "review",
                        "kind": "agent_map",
                        "agent_profile": profile["name"],
                        "items": ["a.py", "b.py"],
                        "prompt_template": "Search docs for {item}.",
                        "capture_dir": "reviews",
                        "max_workers": 2,
                    }
                ],
            }
            with mock.patch(
                "conductor_runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=discovery,
            ), mock.patch(
                "conductor_runtime.runner.restricted_hook_python",
                return_value="/usr/bin/python3",
            ), mock.patch(
                "conductor_runtime.runner.prepare_restricted_hook_state",
                return_value=preflight,
            ):
                mapped = FakeRestrictedRunner(
                    workflow=map_workflow,
                    workspace=root,
                    base_run_dir=root / "map-runs",
                    policy=RuntimePolicy(
                        allow_agent=True,
                        allow_network=True,
                        allow_parallel=True,
                        approvals={"skill-mcp:docs"},
                    ),
                )
                map_run = mapped.execute()
            trace = [
                json.loads(line)
                for line in (
                    map_run.artifacts_dir / "reviews" / ".agent-map-trace.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(trace), 2)
            self.assertTrue(
                all(row["agent_profile_skill_mcp_tool_count"] == 2 for row in trace)
            )

            team_workflow = {
                "schema": "conductor.workflow.v1",
                "name": "skill-mcp-team",
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
                                "instructions": "Lead documentation review.",
                                "agent_profile": profile["name"],
                                "lead": True,
                            },
                            {
                                "id": "peer",
                                "role": "Peer",
                                "instructions": "Peer documentation review.",
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
                "conductor_runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=discovery,
            ):
                team_runner = FakeRestrictedRunner(
                    workflow=team_workflow,
                    workspace=root,
                    base_run_dir=root / "team-runs",
                    policy=RuntimePolicy(
                        allow_agent=True,
                        allow_network=True,
                        allow_parallel=True,
                        approvals={"skill-mcp:docs"},
                    ),
                )
            team_step = team_runner.workflow["steps"][0]
            team_metrics = team_runner._agent_team_metrics(
                team_step,
                initial_agent_team_state(team_step, "0" * 64),
            )
            self.assertEqual(
                team_metrics["agent_team_profile_skill_mcp_dependency_count"], 1
            )
            self.assertEqual(team_metrics["agent_team_profile_skill_mcp_tool_count"], 2)

            workflow_path = root / "skill-mcp-workflow.json"
            manifest_path = root / "skill-mcp-routine.json"
            workflow_path.write_text(json.dumps(direct_workflow), encoding="utf-8")
            with mock.patch(
                "conductor_runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=discovery,
            ), redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "write-routine-manifest",
                            str(manifest_path),
                            "--name",
                            "skill-mcp-routine",
                            "--workflow",
                            str(workflow_path),
                            "--workspace",
                            str(root),
                            "--manual",
                            "--allow-agent",
                            "--allow-network",
                            "--approve",
                            "skill-mcp:docs",
                        ]
                    ),
                    0,
                )
            with mock.patch(
                "conductor_runtime.agent_profiles.discover_codex_skill_metadata",
                return_value=discovery,
            ):
                routine_target = _validate_current_target(
                    load_routine_manifest(manifest_path)
                )
            routine_effective = effective_agent_step(
                routine_target,
                routine_target["steps"][0],
            )
            self.assertEqual(
                routine_effective["_agent_profile_skill_mcp_dependency_count"], 1
            )
            self.assertEqual(
                load_routine_manifest(manifest_path)["launch"]["policy"]["approval_count"],
                1,
            )

    def test_schema_exposes_structured_restricted_rules(self):
        schema = get_schema("agent-profile")
        self.assertEqual(
            schema["properties"]["tool_policy"]["enum"],
            ["isolated", "restricted", "standard"],
        )
        rules = schema["properties"]["command_allowlist"]
        self.assertEqual(rules["maxItems"], 64)
        self.assertIn("allow_apply_patch", schema["properties"])
        skill_scripts = schema["properties"]["skill_scripts"]
        self.assertEqual(skill_scripts["maxItems"], 32)
        self.assertEqual(len(skill_scripts["items"]["oneOf"]), 2)
        self.assertEqual(
            skill_scripts["items"]["oneOf"][0]["required"],
            ["skill", "script", "args"],
        )
        skill_mcp = schema["properties"]["skill_mcp_dependencies"]
        self.assertEqual(skill_mcp["maxItems"], 8)
        self.assertEqual(len(skill_mcp["items"]["oneOf"]), 2)
        http_mcp, stdio_mcp = skill_mcp["items"]["oneOf"]
        self.assertEqual(
            http_mcp["required"],
            ["skill", "name", "url", "tools"],
        )
        self.assertEqual(
            stdio_mcp["required"],
            ["skill", "name", "transport", "script", "args", "tools"],
        )
        auth = http_mcp["properties"]["auth"]
        self.assertEqual(auth["minProperties"], 1)
        self.assertEqual(auth["properties"]["env_http_headers"]["maxProperties"], 16)

    def test_profile_cli_writes_structured_command_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "restricted.json"
            with redirect_stdout(StringIO()):
                code = cli_main(
                    [
                        "write-agent-profile",
                        str(output),
                        "--name",
                        "bounded",
                        "--instructions",
                        "Inspect only.",
                        "--tool-policy",
                        "restricted",
                        "--allow-command-json",
                        '["git","status","--short"]',
                        "--allow-command-prefix-json",
                        '["rg"]',
                        "--allow-apply-patch",
                        "--skill",
                        ".agents/skills/boundary-review",
                        "--allow-skill-script-json",
                        '{"skill":".agents/skills/boundary-review",'
                        '"script":"scripts/verify-boundary.sh","args":[]}',
                        "--allow-skill-mcp-json",
                        '{"skill":".agents/skills/boundary-review",'
                        '"name":"docs","url":"https://example.com/mcp",'
                        '"tools":["search"]}',
                    ]
                )
            self.assertEqual(code, 0)
            profile = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                profile["command_allowlist"],
                [
                    {"argv": ["git", "status", "--short"]},
                    {"argv_prefix": ["rg"]},
                ],
            )
            self.assertTrue(profile["allow_apply_patch"])
            self.assertEqual(
                profile["skill_scripts"],
                [
                    {
                        "skill": ".agents/skills/boundary-review",
                        "script": "scripts/verify-boundary.sh",
                        "args": [],
                    }
                ],
            )
            self.assertEqual(profile["skill_mcp_dependencies"][0]["tools"], ["search"])

            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(cli_main(["inspect-agent-profile", str(output)]), 0)
            self.assertIn("Codex CLI range: >=0.143.0,<0.145.0", stdout.getvalue())
            self.assertIn("Skill script routes: 1", stdout.getvalue())
            self.assertIn("Skill MCP dependencies: 1", stdout.getvalue())
            self.assertIn("Skill MCP tools: 1", stdout.getvalue())

    def test_policy_digest_is_order_and_semantics_sensitive(self):
        base = {
            "schema": AGENT_COMMAND_POLICY_SCHEMA,
            "command_allowlist": [{"argv": ["pwd"]}],
            "allow_apply_patch": False,
        }
        self.assertEqual(command_policy_sha256(base), command_policy_sha256(json.loads(json.dumps(base))))
        self.assertNotEqual(
            command_policy_sha256(base),
            command_policy_sha256(dict(base, allow_apply_patch=True)),
        )


if __name__ == "__main__":
    unittest.main()
