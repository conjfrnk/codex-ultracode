import ast
import copy
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from conductor_runtime import __version__
from conductor_runtime.auto_topology_campaign import (
    AUTO_TOPOLOGY_CAMPAIGN_SCHEMA,
    AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V1,
    AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V2,
    AUTO_TOPOLOGY_RESULT_SCHEMA,
    auto_topology_campaign_status,
    build_auto_topology_campaign,
    derive_auto_topology_resources,
    _dominant_topology,
    _topology_inference,
    render_auto_topology_task_contract,
    validate_auto_topology_campaign,
    write_auto_topology_campaign,
)
from conductor_runtime.auto_topology_run import (
    AUTO_TOPOLOGY_ARM_APPROVAL,
    _receipt_duration_ms,
    run_auto_topology_arm,
)
from conductor_runtime.benchmark import load_parity_tasks
from conductor_runtime.build_identity import current_runtime_build_sha256
from conductor_runtime.legacy_cli import main as cli_main
from conductor_runtime.errors import PolicyError, ValidationError
from conductor_runtime.model_orchestrator import MODEL_WORKFLOW_EXECUTE_APPROVAL
from conductor_runtime.model_planner import MODEL_WORKFLOW_WRITE_APPROVAL
from conductor_runtime.runner import ProcessResult
from conductor_runtime.schemas import get_schema
from conductor_runtime.security import RuntimePolicy, assess_command


REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = REPO_ROOT / "benchmark-suites" / "auto-topology-tasks.json"
RETRY_TASKS_PATH = REPO_ROOT / "benchmark-suites" / "auto-topology-retry-canary.json"
FIXTURE_ROOT = REPO_ROOT / "benchmark-suites" / "implementation-canary"


