import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from conductor_runtime.benchmark import (
    load_parity_tasks,
    parity_prompt_environment,
    render_parity_task_contract,
)
from conductor_runtime.cli import main as cli_main
from conductor_runtime.claude_live import analyze_claude_output
from conductor_runtime.build_identity import current_runtime_build_sha256
from conductor_runtime.errors import ValidationError
from conductor_runtime.model_planner import plan_direct_model_workflow
from conductor_runtime.readonly_parity_campaign import (
    READONLY_PARITY_CAMPAIGN_SCHEMA,
    READONLY_PARITY_CAMPAIGN_SCHEMA_V1,
    _pairwise_summary,
    build_readonly_parity_campaign,
    load_readonly_parity_result,
    readonly_parity_campaign_status,
    validate_readonly_parity_campaign,
    write_readonly_parity_campaign,
)
from conductor_runtime.readonly_parity_run import (
    READONLY_PARITY_ARM_APPROVAL,
    run_readonly_parity_arm,
)
from conductor_runtime.schemas import get_schema
from conductor_runtime.security import RuntimePolicy, assess_command
from conductor_runtime.runner import ProcessResult
from conductor_runtime.staged_workspace import snapshot_workspace
from tools.evaluate_readonly_diagnostic import (
    EVALUATOR_IDENTITY_V1,
    READONLY_DIAGNOSTIC_EVALUATION_SCHEMA,
    READONLY_DIAGNOSTIC_EVALUATION_SCHEMA_V1,
    TASK_SPECS_V1,
    evaluate_readonly_diagnostic,
    validate_readonly_diagnostic_evaluation,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = REPO_ROOT / "benchmark-suites" / "read-only-diagnostic-tasks.json"
PRODUCT_TASKS_PATH = REPO_ROOT / "benchmark-suites" / "read-only-diagnostic-product-tasks.json"
FIXTURE_ROOT = REPO_ROOT / "benchmark-suites" / "implementation-canary"


def retry_answer():
    return json.dumps(
        {
            "findings": [
                {
                    "file": "policy.py",
                    "line": 23,
                    "severity": "high",
                    "summary": "normalize_policy is an unimplemented canonical v1 to v2 migration stub",
                    "evidence": "dict(raw) only returns a copy with no validation or normalization",
                },
                {
                    "file": "service.py",
                    "line": 7,
                    "severity": "high",
                    "summary": "Retry boundary reads retries instead of canonical max_attempts",
                    "evidence": "completed_attempts must be compared with max_attempts, but retries is absent",
                },
                {
                    "file": "service.py",
                    "line": 4,
                    "severity": "medium",
                    "summary": "completed_attempts receives no input validation",
                    "evidence": "The function must reject boolean, non-integer, and negative completed attempts",
                },
            ],
            "verification": ["Inspected production contracts and visible tests."],
            "residual_risk": "No held-out tests were inspected.",
        },
        sort_keys=True,
    )


def event_routing_answer():
    return json.dumps(
        {
            "findings": [
                {
                    "file": "events/names.py",
                    "line": 13,
                    "severity": "high",
                    "summary": "canonical_event_name violates its canonical separator and Unicode contract",
                    "evidence": "It uses lower instead of Unicode casefold and fails to normalize dot, hyphen, and underscore separator runs.",
                },
                {
                    "file": "events/router.py",
                    "line": 12,
                    "severity": "high",
                    "summary": "EventRouter bypasses canonical_event_name",
                    "evidence": "register and dispatch hand-roll incompatible handler keys instead of using the shared canonicalizer.",
                },
                {
                    "file": "events/subscriptions.py",
                    "line": 9,
                    "severity": "medium",
                    "summary": "Subscription container validation is missing",
                    "evidence": "The documented list-only boundary is not validated, so tuple and other iterable containers are accepted independently of per-entry canonicalization.",
                },
                {
                    "file": "events/subscriptions.py",
                    "line": 10,
                    "severity": "high",
                    "summary": "Subscription dedupe bypasses canonical_event_name",
                    "evidence": "The dedupe key uses local string replacements rather than the canonical event-name contract.",
                },
            ],
            "verification": ["Inspected production contracts and visible tests."],
            "residual_risk": "Held-out behavior was not executed.",
        },
        sort_keys=True,
    )


class ReadonlyParityTests(unittest.TestCase):
    def _campaign(
        self,
        *,
        repetitions=2,
        artifacts_dir="artifacts",
        budget_profile="matched-v1",
        tasks_path=TASKS_PATH,
    ):
        return build_readonly_parity_campaign(
            load_parity_tasks(tasks_path),
            run_id="readonly-parity-test",
            seed=20260711,
            repetitions=repetitions,
            artifacts_dir=artifacts_dir,
            codex_model="gpt-5.6-sol",
            codex_effort="ultra",
            budget_profile=budget_profile,
        )

    def test_campaign_is_balanced_deterministic_and_schema_exported(self):
        campaign = self._campaign()
        self.assertEqual(campaign["schema"], READONLY_PARITY_CAMPAIGN_SCHEMA)
        self.assertEqual(campaign["design"]["cohort_count"], 6)
        self.assertEqual(campaign["design"]["arm_count"], 18)
        self.assertEqual(campaign["design"]["budget_profile"], "matched-v1")
        self.assertEqual(
            [system["max_tokens"] for system in campaign["systems"][:2]],
            [18000, 18000],
        )
        self.assertEqual(
            campaign["design"]["system_first_counts"],
            {"serial-codex": 2, "native-codex": 2, "claude-sonnet": 2},
        )
        repeated = self._campaign()
        self.assertEqual(
            {key: value for key, value in campaign.items() if key != "generated_at_utc"},
            {key: value for key, value in repeated.items() if key != "generated_at_utc"},
        )
        validate_readonly_parity_campaign(campaign)
        self.assertEqual(
            get_schema("readonly-parity-campaign")["properties"]["schema"]["const"],
            READONLY_PARITY_CAMPAIGN_SCHEMA,
        )
        self.assertEqual(campaign["runtime_build_sha256"], current_runtime_build_sha256())
        tampered = copy.deepcopy(campaign)
        tampered["cohorts"][0]["arm_order"].reverse()
        with self.assertRaisesRegex(ValidationError, "arm order"):
            validate_readonly_parity_campaign(tampered)

    def test_v1_campaign_compatibility_is_exact_and_schema_exported(self):
        current = self._campaign(repetitions=1)
        legacy = copy.deepcopy(current)
        legacy["schema"] = READONLY_PARITY_CAMPAIGN_SCHEMA_V1
        del legacy["design"]["budget_profile"]
        validate_readonly_parity_campaign(legacy)
        self.assertEqual(
            get_schema("readonly-parity-campaign-v1")["properties"]["schema"]["const"],
            READONLY_PARITY_CAMPAIGN_SCHEMA_V1,
        )
        current_missing_profile = copy.deepcopy(current)
        del current_missing_profile["design"]["budget_profile"]
        with self.assertRaisesRegex(ValidationError, "fields"):
            validate_readonly_parity_campaign(current_missing_profile)
        legacy_with_profile = copy.deepcopy(legacy)
        legacy_with_profile["design"]["budget_profile"] = "matched-v1"
        with self.assertRaisesRegex(ValidationError, "fields"):
            validate_readonly_parity_campaign(legacy_with_profile)

    def test_product_profile_caps_cli_and_dry_runs_are_bound_without_provider_launches(self):
        campaign = self._campaign(
            repetitions=1,
            budget_profile="product-v1",
            tasks_path=PRODUCT_TASKS_PATH,
        )
        self.assertEqual(campaign["design"]["budget_profile"], "product-v1")
        self.assertEqual(
            [system["max_tokens"] for system in campaign["systems"][:2]],
            [20000, 42000],
        )
        self.assertEqual({cohort["budget"]["max_tokens"] for cohort in campaign["cohorts"]}, {42000})
        validate_readonly_parity_campaign(campaign)

        wrong_fixture = load_parity_tasks(TASKS_PATH)
        with self.assertRaisesRegex(ValidationError, "product-v1 token budget"):
            build_readonly_parity_campaign(
                wrong_fixture,
                run_id="wrong-product-fixture",
                seed=1,
                repetitions=1,
                artifacts_dir="artifacts",
                codex_model="gpt-5.6-sol",
                codex_effort="ultra",
                budget_profile="product-v1",
            )
        for system_index, invalid_cap in [(0, 18000), (1, 18000), (1, 20000)]:
            tampered = copy.deepcopy(campaign)
            tampered["systems"][system_index]["max_tokens"] = invalid_cap
            with self.assertRaisesRegex(ValidationError, "system contract"):
                validate_readonly_parity_campaign(tampered)
        tampered_budget = copy.deepcopy(campaign)
        tampered_budget["cohorts"][0]["budget"]["max_tokens"] = 18000
        with self.assertRaisesRegex(ValidationError, "budget profile"):
            validate_readonly_parity_campaign(tampered_budget)

        with tempfile.TemporaryDirectory() as temporary:
            campaign_path = Path(temporary) / "campaign.json"
            write_readonly_parity_campaign(campaign, campaign_path)
            cohort = campaign["cohorts"][0]
            with patch("conductor_runtime.readonly_parity_run.shutil.which") as provider_discovery:
                serial_plan = run_readonly_parity_arm(
                    campaign_path=campaign_path,
                    parity_tasks_path=PRODUCT_TASKS_PATH,
                    cohort_id=cohort["id"],
                    system="serial-codex",
                    dry_run=True,
                )
                native_plan = run_readonly_parity_arm(
                    campaign_path=campaign_path,
                    parity_tasks_path=PRODUCT_TASKS_PATH,
                    cohort_id=cohort["id"],
                    system="native-codex",
                    dry_run=True,
                )
            provider_discovery.assert_not_called()
            self.assertEqual((serial_plan["token_cap"], serial_plan["provider_launches"]), (20000, 0))
            self.assertEqual((native_plan["token_cap"], native_plan["provider_launches"]), (42000, 0))

        output = StringIO()
        with redirect_stdout(output):
            code = cli_main(
                [
                    "plan-readonly-parity-campaign",
                    str(PRODUCT_TASKS_PATH),
                    "--seed",
                    "7",
                    "--repetitions",
                    "1",
                    "--budget-profile",
                    "product-v1",
                ]
            )
        self.assertEqual(code, 0)
        cli_campaign = json.loads(output.getvalue())
        self.assertEqual(cli_campaign["name"], "product-readonly-parity-campaign")
        self.assertEqual(cli_campaign["design"]["budget_profile"], "product-v1")
        self.assertEqual(cli_campaign["systems"][1]["max_tokens"], 42000)

    def test_hidden_evaluator_scores_root_causes_without_retaining_answer_text(self):
        source = FIXTURE_ROOT / "retry-policy" / "source"
        source_sha256 = snapshot_workspace(source).tracked_fingerprint_sha256
        answer = retry_answer()
        evaluation = evaluate_readonly_diagnostic(
            "diagnose-retry-policy",
            answer,
            source,
            expected_source_sha256=source_sha256,
            provider_success=True,
        )
        self.assertEqual(evaluation["score"], 10)
        self.assertTrue(evaluation["passed"])
        self.assertEqual(evaluation["schema"], READONLY_DIAGNOSTIC_EVALUATION_SCHEMA)
        self.assertNotIn("dict(raw)", json.dumps(evaluation))
        invalid = evaluate_readonly_diagnostic(
            "diagnose-retry-policy",
            "not json",
            source,
            expected_source_sha256=source_sha256,
            provider_success=True,
        )
        self.assertEqual(invalid["score"], 0)
        self.assertEqual(invalid["parse_status"], "invalid")

        legacy = copy.deepcopy(evaluation)
        legacy["schema"] = READONLY_DIAGNOSTIC_EVALUATION_SCHEMA_V1
        legacy["evaluator"] = {"identity": EVALUATOR_IDENTITY_V1, "independent": True}
        legacy["answer_key_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    "evaluator": EVALUATOR_IDENTITY_V1,
                    "task_id": "diagnose-retry-policy",
                    "findings": TASK_SPECS_V1["diagnose-retry-policy"]["findings"],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        unsigned = dict(legacy)
        unsigned.pop("evaluation_sha256")
        legacy["evaluation_sha256"] = hashlib.sha256(
            json.dumps(
                unsigned,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        validate_readonly_diagnostic_evaluation(legacy)

    def test_hidden_evaluator_v2_separates_subscription_container_validation(self):
        source = FIXTURE_ROOT / "event-routing" / "source"
        source_sha256 = snapshot_workspace(source).tracked_fingerprint_sha256
        answer = event_routing_answer()
        evaluation = evaluate_readonly_diagnostic(
            "diagnose-event-routing",
            answer,
            source,
            expected_source_sha256=source_sha256,
            provider_success=True,
        )
        self.assertEqual(evaluation["schema"], READONLY_DIAGNOSTIC_EVALUATION_SCHEMA)
        self.assertEqual(evaluation["score"], 10)
        self.assertEqual(evaluation["unmatched_finding_count"], 0)
        self.assertEqual(
            evaluation["matched_finding_ids"],
            [
                "canonical-name-contract",
                "router-bypasses-canonicalizer",
                "subscription-bypasses-contract",
                "subscription-container-validation",
            ],
        )

        missing_container = json.loads(answer)
        missing_container["findings"] = [
            finding
            for finding in missing_container["findings"]
            if not (
                finding["file"] == "events/subscriptions.py"
                and finding["line"] == 9
            )
        ]
        incomplete = evaluate_readonly_diagnostic(
            "diagnose-event-routing",
            json.dumps(missing_container, sort_keys=True),
            source,
            expected_source_sha256=source_sha256,
            provider_success=True,
        )
        self.assertEqual(incomplete["score"], 9)
        self.assertNotIn("subscription-container-validation", incomplete["matched_finding_ids"])

    def test_direct_planner_accepts_matched_override_and_rejects_native_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            policy = RuntimePolicy(allow_agent=True)
            draft = plan_direct_model_workflow(
                task="Inspect the workspace and return one fact.",
                workspace=workspace,
                output_path=root / "workflow.json",
                receipt_path=root / "receipt.json",
                policy=policy,
                max_tokens=18000,
            )
            self.assertEqual(draft.workflow["steps"][0]["max_tokens"], 18000)
            for effort, expected_cap in {
                "low": 4000,
                "medium": 8000,
                "high": 12000,
                "xhigh": 16000,
                "ultra": 20000,
            }.items():
                effort_draft = plan_direct_model_workflow(
                    task="Inspect the workspace and return one fact.",
                    workspace=workspace,
                    output_path=root / ("direct-%s.json" % effort),
                    receipt_path=root / ("direct-%s-receipt.json" % effort),
                    policy=policy,
                    reasoning_effort=effort,
                )
                self.assertEqual(
                    effort_draft.workflow["steps"][0]["max_tokens"],
                    expected_cap,
                )
                self.assertEqual(
                    effort_draft.workflow["generated_by"]["agent_budget_profile"],
                    "bounded-direct-v2",
                )
            with self.assertRaisesRegex(ValidationError, "shared native-agent ceiling"):
                plan_direct_model_workflow(
                    task="Inspect the workspace and return one fact.",
                    workspace=workspace,
                    output_path=root / "native.json",
                    receipt_path=root / "native-receipt.json",
                    policy=RuntimePolicy(
                        allow_agent=True,
                        allow_parallel=True,
                        approvals={"native-agent-delegation"},
                    ),
                    native_agent_threads=1,
                    max_tokens=12000,
                )
            native_policy = RuntimePolicy(
                allow_agent=True,
                allow_parallel=True,
                approvals={"native-agent-delegation"},
            )
            minimum_native = plan_direct_model_workflow(
                task="Inspect the workspace and return one fact.",
                workspace=workspace,
                output_path=root / "native-minimum.json",
                receipt_path=root / "native-minimum-receipt.json",
                policy=native_policy,
                native_agent_threads=1,
                max_tokens=18000,
            )
            self.assertEqual(minimum_native.workflow["steps"][0]["max_tokens"], 18000)
            funded_native = plan_direct_model_workflow(
                task="Inspect the workspace and return one fact.",
                workspace=workspace,
                output_path=root / "native-funded.json",
                receipt_path=root / "native-funded-receipt.json",
                policy=native_policy,
                native_agent_threads=1,
            )
            self.assertEqual(funded_native.workflow["steps"][0]["max_tokens"], 42000)
            self.assertEqual(
                funded_native.workflow["generated_by"]["agent_budget_profile"],
                "bounded-native-direct-v2",
            )

    def test_pending_status_cli_dry_run_and_command_policy_launch_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            campaign_path = Path(temporary) / "campaign.json"
            write_readonly_parity_campaign(self._campaign(), campaign_path)
            status = readonly_parity_campaign_status(campaign_path)
            self.assertEqual(status["arm_lifecycle"]["pending"], 18)
            self.assertFalse(status["inference"]["universal_superiority_supported"])
            output = StringIO()
            with redirect_stdout(output):
                code = cli_main(
                    [
                        "run-readonly-parity-arm",
                        str(campaign_path),
                        str(TASKS_PATH),
                        "--next",
                        "--dry-run",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("Provider launches: 0", output.getvalue())
            base = [
                "python3",
                "-B",
                "-m",
                "conductor_runtime",
                "run-readonly-parity-arm",
                "campaign.json",
                "tasks.json",
                "--next",
            ]
            dry = assess_command([*base, "--dry-run"])
            live = assess_command(base)
            self.assertFalse(dry.writes)
            self.assertFalse(dry.network)
            self.assertTrue(live.writes)
            self.assertTrue(live.network)
            self.assertFalse(live.destructive)

    def test_product_profile_passes_each_cap_to_the_codex_workflow_boundary(self):
        campaign = self._campaign(
            repetitions=1,
            budget_profile="product-v1",
            tasks_path=PRODUCT_TASKS_PATH,
        )
        cohort = campaign["cohorts"][0]
        with tempfile.TemporaryDirectory() as temporary:
            campaign_path = Path(temporary) / "campaign.json"
            write_readonly_parity_campaign(campaign, campaign_path)
            for system, token_cap in [("serial-codex", 20000), ("native-codex", 42000)]:
                approvals = {
                    READONLY_PARITY_ARM_APPROVAL,
                    "model-workflow-execute",
                    "high-risk",
                }
                if system == "native-codex":
                    approvals.add("native-agent-delegation")
                policy = RuntimePolicy(
                    allow_agent=True,
                    allow_parallel=system == "native-codex",
                    approvals=approvals,
                )
                with (
                    patch("conductor_runtime.readonly_parity_run.shutil.which", return_value="/usr/bin/codex"),
                    patch(
                        "conductor_runtime.readonly_parity_run.run_model_workflow",
                        side_effect=RuntimeError("stop before provider execution"),
                    ) as workflow_runner,
                ):
                    with self.assertRaisesRegex(RuntimeError, "stop before provider"):
                        run_readonly_parity_arm(
                            campaign_path=campaign_path,
                            parity_tasks_path=PRODUCT_TASKS_PATH,
                            cohort_id=cohort["id"],
                            system=system,
                            policy=policy,
                        )
                self.assertEqual(workflow_runner.call_args.kwargs["direct_max_tokens"], token_cap)

    def test_mocked_claude_arm_finalizes_without_exposing_answer_in_status(self):
        campaign = self._campaign(repetitions=1)
        cohort = next(item for item in campaign["cohorts"] if item["task_id"] == "diagnose-retry-policy")
        answer = retry_answer()
        stream = "\n".join(
            json.dumps(event)
            for event in [
                {"type": "system", "subtype": "init", "model": "claude-sonnet-5"},
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-sonnet-5",
                        "content": [{"type": "text", "text": answer}],
                    },
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": answer,
                    "total_cost_usd": 0.05,
                    "num_turns": 2,
                    "modelUsage": {
                        "claude-sonnet-5": {
                            "costUSD": 0.05,
                            "inputTokens": 100,
                            "cacheCreationInputTokens": 20,
                            "cacheReadInputTokens": 30,
                            "outputTokens": 50,
                        },
                        "claude-haiku-4-5-20251001": {
                            "costUSD": 0.001,
                            "inputTokens": 10,
                            "cacheCreationInputTokens": 2,
                            "cacheReadInputTokens": 3,
                            "outputTokens": 4,
                        },
                    },
                },
            ]
        ) + "\n"
        analysis = analyze_claude_output(
            ProcessResult(0, stream, ""),
            cli_version="2.1.206 (Claude Code)",
            max_budget_usd=0.25,
            max_turns=8,
            timeout_seconds=600,
            output_limit_bytes=1024 * 1024,
        )
        evidence = analysis["provider_evidence"]
        task_contract = render_parity_task_contract(
            next(
                task
                for task in load_parity_tasks(TASKS_PATH)["tasks"]
                if task["id"] == "diagnose-retry-policy"
            )
        )
        report = {
            "schema": "conductor.benchmark_report.v1",
            "suite": campaign["source"]["suite"],
            "system": "claude-sonnet-ultracode",
            "environment": parity_prompt_environment(task_contract, task_contract),
            "duration_ms": 1234,
            "total_tasks": 1,
            "passed_tasks": 1,
            "failed_tasks": 0,
            "results": [
                {
                    "id": "diagnose-retry-policy",
                    "passed": True,
                    "duration_ms": 1234,
                    "timed_out": False,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "stdout": analysis["output_text"],
                    "stderr": "",
                    "provider_evidence": evidence,
                }
            ],
        }
        policy = RuntimePolicy(
            allow_agent=True,
            allow_network=True,
            approvals={READONLY_PARITY_ARM_APPROVAL, "claude-paid-run", "high-risk"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            campaign_path = Path(temporary) / "campaign.json"
            write_readonly_parity_campaign(campaign, campaign_path)
            with (
                patch("conductor_runtime.readonly_parity_run.shutil.which", return_value="/usr/bin/claude"),
                patch("conductor_runtime.readonly_parity_run.run_claude_readonly_task", return_value=report),
            ):
                result = run_readonly_parity_arm(
                    campaign_path=campaign_path,
                    parity_tasks_path=TASKS_PATH,
                    cohort_id=cohort["id"],
                    system="claude-sonnet",
                    policy=policy,
                )
            self.assertEqual(result["score"], 10)
            self.assertTrue(result["final_success"])
            status = readonly_parity_campaign_status(campaign_path)
            self.assertEqual(status["arm_lifecycle"]["completed"], 1)
            self.assertEqual(status["systems"]["claude-sonnet"]["cap_compliant_arms"], 1)
            self.assertNotIn(
                "Claude read-only evidence used behavior-changing plan permission mode",
                status["inference"]["universal_superiority_blockers"],
            )
            self.assertNotIn("dict(raw)", json.dumps(status))

        legacy_report = copy.deepcopy(report)
        legacy_provider = legacy_report["results"][0]["provider_evidence"]
        legacy_provider["schema"] = "conductor.claude_provider_evidence.v2"
        legacy_provider["requested"]["permission_mode"] = "plan"
        with tempfile.TemporaryDirectory() as temporary:
            campaign_path = Path(temporary) / "campaign.json"
            write_readonly_parity_campaign(campaign, campaign_path)
            with (
                patch("conductor_runtime.readonly_parity_run.shutil.which", return_value="/usr/bin/claude"),
                patch(
                    "conductor_runtime.readonly_parity_run.run_claude_readonly_task",
                    return_value=legacy_report,
                ),
            ):
                run_readonly_parity_arm(
                    campaign_path=campaign_path,
                    parity_tasks_path=TASKS_PATH,
                    cohort_id=cohort["id"],
                    system="claude-sonnet",
                    policy=policy,
                )
            status = readonly_parity_campaign_status(campaign_path)
            self.assertIn(
                "Claude read-only evidence used behavior-changing plan permission mode",
                status["inference"]["universal_superiority_blockers"],
            )

    def test_budget_failed_claude_arm_scores_last_complete_assistant_message(self):
        campaign = self._campaign(repetitions=1)
        cohort = next(
            item
            for item in campaign["cohorts"]
            if item["task_id"] == "diagnose-retry-policy"
        )
        answer = retry_answer()
        stream = "\n".join(
            json.dumps(event)
            for event in [
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "claude-sonnet-5",
                },
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-sonnet-5",
                        "content": [
                            {
                                "type": "text",
                                "text": "I will inspect the relevant files first.",
                            }
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-sonnet-5",
                        "content": [{"type": "text", "text": answer}],
                    },
                },
                {
                    "type": "result",
                    "subtype": "error_max_budget_usd",
                    "is_error": True,
                    "result": "",
                    "total_cost_usd": 0.30,
                    "num_turns": 9,
                    "modelUsage": {
                        "claude-sonnet-5": {
                            "costUSD": 0.30,
                            "inputTokens": 100,
                            "cacheCreationInputTokens": 20,
                            "cacheReadInputTokens": 30,
                            "outputTokens": 50,
                        }
                    },
                },
            ]
        ) + "\n"
        analysis = analyze_claude_output(
            ProcessResult(1, stream, ""),
            cli_version="2.1.206 (Claude Code)",
            max_budget_usd=0.25,
            max_turns=8,
            timeout_seconds=600,
            output_limit_bytes=1024 * 1024,
        )
        self.assertEqual(analysis["output_text"], answer)
        self.assertNotIn("inspect the relevant files", analysis["output_text"])
        evidence = analysis["provider_evidence"]
        self.assertEqual(evidence["status"], "budget-exceeded")
        self.assertTrue(evidence["observed"]["partial_output_preserved"])
        task_contract = render_parity_task_contract(
            next(
                task
                for task in load_parity_tasks(TASKS_PATH)["tasks"]
                if task["id"] == "diagnose-retry-policy"
            )
        )
        report = {
            "schema": "conductor.benchmark_report.v1",
            "suite": campaign["source"]["suite"],
            "system": "claude-sonnet-ultracode",
            "environment": parity_prompt_environment(task_contract, task_contract),
            "duration_ms": 1234,
            "total_tasks": 1,
            "passed_tasks": 0,
            "failed_tasks": 1,
            "results": [
                {
                    "id": "diagnose-retry-policy",
                    "passed": False,
                    "returncode": 1,
                    "duration_ms": 1234,
                    "timed_out": False,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "stdout": analysis["output_text"],
                    "stderr": "",
                    "provider_evidence": evidence,
                }
            ],
        }
        policy = RuntimePolicy(
            allow_agent=True,
            allow_network=True,
            approvals={
                READONLY_PARITY_ARM_APPROVAL,
                "claude-paid-run",
                "high-risk",
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            campaign_path = Path(temporary) / "campaign.json"
            write_readonly_parity_campaign(campaign, campaign_path)
            with (
                patch(
                    "conductor_runtime.readonly_parity_run.shutil.which",
                    return_value="/usr/bin/claude",
                ),
                patch(
                    "conductor_runtime.readonly_parity_run.run_claude_readonly_task",
                    return_value=report,
                ),
            ):
                result = run_readonly_parity_arm(
                    campaign_path=campaign_path,
                    parity_tasks_path=TASKS_PATH,
                    cohort_id=cohort["id"],
                    system="claude-sonnet",
                    policy=policy,
                )
            self.assertEqual(result["score"], 10)
            self.assertFalse(result["final_success"])
            self.assertFalse(result["cap_compliant"])
            status = readonly_parity_campaign_status(campaign_path)
            self.assertEqual(status["systems"]["claude-sonnet"]["score_mean"], 10)
            self.assertEqual(status["systems"]["claude-sonnet"]["final_successes"], 0)
            self.assertEqual(status["systems"]["claude-sonnet"]["cap_overshoot_arms"], 1)

    def test_exact_runtime_build_mismatch_refuses_before_provider_discovery(self):
        campaign = self._campaign(repetitions=1)
        cohort = next(item for item in campaign["cohorts"] if item["task_id"] == "diagnose-retry-policy")
        policy = RuntimePolicy(
            allow_agent=True,
            approvals={READONLY_PARITY_ARM_APPROVAL, "model-workflow-execute", "high-risk"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            campaign_path = Path(temporary) / "campaign.json"
            write_readonly_parity_campaign(campaign, campaign_path)
            with (
                patch(
                    "conductor_runtime.readonly_parity_run.current_runtime_build_sha256",
                    return_value="0" * 64,
                ),
                patch("conductor_runtime.readonly_parity_run.shutil.which") as provider_discovery,
            ):
                with self.assertRaisesRegex(ValidationError, "requires exact runtime build"):
                    run_readonly_parity_arm(
                        campaign_path=campaign_path,
                        parity_tasks_path=TASKS_PATH,
                        cohort_id=cohort["id"],
                        system="serial-codex",
                        policy=policy,
                    )
            provider_discovery.assert_not_called()

    def test_unknown_interrupted_provider_outcome_is_not_replayed(self):
        campaign = self._campaign(repetitions=1)
        cohort = next(item for item in campaign["cohorts"] if item["task_id"] == "diagnose-retry-policy")
        policy = RuntimePolicy(
            allow_agent=True,
            allow_network=True,
            approvals={READONLY_PARITY_ARM_APPROVAL, "claude-paid-run", "high-risk"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            campaign_path = Path(temporary) / "campaign.json"
            write_readonly_parity_campaign(campaign, campaign_path)
            with (
                patch("conductor_runtime.readonly_parity_run.shutil.which", return_value="/usr/bin/claude"),
                patch(
                    "conductor_runtime.readonly_parity_run.run_claude_readonly_task",
                    side_effect=RuntimeError("simulated hard interruption"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated hard interruption"):
                    run_readonly_parity_arm(
                        campaign_path=campaign_path,
                        parity_tasks_path=TASKS_PATH,
                        cohort_id=cohort["id"],
                        system="claude-sonnet",
                        policy=policy,
                    )
            with patch("conductor_runtime.readonly_parity_run.shutil.which", return_value="/usr/bin/claude"):
                with self.assertRaisesRegex(ValidationError, "outcome is unknown"):
                    run_readonly_parity_arm(
                        campaign_path=campaign_path,
                        parity_tasks_path=TASKS_PATH,
                        cohort_id=cohort["id"],
                        system="claude-sonnet",
                        policy=policy,
                        resume=True,
                    )

    def test_product_codex_arms_use_real_workflow_contracts_without_provider(self):
        campaign = self._campaign(
            repetitions=1,
            budget_profile="product-v1",
            tasks_path=PRODUCT_TASKS_PATH,
        )
        cohort = next(item for item in campaign["cohorts"] if item["task_id"] == "diagnose-retry-policy")
        answer = retry_answer()
        stream = "\n".join(
            json.dumps(event)
            for event in [
                {"type": "thread.started", "thread_id": "11111111-1111-4111-8111-111111111111"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {"id": "answer", "type": "agent_message", "text": answer},
                },
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 20,
                        "output_tokens": 50,
                        "reasoning_output_tokens": 10,
                    },
                },
            ]
        ) + "\n"
        policy = RuntimePolicy(
            allow_agent=True,
            approvals={READONLY_PARITY_ARM_APPROVAL, "model-workflow-execute", "high-risk"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_codex = root / "codex"
            fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            campaign_path = root / "campaign.json"
            write_readonly_parity_campaign(campaign, campaign_path)

            def fake_codex_process(command=None, argv=None, **_kwargs):
                command = command or argv
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(answer, encoding="utf-8")
                return ProcessResult(0, stream, "")

            with (
                patch("conductor_runtime.readonly_parity_run.shutil.which", return_value=str(fake_codex)),
                patch(
                    "conductor_runtime.runner.run_process",
                    side_effect=fake_codex_process,
                ),
            ):
                result = run_readonly_parity_arm(
                    campaign_path=campaign_path,
                    parity_tasks_path=PRODUCT_TASKS_PATH,
                    cohort_id=cohort["id"],
                    system="serial-codex",
                    policy=policy,
                )
            self.assertEqual(result["score"], 10)
            self.assertTrue(result["final_success"])
            workflow_path = root / cohort["artifacts"]["serial-codex"]["workflow"]
            workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
            self.assertEqual(workflow["steps"][0]["max_tokens"], 20000)
            self.assertNotIn("native_agents", workflow["steps"][0])
            native_policy = RuntimePolicy(
                allow_agent=True,
                allow_parallel=True,
                approvals={
                    READONLY_PARITY_ARM_APPROVAL,
                    "model-workflow-execute",
                    "native-agent-delegation",
                    "high-risk",
                },
            )
            with (
                patch("conductor_runtime.readonly_parity_run.shutil.which", return_value=str(fake_codex)),
                patch("conductor_runtime.runner.run_process", side_effect=fake_codex_process),
            ):
                native_result = run_readonly_parity_arm(
                    campaign_path=campaign_path,
                    parity_tasks_path=PRODUCT_TASKS_PATH,
                    cohort_id=cohort["id"],
                    system="native-codex",
                    policy=native_policy,
                )
            self.assertEqual(native_result["score"], 10)
            self.assertFalse(native_result["topology_executed"])
            native_workflow_path = root / cohort["artifacts"]["native-codex"]["workflow"]
            native_workflow = json.loads(native_workflow_path.read_text(encoding="utf-8"))
            self.assertEqual(native_workflow["steps"][0]["native_agents"], {"max_threads": 1})
            self.assertEqual(native_workflow["steps"][0]["max_tokens"], 42000)
            status = readonly_parity_campaign_status(campaign_path)
            self.assertEqual(status["arm_lifecycle"]["completed"], 2)
            self.assertEqual(status["systems"]["serial-codex"]["gross_token_coverage"], 0)
            self.assertFalse(status["inference"]["native_topology_complete"])

            result_path = root / cohort["artifacts"]["serial-codex"]["result"]
            tampered_result = json.loads(result_path.read_text(encoding="utf-8"))
            tampered_result["resources"]["token_cap"] = 18000
            result_path.write_text(
                json.dumps(tampered_result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            load_readonly_parity_result(result_path)
            launch_path = root / cohort["artifacts"]["serial-codex"]["launch"]
            launch = json.loads(launch_path.read_text(encoding="utf-8"))
            launch["result_sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
            launch_path.write_text(
                json.dumps(launch, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tampered_status = readonly_parity_campaign_status(campaign_path)
            self.assertEqual(tampered_status["arm_lifecycle"]["invalid"], 1)

    def test_budget_exhausted_codex_stream_retains_scored_answer_and_overshoot(self):
        campaign = self._campaign(repetitions=1)
        cohort = next(item for item in campaign["cohorts"] if item["task_id"] == "diagnose-retry-policy")
        answer = retry_answer()
        message = "shared rollout token budget exhausted"
        stream = "\n".join(
            json.dumps(event)
            for event in [
                {"type": "thread.started", "thread_id": "11111111-1111-4111-8111-111111111111"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {"id": "answer", "type": "agent_message", "text": answer},
                },
                {"type": "error", "message": message},
                {"type": "turn.failed", "error": {"message": message}},
            ]
        ) + "\n"
        usage = {
            "status": "complete",
            "session_count": 1,
            "child_count": 0,
            "input_tokens": 115805,
            "cached_input_tokens": 95488,
            "output_tokens": 4067,
            "total_tokens": 119872,
            "rollout_tokens": 24384,
        }
        policy = RuntimePolicy(
            allow_agent=True,
            approvals={READONLY_PARITY_ARM_APPROVAL, "model-workflow-execute", "high-risk"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_codex = root / "codex"
            fake_codex.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            campaign_path = root / "campaign.json"
            write_readonly_parity_campaign(campaign, campaign_path)
            with (
                patch("conductor_runtime.readonly_parity_run.shutil.which", return_value=str(fake_codex)),
                patch(
                    "conductor_runtime.runner.run_process",
                    return_value=ProcessResult(1, stream, message),
                ),
                patch(
                    "conductor_runtime.readonly_parity_run.reconcile_codex_session_usage",
                    return_value=usage,
                ),
            ):
                run = run_readonly_parity_arm(
                    campaign_path=campaign_path,
                    parity_tasks_path=TASKS_PATH,
                    cohort_id=cohort["id"],
                    system="serial-codex",
                    policy=policy,
                )
            self.assertEqual(run["score"], 10)
            self.assertFalse(run["provider_success"])
            self.assertFalse(run["final_success"])
            self.assertFalse(run["cap_compliant"])
            self.assertEqual(run["cap_overshoot"], 6384)
            result_path = root / cohort["artifacts"]["serial-codex"]["result"]
            result = load_readonly_parity_result(result_path)
            self.assertFalse(result["outcome"]["provider_success"])
            self.assertTrue(result["outcome"]["answer_valid"])
            self.assertFalse(result["resources"]["cap_compliant"])
            self.assertEqual(result["resources"]["cap_overshoot"], 6384)
            self.assertIsNotNone(result["artifacts"]["codex_stream_sha256"])
            status = readonly_parity_campaign_status(campaign_path)
            self.assertEqual(status["arm_lifecycle"]["completed"], 1)
            self.assertEqual(status["systems"]["serial-codex"]["cap_overshoot_arms"], 1)
            self.assertNotIn(answer, json.dumps(status))

    def test_resource_to_success_does_not_reward_early_failure(self):
        success = {
            "outcome": {"score": 9, "final_success": True},
            "resources": {"duration_ms": 100, "token_observed": True, "gross_tokens": 1000},
        }
        early_failure = {
            "outcome": {"score": 0, "final_success": False},
            "resources": {"duration_ms": 10, "token_observed": True, "gross_tokens": 100},
        }
        summary = _pairwise_summary("successful", "failed", [(success, early_failure)])
        self.assertEqual(summary["duration"]["right"], 1)
        self.assertEqual(summary["gross_tokens"]["right"], 1)
        self.assertEqual(summary["duration_to_success"]["left"], 1)
        self.assertEqual(summary["gross_tokens_to_success"]["left"], 1)


if __name__ == "__main__":
    unittest.main()
