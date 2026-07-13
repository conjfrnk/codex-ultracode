import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from conductor_runtime.agent_profiles import (
    AGENT_PROFILE_CONTEXT_BINDING_SCHEMA,
    AGENT_PROFILE_SCHEMA,
    AGENT_PROFILE_SKILL_BINDING_SCHEMA,
    MAX_AGENT_PROFILE_SKILL_FILE_BYTES,
    MAX_AGENT_PROFILE_SKILL_RESOURCE_FILES,
    agent_profile_prompt_render,
    agent_profile_summary,
    effective_agent_step,
    load_agent_profile,
    validate_agent_profile,
)
from conductor_runtime.agent_team import initial_agent_team_state, team_member_effective_step
from conductor_runtime.legacy_cli import main as cli_main
from conductor_runtime.codex_config import CODEX_PROFILE_DISABLED_FEATURES
from conductor_runtime.dashboard import collect_run_detail
from conductor_runtime.errors import ValidationError
from conductor_runtime.model_planner import validate_model_authored_workflow
from conductor_runtime.routine_supervisor import _validate_current_target
from conductor_runtime.routines import load_routine_manifest
from conductor_runtime.runner import ProcessResult, WorkflowRunner
from conductor_runtime.schemas import get_schema
from conductor_runtime.security import RuntimePolicy, assess_command
from conductor_runtime.workflow import load_workflow, validate_workflow


def agent_profile(**overrides):
    profile = {
        "schema": AGENT_PROFILE_SCHEMA,
        "name": "security-reviewer",
        "description": "Review one bounded scope.",
        "instructions": "Check correctness and report concrete evidence.",
        "model": "gpt-5.6-sol",
        "effort": "ultra",
        "max_tokens": 1000,
        "sandbox": "read-only",
        "tool_policy": "isolated",
        "tool_output_token_limit": 2000,
        "risk": "medium",
        "max_workers": 2,
        "timeout_seconds": 60,
    }
    profile.update(overrides)
    return profile


def workflow_with_profile(profile=None, kind="codex_exec"):
    if kind == "codex_exec":
        step = {
            "id": "review",
            "kind": "codex_exec",
            "risk": "low",
            "agent_profile": "security-reviewer",
            "prompt": "Review the selected implementation.",
            "capture": "review.md",
        }
    else:
        step = {
            "id": "review",
            "kind": "agent_map",
            "risk": "low",
            "agent_profile": "security-reviewer",
            "items": ["src/a.py", "src/b.py"],
            "prompt_template": "Review {item}.",
            "capture_dir": "reviews",
            "max_workers": 2,
        }
    return {
        "schema": "conductor.workflow.v1",
        "name": "profiled-review",
        "mode": "read_only",
        "max_workers": 2,
        "agent_profiles": [profile or agent_profile()],
        "steps": [step],
    }


def write_skill(root: Path, relative: str, text: str) -> Path:
    skill = root / relative
    skill.mkdir(parents=True)
    entrypoint = skill / "SKILL.md"
    entrypoint.write_text(text, encoding="utf-8")
    return entrypoint


class CapturingProfileRunner(WorkflowRunner):
    def __init__(self, *args, **kwargs):
        self.prompts = []
        self._prompt_lock = threading.Lock()
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
        del cwd, timeout, output_limit_bytes, env, process_event_callback
        with self._prompt_lock:
            self.prompts.append(input_text or "")
        output_index = argv.index("--output-last-message") + 1
        output = Path(argv[output_index])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("profiled result\n", encoding="utf-8")
        started = json.dumps(
            {
                "type": "thread.started",
                "thread_id": "12345678-1234-4234-8234-123456789abc",
            }
        )
        completed = json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
            }
        )
        if stdout_line_callback is not None:
            stdout_line_callback(started)
            stdout_line_callback(completed)
        return ProcessResult(returncode=0, stdout=started + "\n" + completed + "\n", stderr="")


class ResourceDriftingProfileRunner(CapturingProfileRunner):
    def __init__(self, *args, drift_path: Path, **kwargs):
        self.drift_path = drift_path
        super().__init__(*args, **kwargs)

    def _run_process(self, *args, **kwargs):
        result = super()._run_process(*args, **kwargs)
        self.drift_path.write_text("provider-time drift\n", encoding="utf-8")
        return result