class AutoTopologyCampaignTests(unittest.TestCase):
    def _campaign(self, *, repetitions=1, artifacts_dir="artifacts"):
        return build_auto_topology_campaign(
            load_parity_tasks(TASKS_PATH),
            run_id="auto-topology-test",
            seed=20260709,
            repetitions=repetitions,
            artifacts_dir=artifacts_dir,
            model="gpt-5.6-sol",
            effort="ultra",
            max_iterations=3,
            parallel_workers=4,
        )

    def _legacy_campaign(self):
        campaign = self._campaign()
        campaign["schema"] = AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V1
        campaign.pop("runtime_version")
        campaign.pop("runtime_build_sha256")
        for cohort in campaign["cohorts"]:
            cohort.pop("task_execution_sha256")
        validate_auto_topology_campaign(campaign)
        return campaign

    def _v2_campaign(self):
        campaign = self._campaign()
        campaign["schema"] = AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V2
        for cohort in campaign["cohorts"]:
            cohort.pop("task_execution_sha256")
        validate_auto_topology_campaign(campaign)
        return campaign

    def test_campaign_is_balanced_deterministic_and_schema_exported(self):
        campaign = self._campaign(repetitions=3)
        self.assertEqual(campaign["schema"], AUTO_TOPOLOGY_CAMPAIGN_SCHEMA)
        self.assertEqual(campaign["runtime_version"], __version__)
        self.assertEqual(campaign["runtime_build_sha256"], current_runtime_build_sha256())
        self.assertEqual(len(campaign["cohorts"]), 15)
        self.assertEqual(
            campaign["design"]["topology_first_counts"],
            {"direct": 5, "progressive": 5, "plan-first": 5},
        )
        repeated = self._campaign(repetitions=3)
        self.assertEqual(
            {key: value for key, value in campaign.items() if key != "generated_at_utc"},
            {key: value for key, value in repeated.items() if key != "generated_at_utc"},
        )
        validate_auto_topology_campaign(campaign)
        tasks = {
            task["id"]: task for task in load_parity_tasks(TASKS_PATH)["tasks"]
        }
        for cohort in campaign["cohorts"]:
            rendered = render_auto_topology_task_contract(tasks[cohort["task_id"]])
            self.assertEqual(
                cohort["task_execution_sha256"],
                hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            )
        self.assertEqual(
            get_schema("auto-topology-campaign")["properties"]["schema"]["const"],
            AUTO_TOPOLOGY_CAMPAIGN_SCHEMA,
        )
        self.assertIn(
            "task_execution_sha256",
            get_schema("auto-topology-campaign")["properties"]["cohorts"]["items"][
                "properties"
            ],
        )
        self.assertEqual(
            get_schema("auto-topology-campaign-v2")["properties"]["schema"]["const"],
            AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V2,
        )
        self.assertNotIn(
            "task_execution_sha256",
            get_schema("auto-topology-campaign-v2")["properties"]["cohorts"]["items"][
                "properties"
            ],
        )
        self.assertEqual(
            get_schema("auto-topology-campaign-v1")["properties"]["schema"]["const"],
            AUTO_TOPOLOGY_CAMPAIGN_SCHEMA_V1,
        )
        self.assertNotIn(
            "runtime_build_sha256",
            get_schema("auto-topology-campaign-v1")["properties"],
        )

        tampered = copy.deepcopy(campaign)
        tampered["cohorts"][0]["arm_order"].reverse()
        with self.assertRaisesRegex(ValidationError, "arm order does not match"):
            validate_auto_topology_campaign(tampered)

        tampered = copy.deepcopy(campaign)
        tampered["runtime_version"] = "current"
        with self.assertRaisesRegex(ValidationError, "runtime_version"):
            validate_auto_topology_campaign(tampered)

    def test_legacy_campaign_is_readable_but_cannot_launch_new_provider_work(self):
        policy = RuntimePolicy(
            allow_agent=True,
            allow_writes=True,
            approvals={
                AUTO_TOPOLOGY_ARM_APPROVAL,
                MODEL_WORKFLOW_EXECUTE_APPROVAL,
                MODEL_WORKFLOW_WRITE_APPROVAL,
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = self._legacy_campaign()
            campaign_path = root / "campaign.json"
            write_auto_topology_campaign(campaign, campaign_path)
            cohort = campaign["cohorts"][0]
            status = auto_topology_campaign_status(campaign_path)
            self.assertFalse(status["campaign"]["release_bound"])
            self.assertFalse(status["inference"]["release_bound"])
            self.assertIn(
                "release-unbound-legacy-campaign",
                status["inference"]["quality_latency_token_reasons"],
            )
            plan = run_auto_topology_arm(
                campaign_path=campaign_path,
                parity_tasks_path=TASKS_PATH,
                cohort_id=cohort["id"],
                topology="direct",
                fixture_root=FIXTURE_ROOT,
                dry_run=True,
            )
            self.assertEqual(plan["provider_launches"], 0)
            self.assertFalse(plan["runtime_release_compatible"])
            self.assertFalse(plan["runtime_build_compatible"])
            with self.assertRaisesRegex(ValidationError, "frozen auto topology campaign"):
                run_auto_topology_arm(
                    campaign_path=campaign_path,
                    parity_tasks_path=TASKS_PATH,
                    cohort_id=cohort["id"],
                    topology="direct",
                    fixture_root=FIXTURE_ROOT,
                    policy=policy,
                )
            self.assertFalse((root / cohort["artifacts"]["direct"]["launch"]).exists())

    def test_frozen_v2_campaign_is_release_bound_but_cannot_launch(self):
        policy = RuntimePolicy(
            allow_agent=True,
            allow_writes=True,
            approvals={
                AUTO_TOPOLOGY_ARM_APPROVAL,
                MODEL_WORKFLOW_EXECUTE_APPROVAL,
                MODEL_WORKFLOW_WRITE_APPROVAL,
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = self._v2_campaign()
            campaign_path = root / "campaign.json"
            write_auto_topology_campaign(campaign, campaign_path)
            cohort = campaign["cohorts"][0]
            status = auto_topology_campaign_status(campaign_path)
            self.assertTrue(status["campaign"]["release_bound"])
            self.assertTrue(status["inference"]["release_bound"])
            plan = run_auto_topology_arm(
                campaign_path=campaign_path,
                parity_tasks_path=TASKS_PATH,
                cohort_id=cohort["id"],
                topology="direct",
                fixture_root=FIXTURE_ROOT,
                dry_run=True,
            )
            self.assertEqual(plan["provider_launches"], 0)
            self.assertTrue(plan["runtime_release_compatible"])
            self.assertTrue(plan["runtime_build_compatible"])
            with patch("conductor_runtime.auto_topology_run.run_auto_orchestration") as provider:
                with self.assertRaisesRegex(ValidationError, "frozen auto topology campaign"):
                    run_auto_topology_arm(
                        campaign_path=campaign_path,
                        parity_tasks_path=TASKS_PATH,
                        cohort_id=cohort["id"],
                        topology="direct",
                        fixture_root=FIXTURE_ROOT,
                        policy=policy,
                    )
            provider.assert_not_called()
            self.assertFalse((root / cohort["artifacts"]["direct"]["launch"]).exists())

    def test_v3_binds_and_forwards_the_full_public_task_contract(self):
        tasks = load_parity_tasks(TASKS_PATH)
        task = next(item for item in tasks["tasks"] if item["id"] == "slug-normalization")
        campaign = self._campaign()
        cohort = next(
            item for item in campaign["cohorts"] if item["task_id"] == task["id"]
        )
        rendered = render_auto_topology_task_contract(task)
        self.assertEqual(rendered, rendered.strip())
        self.assertNotIn("held-out-correctness", rendered)
        self.assertNotIn("max_score", rendered)
        for value in [task["prompt"], *task["success_criteria"], *task["required_evidence"]]:
            self.assertIn(value, rendered)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path = root / "campaign.json"
            write_auto_topology_campaign(campaign, campaign_path)
            policy = RuntimePolicy(
                allow_agent=True,
                allow_writes=True,
                approvals={
                    AUTO_TOPOLOGY_ARM_APPROVAL,
                    MODEL_WORKFLOW_EXECUTE_APPROVAL,
                    MODEL_WORKFLOW_WRITE_APPROVAL,
                },
            )
            with patch(
                "conductor_runtime.auto_topology_run.run_auto_orchestration",
                side_effect=RuntimeError("captured full task contract"),
            ) as provider:
                with self.assertRaisesRegex(RuntimeError, "captured full task contract"):
                    run_auto_topology_arm(
                        campaign_path=campaign_path,
                        parity_tasks_path=TASKS_PATH,
                        cohort_id=cohort["id"],
                        topology="direct",
                        fixture_root=FIXTURE_ROOT,
                        policy=policy,
                    )
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(provider.call_args.kwargs["task"], rendered)

    def test_v3_execution_contract_tamper_fails_before_provider_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = self._campaign()
            campaign["cohorts"][0]["task_execution_sha256"] = "0" * 64
            campaign_path = root / "campaign.json"
            write_auto_topology_campaign(campaign, campaign_path)
            cohort = campaign["cohorts"][0]
            with patch("conductor_runtime.auto_topology_run.run_auto_orchestration") as provider:
                with self.assertRaisesRegex(ValidationError, "execution contract"):
                    run_auto_topology_arm(
                        campaign_path=campaign_path,
                        parity_tasks_path=TASKS_PATH,
                        cohort_id=cohort["id"],
                        topology="direct",
                        fixture_root=FIXTURE_ROOT,
                        dry_run=True,
                    )
            provider.assert_not_called()

    def test_exact_runtime_mismatch_fails_before_policy_or_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = self._campaign()
            campaign["runtime_build_sha256"] = "0" * 64
            campaign_path = root / "campaign.json"
            write_auto_topology_campaign(campaign, campaign_path)
            cohort = campaign["cohorts"][0]
            plan = run_auto_topology_arm(
                campaign_path=campaign_path,
                parity_tasks_path=TASKS_PATH,
                cohort_id=cohort["id"],
                topology="direct",
                fixture_root=FIXTURE_ROOT,
                dry_run=True,
            )
            self.assertTrue(plan["runtime_release_compatible"])
            self.assertFalse(plan["runtime_build_compatible"])
            with patch("conductor_runtime.auto_topology_run.run_auto_orchestration") as provider:
                with self.assertRaisesRegex(ValidationError, "exact runtime build"):
                    run_auto_topology_arm(
                        campaign_path=campaign_path,
                        parity_tasks_path=TASKS_PATH,
                        cohort_id=cohort["id"],
                        topology="direct",
                        fixture_root=FIXTURE_ROOT,
                        policy=RuntimePolicy(),
                    )
            provider.assert_not_called()
            self.assertFalse((root / cohort["artifacts"]["direct"]["launch"]).exists())

    def test_retry_canary_is_contract_identical_and_balances_three_arms(self):
        full = load_parity_tasks(TASKS_PATH)
        retry = load_parity_tasks(RETRY_TASKS_PATH)
        source_task = next(task for task in full["tasks"] if task["id"] == "retry-policy-migration")
        self.assertEqual(retry["tasks"], [source_task])
        campaign = build_auto_topology_campaign(
            retry,
            run_id="auto-topology-retry-test",
            seed=84020260710,
            repetitions=3,
            artifacts_dir="artifacts",
            model="gpt-5.6-sol",
            effort="ultra",
            max_iterations=3,
            parallel_workers=4,
        )
        self.assertEqual(campaign["design"]["cohort_count"], 3)
        self.assertEqual(campaign["design"]["arm_count"], 9)
        self.assertEqual(
            campaign["design"]["topology_first_counts"],
            {"direct": 1, "progressive": 1, "plan-first": 1},
        )

    def test_retry_canary_publicly_documents_held_out_input_contract(self):
        module = ast.parse(
            (FIXTURE_ROOT / "retry-policy" / "source" / "service.py").read_text(
                encoding="utf-8"
            )
        )
        function = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "should_retry"
        )
        contract = ast.get_docstring(function) or ""
        self.assertIn("non-negative integer", contract)
        self.assertIn("booleans are not", contract)
        self.assertIn("TypeError", contract)
        self.assertIn("ValueError", contract)

    def test_empty_status_and_cli_dry_run_launch_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path = root / "campaign.json"
            write_auto_topology_campaign(self._campaign(), campaign_path)
            status = auto_topology_campaign_status(campaign_path)
            self.assertEqual(status["lifecycle"]["completed"], 0)
            self.assertEqual(status["arm_lifecycle"]["pending"], 15)
            self.assertFalse(status["inference"]["recommendation_supported"])
            self.assertFalse(status["inference"]["quality_latency_token_recommendation_supported"])
            self.assertFalse(status["inference"]["dollar_cost_comparable"])
            self.assertTrue(status["campaign"]["release_bound"])
            self.assertFalse(status["inference"]["release_uniform"])

            output = StringIO()
            with redirect_stdout(output):
                code = cli_main(
                    [
                        "run-auto-topology-arm",
                        str(campaign_path),
                        str(TASKS_PATH),
                        "--next",
                        "--dry-run",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("Runtime compatibility: release=yes build=yes", output.getvalue())
            self.assertIn("Provider launches: 0", output.getvalue())
            self.assertEqual(auto_topology_campaign_status(campaign_path)["arm_lifecycle"]["pending"], 15)

    def test_failed_planner_attempt_is_not_reported_as_zero_calls(self):
        goal = {
            "planner": {"kind": "model-authored-per-iteration"},
            "iterations": [{"iteration": 1, "status": "workflow_failed"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources, runs, receipts = derive_auto_topology_resources(
                goal,
                workspace=root,
                runs_dir=root / "runs",
                planning_dir=root / "planning",
                duration_ms=1,
            )
        self.assertEqual(resources["planning_provider_calls"], 1)
        self.assertEqual(resources["total_provider_calls"], 1)
        self.assertFalse(resources["token_observed"])
        self.assertFalse(resources["cost_observed"])
        self.assertEqual(runs, [])
        self.assertEqual(receipts, [])

    def test_arm_duration_prefers_full_goal_interval_over_final_receipt_timestamp(self):
        receipt = {
            "generated_at_utc": "2026-07-10T15:46:16.216Z",
            "updated_at_utc": "2026-07-10T15:46:16.216Z",
        }
        goal = {
            "started_at_utc": "2026-07-10T15:44:28.086Z",
            "finished_at_utc": "2026-07-10T15:46:16.215Z",
        }
        self.assertEqual(_receipt_duration_ms(receipt, goal=goal), 108129)
        self.assertEqual(_receipt_duration_ms(receipt), 0)

    def test_command_policy_separates_dry_run_from_provider_execution(self):
        base = [
            "python3",
            "-B",
            "-m",
            "conductor_runtime",
            "run-auto-topology-arm",
            "campaign.json",
            "tasks.json",
            "--next",
        ]
        dry = assess_command([*base, "--dry-run"])
        self.assertFalse(dry.writes)
        self.assertFalse(dry.network)
        self.assertFalse(dry.destructive)
        live = assess_command(base)
        self.assertTrue(live.writes)
        self.assertTrue(live.network)
        self.assertFalse(live.destructive)

    def test_high_risk_task_requires_approval_before_launch_receipt(self):
        retry = load_parity_tasks(RETRY_TASKS_PATH)
        campaign = build_auto_topology_campaign(
            retry,
            run_id="auto-topology-high-risk-test",
            seed=85020260710,
            repetitions=1,
            artifacts_dir="artifacts",
            model="gpt-5.6-sol",
            effort="ultra",
            max_iterations=3,
            parallel_workers=4,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path = root / "campaign.json"
            write_auto_topology_campaign(campaign, campaign_path)
            cohort = campaign["cohorts"][0]
            policy = RuntimePolicy(
                allow_agent=True,
                allow_writes=True,
                allow_parallel=True,
                approvals={
                    AUTO_TOPOLOGY_ARM_APPROVAL,
                    MODEL_WORKFLOW_EXECUTE_APPROVAL,
                    MODEL_WORKFLOW_WRITE_APPROVAL,
                },
            )
            with self.assertRaisesRegex(PolicyError, "--approve high-risk"):
                run_auto_topology_arm(
                    campaign_path=campaign_path,
                    parity_tasks_path=RETRY_TASKS_PATH,
                    cohort_id=cohort["id"],
                    topology="progressive",
                    fixture_root=FIXTURE_ROOT,
                    policy=policy,
                )
            self.assertFalse((root / cohort["artifacts"]["progressive"]["launch"]).exists())

    def test_planner_failure_class_is_visible_without_persisting_stderr(self):
        campaign = self._campaign()
        cohort = next(item for item in campaign["cohorts"] if item["task_id"] == "slug-normalization")
        policy = RuntimePolicy(
            allow_agent=True,
            allow_writes=True,
            allow_parallel=True,
            approvals={
                AUTO_TOPOLOGY_ARM_APPROVAL,
                MODEL_WORKFLOW_EXECUTE_APPROVAL,
                MODEL_WORKFLOW_WRITE_APPROVAL,
            },
        )

        def failed_planner(argv, **kwargs):
            return ProcessResult(17, "", "token=topology-planner-secret")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path = root / "campaign.json"
            write_auto_topology_campaign(campaign, campaign_path)
            with patch("conductor_runtime.model_planner.run_process", side_effect=failed_planner):
                result = run_auto_topology_arm(
                    campaign_path=campaign_path,
                    parity_tasks_path=TASKS_PATH,
                    cohort_id=cohort["id"],
                    topology="plan-first",
                    fixture_root=FIXTURE_ROOT,
                    policy=policy,
                )

            self.assertEqual(result["outcome"]["score"], 0)
            status = auto_topology_campaign_status(campaign_path)
            arm = next(item for item in status["cohorts"] if item["id"] == cohort["id"])["arms"][
                "plan-first"
            ]
            self.assertEqual(arm["status"], "completed")
            self.assertEqual(arm["reason"], "ModelPlannerProviderError")
            goal_path = root / cohort["artifacts"]["plan-first"]["goal"]
            goal = json.loads(goal_path.read_text(encoding="utf-8"))
            self.assertEqual(
                goal["iterations"][0]["error_class"],
                "ModelPlannerProviderError",
            )
            planning_dir = root / cohort["artifacts"]["plan-first"]["planning"]
            run_receipt = next(planning_dir.glob("*.run-receipt.json"))
            receipt_text = run_receipt.read_text(encoding="utf-8")
            self.assertIn("ModelPlannerProviderError", receipt_text)
            self.assertNotIn("topology-planner-secret", receipt_text)

    def test_dominance_gate_requires_noninferiority_and_resource_improvement(self):
        def pair(left, right, score_low, score_high, token, duration, cost):
            return {
                "left": left,
                "right": right,
                "completed_pairs": 3,
                "score_delta_mean": 0,
                "score_delta_ci95_low": score_low,
                "score_delta_ci95_high": score_high,
                "token_delta_mean": token,
                "duration_delta_ms_mean": duration,
                "cost_delta_usd_mean": cost,
            }

        summaries = [
            pair("direct", "progressive", 0, 0, -10, -5, -0.01),
            pair("direct", "plan-first", 0, 0, -20, -10, -0.02),
            pair("progressive", "plan-first", 0, 0, -10, -5, -0.01),
        ]
        self.assertEqual(_dominant_topology(summaries), "direct")
        tied = [pair(item["left"], item["right"], 0, 0, 0, 0, 0) for item in summaries]
        self.assertIsNone(_dominant_topology(tied))

        missing_cost = [dict(item, cost_delta_usd_mean=None) for item in summaries]
        self.assertIsNone(_dominant_topology(missing_cost))
        self.assertEqual(_dominant_topology(missing_cost, require_cost=False), "direct")

    def test_inference_separates_measured_resources_from_unavailable_dollars(self):
        pairwise = [
            {
                "left": "direct",
                "right": "progressive",
                "completed_pairs": 3,
                "score_delta_mean": 0,
                "score_delta_ci95_low": 0,
                "score_delta_ci95_high": 0,
                "token_delta_mean": -10,
                "duration_delta_ms_mean": -5,
                "cost_delta_usd_mean": None,
            },
            {
                "left": "direct",
                "right": "plan-first",
                "completed_pairs": 3,
                "score_delta_mean": 0,
                "score_delta_ci95_low": 0,
                "score_delta_ci95_high": 0,
                "token_delta_mean": -20,
                "duration_delta_ms_mean": -10,
                "cost_delta_usd_mean": None,
            },
            {
                "left": "progressive",
                "right": "plan-first",
                "completed_pairs": 3,
                "score_delta_mean": 0,
                "score_delta_ci95_low": 0,
                "score_delta_ci95_high": 0,
                "token_delta_mean": -10,
                "duration_delta_ms_mean": -5,
                "cost_delta_usd_mean": None,
            },
        ]
        cohorts = [
            {
                "arms": {
                    "progressive": {
                        "status": "completed",
                        "outcome": {"score": 10},
                        "resources": {"token_observed": True, "total_tokens": 10},
                    },
                    "plan-first": {
                        "status": "completed",
                        "outcome": {"score": 10},
                        "resources": {"token_observed": True, "total_tokens": 20},
                    },
                }
            }
            for _ in range(3)
        ]
        inference = _topology_inference(
            {
                "schema": AUTO_TOPOLOGY_CAMPAIGN_SCHEMA,
                "design": {"cohort_count": 3, "repetitions": 3},
            },
            {"completed": 3, "invalid": 0},
            {"completed": 9, "invalid": 0},
            {"token_complete": True, "cost_complete": False},
            pairwise,
            cohorts,
        )
        self.assertFalse(inference["recommendation_supported"])
        self.assertIsNone(inference["recommended_topology"])
        self.assertIn("cost-observability-incomplete", inference["reasons"])
        self.assertTrue(inference["quality_latency_token_recommendation_supported"])
        self.assertEqual(inference["quality_latency_token_recommended_topology"], "direct")
        self.assertEqual(inference["quality_latency_token_reasons"], [])
        self.assertFalse(inference["dollar_cost_comparable"])
        self.assertTrue(inference["release_bound"])
        self.assertTrue(inference["release_uniform"])

    def test_direct_arm_is_exactly_once_and_no_change_scores_zero(self):
        worker_calls = []

        def fake_worker(argv, **kwargs):
            worker_calls.append(list(argv))
            output_path = Path(argv[argv.index("--output-last-message") + 1])
            output_path.write_text("bounded direct result\n", encoding="utf-8")
            return ProcessResult(
                0,
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
                    }
                )
                + "\n",
                "",
            )

        policy = RuntimePolicy(
            allow_agent=True,
            allow_writes=True,
            approvals={
                AUTO_TOPOLOGY_ARM_APPROVAL,
                MODEL_WORKFLOW_EXECUTE_APPROVAL,
                MODEL_WORKFLOW_WRITE_APPROVAL,
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path = root / "campaign.json"
            campaign = self._campaign()
            write_auto_topology_campaign(campaign, campaign_path)
            cohort = next(
                item for item in campaign["cohorts"] if item["task_id"] == "slug-normalization"
            )
            with patch.dict(os.environ, {"CODEX_CONDUCTOR_HOME": str(root / "home")}, clear=False):
                with patch("conductor_runtime.model_planner.run_process") as planner_process:
                    with patch("conductor_runtime.runner.run_process", side_effect=fake_worker):
                        with patch(
                            "conductor_runtime.goal_loop.run_process",
                            return_value=ProcessResult(0, "verified", ""),
                        ):
                            result = run_auto_topology_arm(
                                campaign_path=campaign_path,
                                parity_tasks_path=TASKS_PATH,
                                cohort_id=cohort["id"],
                                topology="direct",
                                fixture_root=FIXTURE_ROOT,
                                policy=policy,
                            )

                planner_process.assert_not_called()
                self.assertEqual(result["status"], "completed")
                result_path = root / cohort["artifacts"]["direct"]["result"]
                raw_result = result_path.read_text(encoding="utf-8")
                stored_result = json.loads(raw_result)
                self.assertEqual(stored_result["schema"], AUTO_TOPOLOGY_RESULT_SCHEMA)
                self.assertEqual(stored_result["runtime_version"], __version__)
                self.assertEqual(
                    stored_result["runtime_build_sha256"],
                    campaign["runtime_build_sha256"],
                )
                self.assertEqual(result["outcome"]["score"], 0)
                self.assertFalse(result["outcome"]["passed"])
                self.assertEqual(result["resources"]["total_provider_calls"], 1)
                self.assertEqual(result["resources"]["total_tokens"], 12)
                self.assertFalse(result["resources"]["cost_observed"])
                self.assertEqual(len(worker_calls), 1)

                repeated = run_auto_topology_arm(
                    campaign_path=campaign_path,
                    parity_tasks_path=TASKS_PATH,
                    cohort_id=cohort["id"],
                    topology="direct",
                    fixture_root=FIXTURE_ROOT,
                    policy=policy,
                )
                self.assertEqual(repeated["provider_launches"], 0)
                self.assertEqual(len(worker_calls), 1)
                status = auto_topology_campaign_status(campaign_path)
                arm = next(item for item in status["cohorts"] if item["id"] == cohort["id"])["arms"]["direct"]
                self.assertEqual(arm["status"], "completed")
                self.assertEqual(arm["outcome"]["score"], 0)
                stored_result["runtime_build_sha256"] = "f" * 64
                result_path.write_text(
                    json.dumps(stored_result, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                release_drifted = auto_topology_campaign_status(campaign_path)
                release_drifted_arm = next(
                    item for item in release_drifted["cohorts"] if item["id"] == cohort["id"]
                )["arms"]["direct"]
                self.assertEqual(release_drifted_arm["status"], "invalid")
                result_path.write_text(raw_result, encoding="utf-8")
                launch_path = root / cohort["artifacts"]["direct"]["launch"]
                launch = json.loads(launch_path.read_text(encoding="utf-8"))
                launch["auto_receipt_sha256"] = "0" * 64
                launch_path.write_text(json.dumps(launch, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                drifted = auto_topology_campaign_status(campaign_path)
                drifted_arm = next(
                    item for item in drifted["cohorts"] if item["id"] == cohort["id"]
                )["arms"]["direct"]
                self.assertEqual(drifted_arm["status"], "invalid")

    def test_all_three_topologies_form_one_matched_resource_pair(self):
        generated = {
            "schema": "conductor.workflow.v1",
            "name": "r001-slug-normalization-plan-first",
            "description": "Bounded topology campaign implementation.",
            "mode": "workspace_write",
            "risk": "medium",
            "max_workers": 2,
            "steps": [
                {
                    "id": "write-gate",
                    "kind": "manual_gate",
                    "risk": "medium",
                    "approval_id": MODEL_WORKFLOW_WRITE_APPROVAL,
                    "prompt": "Approve the bounded campaign write.",
                },
                {
                    "id": "implement",
                    "kind": "codex_exec",
                    "description": "Complete the bounded campaign task.",
                    "risk": "medium",
                    "depends_on": ["write-gate"],
                    "sandbox": "workspace-write",
                    "capture": "implementation.md",
                    "prompt": "Implement the scoped task and report verification.",
                },
                {
                    "id": "final-review",
                    "kind": "codex_exec",
                    "risk": "medium",
                    "depends_on": ["implement"],
                    "sandbox": "read-only",
                    "capture": "final-review.md",
                    "prompt": "Review the implementation evidence and report remaining gaps.",
                },
            ],
        }
        planner_calls = []
        worker_calls = []

        def fake_planner(argv, **kwargs):
            planner_calls.append(list(argv))
            output_path = Path(argv[argv.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(generated), encoding="utf-8")
            return ProcessResult(
                0,
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "thread.started",
                                "thread_id": "019f0000-0000-7000-8000-000000000001",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {"input_tokens": 6, "output_tokens": 4, "total_tokens": 10},
                            }
                        ),
                    ]
                )
                + "\n",
                "",
            )

        def fake_worker(argv, **kwargs):
            worker_calls.append(list(argv))
            output_path = Path(argv[argv.index("--output-last-message") + 1])
            output_path.write_text("topology worker result\n", encoding="utf-8")
            return ProcessResult(
                0,
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
                    }
                )
                + "\n",
                "",
            )

        approvals = {
            AUTO_TOPOLOGY_ARM_APPROVAL,
            MODEL_WORKFLOW_EXECUTE_APPROVAL,
            MODEL_WORKFLOW_WRITE_APPROVAL,
        }
        arm_results = {}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign_path = root / "campaign.json"
            campaign = self._campaign()
            write_auto_topology_campaign(campaign, campaign_path)
            cohort = next(item for item in campaign["cohorts"] if item["task_id"] == "slug-normalization")
            with patch.dict(os.environ, {"CODEX_CONDUCTOR_HOME": str(root / "home")}, clear=False):
                with patch("conductor_runtime.model_planner.run_process", side_effect=fake_planner):
                    with patch("conductor_runtime.runner.run_process", side_effect=fake_worker):
                        with patch(
                            "conductor_runtime.goal_loop.run_process",
                            return_value=ProcessResult(0, "verified", ""),
                        ):
                            for topology in ["direct", "progressive", "plan-first"]:
                                arm_results[topology] = run_auto_topology_arm(
                                    campaign_path=campaign_path,
                                    parity_tasks_path=TASKS_PATH,
                                    cohort_id=cohort["id"],
                                    topology=topology,
                                    fixture_root=FIXTURE_ROOT,
                                    policy=RuntimePolicy(
                                        allow_agent=True,
                                        allow_writes=True,
                                        allow_parallel=topology != "direct",
                                        approvals=approvals,
                                    ),
                                )

            self.assertEqual(len(planner_calls), 1)
            self.assertEqual(len(worker_calls), 4, arm_results)
            status = auto_topology_campaign_status(campaign_path)
            matched = next(item for item in status["cohorts"] if item["id"] == cohort["id"])
            self.assertEqual(matched["status"], "completed")
            for topology in ["direct", "progressive", "plan-first"]:
                self.assertEqual(matched["arms"][topology]["outcome"]["score"], 0)
            progressive_vs_plan = next(
                item
                for item in status["pairwise"]
                if item["left"] == "progressive" and item["right"] == "plan-first"
            )
            self.assertEqual(progressive_vs_plan["completed_pairs"], 1)
            self.assertEqual(progressive_vs_plan["token_comparable_pairs"], 1)
            self.assertEqual(progressive_vs_plan["token_delta_mean"], -22)


if __name__ == "__main__":
    unittest.main()
