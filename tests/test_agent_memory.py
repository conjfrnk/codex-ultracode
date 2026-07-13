import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from conductor_runtime.agent_memory import (
    AGENT_MEMORY_SCHEMA,
    AGENT_MEMORY_WRITE_APPROVAL,
    MAX_AGENT_MEMORY_QUERY_BYTES,
    agent_memory_selection_policy,
    agent_memory_path,
    append_agent_memory,
    effective_agent_memory_config,
    load_agent_memory,
    remove_agent_memory_entry,
    select_agent_memory_snapshot,
)
from conductor_runtime.agent_profiles import (
    AGENT_PROFILE_MEMORY_BINDING_SCHEMA,
    AGENT_PROFILE_SCHEMA,
    AGENT_PROFILE_MEMORY_RECORD_FIELDS,
    agent_profile_memory_query,
    agent_profile_prompt_section,
    effective_agent_step,
)
from conductor_runtime.cli import main as cli_main
from conductor_runtime.dashboard import collect_run_detail
from conductor_runtime.errors import PolicyError, ValidationError
from conductor_runtime.goal_loop import run_goal_loop
from conductor_runtime.model_planner import validate_model_authored_workflow
from conductor_runtime.routine_supervisor import _validate_current_target
from conductor_runtime.routines import load_routine_manifest
from conductor_runtime.runner import WorkflowRunner
from conductor_runtime.schemas import get_schema
from conductor_runtime.security import RuntimePolicy, assess_command
from conductor_runtime.workflow import validate_workflow, workflow_fingerprint


def memory_profile(mode="read-write", **overrides):
    profile = {
        "schema": AGENT_PROFILE_SCHEMA,
        "name": "reviewer",
        "instructions": "Review current evidence and preserve only durable verified facts.",
        "sandbox": "read-only",
        "risk": "medium",
        "memory": {
            "mode": mode,
            "max_entries": 8,
            "max_bytes": 8192,
        },
    }
    profile.update(overrides)
    return profile


def memory_update_workflow(profile=None):
    return {
        "schema": "conductor.workflow.v1",
        "name": "remember-review",
        "mode": "custom",
        "agent_profiles": [profile or memory_profile()],
        "steps": [
            {
                "id": "note",
                "kind": "write_artifact",
                "output": "durable-note.md",
                "content": "Boundary checks are required for retry changes.\n",
            },
            {
                "id": "remember",
                "kind": "agent_memory",
                "risk": "medium",
                "agent_profile": "reviewer",
                "source_artifact": "durable-note.md",
                "tags": ["retry", "verification"],
                "depends_on": ["note"],
            },
        ],
    }


def memory_reader_workflow(profile=None):
    return {
        "schema": "conductor.workflow.v1",
        "name": "memory-reader",
        "mode": "read_only",
        "agent_profiles": [profile or memory_profile(mode="read-only")],
        "steps": [
            {
                "id": "review",
                "kind": "codex_exec",
                "risk": "medium",
                "agent_profile": "reviewer",
                "prompt": "Review the current retry implementation.",
            }
        ],
    }