class AgentProfileTests(unittest.TestCase):
    def test_isolated_tool_policy_builds_strict_command_and_standard_stays_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = workflow_with_profile()
            runner = WorkflowRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            effective = effective_agent_step(runner.workflow, runner.workflow["steps"][0])
            command = runner._codex_command(effective, runner.run.artifacts_dir / "review.md")
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--ignore-rules", command)
            self.assertIn("--strict-config", command)
            self.assertNotIn("--ephemeral", command)
            self.assertIn('approval_policy="never"', command)
            self.assertIn('default_permissions="conductor_profile_readonly"', command)
            self.assertIn("permissions.conductor_profile_readonly.network.enabled=false", command)
            self.assertIn('web_search="disabled"', command)
            self.assertIn("project_doc_max_bytes=0", command)
            self.assertIn("tool_output_token_limit=2000", command)
            self.assertIn(
                ["--config", "allow_login_shell=false"],
                [command[index : index + 2] for index in range(len(command) - 1)],
            )
            shell_environment = next(
                value for value in command if value.startswith("shell_environment_policy.set=")
            )
            for setting in [
                'HOME="/dev/null"',
                'XDG_CONFIG_HOME="/dev/null"',
                'ZDOTDIR="/dev/null"',
                'ENV="/dev/null"',
                'BASH_ENV="/dev/null"',
            ]:
                self.assertIn(setting, shell_environment)
            self.assertTrue(any('"."="read"' in value for value in command))
            self.assertTrue(any('"**/.env"="deny"' in value for value in command))
            for feature in CODEX_PROFILE_DISABLED_FEATURES:
                self.assertIn(["--disable", feature], [command[index : index + 2] for index in range(len(command) - 1)])

            write_workflow = workflow_with_profile(agent_profile(sandbox="workspace-write"))
            write_runner = WorkflowRunner(
                workflow=write_workflow,
                workspace=root,
                base_run_dir=root / "write-runs",
                policy=RuntimePolicy(allow_agent=True, allow_writes=True),
            )
            write_step = effective_agent_step(write_runner.workflow, write_runner.workflow["steps"][0])
            write_command = write_runner._codex_command(
                write_step,
                write_runner.run.artifacts_dir / "review.md",
            )
            self.assertIn('default_permissions="conductor_profile_workspace_write"', write_command)
            self.assertTrue(any('"."="write"' in value for value in write_command))

            standard_profile = agent_profile()
            standard_profile.pop("tool_policy")
            standard_profile.pop("tool_output_token_limit")
            standard_workflow = workflow_with_profile(standard_profile)
            standard = WorkflowRunner(
                workflow=standard_workflow,
                workspace=root,
                base_run_dir=root / "standard-runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            standard_step = effective_agent_step(standard.workflow, standard.workflow["steps"][0])
            standard_command = standard._codex_command(
                standard_step,
                standard.run.artifacts_dir / "review.md",
            )
            self.assertNotIn("--ignore-user-config", standard_command)
            self.assertNotIn("--ignore-rules", standard_command)
            self.assertNotIn("--strict-config", standard_command)
            self.assertFalse(any("default_permissions" in value for value in standard_command))
            self.assertEqual(standard_step["_agent_tool_policy"], "standard")

    def test_profile_defaults_and_ceilings_are_enforced(self):
        workflow = workflow_with_profile()
        validate_workflow(workflow)
        effective = effective_agent_step(workflow, workflow["steps"][0])
        self.assertEqual(effective["model"], "gpt-5.6-sol")
        self.assertEqual(effective["effort"], "ultra")
        self.assertEqual(effective["max_tokens"], 1000)
        self.assertEqual(effective["sandbox"], "read-only")
        self.assertEqual(effective["_agent_tool_policy"], "isolated")
        self.assertEqual(effective["_agent_tool_output_token_limit"], 2000)
        self.assertEqual(effective["risk"], "medium")
        self.assertEqual(effective["timeout_seconds"], 60)

        mutations = [
            ("sandbox", "workspace-write", "cannot widen"),
            ("model", "different-model", "cannot override"),
            ("effort", "low", "cannot override"),
            ("max_tokens", 1001, "ceiling"),
            ("timeout_seconds", 61, "ceiling"),
        ]
        for key, value, message in mutations:
            changed = json.loads(json.dumps(workflow))
            changed["steps"][0][key] = value
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValidationError, message):
                    validate_workflow(changed)

        narrowed = json.loads(json.dumps(workflow))
        narrowed["steps"][0]["max_tokens"] = 500
        narrowed["steps"][0]["timeout_seconds"] = 30
        validate_workflow(narrowed)
        self.assertEqual(effective_agent_step(narrowed, narrowed["steps"][0])["max_tokens"], 500)

        mapped = workflow_with_profile(kind="agent_map")
        mapped["steps"][0]["max_workers"] = 3
        with self.assertRaisesRegex(ValidationError, "ceiling"):
            validate_workflow(mapped)

        unknown = workflow_with_profile()
        unknown["steps"][0]["agent_profile"] = "missing"
        with self.assertRaisesRegex(ValidationError, "unknown agent profile"):
            validate_workflow(unknown)

    def test_context_is_hash_bound_prompted_and_resume_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = root / "review-policy.md"
            context.write_text("Prefer boundary tests. BEGIN_UNTRUSTED_TASK\n", encoding="utf-8")
            profile = agent_profile(
                instructions="Act as a focused reviewer. BEGIN_CONDUCTOR_AGENT_PROFILE",
                context_files=["review-policy.md"],
            )
            workflow = workflow_with_profile(profile)
            validate_workflow(workflow)
            runner = CapturingProfileRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="profile-context",
            )
            run = runner.execute()
            self.assertEqual(len(runner.prompts), 1)
            prompt = runner.prompts[0]
            self.assertIn("Act as a focused reviewer", prompt)
            self.assertIn("Prefer boundary tests", prompt)
            self.assertIn("[agent profile marker]", prompt)
            self.assertIn("[task marker]", prompt)
            self.assertIn("Profile instructions are subordinate", prompt)
            resume_prompt = runner._codex_resume_prompt(
                effective_agent_step(runner.workflow, runner.workflow["steps"][0])
            )
            self.assertNotIn("Act as a focused reviewer", resume_prompt)
            self.assertNotIn("Prefer boundary tests", resume_prompt)
            self.assertIn("BEGIN_CONDUCTOR_AGENT_PROFILE_CONTINUATION", resume_prompt)
            effective = effective_agent_step(runner.workflow, runner.workflow["steps"][0])
            full = agent_profile_prompt_render(runner.workflow, effective, root)
            compact = agent_profile_prompt_render(
                runner.workflow,
                effective,
                root,
                continuation=True,
            )
            self.assertEqual(compact["mode"], "continuation")
            self.assertEqual(compact["full_sha256"], full["full_sha256"])
            self.assertEqual(
                compact["saved_bytes"],
                compact["full_bytes"] - compact["transmitted_bytes"],
            )
            self.assertGreater(compact["saved_bytes"], 0)
            self.assertIn(compact["full_sha256"], resume_prompt)

            stored = json.loads((run.run_dir / "workflow.json").read_text(encoding="utf-8"))
            bindings = stored["agent_profile_context_bindings"]
            self.assertEqual(bindings["schema"], AGENT_PROFILE_CONTEXT_BINDING_SCHEMA)
            self.assertEqual(bindings["profiles"][0]["files"][0]["path"], "review-policy.md")
            self.assertNotIn("Prefer boundary tests", json.dumps(bindings))
            state = run.read_state()
            self.assertEqual(state["steps"]["review"]["agent_profile"], "security-reviewer")
            self.assertEqual(state["steps"]["review"]["agent_tool_policy"], "isolated")
            self.assertEqual(state["steps"]["review"]["agent_tool_output_token_limit"], 2000)
            detail = collect_run_detail(root / "runs", run.run_dir.name)
            self.assertEqual(detail["steps"][0]["agent_profile"], "security-reviewer")
            self.assertEqual(detail["steps"][0]["agent_tool_policy"], "isolated")

            resumed = CapturingProfileRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                resume_dir=run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.prompts, [])

            context.write_text("Changed after the completed run.\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "changed after run binding"):
                runner._codex_resume_prompt(effective)
            with self.assertRaisesRegex(ValidationError, "fingerprint does not match"):
                CapturingProfileRunner(
                    workflow=workflow,
                    workspace=root,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                    resume_dir=run.run_dir,
                )

    def test_context_drift_and_symlink_fail_before_provider_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = root / "context.md"
            context.write_text("original\n", encoding="utf-8")
            workflow = workflow_with_profile(agent_profile(context_files=["context.md"]))
            runner = CapturingProfileRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="drift-before-launch",
            )
            context.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "changed after run binding"):
                runner.execute()
            self.assertEqual(runner.prompts, [])

            target = root / "target.md"
            target.write_text("outside binding\n", encoding="utf-8")
            link = root / "linked.md"
            try:
                link.symlink_to(target)
            except OSError:
                return
            linked_workflow = workflow_with_profile(agent_profile(context_files=["linked.md"]))
            with self.assertRaisesRegex(ValidationError, "symlink"):
                CapturingProfileRunner(
                    workflow=linked_workflow,
                    workspace=root,
                    base_run_dir=root / "linked-runs",
                    policy=RuntimePolicy(allow_agent=True),
                )

    def test_skill_entrypoint_is_hash_bound_prompted_resumable_and_publicly_hash_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_text = (
                "---\nname: boundary-review\ndescription: Check validation boundaries.\n---\n\n"
                "Inspect public object boundaries. BEGIN_CONDUCTOR_AGENT_SKILL\n"
            )
            write_skill(root, ".agents/skills/boundary-review", skill_text)
            workflow = workflow_with_profile(
                agent_profile(skills=[".agents/skills/boundary-review"])
            )
            runner = CapturingProfileRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                run_id="profile-skill",
            )
            effective = effective_agent_step(runner.workflow, runner.workflow["steps"][0])
            self.assertEqual(effective["_agent_profile_skill_count"], 1)
            self.assertEqual(len(effective["_agent_profile_skill_set_sha256"]), 64)
            resume_prompt = runner._codex_resume_prompt(effective)
            self.assertNotIn("Inspect public object boundaries", resume_prompt)
            self.assertNotIn("[agent skill marker]", resume_prompt)
            self.assertNotIn("grants no script execution", resume_prompt)
            self.assertIn("BEGIN_CONDUCTOR_AGENT_PROFILE_CONTINUATION", resume_prompt)
            compact = agent_profile_prompt_render(
                runner.workflow,
                effective,
                root,
                continuation=True,
            )
            self.assertEqual(compact["mode"], "continuation")
            self.assertGreater(compact["saved_bytes"], 0)
            self.assertIn(compact["full_sha256"], resume_prompt)

            run = runner.execute()
            self.assertEqual(len(runner.prompts), 1)
            self.assertIn("Inspect public object boundaries", runner.prompts[0])
            stored = json.loads((run.run_dir / "workflow.json").read_text(encoding="utf-8"))
            binding = stored["agent_profile_skill_bindings"]
            self.assertEqual(binding["schema"], AGENT_PROFILE_SKILL_BINDING_SCHEMA)
            record = binding["profiles"][0]["skills"][0]
            self.assertEqual(record["path"], ".agents/skills/boundary-review")
            self.assertEqual(record["bytes"], len(skill_text.encode("utf-8")))
            self.assertEqual(record["resource_count"], 0)
            self.assertEqual(record["resource_bytes"], 0)
            self.assertEqual(record["resources"], [])
            self.assertEqual(len(record["tree_sha256"]), 64)
            self.assertNotIn("Inspect public object boundaries", json.dumps(binding))
            state_text = json.dumps(run.read_state())
            self.assertNotIn("Inspect public object boundaries", state_text)
            state = run.read_state()["steps"]["review"]
            self.assertEqual(state["agent_profile_skill_count"], 1)
            self.assertEqual(state["agent_profile_skill_resource_count"], 0)
            self.assertEqual(state["agent_profile_skill_resource_bytes"], 0)
            self.assertEqual(
                state["agent_profile_skill_set_sha256"],
                effective["_agent_profile_skill_set_sha256"],
            )
            detail = collect_run_detail(root / "runs", run.run_dir.name)["steps"][0]
            self.assertEqual(detail["agent_profile_skill_count"], 1)
            self.assertEqual(detail["agent_profile_skill_resource_count"], 0)
            self.assertEqual(detail["agent_profile_skill_resource_bytes"], 0)
            self.assertEqual(
                detail["agent_profile_skill_set_sha256"],
                effective["_agent_profile_skill_set_sha256"],
            )

            resumed = CapturingProfileRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
                resume_dir=run.run_dir,
            )
            resumed.execute()
            self.assertEqual(resumed.prompts, [])

            (root / ".agents/skills/boundary-review/SKILL.md").write_text(
                "changed skill\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "changed after run binding"):
                runner._codex_resume_prompt(effective)
            with self.assertRaisesRegex(ValidationError, "fingerprint does not match"):
                CapturingProfileRunner(
                    workflow=workflow,
                    workspace=root,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                    resume_dir=run.run_dir,
                )

    def test_skill_drift_and_symlink_fail_before_provider_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entrypoint = write_skill(root, ".agents/skills/review", "original skill\n")
            workflow = workflow_with_profile(
                agent_profile(skills=[".agents/skills/review"])
            )
            runner = CapturingProfileRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            entrypoint.write_text("changed after binding\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "changed after run binding"):
                runner.execute()
            self.assertEqual(runner.prompts, [])

            target = write_skill(root, ".agents/skills/target", "target skill\n")
            link_dir = root / ".agents/skills/linked"
            try:
                link_dir.symlink_to(target.parent, target_is_directory=True)
            except OSError:
                return
            linked = workflow_with_profile(
                agent_profile(skills=[".agents/skills/linked"])
            )
            with self.assertRaisesRegex(ValidationError, "symlink"):
                CapturingProfileRunner(
                    workflow=linked,
                    workspace=root,
                    base_run_dir=root / "linked-runs",
                    policy=RuntimePolicy(allow_agent=True),
                )

            entry_link_dir = root / ".agents/skills/linked-entry"
            entry_link_dir.mkdir()
            try:
                (entry_link_dir / "SKILL.md").symlink_to(target)
            except OSError:
                return
            with self.assertRaisesRegex(ValidationError, "symlink"):
                CapturingProfileRunner(
                    workflow=workflow_with_profile(
                        agent_profile(skills=[".agents/skills/linked-entry"])
                    ),
                    workspace=root,
                    base_run_dir=root / "linked-entry-runs",
                    policy=RuntimePolicy(allow_agent=True),
                )

    def test_skill_validation_rejects_bad_paths_content_bounds_and_forged_bindings(self):
        bad_paths = [
            "/absolute/skill",
            "../outside",
            ".",
            "skills//review",
            "skills/review/",
            "skills\\review",
            "skills/review\nnext",
        ]
        for value in bad_paths:
            with self.subTest(path=value):
                with self.assertRaisesRegex(ValidationError, "skills"):
                    validate_agent_profile(agent_profile(skills=[value]))
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            validate_agent_profile(agent_profile(skills=["skills/review", "skills/review"]))
        with self.assertRaisesRegex(ValidationError, "at most 8"):
            validate_agent_profile(
                agent_profile(skills=["skills/review-%d" % index for index in range(9)])
            )

        cases = [
            ("missing", None, "missing"),
            ("empty", b"", "non-empty"),
            ("binary", b"\xff\xfe", "UTF-8"),
            ("nul", b"valid\x00text", "NUL"),
            ("oversized", b"x" * (MAX_AGENT_PROFILE_SKILL_FILE_BYTES + 1), "at most"),
        ]
        for name, content, message in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                skill_dir = root / ".agents/skills" / name
                skill_dir.mkdir(parents=True)
                if content is not None:
                    (skill_dir / "SKILL.md").write_bytes(content)
                workflow = workflow_with_profile(
                    agent_profile(skills=[".agents/skills/%s" % name])
                )
                with self.assertRaisesRegex((ValidationError, FileNotFoundError), message):
                    CapturingProfileRunner(
                        workflow=workflow,
                        workspace=root,
                        base_run_dir=root / "runs",
                        policy=RuntimePolicy(allow_agent=True),
                    )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for index in range(3):
                relative = ".agents/skills/large-%d" % index
                entrypoint = write_skill(root, relative, "x")
                entrypoint.write_bytes(b"x" * (100 * 1024))
                paths.append(relative)
            with self.assertRaisesRegex(ValidationError, "workflow limit"):
                CapturingProfileRunner(
                    workflow=workflow_with_profile(agent_profile(skills=paths)),
                    workspace=root,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles = []
            steps = []
            for profile_index in range(9):
                profile_name = "reviewer-%d" % profile_index
                skill_paths = []
                for skill_index in range(8):
                    relative = ".agents/skills/%s-%d" % (profile_name, skill_index)
                    write_skill(root, relative, "skill\n")
                    skill_paths.append(relative)
                profiles.append(
                    agent_profile(name=profile_name, skills=skill_paths)
                )
                steps.append(
                    {
                        "id": "review-%d" % profile_index,
                        "kind": "codex_exec",
                        "risk": "low",
                        "agent_profile": profile_name,
                        "prompt": "Review one boundary.",
                        "capture": "review-%d.md" % profile_index,
                    }
                )
            with self.assertRaisesRegex(ValidationError, "workflow limit of 64"):
                CapturingProfileRunner(
                    workflow={
                        "schema": "conductor.workflow.v1",
                        "name": "too-many-skill-bindings",
                        "mode": "read_only",
                        "max_workers": 9,
                        "agent_profiles": profiles,
                        "steps": steps,
                    },
                    workspace=root,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, ".agents/skills/review", "bound skill\n")
            runner = CapturingProfileRunner(
                workflow=workflow_with_profile(
                    agent_profile(skills=[".agents/skills/review"])
                ),
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True),
            )
            forged = json.loads(json.dumps(runner.workflow))
            forged["agent_profile_skill_bindings"]["profiles"][0]["skills"][0][
                "sha256"
            ] = "0" * 64
            with self.assertRaisesRegex(ValidationError, "tree hash does not match"):
                CapturingProfileRunner(
                    workflow=forged,
                    workspace=root,
                    base_run_dir=root / "forged-runs",
                    policy=RuntimePolicy(allow_agent=True),
                )

    def test_skill_resources_are_progressive_hash_bound_and_drift_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relative = ".agents/skills/resource-review"
            skill_text = (
                "---\nname: resource-review\ndescription: Use bounded resources.\n---\n\n"
                "Read references/guide.md only when the task needs it.\n"
            )

            def build(case_root: Path, runner_type=CapturingProfileRunner):
                entrypoint = write_skill(case_root, relative, skill_text)
                skill_dir = entrypoint.parent
                (skill_dir / "references").mkdir()
                guide = skill_dir / "references/guide.md"
                guide.write_text("GUIDE_RESOURCE_0150\n", encoding="utf-8")
                (skill_dir / "scripts").mkdir()
                script = skill_dir / "scripts/check.sh"
                script.write_text("#!/bin/sh\nprintf checked\\n\n", encoding="utf-8")
                script.chmod(0o755)
                (skill_dir / "assets").mkdir()
                asset = skill_dir / "assets/blob.bin"
                asset.write_bytes(b"\x00\xffasset")
                kwargs = {
                    "workflow": workflow_with_profile(agent_profile(skills=[relative])),
                    "workspace": case_root,
                    "base_run_dir": case_root / "runs",
                    "policy": RuntimePolicy(allow_agent=True),
                }
                if runner_type is ResourceDriftingProfileRunner:
                    kwargs["drift_path"] = guide
                return runner_type(**kwargs), entrypoint, guide, script, asset

            runner, entrypoint, guide, script, asset = build(root / "stable")
            record = runner.workflow["agent_profile_skill_bindings"]["profiles"][0][
                "skills"
            ][0]
            self.assertEqual(
                [resource["path"] for resource in record["resources"]],
                ["assets/blob.bin", "references/guide.md", "scripts/check.sh"],
            )
            self.assertEqual(record["resource_count"], 3)
            self.assertEqual(
                record["resource_bytes"],
                len(asset.read_bytes()) + len(guide.read_bytes()) + len(script.read_bytes()),
            )
            self.assertEqual(record["mode"], entrypoint.stat().st_mode & 0o777)
            self.assertEqual(record["resources"][2]["mode"], 0o755)
            effective = effective_agent_step(runner.workflow, runner.workflow["steps"][0])
            self.assertEqual(effective["_agent_profile_skill_resource_count"], 3)
            prompt = runner._agent_prompt(effective, "Use the selected workflow.")
            self.assertIn("resources=3", prompt)
            self.assertIn("tree_sha256=%s" % record["tree_sha256"], prompt)
            self.assertIn("available on demand", prompt)
            self.assertNotIn("GUIDE_RESOURCE_0150", prompt)
            state = runner.execute().read_state()["steps"]["review"]
            self.assertEqual(state["agent_profile_skill_resource_count"], 3)
            self.assertEqual(state["agent_profile_skill_resource_bytes"], record["resource_bytes"])

            for label in ["content", "addition", "deletion", "mode", "entrypoint-mode"]:
                with self.subTest(drift=label):
                    case_runner, case_entrypoint, case_guide, case_script, case_asset = build(
                        root / label
                    )
                    if label == "content":
                        case_guide.write_text("changed\n", encoding="utf-8")
                    elif label == "addition":
                        (case_asset.parent / "new.txt").write_text("new\n", encoding="utf-8")
                    elif label == "deletion":
                        case_asset.unlink()
                    elif label == "mode":
                        case_script.chmod(0o700)
                    else:
                        case_entrypoint.chmod((case_entrypoint.stat().st_mode & 0o777) ^ 0o100)
                    with self.assertRaisesRegex(ValidationError, "changed after run binding"):
                        case_runner.execute()
                    self.assertEqual(case_runner.prompts, [])

            drifting, _, _, _, _ = build(
                root / "provider-drift",
                ResourceDriftingProfileRunner,
            )
            with self.assertRaisesRegex(ValidationError, "changed after run binding"):
                drifting.execute()
            self.assertEqual(len(drifting.prompts), 1)

    def test_skill_resources_reject_unsafe_or_unbounded_trees(self):
        def expect_invalid(build, message):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                skill_dir = write_skill(root, ".agents/skills/review", "safe skill\n").parent
                build(skill_dir)
                with self.assertRaisesRegex(ValidationError, message):
                    CapturingProfileRunner(
                        workflow=workflow_with_profile(
                            agent_profile(skills=[".agents/skills/review"])
                        ),
                        workspace=root,
                        base_run_dir=root / "runs",
                        policy=RuntimePolicy(allow_agent=True),
                    )

        expect_invalid(
            lambda skill: (skill / ".env").write_text("TOKEN=secret\n", encoding="utf-8"),
            "secret material",
        )

        def deep_tree(skill):
            directory = skill
            for index in range(8):
                directory = directory / ("d%d" % index)
                directory.mkdir()
            (directory / "too-deep.txt").write_text("x\n", encoding="utf-8")

        expect_invalid(deep_tree, "bounded canonical relative path")

        def too_many(skill):
            resources = skill / "references"
            resources.mkdir()
            for index in range(MAX_AGENT_PROFILE_SKILL_RESOURCE_FILES + 1):
                (resources / ("%03d.txt" % index)).write_bytes(b"")

        expect_invalid(too_many, "per-Skill limit")

        def symlink_resource(skill):
            target = skill / "target.txt"
            target.write_text("target\n", encoding="utf-8")
            (skill / "linked.txt").symlink_to(target)

        try:
            expect_invalid(symlink_resource, "symlinks")
        except OSError:
            pass

        if hasattr(os, "mkfifo"):
            expect_invalid(lambda skill: os.mkfifo(skill / "pipe"), "regular files")

    def test_skill_preload_covers_map_team_cli_schema_routine_and_omitted_fast_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entrypoint = write_skill(root, ".agents/skills/review", "Use the Skill boundary method.\n")
            (root / "src").mkdir()
            (root / "src/a.py").write_text("A = 1\n", encoding="utf-8")
            (root / "src/b.py").write_text("B = 2\n", encoding="utf-8")
            mapped_workflow = workflow_with_profile(
                agent_profile(skills=[".agents/skills/review"]),
                kind="agent_map",
            )
            mapped = CapturingProfileRunner(
                workflow=mapped_workflow,
                workspace=root,
                base_run_dir=root / "map-runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
            )
            run = mapped.execute()
            self.assertEqual(len(mapped.prompts), 2)
            self.assertTrue(all("Use the Skill boundary method" in value for value in mapped.prompts))
            trace = [
                json.loads(line)
                for line in (run.artifacts_dir / "reviews/.agent-map-trace.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(all(row["agent_profile_skill_count"] == 1 for row in trace))
            self.assertTrue(all(len(row["agent_profile_skill_set_sha256"]) == 64 for row in trace))
            first_key = mapped._agent_cache_fingerprint(
                effective_agent_step(mapped.workflow, mapped.workflow["steps"][0]),
                "src/a.py",
                1,
            )["key"]
            entrypoint.write_text("Use a changed Skill method.\n", encoding="utf-8")
            changed = CapturingProfileRunner(
                workflow=mapped_workflow,
                workspace=root,
                base_run_dir=root / "changed-map-runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
            )
            second_key = changed._agent_cache_fingerprint(
                effective_agent_step(changed.workflow, changed.workflow["steps"][0]),
                "src/a.py",
                1,
            )["key"]
            self.assertNotEqual(first_key, second_key)

            team_workflow = {
                "schema": "conductor.workflow.v1",
                "name": "skill-team",
                "mode": "read_only",
                "max_workers": 2,
                "agent_profiles": [agent_profile(skills=[".agents/skills/review"])],
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
                                "instructions": "Lead review.",
                                "agent_profile": "security-reviewer",
                                "lead": True,
                            },
                            {
                                "id": "peer",
                                "role": "Peer",
                                "instructions": "Peer review.",
                                "agent_profile": "security-reviewer",
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
            team_runner = CapturingProfileRunner(
                workflow=team_workflow,
                workspace=root,
                base_run_dir=root / "team-runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
            )
            team_effective = team_member_effective_step(
                team_runner.workflow,
                team_runner.workflow["steps"][0],
                "lead",
            )
            self.assertIn("Use a changed Skill method", team_runner._agent_prompt(team_effective, "work"))
            team_metrics = team_runner._agent_team_metrics(
                team_runner.workflow["steps"][0],
                initial_agent_team_state(team_runner.workflow["steps"][0], "0" * 64),
            )
            self.assertEqual(team_metrics["agent_team_profile_skill_profiles"], 1)
            self.assertEqual(team_metrics["agent_team_profile_skill_count"], 1)
            self.assertEqual(len(team_metrics["agent_team_profile_skill_sets_sha256"]), 64)

            profile_path = root / "skill-profile.json"
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "write-agent-profile",
                            str(profile_path),
                            "--name",
                            "skill-reviewer",
                            "--instructions",
                            "Review with one selected Skill.",
                            "--skill",
                            ".agents/skills/review",
                        ]
                    ),
                    0,
                )
            self.assertEqual(load_agent_profile(profile_path)["skills"], [".agents/skills/review"])
            schema = get_schema("agent-profile")
            self.assertEqual(schema["properties"]["skills"]["maxItems"], 8)
            workflow_schema = get_schema("workflow")
            self.assertIn("agent_profile_skill_bindings", workflow_schema["properties"])
            context_record_schema = workflow_schema["properties"][
                "agent_profile_context_bindings"
            ]["properties"]["profiles"]["items"]["properties"]["files"]["items"]
            self.assertEqual(context_record_schema["required"], ["path", "sha256", "bytes"])
            skill_record_schema = workflow_schema["properties"][
                "agent_profile_skill_bindings"
            ]["properties"]["profiles"]["items"]["properties"]["skills"]["items"]
            self.assertEqual(
                set(skill_record_schema["required"]),
                {
                    "path",
                    "sha256",
                    "bytes",
                    "mode",
                    "tree_sha256",
                    "resource_count",
                    "resource_bytes",
                    "resources",
                },
            )

            routine_workflow = workflow_with_profile(
                agent_profile(skills=[".agents/skills/review"])
            )
            workflow_path = root / "skill-workflow.json"
            manifest_path = root / "skill-routine.json"
            workflow_path.write_text(json.dumps(routine_workflow), encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "write-routine-manifest",
                            str(manifest_path),
                            "--name",
                            "skill-routine",
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
            current = _validate_current_target(load_routine_manifest(manifest_path))
            self.assertIn("agent_profile_skill_bindings", current)
            entrypoint.write_text("routine Skill drift\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "routine target changed"):
                _validate_current_target(load_routine_manifest(manifest_path))

            plain = workflow_with_profile()
            with patch(
                "conductor_runtime.agent_profiles.bind_agent_profile_skills",
                side_effect=AssertionError("omitted Skill path performed binding work"),
            ):
                plain_runner = CapturingProfileRunner(
                    workflow=plain,
                    workspace=root,
                    base_run_dir=root / "plain-runs",
                    policy=RuntimePolicy(allow_agent=True),
                )
            self.assertNotIn("agent_profile_skill_bindings", plain_runner.workflow)
            plain_effective = effective_agent_step(
                plain_runner.workflow,
                plain_runner.workflow["steps"][0],
            )
            self.assertNotIn("_agent_profile_skill_count", plain_effective)

    def test_model_authored_workflow_cannot_select_profile_skills(self):
        workflow = workflow_with_profile(
            agent_profile(skills=[".agents/skills/review"])
        )
        with self.assertRaisesRegex(ValidationError, "cannot preload Skills"):
            validate_model_authored_workflow(
                workflow,
                write_capable=False,
                max_workers=2,
                max_items=10,
                max_steps=4,
            )

    def test_agent_map_uses_profile_in_prompt_budget_trace_and_cache_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
            (root / "src" / "b.py").write_text("B = 2\n", encoding="utf-8")
            workflow = workflow_with_profile(kind="agent_map")
            runner = CapturingProfileRunner(
                workflow=workflow,
                workspace=root,
                base_run_dir=root / "runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
                run_id="profile-map",
            )
            run = runner.execute()
            self.assertEqual(len(runner.prompts), 2)
            self.assertTrue(all("Check correctness" in prompt for prompt in runner.prompts))
            state = run.read_state()["steps"]["review"]
            self.assertEqual(state["agent_profile"], "security-reviewer")
            self.assertEqual(state["agent_tool_policy"], "isolated")
            self.assertEqual(state["agent_tool_output_token_limit"], 2000)
            self.assertEqual(state["agent_effort"], "ultra")
            self.assertEqual(state["agent_max_tokens"], 1000)
            trace_lines = (run.artifacts_dir / "reviews" / ".agent-map-trace.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertTrue(trace_lines)
            self.assertTrue(all(json.loads(line)["agent_profile"] == "security-reviewer" for line in trace_lines))
            self.assertTrue(all(json.loads(line)["agent_tool_policy"] == "isolated" for line in trace_lines))

            first = runner._agent_cache_fingerprint(
                effective_agent_step(runner.workflow, runner.workflow["steps"][0]),
                "src/a.py",
                1,
            )["key"]
            changed = workflow_with_profile(agent_profile(instructions="Use a distinct review method."), kind="agent_map")
            other = CapturingProfileRunner(
                workflow=changed,
                workspace=root,
                base_run_dir=root / "other-runs",
                policy=RuntimePolicy(allow_agent=True, allow_parallel=True),
            )
            second = other._agent_cache_fingerprint(
                effective_agent_step(other.workflow, other.workflow["steps"][0]),
                "src/a.py",
                1,
            )["key"]
            self.assertNotEqual(first, second)

    def test_profile_lifecycle_cli_schema_and_command_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_path = root / "reviewer.json"
            workflow_path = root / "workflow.json"
            bound_path = root / "bound.json"
            plain = workflow_with_profile()
            plain.pop("agent_profiles")
            plain["steps"][0].pop("agent_profile")
            workflow_path.write_text(json.dumps(plain), encoding="utf-8")

            stdout = StringIO()
            with redirect_stdout(stdout):
                code = cli_main(
                    [
                        "write-agent-profile",
                        str(profile_path),
                        "--name",
                        "security-reviewer",
                        "--instructions",
                        "Review correctness and tests.",
                        "--effort",
                        "ultra",
                        "--max-tokens",
                        "1000",
                        "--tool-policy",
                        "isolated",
                        "--tool-output-token-limit",
                        "2000",
                        "--memory-mode",
                        "read-only",
                        "--memory-selection",
                        "relevant",
                        "--memory-max-entries",
                        "16",
                        "--memory-max-bytes",
                        "32768",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(load_agent_profile(profile_path)["effort"], "ultra")

            with redirect_stdout(StringIO()):
                self.assertEqual(cli_main(["validate-agent-profile", str(profile_path)]), 0)
            inspected = StringIO()
            with redirect_stdout(inspected):
                self.assertEqual(cli_main(["inspect-agent-profile", str(profile_path), "--json"]), 0)
            summary = json.loads(inspected.getvalue())
            self.assertNotIn("instructions", summary)
            self.assertEqual(summary["name"], "security-reviewer")
            self.assertEqual(summary["tool_policy"], "isolated")
            self.assertEqual(summary["tool_output_token_limit"], 2000)
            self.assertEqual(summary["memory"]["mode"], "read-only")
            self.assertEqual(summary["memory"]["selection"], "relevant")
            self.assertEqual(summary["memory"]["max_entries"], 16)

            inspected_with_workspace = StringIO()
            with redirect_stdout(inspected_with_workspace):
                self.assertEqual(
                    cli_main(
                        [
                            "inspect-agent-profile",
                            str(profile_path),
                            "--workspace",
                            str(root),
                            "--json",
                        ]
                    ),
                    0,
                )
            prompt_summary = json.loads(inspected_with_workspace.getvalue())[
                "prompt_compaction"
            ]
            self.assertRegex(prompt_summary["full_static_sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(prompt_summary["full_static_bytes"], 0)
            self.assertGreaterEqual(prompt_summary["resume_static_saved_bytes"], 0)

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "bind-agent-profile",
                            str(workflow_path),
                            str(profile_path),
                            str(bound_path),
                            "--step",
                            "review",
                        ]
                    ),
                    0,
                )
            bound = load_workflow(bound_path)
            self.assertEqual(bound["steps"][0]["agent_profile"], "security-reviewer")
            self.assertEqual(bound["agent_profiles"][0]["schema"], AGENT_PROFILE_SCHEMA)

            schema = get_schema("agent-profile")
            self.assertEqual(schema["properties"]["schema"]["const"], AGENT_PROFILE_SCHEMA)
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(
                schema["properties"]["tool_policy"]["enum"],
                ["isolated", "restricted", "standard"],
            )
            self.assertEqual(schema["properties"]["tool_output_token_limit"]["minimum"], 256)
            self.assertEqual(schema["properties"]["tool_output_token_limit"]["maximum"], 32768)
            workflow_schema = get_schema("workflow")
            self.assertIn("agent_profiles", workflow_schema["properties"])

            read_assessment = assess_command(
                ["python3", "-m", "conductor_runtime", "validate-agent-profile", "profile.json"]
            )
            self.assertFalse(read_assessment.writes)
            self.assertFalse(read_assessment.network)
            write_assessment = assess_command(
                ["python3", "-m", "conductor_runtime", "write-agent-profile", "profile.json"]
            )
            self.assertTrue(write_assessment.writes)
            self.assertFalse(write_assessment.network)
            self.assertFalse(write_assessment.destructive)

    def test_model_authored_workflow_accepts_bounded_profiles(self):
        workflow = workflow_with_profile()
        workflow["steps"][0]["sandbox"] = "read-only"
        validate_model_authored_workflow(
            workflow,
            write_capable=False,
            max_workers=2,
            max_items=10,
            max_steps=4,
        )
        unsafe = workflow_with_profile(agent_profile(sandbox="workspace-write"))
        with self.assertRaisesRegex(ValidationError, "requires --write-capable"):
            validate_model_authored_workflow(
                unsafe,
                write_capable=False,
                max_workers=2,
                max_items=10,
                max_steps=4,
            )

    def test_routine_target_binds_profile_context_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = root / "routine-context.md"
            context.write_text("approved routine context\n", encoding="utf-8")
            workflow = workflow_with_profile(agent_profile(context_files=["routine-context.md"]))
            workflow_path = root / "workflow.json"
            manifest_path = root / "routine.json"
            workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
            with redirect_stdout(StringIO()):
                code = cli_main(
                    [
                        "write-routine-manifest",
                        str(manifest_path),
                        "--name",
                        "profile-routine",
                        "--workflow",
                        str(workflow_path),
                        "--workspace",
                        str(root),
                        "--manual",
                        "--allow-agent",
                    ]
                )
            self.assertEqual(code, 0)
            manifest = load_routine_manifest(manifest_path)
            current = _validate_current_target(manifest)
            self.assertIn("agent_profile_context_bindings", current)
            context.write_text("changed routine context\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "target changed"):
                _validate_current_target(manifest)

    def test_profile_validation_is_strict_and_summary_is_non_secret(self):
        profile = agent_profile()
        validate_agent_profile(profile)
        summary = agent_profile_summary(profile)
        self.assertNotIn("instructions", summary)
        self.assertEqual(summary["instructions_bytes"], len(profile["instructions"].encode("utf-8")))
        bad = dict(profile)
        bad["unknown"] = True
        with self.assertRaisesRegex(ValidationError, "unsupported field"):
            validate_agent_profile(bad)

        standard_with_limit = agent_profile(tool_policy="standard")
        with self.assertRaisesRegex(ValidationError, "requires tool_policy isolated"):
            validate_agent_profile(standard_with_limit)
        denied_context = agent_profile(context_files=["secrets/.env.production"])
        with self.assertRaisesRegex(ValidationError, "denied secret path"):
            validate_agent_profile(denied_context)
        invalid_policy = agent_profile(tool_policy="unbounded")
        with self.assertRaisesRegex(ValidationError, "tool_policy"):
            validate_agent_profile(invalid_policy)
        for field in ["tool_policy", "tool_output_token_limit", "_agent_tool_policy"]:
            misplaced = workflow_with_profile()
            misplaced["steps"][0][field] = "isolated" if "limit" not in field else 2000
            with self.subTest(misplaced=field):
                with self.assertRaisesRegex(ValidationError, "agent_profile|reserved internal"):
                    validate_workflow(misplaced)

        stderr = StringIO()
        with redirect_stderr(stderr):
            code = cli_main(["validate-agent-profile", "missing-profile.json"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