class AgentMemoryTests(unittest.TestCase):
    def test_recent_selection_is_compatible_and_relevant_selection_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            memory_dir = root / "memory"
            workspace.mkdir()
            append_agent_memory(
                workspace,
                "reviewer",
                "Legacy retry policy conversion must preserve timeout boundaries.",
                entry_id="retry-policy",
                tags=["retry", "migration"],
                memory_dir=memory_dir,
            )
            append_agent_memory(
                workspace,
                "reviewer",
                "The repository uses standard-library unit tests.",
                entry_id="generic-testing",
                tags=["testing"],
                memory_dir=memory_dir,
            )
            append_agent_memory(
                workspace,
                "reviewer",
                "Dashboard colors require a contrast review.",
                entry_id="dashboard-colors",
                tags=["dashboard"],
                memory_dir=memory_dir,
            )
            memory = load_agent_memory(workspace, "reviewer", memory_dir=memory_dir)
            base = {"mode": "read-only", "max_entries": 1, "max_bytes": 8192}
            legacy = select_agent_memory_snapshot(memory, base)
            explicit_recent = select_agent_memory_snapshot(
                memory,
                {**base, "selection": "recent"},
                "retry policy",
            )
            self.assertEqual(legacy, explicit_recent)
            self.assertEqual(legacy["entries"][0]["id"], "dashboard-colors")
            self.assertEqual(effective_agent_memory_config(base)["selection"], "recent")

            relevant_config = {**base, "selection": "relevant"}
            relevant = select_agent_memory_snapshot(memory, relevant_config, "repair retry policy migration")
            repeated = select_agent_memory_snapshot(memory, relevant_config, "repair retry policy migration")
            self.assertEqual(relevant, repeated)
            self.assertEqual(relevant["entries"][0]["id"], "retry-policy")
            self.assertLessEqual(relevant["bytes"], relevant_config["max_bytes"])

            no_match = select_agent_memory_snapshot(memory, relevant_config, "quantum frobnication")
            self.assertEqual(no_match["entries"][0]["id"], "dashboard-colors")
            with self.assertRaisesRegex(ValidationError, "query exceeds"):
                select_agent_memory_snapshot(
                    memory,
                    relevant_config,
                    "x" * (MAX_AGENT_MEMORY_QUERY_BYTES + 1),
                )

    def test_hybrid_selection_prefers_exact_code_phrase_over_newer_bag_of_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            memory_dir = root / "memory"
            workspace.mkdir()
            append_agent_memory(
                workspace,
                "reviewer",
                "Use the retry_policy migration guard before converting legacy attempts.",
                entry_id="exact-identifier",
                tags=["migration"],
                memory_dir=memory_dir,
            )
            append_agent_memory(
                workspace,
                "reviewer",
                "Migration ownership changed. Retry counters are separate from dashboard policy.",
                entry_id="newer-bag-of-words",
                tags=["migration"],
                memory_dir=memory_dir,
            )
            memory = load_agent_memory(workspace, "reviewer", memory_dir=memory_dir)
            base = {
                "mode": "read-only",
                "max_entries": 1,
                "max_bytes": 8192,
            }
            query = "retry_policy migration"
            relevant = select_agent_memory_snapshot(
                memory,
                {**base, "selection": "relevant"},
                query,
            )
            hybrid = select_agent_memory_snapshot(
                memory,
                {**base, "selection": "hybrid"},
                query,
            )
            repeated = select_agent_memory_snapshot(
                memory,
                {**base, "selection": "hybrid"},
                query,
            )
            self.assertEqual(relevant["entries"][0]["id"], "newer-bag-of-words")
            self.assertEqual(hybrid, repeated)
            self.assertEqual(hybrid["entries"][0]["id"], "exact-identifier")
            self.assertEqual(agent_memory_selection_policy("hybrid"), "hybrid-lexical-v1")

            no_match = select_agent_memory_snapshot(
                memory,
                {**base, "selection": "hybrid"},
                "quantum frobnication",
            )
            self.assertEqual(no_match["entries"][0]["id"], "newer-bag-of-words")

    def test_relevant_profile_binding_uses_task_query_and_resume_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            state = root / "state"
            runs = root / "runs"
            workspace.mkdir()
            profile = memory_profile(mode="read-only")
            profile["memory"]["selection"] = "relevant"
            profile["memory"]["max_entries"] = 1
            workflow = memory_reader_workflow(profile)
            with patch.dict(os.environ, {"CODEX_CONDUCTOR_HOME": str(state)}):
                append_agent_memory(
                    workspace,
                    "reviewer",
                    "Retry migration requires strict completed-attempt validation.",
                    entry_id="retry-validation",
                    tags=["retry", "migration"],
                )
                append_agent_memory(
                    workspace,
                    "reviewer",
                    "Dashboard typography should remain compact.",
                    entry_id="dashboard-type",
                    tags=["dashboard"],
                )
                runner = WorkflowRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=runs,
                    policy=RuntimePolicy(allow_agent=True),
                    run_id="relevant-memory",
                )
                record = runner.workflow["agent_profile_memory_bindings"]["profiles"][0]
                self.assertEqual(set(record), AGENT_PROFILE_MEMORY_RECORD_FIELDS)
                self.assertEqual(record["entries"][0]["id"], "retry-validation")
                query = agent_profile_memory_query(runner.workflow, "reviewer")
                self.assertIn("current retry implementation", query)
                effective = effective_agent_step(runner.workflow, runner.workflow["steps"][0])
                self.assertEqual(effective["_agent_memory_selection"], "relevant")
                self.assertEqual(
                    effective["_agent_memory_selection_policy"],
                    "lexical-relevance-v1",
                )
                self.assertEqual(len(effective["_agent_memory_query_sha256"]), 64)
                metrics = runner._codex_budget_metrics(effective)
                self.assertEqual(metrics["agent_memory_selection"], "relevant")
                self.assertEqual(
                    metrics["agent_memory_query_sha256"],
                    effective["_agent_memory_query_sha256"],
                )
                prompt = agent_profile_prompt_section(runner.workflow, effective, workspace)
                self.assertIn("selection=relevant", prompt)
                self.assertIn("selection_policy=lexical-relevance-v1", prompt)

                append_agent_memory(
                    workspace,
                    "reviewer",
                    "A newer retry policy observation.",
                    entry_id="newer-retry",
                    tags=["retry"],
                )
                resumed = WorkflowRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=runs,
                    policy=RuntimePolicy(allow_agent=True),
                    resume_dir=runner.run.run_dir,
                )
                resumed_record = resumed.workflow["agent_profile_memory_bindings"]["profiles"][0]
                self.assertEqual(record, resumed_record)

    def test_hybrid_profile_binding_exposes_policy_and_query_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir()
            profile = memory_profile(mode="read-only")
            profile["memory"]["selection"] = "hybrid"
            profile["memory"]["max_entries"] = 1
            workflow = memory_reader_workflow(profile)
            with patch.dict(os.environ, {"CODEX_CONDUCTOR_HOME": str(state)}):
                append_agent_memory(
                    workspace,
                    "reviewer",
                    "The retry implementation requires a retry_policy migration guard.",
                    entry_id="hybrid-match",
                    tags=["retry"],
                )
                append_agent_memory(
                    workspace,
                    "reviewer",
                    "Dashboard policy and migration notes mention retry counters.",
                    entry_id="hybrid-decoy",
                    tags=["dashboard"],
                )
                runner = WorkflowRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=RuntimePolicy(allow_agent=True),
                )
                record = runner.workflow["agent_profile_memory_bindings"]["profiles"][0]
                self.assertEqual(record["entries"][0]["id"], "hybrid-match")
                effective = effective_agent_step(
                    runner.workflow,
                    runner.workflow["steps"][0],
                )
                self.assertEqual(effective["_agent_memory_selection"], "hybrid")
                self.assertEqual(
                    effective["_agent_memory_selection_policy"],
                    "hybrid-lexical-v1",
                )
                self.assertRegex(
                    effective["_agent_memory_query_sha256"],
                    r"^[0-9a-f]{64}$",
                )
                prompt = agent_profile_prompt_section(
                    runner.workflow,
                    effective,
                    workspace,
                )
                self.assertIn("selection=hybrid", prompt)
                self.assertIn("selection_policy=hybrid-lexical-v1", prompt)

    def test_store_lifecycle_redacts_secrets_and_enforces_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            memory_dir = root / "state" / "agent-memory"
            workspace.mkdir()
            first = append_agent_memory(
                workspace,
                "reviewer",
                "Use boundary tests. api_key=super-secret-value",
                entry_id="boundary-tests",
                tags=["testing"],
                expected_revision=0,
                memory_dir=memory_dir,
            )
            self.assertTrue(first["changed"])
            self.assertEqual(first["memory"]["revision"], 1)
            self.assertNotIn("super-secret-value", first["entry"]["content"])
            self.assertIn("<redacted>", first["entry"]["content"])

            repeated = append_agent_memory(
                workspace,
                "reviewer",
                "Use boundary tests. api_key=super-secret-value",
                entry_id="boundary-tests",
                tags=["testing"],
                expected_revision=0,
                memory_dir=memory_dir,
            )
            self.assertFalse(repeated["changed"])
            self.assertEqual(repeated["memory"]["revision"], 1)

            with self.assertRaisesRegex(ValidationError, "revision changed"):
                append_agent_memory(
                    workspace,
                    "reviewer",
                    "A new fact.",
                    entry_id="new-fact",
                    expected_revision=0,
                    memory_dir=memory_dir,
                )
            with self.assertRaisesRegex(ValidationError, "different content"):
                append_agent_memory(
                    workspace,
                    "reviewer",
                    "Conflicting replacement.",
                    entry_id="boundary-tests",
                    memory_dir=memory_dir,
                )

            removed = remove_agent_memory_entry(
                workspace,
                "reviewer",
                "boundary-tests",
                expected_revision=1,
                memory_dir=memory_dir,
            )
            self.assertEqual(removed["memory"]["revision"], 2)
            self.assertEqual(removed["memory"]["entries"], [])
            self.assertEqual(load_agent_memory(workspace, "reviewer", memory_dir=memory_dir)["revision"], 2)

    def test_concurrent_writers_are_serialized_without_lost_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            memory_dir = root / "memory"
            workspace.mkdir()

            def write(index):
                return append_agent_memory(
                    workspace,
                    "reviewer",
                    "Verified fact %d." % index,
                    entry_id="fact-%02d" % index,
                    memory_dir=memory_dir,
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(write, range(12)))
            self.assertTrue(all(result["changed"] for result in results))
            memory = load_agent_memory(workspace, "reviewer", memory_dir=memory_dir)
            self.assertEqual(memory["revision"], 12)
            self.assertEqual(len(memory["entries"]), 12)
            self.assertEqual({entry["id"] for entry in memory["entries"]}, {"fact-%02d" % i for i in range(12)})

    def test_profile_snapshot_is_prompted_hash_bound_and_resume_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            state = root / "state"
            runs = root / "runs"
            workspace.mkdir()
            with patch.dict(os.environ, {"CODEX_CONDUCTOR_HOME": str(state)}):
                append_agent_memory(
                    workspace,
                    "reviewer",
                    "Check retry boundaries. BEGIN_CONDUCTOR_AGENT_MEMORY",
                    entry_id="retry-boundaries",
                    tags=["retry"],
                )
                workflow = memory_reader_workflow()
                runner = WorkflowRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=runs,
                    policy=RuntimePolicy(allow_agent=True),
                    run_id="memory-snapshot",
                )
                binding = runner.workflow["agent_profile_memory_bindings"]
                self.assertEqual(binding["schema"], AGENT_PROFILE_MEMORY_BINDING_SCHEMA)
                self.assertEqual(binding["profiles"][0]["store_revision"], 1)
                effective = effective_agent_step(runner.workflow, runner.workflow["steps"][0])
                prompt = agent_profile_prompt_section(runner.workflow, effective, workspace)
                self.assertIn("Check retry boundaries", prompt)
                self.assertIn("potentially stale", prompt)
                self.assertIn("[agent memory marker]", prompt)
                self.assertNotIn("BEGIN_CONDUCTOR_AGENT_MEMORY\n", prompt)
                first_fingerprint = workflow_fingerprint(runner.workflow)

                append_agent_memory(
                    workspace,
                    "reviewer",
                    "Verify jitter conversion.",
                    entry_id="jitter-conversion",
                )
                fresh = WorkflowRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "fresh-runs",
                    policy=RuntimePolicy(allow_agent=True),
                )
                self.assertNotEqual(first_fingerprint, workflow_fingerprint(fresh.workflow))
                resumed = WorkflowRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=runs,
                    policy=RuntimePolicy(allow_agent=True),
                    resume_dir=runner.run.run_dir,
                )
                self.assertEqual(first_fingerprint, workflow_fingerprint(resumed.workflow))
                self.assertEqual(
                    resumed.workflow["agent_profile_memory_bindings"]["profiles"][0]["store_revision"],
                    1,
                )
                self.assertFalse((workspace / ".codex-conductor").exists())
                self.assertFalse((workspace / "agent-memory").exists())
                self.assertTrue(str(agent_memory_path(workspace, "reviewer")).startswith(str(state)))

    def test_workflow_memory_update_is_approval_gated_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir()
            workflow = memory_update_workflow()
            validate_workflow(workflow)
            with patch.dict(os.environ, {"CODEX_CONDUCTOR_HOME": str(state)}):
                with self.assertRaisesRegex(PolicyError, "allow-writes"):
                    WorkflowRunner(
                        workflow=workflow,
                        workspace=workspace,
                        base_run_dir=root / "denied-runs",
                        policy=RuntimePolicy(),
                        dry_run=True,
                    ).execute()
                with self.assertRaisesRegex(PolicyError, "agent-memory-write"):
                    WorkflowRunner(
                        workflow=workflow,
                        workspace=workspace,
                        base_run_dir=root / "unapproved-runs",
                        policy=RuntimePolicy(allow_writes=True),
                        dry_run=True,
                    ).execute()

                policy = RuntimePolicy(
                    allow_writes=True,
                    approvals={AGENT_MEMORY_WRITE_APPROVAL},
                )
                runner = WorkflowRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    run_id="remember-once",
                )
                run = runner.execute()
                memory = load_agent_memory(workspace, "reviewer")
                self.assertEqual(memory["revision"], 1)
                self.assertEqual(len(memory["entries"]), 1)
                step = run.read_state()["steps"]["remember"]
                self.assertEqual(step["agent_memory_start_revision"], 0)
                self.assertEqual(step["agent_memory_revision"], 1)
                self.assertTrue(step["agent_memory_changed"])
                detail = collect_run_detail(root / "runs", "remember-once")
                memory_step = next(item for item in detail["steps"] if item["id"] == "remember")
                self.assertEqual(memory_step["agent_memory_revision"], 1)

                stored_state = run.read_state()
                stored_state["steps"]["remember"]["status"] = "pending"
                run.save_state(stored_state)
                resumed = WorkflowRunner(
                    workflow=workflow,
                    workspace=workspace,
                    base_run_dir=root / "runs",
                    policy=policy,
                    resume_dir=run.run_dir,
                )
                resumed.execute()
                memory = load_agent_memory(workspace, "reviewer")
                self.assertEqual(memory["revision"], 1)
                self.assertEqual(len(memory["entries"]), 1)
                self.assertFalse(resumed.run.read_state()["steps"]["remember"]["agent_memory_changed"])

    def test_memory_workflow_validation_rejects_unsafe_shapes(self):
        read_only = memory_update_workflow(memory_profile(mode="read-only"))
        with self.assertRaisesRegex(ValidationError, "read-write"):
            validate_workflow(read_only)
        low_risk = memory_update_workflow()
        low_risk["steps"][1]["risk"] = "low"
        with self.assertRaisesRegex(ValidationError, "medium or high"):
            validate_workflow(low_risk)
        duplicate = memory_update_workflow()
        duplicate["steps"].append(
            {
                "id": "remember-again",
                "kind": "agent_memory",
                "risk": "medium",
                "agent_profile": "reviewer",
                "source_artifact": "durable-note.md",
            }
        )
        with self.assertRaisesRegex(ValidationError, "at most one"):
            validate_workflow(duplicate)
        invalid_tag = memory_update_workflow()
        invalid_tag["steps"][1]["tags"] = [["not-hashable"]]
        with self.assertRaisesRegex(ValidationError, "invalid tag"):
            validate_workflow(invalid_tag)
        invalid_selection = memory_reader_workflow()
        invalid_selection["agent_profiles"][0]["memory"]["selection"] = "semantic"
        with self.assertRaisesRegex(
            ValidationError,
            "selection must be hybrid, recent, or relevant",
        ):
            validate_workflow(invalid_selection)
        with self.assertRaisesRegex(ValidationError, "cannot opt into external memory"):
            validate_model_authored_workflow(
                memory_reader_workflow(),
                write_capable=False,
                max_workers=1,
                max_items=10,
                max_steps=4,
            )

    def test_goal_updates_are_rejected_and_routine_target_ignores_memory_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir()
            update_workflow = memory_update_workflow()
            with patch.dict(os.environ, {"CODEX_CONDUCTOR_HOME": str(state)}):
                with self.assertRaisesRegex(ValidationError, "not supported inside run-goal"):
                    run_goal_loop(
                        workflow=update_workflow,
                        workspace=workspace,
                        runs_dir=root / "goal-runs",
                        goals_dir=root / "goals",
                        policy=RuntimePolicy(
                            allow_writes=True,
                            approvals={AGENT_MEMORY_WRITE_APPROVAL},
                        ),
                        check_command=["true"],
                        max_iterations=2,
                    )

                reader = memory_reader_workflow()
                workflow_path = root / "reader.json"
                manifest_path = root / "routine.json"
                workflow_path.write_text(json.dumps(reader), encoding="utf-8")
                with redirect_stdout(StringIO()):
                    self.assertEqual(
                        cli_main(
                            [
                                "write-routine-manifest",
                                str(manifest_path),
                                "--name",
                                "memory-review",
                                "--workflow",
                                str(workflow_path),
                                "--workspace",
                                str(workspace),
                                "--manual",
                                "--allow-agent",
                            ]
                        ),
                        0,
                    )
                manifest = load_routine_manifest(manifest_path)
                before = workflow_fingerprint(_validate_current_target(manifest))
                append_agent_memory(
                    workspace,
                    "reviewer",
                    "A newly verified routine fact.",
                    entry_id="routine-fact",
                )
                after = workflow_fingerprint(_validate_current_target(manifest))
                self.assertEqual(before, after)

    def test_cli_schema_and_command_policy_cover_memory_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            memory_dir = root / "memory"
            workspace.mkdir()
            output = StringIO()
            with redirect_stdout(output):
                code = cli_main(
                    [
                        "remember-agent-memory",
                        "reviewer",
                        "--workspace",
                        str(workspace),
                        "--memory-dir",
                        str(memory_dir),
                        "--content",
                        "Preserve boundary behavior.",
                        "--entry-id",
                        "boundary-behavior",
                        "--tag",
                        "compatibility",
                        "--expected-revision",
                        "0",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("Revision: 1", output.getvalue())
            memory_path = memory_dir / "reviewer.json"
            with redirect_stdout(StringIO()):
                self.assertEqual(cli_main(["validate-agent-memory", str(memory_path)]), 0)
            listed = StringIO()
            with redirect_stdout(listed):
                self.assertEqual(
                    cli_main(
                        [
                            "list-agent-memory",
                            "--workspace",
                            str(workspace),
                            "--memory-dir",
                            str(memory_dir),
                            "--json",
                        ]
                    ),
                    0,
                )
            self.assertEqual(json.loads(listed.getvalue())["memories"][0]["revision"], 1)
            inspected = StringIO()
            with redirect_stdout(inspected):
                self.assertEqual(
                    cli_main(
                        [
                            "inspect-agent-memory",
                            "reviewer",
                            "--workspace",
                            str(workspace),
                            "--memory-dir",
                            str(memory_dir),
                            "--json",
                        ]
                    ),
                    0,
                )
            inspected_value = json.loads(inspected.getvalue())
            self.assertNotIn("content", inspected_value["entries"][0])
            self.assertEqual(inspected_value["entry_count"], 1)
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "forget-agent-memory",
                            "reviewer",
                            "boundary-behavior",
                            "--workspace",
                            str(workspace),
                            "--memory-dir",
                            str(memory_dir),
                            "--expected-revision",
                            "1",
                        ]
                    ),
                    0,
                )

            schema = get_schema("agent-memory")
            self.assertEqual(schema["properties"]["schema"]["const"], AGENT_MEMORY_SCHEMA)
            workflow_schema = get_schema("workflow")
            self.assertIn("agent_profile_memory_bindings", workflow_schema["properties"])
            profile_schema = get_schema("agent-profile")
            self.assertIn("memory", profile_schema["properties"])
            self.assertEqual(
                set(profile_schema["properties"]["memory"]["properties"]["selection"]["enum"]),
                {"hybrid", "recent", "relevant"},
            )

            read = assess_command(
                ["python3", "-m", "conductor_runtime", "inspect-agent-memory", "reviewer"]
            )
            self.assertFalse(read.writes)
            self.assertFalse(read.network)
            write = assess_command(
                ["python3", "-m", "conductor_runtime", "remember-agent-memory", "reviewer"]
            )
            self.assertTrue(write.writes)
            self.assertFalse(write.network)
            self.assertFalse(write.destructive)

            stderr = StringIO()
            with redirect_stderr(stderr):
                code = cli_main(
                    [
                        "remember-agent-memory",
                        "reviewer",
                        "--workspace",
                        str(workspace),
                        "--memory-dir",
                        str(workspace / "tracked-memory"),
                        "--content",
                        "Must stay external.",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("outside the source workspace", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
