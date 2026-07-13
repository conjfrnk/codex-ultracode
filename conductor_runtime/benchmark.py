import hashlib
import json
import math
import os
import platform
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Dict, List

from . import __version__
from .clock import utc_from_timestamp, utc_now
from .benchmark_quality import (
    QUALITY_REVIEWER_KINDS,
    quality_evidence_sha256,
    validate_benchmark_quality_evidence,
)
from .errors import ValidationError
from .paths import default_live_parity_report_dir, default_live_parity_workflow_dir
from .redaction import contains_secret_like, redact_text
from .runner import DEFAULT_OUTPUT_LIMIT_BYTES, run_process
from .security import (
    RuntimePolicy,
    ensure_dir_no_follow,
    enforce_shell_policy,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    require_no_path_escape,
)
from .workflow import MAX_TIMEOUT_SECONDS, SAFE_ID


SCHEMA = "conductor.benchmark.v1"
BENCHMARK_REPORT_SCHEMA = "conductor.benchmark_report.v1"
COMPARISON_SCHEMA = "conductor.comparison.v1"
PARITY_TASKS_SCHEMA = "conductor.parity_tasks.v1"
RUN_MANIFEST_SCHEMA = "conductor.run_manifest.v1"
LIVE_PARITY_RUNBOOK_SCHEMA = "conductor.live_parity_runbook.v1"
MAX_OUTPUT_LIMIT_BYTES = 16 * DEFAULT_OUTPUT_LIMIT_BYTES
# Aggregate repair reports may embed up to five individually bounded attempt reports.
MAX_BENCHMARK_JSON_BYTES = 32 * 1024 * 1024
MAX_PARITY_TASK_CONTRACT_BYTES = 128 * 1024
MAX_PARITY_PROVIDER_PROMPT_BYTES = 256 * 1024
PARITY_TASK_PROMPT_SCHEMA = "conductor.parity_task_prompt.v1"
IMPLEMENTATION_CANARY_TERMINAL_ZERO_EVALUATORS = {
    "implementation-canary-evaluator-v3",
    "implementation-canary-evaluator-v4",
}
PARITY_PROMPT_ENVIRONMENT_FIELDS = {
    "conductor_runtime_version",
    "task_prompt_schema",
    "task_contract_prompt_sha256",
    "provider_prompt_sha256",
}
TOOL_VERSION_PROBE_APPROVAL = "tool-version-probes"
LIVE_TOOL_COST_LEVELS = {"none", "low", "medium", "high"}
LIVE_TOOL_COST_ORDER = ["none", "low", "medium", "high"]
SAFETY_SEVERITIES = {"info", "low", "medium", "high", "critical"}
RUN_MANIFEST_FIELDS = {
    "schema",
    "run_id",
    "system",
    "suite",
    "generated_at_utc",
    "live_run",
    "repository",
    "model",
    "budget",
    "permissions",
    "scoring",
    "safety",
}
REPOSITORY_FIELDS = {"state", "commit", "dirty", "branch"}
MODEL_FIELDS = {"provider", "name", "effort", "version"}
BUDGET_FIELDS = {
    "max_minutes",
    "max_agents",
    "max_live_tool_cost",
    "max_cost_usd",
    "max_tokens",
    "cost_cap_enforcement",
    "token_cap_enforcement",
}
BUDGET_CAP_ENFORCEMENT_LEVELS = {
    "hard-provider",
    "runtime-hard",
    "post-run-fail-closed",
    "declared-only",
    "unavailable",
    "not-applicable",
}
PERMISSION_FIELDS = {
    "allow_writes",
    "allow_destructive",
    "allow_network",
    "allow_agent",
    "allow_parallel",
    "approved_tokens",
}
SCORING_FIELDS = {"tasks"}
SCORING_TASK_FIELDS = {
    "id",
    "score",
    "max_score",
    "notes",
    "quality_evidence_sha256",
    "task_contract_sha256",
    "pass_threshold",
    "reviewer_kind",
    "reviewer_independent",
}
SAFETY_FIELDS = {"incidents"}
SAFETY_INCIDENT_FIELDS = {"id", "severity", "task_id", "description"}
PARITY_TASKS_FIELDS = {"schema", "name", "description", "required_dimensions", "tasks"}
PARITY_TASK_FIELDS = {
    "id",
    "dimension",
    "tags",
    "prompt",
    "success_criteria",
    "required_evidence",
    "scoring",
    "budget",
}
PARITY_BUDGET_FIELDS = {"max_minutes", "max_agents", "max_live_tool_cost", "max_cost_usd", "max_tokens"}
CLAIM_POLICY_FIELDS = {"model"}
MODEL_CLAIM_POLICY_FIELDS = {"mode", "rationale"}
MODEL_CLAIM_MODES = {"exact", "declared-comparable"}
MODEL_PROVENANCE_FIELDS = ["provider", "name", "effort", "version"]
LIVE_PARITY_RUNBOOK_FIELDS = {
    "schema",
    "name",
    "suite",
    "generated_at_utc",
    "evidence_status",
    "source",
    "aggregate_budget",
    "systems",
    "tasks",
    "operator_checklist",
    "claim_gate",
}
LIVE_PARITY_RUNBOOK_SOURCE_FIELDS = {
    "parity_tasks_schema",
    "parity_tasks_source",
    "parity_tasks_sha256",
    "task_count",
    "required_dimensions",
}
LIVE_PARITY_RUNBOOK_SYSTEM_FIELDS = {
    "name",
    "provider",
    "tool",
    "model_effort",
    "requires_external_tool",
    "requires_paid_approval",
    "report_path",
    "manifest_path",
    "model_manifest_template",
    "permissions_template",
    "invocation_templates",
    "notes",
}
LIVE_PARITY_RUNBOOK_INVOCATION_FIELDS = {
    "description",
    "status",
    "launches_external_tools",
    "requires_paid_approval",
    "argv_template",
    "notes",
}
LIVE_PARITY_RUNBOOK_TASK_FIELDS = {
    "id",
    "dimension",
    "tags",
    "prompt",
    "prompt_path",
    "success_criteria",
    "required_evidence",
    "scoring",
    "budget",
    "scorecard_template",
    "safety_incident_template",
    "system_artifacts",
}
LIVE_PARITY_RUNBOOK_SCORECARD_FIELDS = {"score", "max_score", "notes"}
LIVE_PARITY_RUNBOOK_SAFETY_TEMPLATE_FIELDS = {"id", "severity", "task_id", "description"}
LIVE_PARITY_RUNBOOK_CLAIM_GATE_FIELDS = {
    "eligible_for_parity_claims",
    "required_before_claim",
    "comparison_template",
    "claim_check_command",
}
LIVE_PARITY_RUNBOOK_EVIDENCE_STATUS = "planned-not-evidence"
DEFAULT_LIVE_PARITY_RUN_ID = "conductor-vs-claude-live-parity"
DEFAULT_LIVE_PARITY_REPORT_DIR = default_live_parity_report_dir()
DEFAULT_LIVE_PARITY_WORKFLOW_DIR = default_live_parity_workflow_dir()


def load_benchmark_suite(path: Path) -> Dict:
    try:
        suite = json.loads(_read_json_text(path, "benchmark suite"))
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    validate_benchmark_suite(suite, source=str(path))
    suite["_source_path"] = str(path)
    return suite


def validate_benchmark_suite(suite: Dict, source: str = "<memory>") -> None:
    if not isinstance(suite, dict):
        raise ValidationError("%s must contain a JSON object" % source)
    if suite.get("schema") != SCHEMA:
        raise ValidationError("%s must set schema to %s" % (source, SCHEMA))
    name = suite.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("%s must set a non-empty name" % source)
    tasks = suite.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValidationError("%s must contain one or more tasks" % source)
    if "default_timeout_seconds" in suite:
        _validate_int_range(
            suite["default_timeout_seconds"],
            "%s default_timeout_seconds" % source,
            minimum=1,
            maximum=MAX_TIMEOUT_SECONDS,
        )
    if "output_limit_bytes" in suite:
        _validate_int_range(
            suite["output_limit_bytes"],
            "%s output_limit_bytes" % source,
            minimum=1,
            maximum=MAX_OUTPUT_LIMIT_BYTES,
        )
    seen = set()
    for task in tasks:
        validate_benchmark_task(task, seen)


def validate_benchmark_task(task: Dict, seen: set) -> None:
    if not isinstance(task, dict):
        raise ValidationError("benchmark tasks must be objects")
    task_id = task.get("id")
    if not isinstance(task_id, str) or not SAFE_ID.match(task_id):
        raise ValidationError("benchmark task id must be a safe identifier")
    if task_id in seen:
        raise ValidationError("duplicate benchmark task id %s" % task_id)
    seen.add(task_id)
    if "command" not in task:
        raise ValidationError("benchmark task %s must set command" % task_id)
    permissive_policy = RuntimePolicy(
        allow_writes=True,
        allow_network=True,
        allow_destructive=True,
        approvals={"external-path", "high-risk", task_id},
    )
    enforce_shell_policy({**task, "risk": task.get("risk", "low")}, permissive_policy)
    timeout = task.get("timeout_seconds", 120)
    _validate_int_range(
        timeout,
        "benchmark task %s timeout_seconds" % task_id,
        minimum=1,
        maximum=MAX_TIMEOUT_SECONDS,
    )
    if "output_limit_bytes" in task:
        _validate_int_range(
            task["output_limit_bytes"],
            "benchmark task %s output_limit_bytes" % task_id,
            minimum=1,
            maximum=MAX_OUTPUT_LIMIT_BYTES,
        )
    success_contains = task.get("success_contains")
    if success_contains is not None and not isinstance(success_contains, str):
        raise ValidationError("benchmark task %s success_contains must be a string" % task_id)


def load_parity_tasks(path: Path) -> Dict:
    try:
        tasks = json.loads(_read_json_text(path, "parity tasks"))
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    validate_parity_tasks(tasks, source=str(path))
    tasks["_source_path"] = str(path)
    return tasks


def load_run_manifest(path: Path) -> Dict:
    try:
        manifest = json.loads(_read_json_text(path, "run manifest"))
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    validate_run_manifest(manifest, source=str(path))
    manifest["_source_path"] = str(path)
    return manifest


def build_run_manifest_from_report(
    report: Dict,
    run_id: str,
    live_run: bool,
    repository: Dict,
    model: Dict,
    budget: Dict,
    permissions: Dict,
    system_name: str = None,
    task_max_score: int = 10,
    safety_incidents: List[Dict] = None,
) -> Dict:
    validate_benchmark_report(report)
    if not isinstance(run_id, str) or not SAFE_ID.match(run_id):
        raise ValidationError("run_id must be a safe identifier")
    if task_max_score < 1:
        raise ValidationError("task_max_score must be at least 1")
    system = system_name or report.get("system")
    if not isinstance(system, str) or not SAFE_ID.match(system):
        raise ValidationError("system must be a safe identifier")
    cleaned_model = _clean_manifest_object(model, "model")
    cleaned_budget = dict(budget)
    cleaned_permissions = _clean_permissions(permissions)
    provider_incidents = _provider_safety_incidents(report)
    has_provider_evidence = any(_has_provider_backed_evidence(result) for result in report.get("results") or [])
    if has_provider_evidence and live_run:
        _validate_live_provider_manifest_contract(
            report,
            model=cleaned_model,
            budget=cleaned_budget,
            permissions=cleaned_permissions,
        )
    manifest = {
        "schema": RUN_MANIFEST_SCHEMA,
        "run_id": run_id,
        "system": system,
        "suite": report["suite"],
        "generated_at_utc": utc_now().isoformat(timespec="seconds") + "Z",
        "live_run": bool(live_run),
        "repository": _clean_manifest_object(repository, "repository"),
        "model": cleaned_model,
        "budget": cleaned_budget,
        "permissions": cleaned_permissions,
        "scoring": {
            "tasks": [_manifest_score_record(result, task_max_score) for result in report["results"]]
        },
        "safety": {"incidents": provider_incidents + list(safety_incidents or [])},
    }
    _reject_secret_like_manifest(manifest)
    validate_run_manifest(manifest)
    _validate_run_manifest_matches_report(manifest, report, source="<generated manifest>")
    return manifest


def _manifest_score_record(result: Dict, task_max_score: int) -> Dict:
    quality = result.get("quality_evidence")
    if not isinstance(quality, dict):
        return {
            "id": result["id"],
            "score": task_max_score if result["passed"] else 0,
            "max_score": task_max_score,
            "notes": "Benchmark task passed." if result["passed"] else "Benchmark task failed.",
        }
    reviewer = quality["reviewer"]
    return {
        "id": result["id"],
        "score": quality["score"],
        "max_score": quality["max_score"],
        "notes": "Hash-bound benchmark quality evidence.",
        "quality_evidence_sha256": quality_evidence_sha256(quality),
        "task_contract_sha256": quality["task_contract_sha256"],
        "pass_threshold": quality["pass_threshold"],
        "reviewer_kind": reviewer["kind"],
        "reviewer_independent": reviewer["independent"],
    }


def _provider_safety_incidents(report: Dict) -> List[Dict]:
    incidents = []
    for result in report.get("results") or []:
        if not isinstance(result, dict):
            continue
        for field, prefix in [("provider_evidence", "provider"), ("staged_evidence", "staged")]:
            evidence = result.get(field)
            if not isinstance(evidence, dict):
                continue
            for incident in evidence.get("incidents") or []:
                incidents.append(
                    {
                        "id": "%s-%s-%s" % (prefix, result["id"], incident["id"]),
                        "severity": incident["severity"],
                        "task_id": result["id"],
                        "description": incident["description"],
                    }
                )
        repair = result.get("repair_evidence")
        if isinstance(repair, dict):
            for incident in repair.get("incidents") or []:
                incidents.append(
                    {
                        "id": "repair-%s-%s" % (result["id"], incident["id"]),
                        "severity": incident["severity"],
                        "task_id": result["id"],
                        "description": incident["description"],
                    }
                )
            for index, attempt_report in enumerate(result.get("repair_attempt_reports") or [], 1):
                for nested in _provider_safety_incidents(attempt_report):
                    nested = dict(nested)
                    nested["id"] = "repair-attempt-%03d-%s" % (index, nested["id"])
                    incidents.append(nested)
    return incidents


def _has_provider_backed_evidence(result) -> bool:
    return isinstance(result, dict) and (
        isinstance(result.get("provider_evidence"), dict)
        or isinstance(result.get("repair_evidence"), dict)
    )


def _validate_live_provider_manifest_contract(report: Dict, *, model: Dict, budget: Dict, permissions: Dict) -> None:
    repair_results = [
        result
        for result in report.get("results") or []
        if isinstance(result, dict) and isinstance(result.get("repair_evidence"), dict)
    ]
    evidence_records = [
        result["provider_evidence"]
        for result in report.get("results") or []
        if isinstance(result, dict) and isinstance(result.get("provider_evidence"), dict)
    ]
    if repair_results:
        if evidence_records or len(repair_results) != len(report.get("results") or []):
            raise ValidationError("live reports cannot mix aggregate repair and single-attempt provider evidence")
        _validate_live_codex_repair_manifest_contract(
            repair_results,
            model=model,
            budget=budget,
            permissions=permissions,
        )
        return
    if not evidence_records:
        return
    schemas = {evidence.get("schema") for evidence in evidence_records}
    if len(schemas) != 1:
        raise ValidationError("live provider evidence mixes incompatible provider schemas")
    schema = next(iter(schemas))
    from .claude_live import (
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA,
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V1,
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V2,
    )
    from .codex_live import (
        CODEX_PROVIDER_EVIDENCE_SCHEMA,
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V1,
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V2,
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V3,
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V4,
    )

    if schema in {
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V1,
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V2,
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA,
    }:
        _validate_live_claude_manifest_contract(
            report,
            evidence_records=evidence_records,
            model=model,
            budget=budget,
            permissions=permissions,
        )
        return
    if schema in {
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V1,
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V2,
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V3,
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V4,
        CODEX_PROVIDER_EVIDENCE_SCHEMA,
    }:
        _validate_live_codex_manifest_contract(
            report,
            evidence_records=evidence_records,
            model=model,
            budget=budget,
            permissions=permissions,
        )
        return
    raise ValidationError("live provider evidence uses an unsupported provider schema")


def _validate_live_codex_repair_manifest_contract(
    repair_results: List[Dict],
    *,
    model: Dict,
    budget: Dict,
    permissions: Dict,
) -> None:
    allowed_statuses = {"success", "max-attempts-exhausted", "token-budget-exceeded"}
    evidence_records = [result["repair_evidence"] for result in repair_results]
    unsupported = sorted({evidence["status"] for evidence in evidence_records} - allowed_statuses)
    if unsupported:
        raise ValidationError(
            "Codex repair evidence status cannot be bound as a live run: %s" % ", ".join(unsupported)
        )
    attempts = [attempt for evidence in evidence_records for attempt in evidence["attempts"]]
    if model.get("provider") != "openai":
        raise ValidationError("live Codex repair evidence requires openai model provenance")
    if len({attempt["model"] for attempt in attempts}) != 1 or model.get("name") != attempts[0]["model"]:
        raise ValidationError("run manifest model name must match Codex repair attempts")
    if len({attempt["effort"] for attempt in attempts}) != 1 or model.get("effort") != attempts[0]["effort"]:
        raise ValidationError("run manifest effort must match Codex repair attempts")
    if len({attempt["cli_version"] for attempt in attempts}) != 1 or model.get("version") != attempts[0]["cli_version"]:
        raise ValidationError("run manifest version must match Codex repair attempts")
    total_caps = {evidence["limits"]["total_token_cap"] for evidence in evidence_records}
    if len(total_caps) != 1 or budget.get("max_tokens") not in total_caps:
        raise ValidationError("run manifest max_tokens must match the aggregate Codex repair cap")
    if budget.get("cost_cap_enforcement") != "unavailable":
        raise ValidationError("live Codex repair evidence must record unavailable cost cap enforcement")
    if budget.get("token_cap_enforcement") != "runtime-hard":
        raise ValidationError("live Codex repair evidence requires runtime-hard token enforcement")
    if any(not evidence["source"]["unchanged"] for evidence in evidence_records):
        raise ValidationError("live Codex repair evidence must preserve the original source")
    expected_permissions = {
        "allow_writes": True,
        "allow_destructive": False,
        "allow_network": True,
        "allow_agent": True,
        "allow_parallel": False,
        "approved_tokens": [],
    }
    if permissions != expected_permissions:
        raise ValidationError("run manifest permissions must match the Codex repair execution contract")


def _validate_live_claude_manifest_contract(
    report: Dict,
    *,
    evidence_records: List[Dict],
    model: Dict,
    budget: Dict,
    permissions: Dict,
) -> None:
    allowed_statuses = {"success", "budget-exceeded"}
    unsupported_statuses = sorted({evidence["status"] for evidence in evidence_records} - allowed_statuses)
    if unsupported_statuses:
        raise ValidationError(
            "Claude provider evidence status cannot be bound as a live run: %s"
            % ", ".join(unsupported_statuses)
        )
    if model.get("provider") != "anthropic" or model.get("effort") != "ultracode":
        raise ValidationError("live Claude provider evidence requires anthropic model provenance with ultracode effort")
    observed_models = {
        observed_model
        for evidence in evidence_records
        for observed_model in evidence["observed"]["main_models"]
    }
    if model.get("name") not in observed_models:
        raise ValidationError("run manifest model name must match an observed Claude main model")
    observed_versions = {evidence["observed"]["cli_version"] for evidence in evidence_records}
    if len(observed_versions) != 1 or model.get("version") not in observed_versions:
        raise ValidationError("run manifest model version must match the observed Claude CLI version")
    requested_budgets = {evidence["requested"]["max_budget_usd"] for evidence in evidence_records}
    if len(requested_budgets) != 1 or budget.get("max_cost_usd") not in requested_budgets:
        raise ValidationError("run manifest max_cost_usd must match the requested Claude budget cap")
    if budget.get("cost_cap_enforcement") != "hard-provider":
        raise ValidationError("live Claude provider evidence requires hard-provider cost cap enforcement")
    if budget.get("token_cap_enforcement") != "unavailable":
        raise ValidationError("live Claude provider evidence must record unavailable token cap enforcement")
    read_only_profiles = {evidence["policy"]["read_only_tools"] for evidence in evidence_records}
    if len(read_only_profiles) != 1:
        raise ValidationError("live Claude provider evidence mixes incompatible tool profiles")
    read_only_profile = next(iter(read_only_profiles))
    if not read_only_profile:
        staged_results = [
            result
            for result in report.get("results") or []
            if isinstance(result, dict) and isinstance(result.get("provider_evidence"), dict)
        ]
        if len(staged_results) != len(evidence_records) or any(
            not _manifest_bindable_staged_result(
                result,
                expected_schema="conductor.claude_staged_evidence.v1",
            )
            for result in staged_results
        ):
            raise ValidationError("write-capable Claude provider evidence requires safe terminal staged evidence")
    else:
        _require_readonly_provider_quality(report, "Claude")
    expected_permissions = {
        "allow_writes": not read_only_profile,
        "allow_destructive": False,
        "allow_network": True,
        "allow_agent": True,
        "allow_parallel": False,
        "approved_tokens": [],
    }
    if permissions != expected_permissions:
        raise ValidationError("run manifest permissions must match the Claude live-run tool contract")


def _validate_live_codex_manifest_contract(
    report: Dict,
    *,
    evidence_records: List[Dict],
    model: Dict,
    budget: Dict,
    permissions: Dict,
) -> None:
    from .codex_live import CODEX_PROVIDER_EVIDENCE_SCHEMA_V1

    allowed_statuses = {"success", "token-budget-exceeded"}
    unsupported_statuses = sorted({evidence["status"] for evidence in evidence_records} - allowed_statuses)
    if unsupported_statuses:
        raise ValidationError(
            "Codex provider evidence status cannot be bound as a live run: %s"
            % ", ".join(unsupported_statuses)
        )
    if model.get("provider") != "openai":
        raise ValidationError("live Codex provider evidence requires openai model provenance")
    requested_models = {evidence["requested"]["model"] for evidence in evidence_records}
    requested_efforts = {evidence["requested"]["effort"] for evidence in evidence_records}
    observed_versions = {evidence["observed"]["cli_version"] for evidence in evidence_records}
    requested_tokens = {evidence["requested"]["max_tokens"] for evidence in evidence_records}
    if len(requested_models) != 1 or model.get("name") not in requested_models:
        raise ValidationError("run manifest model name must match the command-enforced Codex model")
    if len(requested_efforts) != 1 or model.get("effort") not in requested_efforts:
        raise ValidationError("run manifest effort must match the requested Codex reasoning effort")
    if len(observed_versions) != 1 or model.get("version") not in observed_versions:
        raise ValidationError("run manifest model version must match the observed Codex CLI version")
    if len(requested_tokens) != 1 or budget.get("max_tokens") not in requested_tokens:
        raise ValidationError("run manifest max_tokens must match the requested Codex post-run token cap")
    schema = evidence_records[0]["schema"]
    execution_modes = {
        evidence["requested"].get("execution_mode", "read-only")
        for evidence in evidence_records
    }
    if len(execution_modes) != 1:
        raise ValidationError("live Codex provider evidence mixes execution modes")
    execution_mode = next(iter(execution_modes))
    if budget.get("cost_cap_enforcement") != "unavailable":
        raise ValidationError("live Codex provider evidence must record unavailable cost cap enforcement")
    expected_token_enforcement = (
        "post-run-fail-closed" if schema == CODEX_PROVIDER_EVIDENCE_SCHEMA_V1 else "runtime-hard"
    )
    if budget.get("token_cap_enforcement") != expected_token_enforcement:
        raise ValidationError(
            "live Codex provider evidence requires %s token cap enforcement" % expected_token_enforcement
        )
    if any(
        not evidence["workspace"]["source_unchanged"]
        or (execution_mode == "read-only" and not evidence["workspace"]["stage_unchanged"])
        or evidence["policy"]["read_only_tools"] != (execution_mode == "read-only")
        for evidence in evidence_records
    ):
        raise ValidationError("live Codex provider evidence must preserve its isolated workspace contract")
    if execution_mode == "staged-write":
        staged_results = [
            result
            for result in report.get("results") or []
            if isinstance(result, dict) and isinstance(result.get("provider_evidence"), dict)
        ]
        if len(staged_results) != len(evidence_records) or any(
            not _manifest_bindable_staged_result(
                result,
                expected_schema="conductor.codex_staged_evidence.v1",
            )
            for result in staged_results
        ):
            raise ValidationError("write-capable Codex provider evidence requires safe terminal staged evidence")
    expected_permissions = {
        "allow_writes": execution_mode == "staged-write",
        "allow_destructive": False,
        "allow_network": True,
        "allow_agent": True,
        "allow_parallel": False,
        "approved_tokens": [],
    }
    if permissions != expected_permissions:
        raise ValidationError("run manifest permissions must match the Codex isolated execution contract")
    if execution_mode == "read-only":
        _require_readonly_provider_quality(report, "Codex")


def _manifest_bindable_staged_evidence(evidence, *, expected_schema: str) -> bool:
    if not isinstance(evidence, dict) or evidence.get("schema") != expected_schema:
        return False
    status = evidence.get("status")
    if status not in {"success", "no-changes", "verification-failed", "verification-timed-out"}:
        return False
    source = evidence.get("source")
    stage = evidence.get("stage")
    changes = evidence.get("changes")
    verification = evidence.get("verification")
    policy = evidence.get("policy")
    if not all(isinstance(record, dict) for record in [source, stage, changes, verification, policy]):
        return False
    if (
        source.get("unchanged") is not True
        or stage.get("persisted") is not True
        or stage.get("verifier_mutated_files") is not False
        or policy.get("automatic_apply") is not False
    ):
        return False
    expected_verification = {
        "success": "passed",
        "no-changes": "skipped-no-changes",
        "verification-failed": "failed",
        "verification-timed-out": "timed-out",
    }[status]
    if verification.get("status") != expected_verification:
        return False
    if status == "no-changes":
        return bool(
            changes.get("change_count") == 0
            and changes.get("added") == []
            and changes.get("modified") == []
            and changes.get("deleted") == []
            and changes.get("binary") == []
            and changes.get("mode_changed") == []
            and changes.get("unpatchable") == []
            and changes.get("patch_written") is False
            and changes.get("patch_name") is None
            and changes.get("patch_sha256") is None
            and changes.get("patch_bytes") == 0
            and changes.get("validation_error") is None
            and changes.get("patch_error") is None
        )
    return status == "success" or changes.get("patch_written") is True


def _manifest_bindable_staged_result(result, *, expected_schema: str) -> bool:
    evidence = result.get("staged_evidence") if isinstance(result, dict) else None
    if not _manifest_bindable_staged_evidence(evidence, expected_schema=expected_schema):
        return False
    if evidence.get("status") != "no-changes":
        return True
    quality = result.get("quality_evidence")
    if not isinstance(quality, dict):
        return False
    reviewer = quality.get("reviewer") or {}
    criteria = quality.get("criteria") or []
    return bool(
        quality.get("score") == 0
        and quality.get("max_score") == 10
        and quality.get("pass_threshold") == 10
        and quality.get("passed") is False
        and reviewer.get("kind") == "deterministic"
        and reviewer.get("identity") in IMPLEMENTATION_CANARY_TERMINAL_ZERO_EVALUATORS
        and reviewer.get("independent") is True
        and len(criteria) == 5
        and all(item.get("score") == 0 for item in criteria if isinstance(item, dict))
        and all(isinstance(item, dict) for item in criteria)
    )


def _require_readonly_provider_quality(report: Dict, provider_name: str) -> None:
    missing = [
        result.get("id", "unknown")
        for result in report.get("results") or []
        if isinstance(result, dict)
        and isinstance(result.get("provider_evidence"), dict)
        and result["provider_evidence"]["policy"]["read_only_tools"] is True
        and not isinstance(result.get("quality_evidence"), dict)
    ]
    if missing:
        raise ValidationError(
            "live %s read-only provider evidence requires hash-bound quality evidence for: %s"
            % (provider_name, ", ".join(missing))
        )


def write_run_manifest(manifest: Dict, output: Path) -> Path:
    validate_run_manifest(manifest, source=str(output))
    if output.exists():
        raise ValidationError("run manifest output already exists: %s" % output)
    reject_symlink_path(output, "run manifest output")
    parent_fd = ensure_dir_no_follow(output.parent, "run manifest output parent")
    os.close(parent_fd)
    reject_symlink_path(output.parent, "run manifest output parent")
    _write_new_json_file(output, manifest, "run manifest output")
    return output


def load_live_parity_runbook(path: Path) -> Dict:
    try:
        runbook = json.loads(_read_json_text(path, "live parity runbook"))
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    validate_live_parity_runbook(runbook, source=str(path))
    runbook["_source_path"] = str(path)
    return runbook


def build_live_parity_runbook(
    parity_tasks: Dict,
    run_id: str = DEFAULT_LIVE_PARITY_RUN_ID,
    report_dir: str = DEFAULT_LIVE_PARITY_REPORT_DIR,
    workflow_dir: str = DEFAULT_LIVE_PARITY_WORKFLOW_DIR,
) -> Dict:
    parity_source_path = parity_tasks.get("_source_path") if isinstance(parity_tasks, dict) else None
    public_parity_tasks = _parity_tasks_public_payload(parity_tasks)
    validate_parity_tasks(public_parity_tasks)
    if not isinstance(run_id, str) or not SAFE_ID.match(run_id):
        raise ValidationError("live parity runbook run_id must be a safe identifier")
    report_root = _clean_runbook_path(report_dir, "live parity runbook report_dir")
    workflow_root = _clean_runbook_path(workflow_dir, "live parity runbook workflow_dir")
    tasks = public_parity_tasks["tasks"]
    required_dimensions = public_parity_tasks.get("required_dimensions") or sorted({task["dimension"] for task in tasks})
    runbook = {
        "schema": LIVE_PARITY_RUNBOOK_SCHEMA,
        "name": run_id,
        "suite": redact_text(public_parity_tasks["name"]),
        "generated_at_utc": utc_now().isoformat(timespec="seconds") + "Z",
        "evidence_status": LIVE_PARITY_RUNBOOK_EVIDENCE_STATUS,
        "source": {
            "parity_tasks_schema": public_parity_tasks["schema"],
            "parity_tasks_source": _redact_optional_path(parity_source_path),
            "parity_tasks_sha256": _sha256_json(public_parity_tasks),
            "task_count": len(tasks),
            "required_dimensions": [redact_text(dimension) for dimension in required_dimensions],
        },
        "aggregate_budget": _aggregate_parity_budget(tasks),
        "systems": [
            _live_runbook_system("conductor", report_root),
            _live_runbook_system("claude", report_root),
        ],
        "tasks": [_live_runbook_task(task, workflow_root, report_root) for task in tasks],
        "operator_checklist": [
            "Review this runbook before running any external or paid tools.",
            "Validate each model-authored Conductor workflow receipt and inspect the draft before executing it.",
            "Run each task against each system from the same repository state with the same recorded budget, cap-enforcement method, and permissions.",
            "Record one benchmark report and one live run manifest per system after execution.",
            "Attach hash-bound scorecards from independent reviewers and record safety incidents from evidence, not expectations.",
            "Run the comparison with --require-claim-eligible before making any parity or superiority claim.",
        ],
        "claim_gate": {
            "eligible_for_parity_claims": False,
            "required_before_claim": [
                "live Conductor benchmark report",
                "live Claude/Ultracode benchmark report",
                "matching live run manifests for every compared system",
                "equivalent repository, numeric budget, cap enforcement, permissions, and complete declared-comparable model provenance",
                "hash-bound independent quality evidence with equivalent task contracts and score scales",
                "comparison command exits zero with --require-claim-eligible",
            ],
            "comparison_template": "benchmark-suites/comparisons/conductor-vs-claude-parity-template.json",
            "claim_check_command": [
                "python3",
                "-B",
                "-m",
                "conductor_runtime",
                "compare",
                "benchmark-suites/comparisons/conductor-vs-claude-parity-template.json",
                "--base-dir",
                "benchmark-suites",
                "--require-claim-eligible",
            ],
        },
    }
    validate_live_parity_runbook(runbook)
    return runbook


def write_live_parity_runbook(runbook: Dict, output: Path) -> Path:
    validate_live_parity_runbook(runbook, source=str(output))
    if output.exists():
        raise ValidationError("live parity runbook output already exists: %s" % output)
    reject_symlink_path(output, "live parity runbook output")
    parent_fd = ensure_dir_no_follow(output.parent, "live parity runbook output parent")
    os.close(parent_fd)
    reject_symlink_path(output.parent, "live parity runbook output parent")
    _write_new_json_file(output, runbook, "live parity runbook output")
    return output


def _write_new_json_file(output: Path, data: Dict, label: str) -> None:
    parent = output.parent
    dir_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        dir_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        dir_flags |= os.O_NOFOLLOW
    try:
        parent_fd = os.open(parent, dir_flags)
    except OSError as exc:
        raise ValidationError("failed to open %s parent %s: %s" % (label, parent, exc.__class__.__name__))
    fd = None
    try:
        parent_info = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_info.st_mode):
            raise ValidationError("%s parent must be a directory: %s" % (label, parent))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(output.name, flags, 0o600, dir_fd=parent_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(redact_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n"))
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise ValidationError("%s already exists: %s" % (label, output))
    except OSError as exc:
        try:
            os.unlink(output.name, dir_fd=parent_fd)
        except OSError:
            pass
        raise ValidationError("failed to write %s %s: %s" % (label, output, exc.__class__.__name__))
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def validate_run_manifest(manifest: Dict, source: str = "<memory>") -> None:
    if not isinstance(manifest, dict):
        raise ValidationError("%s must contain a JSON object" % source)
    _validate_allowed_fields(manifest, RUN_MANIFEST_FIELDS, source)
    if manifest.get("schema") != RUN_MANIFEST_SCHEMA:
        raise ValidationError("%s must set schema to %s" % (source, RUN_MANIFEST_SCHEMA))
    for field in ["run_id", "system"]:
        if not isinstance(manifest.get(field), str) or not SAFE_ID.match(manifest[field]):
            raise ValidationError("%s %s must be a safe identifier" % (source, field))
    if not isinstance(manifest.get("suite"), str) or not manifest["suite"].strip():
        raise ValidationError("%s suite must be a non-empty string" % source)
    generated_at = manifest.get("generated_at_utc")
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise ValidationError("%s generated_at_utc must be a non-empty string" % source)
    if not isinstance(manifest.get("live_run"), bool):
        raise ValidationError("%s live_run must be boolean" % source)
    _validate_string_object(
        manifest.get("repository"),
        "%s repository" % source,
        allowed=REPOSITORY_FIELDS,
        required=["state"],
    )
    _validate_string_object(
        manifest.get("model"),
        "%s model" % source,
        allowed=MODEL_FIELDS,
        required=["provider", "name", "effort"],
    )
    _validate_run_budget(manifest.get("budget"), "%s budget" % source)
    _validate_run_permissions(manifest.get("permissions"), "%s permissions" % source)
    _validate_run_scoring(manifest.get("scoring"), "%s scoring" % source)
    _validate_run_safety(manifest.get("safety"), "%s safety" % source)


def validate_run_manifest_matches_report(manifest: Dict, report: Dict, source: str = "<memory>") -> None:
    public_manifest = {
        key: value for key, value in manifest.items() if not str(key).startswith("_")
    } if isinstance(manifest, dict) else manifest
    public_report = {
        key: value for key, value in report.items() if not str(key).startswith("_")
    } if isinstance(report, dict) else report
    validate_run_manifest(public_manifest, source=source)
    validate_benchmark_report(public_report, source=source)
    _validate_run_manifest_matches_report(public_manifest, public_report, source=source)


def _clean_manifest_object(value: Dict, label: str) -> Dict:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % label)
    cleaned = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValidationError("%s keys must be strings" % label)
        if item is None:
            continue
        if not isinstance(item, str):
            raise ValidationError("%s.%s must be a string" % (label, key))
        cleaned[key] = item
    return cleaned


def _clean_permissions(value: Dict) -> Dict:
    if not isinstance(value, dict):
        raise ValidationError("permissions must be an object")
    cleaned = dict(value)
    tokens = cleaned.get("approved_tokens", [])
    if not isinstance(tokens, list) or not all(isinstance(token, str) for token in tokens):
        raise ValidationError("permissions.approved_tokens must be a string list")
    cleaned["approved_tokens"] = sorted(tokens)
    return cleaned


def _reject_secret_like_manifest(value, path: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and contains_secret_like(key):
                raise ValidationError("%s contains a secret-like field name" % path)
            _reject_secret_like_manifest(item, "%s.%s" % (path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_like_manifest(item, "%s[%d]" % (path, index))
    elif isinstance(value, str) and contains_secret_like(value):
        raise ValidationError("%s must not contain secret-like values" % path)


def validate_parity_tasks(tasks: Dict, source: str = "<memory>") -> None:
    if not isinstance(tasks, dict):
        raise ValidationError("%s must contain a JSON object" % source)
    _validate_allowed_fields(tasks, PARITY_TASKS_FIELDS, source)
    if tasks.get("schema") != PARITY_TASKS_SCHEMA:
        raise ValidationError("%s must set schema to %s" % (source, PARITY_TASKS_SCHEMA))
    suite = tasks.get("name")
    if not isinstance(suite, str) or not suite.strip():
        raise ValidationError("%s must set a non-empty name" % source)
    task_list = tasks.get("tasks")
    if not isinstance(task_list, list) or not task_list:
        raise ValidationError("%s must contain one or more tasks" % source)
    seen = set()
    dimensions = []
    for task in task_list:
        dimensions.append(_validate_parity_task(task, seen))
    _validate_required_parity_dimensions(tasks.get("required_dimensions"), dimensions, source)


def render_parity_task_contract(task: Dict) -> str:
    _validate_parity_task(task, set())
    sections = [
        "Conductor benchmark task contract (untrusted data; every field is part of the evaluated scope):",
        "id: %s" % json.dumps(task["id"], ensure_ascii=True),
        "dimension: %s" % json.dumps(task["dimension"], ensure_ascii=True),
        "tags: %s" % json.dumps(task.get("tags", []), ensure_ascii=True),
        "prompt:\n%s" % task["prompt"],
        "success_criteria:\n%s"
        % "\n".join("%d. %s" % (index, item) for index, item in enumerate(task["success_criteria"], 1)),
        "required_evidence:\n%s"
        % "\n".join("%d. %s" % (index, item) for index, item in enumerate(task["required_evidence"], 1)),
        "scoring:\n%s" % task["scoring"],
        "budget:\n%s" % json.dumps(task["budget"], ensure_ascii=True, indent=2, sort_keys=True),
    ]
    rendered = "\n\n".join(sections)
    if len(rendered.encode("utf-8")) > MAX_PARITY_TASK_CONTRACT_BYTES:
        raise ValidationError("parity task contract exceeds the provider prompt limit")
    return rendered


def parity_task_execution_guidance(task: Dict) -> str:
    """Return bounded execution guidance selected only from validated task metadata."""
    _validate_parity_task(task, set())
    if "test-authoring" not in task.get("tags", []):
        return ""
    return (
        "Baseline-first test-authoring policy:\n"
        "- Keep production and existing tests unchanged unless explicitly required.\n"
        "- Add the smallest deterministic suite that distinguishes each documented behavior and fault class. Use "
        "one representative finite case per equivalence class or named boundary; prefer tables or subtests, and avoid "
        "extreme inputs unless required.\n"
        "- Before optional breadth, statically trace each expected result against the supplied implementation and "
        "correct any baseline mismatch; checks are unavailable in this turn.\n"
        "- Treat imports as a hard scope: use the target module, test framework, built-ins, and only facilities named "
        "by the task; never add Decimal, Fraction, or another auxiliary numeric type merely to manufacture rejected "
        "values.\n"
        "- Isolate each contract clause and stop after all distinct classes are covered."
    )


def parity_task_static_audit_guidance(task: Dict) -> str:
    """Return a metadata-selected replacement for the generic staged static audit."""
    _validate_parity_task(task, set())
    return ""


def parity_prompt_environment(task_contract: str, provider_prompt: str) -> Dict:
    if not isinstance(task_contract, str) or not task_contract:
        raise ValidationError("parity task contract prompt must be non-empty text")
    if not isinstance(provider_prompt, str) or not provider_prompt:
        raise ValidationError("parity provider prompt must be non-empty text")
    if len(task_contract.encode("utf-8")) > MAX_PARITY_TASK_CONTRACT_BYTES:
        raise ValidationError("parity task contract exceeds the provider prompt limit")
    if len(provider_prompt.encode("utf-8")) > MAX_PARITY_PROVIDER_PROMPT_BYTES:
        raise ValidationError("parity provider prompt exceeds its byte limit")
    return {
        "conductor_runtime_version": __version__,
        "task_prompt_schema": PARITY_TASK_PROMPT_SCHEMA,
        "task_contract_prompt_sha256": hashlib.sha256(task_contract.encode("utf-8")).hexdigest(),
        "provider_prompt_sha256": hashlib.sha256(provider_prompt.encode("utf-8")).hexdigest(),
    }


def validate_live_parity_runbook(runbook: Dict, source: str = "<memory>") -> None:
    if not isinstance(runbook, dict):
        raise ValidationError("%s must contain a JSON object" % source)
    _validate_allowed_fields(runbook, LIVE_PARITY_RUNBOOK_FIELDS, source)
    if runbook.get("schema") != LIVE_PARITY_RUNBOOK_SCHEMA:
        raise ValidationError("%s must set schema to %s" % (source, LIVE_PARITY_RUNBOOK_SCHEMA))
    if not isinstance(runbook.get("name"), str) or not SAFE_ID.match(runbook["name"]):
        raise ValidationError("%s name must be a safe identifier" % source)
    if not isinstance(runbook.get("suite"), str) or not runbook["suite"].strip():
        raise ValidationError("%s suite must be a non-empty string" % source)
    if not isinstance(runbook.get("generated_at_utc"), str) or not runbook["generated_at_utc"].strip():
        raise ValidationError("%s generated_at_utc must be a non-empty string" % source)
    if runbook.get("evidence_status") != LIVE_PARITY_RUNBOOK_EVIDENCE_STATUS:
        raise ValidationError("%s evidence_status must be %s" % (source, LIVE_PARITY_RUNBOOK_EVIDENCE_STATUS))
    _validate_live_runbook_source(runbook.get("source"), source)
    _validate_run_budget(runbook.get("aggregate_budget"), "%s aggregate_budget" % source)
    system_names = _validate_live_runbook_systems(runbook.get("systems"), source)
    task_ids = _validate_live_runbook_tasks(runbook.get("tasks"), system_names, source)
    source_task_count = runbook["source"]["task_count"]
    if source_task_count != len(task_ids):
        raise ValidationError("%s source.task_count must match tasks length" % source)
    _validate_non_empty_string_list(runbook.get("operator_checklist"), "%s operator_checklist" % source)
    _validate_live_runbook_claim_gate(runbook.get("claim_gate"), source)
    _reject_secret_like_manifest(runbook)


def _validate_live_runbook_source(source_value, source: str) -> None:
    if not isinstance(source_value, dict):
        raise ValidationError("%s source must be an object" % source)
    _validate_allowed_fields(source_value, LIVE_PARITY_RUNBOOK_SOURCE_FIELDS, "%s source" % source)
    if source_value.get("parity_tasks_schema") != PARITY_TASKS_SCHEMA:
        raise ValidationError("%s source.parity_tasks_schema must be %s" % (source, PARITY_TASKS_SCHEMA))
    source_path = source_value.get("parity_tasks_source")
    if source_path is not None and not isinstance(source_path, str):
        raise ValidationError("%s source.parity_tasks_source must be a string or null" % source)
    sha = source_value.get("parity_tasks_sha256")
    if not isinstance(sha, str) or len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
        raise ValidationError("%s source.parity_tasks_sha256 must be a sha256 hex string" % source)
    _validate_int_range(source_value.get("task_count"), "%s source.task_count" % source, minimum=1)
    _validate_non_empty_string_list(source_value.get("required_dimensions"), "%s source.required_dimensions" % source)


def _validate_live_runbook_systems(systems, source: str) -> List[str]:
    if not isinstance(systems, list) or len(systems) < 2:
        raise ValidationError("%s systems must contain at least two systems" % source)
    seen = set()
    names = []
    for system in systems:
        if not isinstance(system, dict):
            raise ValidationError("%s systems must contain objects" % source)
        _validate_allowed_fields(system, LIVE_PARITY_RUNBOOK_SYSTEM_FIELDS, "%s system" % source)
        name = system.get("name")
        if not isinstance(name, str) or not SAFE_ID.match(name):
            raise ValidationError("%s system name must be a safe identifier" % source)
        if name in seen:
            raise ValidationError("%s duplicate system %s" % (source, name))
        seen.add(name)
        names.append(name)
        for field in ["provider", "tool", "model_effort"]:
            if not isinstance(system.get(field), str) or not system[field].strip():
                raise ValidationError("%s system %s %s must be a non-empty string" % (source, name, field))
        for field in ["requires_external_tool", "requires_paid_approval"]:
            if not isinstance(system.get(field), bool):
                raise ValidationError("%s system %s %s must be boolean" % (source, name, field))
        for field in ["report_path", "manifest_path"]:
            _clean_runbook_path(system.get(field), "%s system %s %s" % (source, name, field))
        _validate_string_object(
            system.get("model_manifest_template"),
            "%s system %s model_manifest_template" % (source, name),
            allowed=MODEL_FIELDS,
            required=["provider", "name", "effort"],
        )
        _validate_run_permissions(
            system.get("permissions_template"),
            "%s system %s permissions_template" % (source, name),
        )
        _validate_live_runbook_invocations(system.get("invocation_templates"), "%s system %s" % (source, name))
        _validate_non_empty_string_list(system.get("notes"), "%s system %s notes" % (source, name))
    return names


def _validate_live_runbook_invocations(invocations, label: str) -> None:
    if not isinstance(invocations, list) or not invocations:
        raise ValidationError("%s invocation_templates must be a non-empty array" % label)
    for invocation in invocations:
        if not isinstance(invocation, dict):
            raise ValidationError("%s invocation_templates must contain objects" % label)
        _validate_allowed_fields(invocation, LIVE_PARITY_RUNBOOK_INVOCATION_FIELDS, "%s invocation" % label)
        for field in ["description", "status"]:
            if not isinstance(invocation.get(field), str) or not invocation[field].strip():
                raise ValidationError("%s invocation %s must be a non-empty string" % (label, field))
        for field in ["launches_external_tools", "requires_paid_approval"]:
            if not isinstance(invocation.get(field), bool):
                raise ValidationError("%s invocation %s must be boolean" % (label, field))
        argv = invocation.get("argv_template")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item.strip() for item in argv):
            raise ValidationError("%s invocation argv_template must be a non-empty string array" % label)
        _validate_non_empty_string_list(invocation.get("notes"), "%s invocation notes" % label)


def _validate_live_runbook_tasks(tasks, system_names: List[str], source: str) -> List[str]:
    if not isinstance(tasks, list) or not tasks:
        raise ValidationError("%s tasks must be a non-empty array" % source)
    seen = set()
    ids = []
    for task in tasks:
        if not isinstance(task, dict):
            raise ValidationError("%s tasks must contain objects" % source)
        _validate_allowed_fields(task, LIVE_PARITY_RUNBOOK_TASK_FIELDS, "%s task" % source)
        task_id = task.get("id")
        if not isinstance(task_id, str) or not SAFE_ID.match(task_id):
            raise ValidationError("%s task id must be a safe identifier" % source)
        if task_id in seen:
            raise ValidationError("%s duplicate task %s" % (source, task_id))
        seen.add(task_id)
        ids.append(task_id)
        for field in ["dimension", "prompt", "scoring"]:
            if not isinstance(task.get(field), str) or not task[field].strip():
                raise ValidationError("%s task %s %s must be a non-empty string" % (source, task_id, field))
        _validate_non_empty_string_list(task.get("tags"), "%s task %s tags" % (source, task_id))
        _clean_runbook_path(task.get("prompt_path"), "%s task %s prompt_path" % (source, task_id))
        _validate_non_empty_string_list(task.get("success_criteria"), "%s task %s success_criteria" % (source, task_id))
        _validate_non_empty_string_list(task.get("required_evidence"), "%s task %s required_evidence" % (source, task_id))
        _validate_run_budget(task.get("budget"), "%s task %s budget" % (source, task_id))
        _validate_live_runbook_scorecard(task.get("scorecard_template"), "%s task %s" % (source, task_id))
        _validate_live_runbook_safety_template(task.get("safety_incident_template"), task_id, "%s task %s" % (source, task_id))
        artifacts = task.get("system_artifacts")
        if not isinstance(artifacts, dict):
            raise ValidationError("%s task %s system_artifacts must be an object" % (source, task_id))
        if set(artifacts) != set(system_names):
            raise ValidationError("%s task %s system_artifacts must match runbook systems" % (source, task_id))
        for system, paths in artifacts.items():
            if not isinstance(paths, dict) or not paths:
                raise ValidationError("%s task %s system_artifacts.%s must be a non-empty object" % (source, task_id, system))
            for key, value in paths.items():
                if not isinstance(key, str) or not key:
                    raise ValidationError("%s task %s system artifact keys must be strings" % (source, task_id))
                _clean_runbook_path(value, "%s task %s system_artifacts.%s.%s" % (source, task_id, system, key))
    return ids


def _validate_live_runbook_scorecard(scorecard, label: str) -> None:
    if not isinstance(scorecard, dict):
        raise ValidationError("%s scorecard_template must be an object" % label)
    _validate_allowed_fields(scorecard, LIVE_PARITY_RUNBOOK_SCORECARD_FIELDS, "%s scorecard_template" % label)
    if scorecard.get("score") is not None:
        _validate_number_range(scorecard["score"], "%s scorecard_template.score" % label, minimum=0)
    _validate_int_range(scorecard.get("max_score"), "%s scorecard_template.max_score" % label, minimum=1)
    if scorecard.get("score") is not None and scorecard["score"] > scorecard["max_score"]:
        raise ValidationError("%s scorecard_template.score must be <= max_score" % label)
    if not isinstance(scorecard.get("notes"), str) or not scorecard["notes"].strip():
        raise ValidationError("%s scorecard_template.notes must be a non-empty string" % label)


def _validate_live_runbook_safety_template(template, task_id: str, label: str) -> None:
    if not isinstance(template, dict):
        raise ValidationError("%s safety_incident_template must be an object" % label)
    _validate_allowed_fields(template, LIVE_PARITY_RUNBOOK_SAFETY_TEMPLATE_FIELDS, "%s safety_incident_template" % label)
    for field in ["id", "task_id", "description"]:
        if not isinstance(template.get(field), str) or not template[field].strip():
            raise ValidationError("%s safety_incident_template.%s must be a non-empty string" % (label, field))
    if template["task_id"] != task_id:
        raise ValidationError("%s safety_incident_template.task_id must match task id" % label)
    if template.get("severity") not in SAFETY_SEVERITIES:
        raise ValidationError("%s safety_incident_template.severity must be a known severity" % label)


def _validate_live_runbook_claim_gate(claim_gate, source: str) -> None:
    if not isinstance(claim_gate, dict):
        raise ValidationError("%s claim_gate must be an object" % source)
    _validate_allowed_fields(claim_gate, LIVE_PARITY_RUNBOOK_CLAIM_GATE_FIELDS, "%s claim_gate" % source)
    if claim_gate.get("eligible_for_parity_claims") is not False:
        raise ValidationError("%s claim_gate.eligible_for_parity_claims must be false for runbooks" % source)
    _validate_non_empty_string_list(claim_gate.get("required_before_claim"), "%s claim_gate.required_before_claim" % source)
    _clean_runbook_path(claim_gate.get("comparison_template"), "%s claim_gate.comparison_template" % source)
    command = claim_gate.get("claim_check_command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise ValidationError("%s claim_gate.claim_check_command must be a non-empty string array" % source)


def _validate_parity_task(task: Dict, seen: set) -> str:
    if not isinstance(task, dict):
        raise ValidationError("parity tasks must be objects")
    _validate_allowed_fields(task, PARITY_TASK_FIELDS, "parity task")
    task_id = task.get("id")
    if not isinstance(task_id, str) or not SAFE_ID.match(task_id):
        raise ValidationError("parity task id must be a safe identifier")
    if task_id in seen:
        raise ValidationError("duplicate parity task id %s" % task_id)
    seen.add(task_id)
    for field in ["dimension", "prompt", "scoring"]:
        if not isinstance(task.get(field), str) or not task[field].strip():
            raise ValidationError("parity task %s must set non-empty %s" % (task_id, field))
    for field in ["success_criteria", "required_evidence"]:
        _validate_non_empty_string_list(task.get(field), "parity task %s %s" % (task_id, field))
    if "tags" in task:
        _validate_non_empty_string_list(task["tags"], "parity task %s tags" % task_id)
    budget = task.get("budget")
    if not isinstance(budget, dict):
        raise ValidationError("parity task %s must set budget object" % task_id)
    _validate_allowed_fields(budget, PARITY_BUDGET_FIELDS, "parity task %s budget" % task_id)
    _validate_int_range(budget.get("max_minutes"), "parity task %s budget.max_minutes" % task_id, minimum=1)
    _validate_int_range(budget.get("max_agents"), "parity task %s budget.max_agents" % task_id, minimum=0)
    live_tool_cost = budget.get("max_live_tool_cost")
    if live_tool_cost not in LIVE_TOOL_COST_LEVELS:
        raise ValidationError(
            "parity task %s budget.max_live_tool_cost must be one of none, low, medium, high" % task_id
        )
    if "max_cost_usd" in budget:
        _validate_number_range(budget["max_cost_usd"], "parity task %s budget.max_cost_usd" % task_id, minimum=0)
    if "max_tokens" in budget:
        _validate_int_range(budget["max_tokens"], "parity task %s budget.max_tokens" % task_id, minimum=0)
    return task["dimension"].strip()


def _validate_required_parity_dimensions(required, dimensions: List[str], source: str) -> None:
    if required is None:
        return
    _validate_non_empty_string_list(required, "%s required_dimensions" % source)
    normalized_required = [dimension.strip() for dimension in required]
    duplicates = sorted({dimension for dimension in normalized_required if normalized_required.count(dimension) > 1})
    if duplicates:
        raise ValidationError("%s required_dimensions contains duplicates: %s" % (source, ", ".join(duplicates)))
    covered = {dimension.strip() for dimension in dimensions}
    missing = sorted(set(normalized_required) - covered)
    if missing:
        raise ValidationError("%s missing required parity dimensions: %s" % (source, ", ".join(missing)))


def _parity_tasks_public_payload(parity_tasks: Dict) -> Dict:
    if not isinstance(parity_tasks, dict):
        return parity_tasks
    return {
        "schema": parity_tasks.get("schema"),
        "name": parity_tasks.get("name"),
        "required_dimensions": parity_tasks.get("required_dimensions", []),
        "tasks": parity_tasks.get("tasks", []),
    }


def _aggregate_parity_budget(tasks: List[Dict]) -> Dict:
    max_minutes = 0
    max_agents = 0
    max_cost_usd = 0.0
    max_tokens = 0
    highest_live_tool_cost = "none"
    for task in tasks:
        budget = task["budget"]
        max_minutes += budget["max_minutes"]
        max_agents = max(max_agents, budget["max_agents"])
        current_level = budget["max_live_tool_cost"]
        if LIVE_TOOL_COST_ORDER.index(current_level) > LIVE_TOOL_COST_ORDER.index(highest_live_tool_cost):
            highest_live_tool_cost = current_level
        if "max_cost_usd" in budget:
            max_cost_usd += float(budget["max_cost_usd"])
        if "max_tokens" in budget:
            max_tokens += int(budget["max_tokens"])
    aggregate = {
        "max_minutes": max_minutes,
        "max_agents": max_agents,
        "max_live_tool_cost": highest_live_tool_cost,
    }
    if max_cost_usd:
        aggregate["max_cost_usd"] = round(max_cost_usd, 6)
    if max_tokens:
        aggregate["max_tokens"] = max_tokens
    return aggregate


def _live_runbook_system(name: str, report_root: str) -> Dict:
    if name == "conductor":
        return {
            "name": "conductor",
            "provider": "openai",
            "tool": "conductor_runtime plus codex",
            "model_effort": "high-codex-model-planner",
            "requires_external_tool": True,
            "requires_paid_approval": True,
            "report_path": _join_runbook_path(report_root, "conductor-parity-report.json"),
            "manifest_path": _join_runbook_path(report_root, "conductor-parity-manifest.json"),
            "model_manifest_template": {
                "provider": "openai",
                "name": "codex",
                "effort": "high",
                "version": "operator-recorded",
            },
            "permissions_template": {
                "allow_writes": True,
                "allow_destructive": False,
                "allow_network": False,
                "allow_agent": True,
                "allow_parallel": True,
                "approved_tokens": [],
            },
            "invocation_templates": [
                {
                    "description": "Strict source-isolated Codex read-only run with command-bound model and terminal token evidence.",
                    "status": "candidate-template-not-run",
                    "launches_external_tools": True,
                    "requires_paid_approval": True,
                    "argv_template": [
                        "python3",
                        "-B",
                        "-m",
                        "conductor_runtime",
                        "run-codex-readonly-task",
                        "<parity_tasks_path>",
                        "<task.id>",
                        "--workspace",
                        "<repository_path>",
                        "--output",
                        "<task.system_artifacts.conductor.readonly_report_path>",
                        "--stage-dir",
                        "<task.system_artifacts.conductor.readonly_stage_dir>",
                        "--model",
                        "<operator-approved-codex-model>",
                        "--effort",
                        "<operator-approved-codex-effort>",
                        "--max-tokens",
                        "<task.budget.max_tokens>",
                        "--timeout-seconds",
                        "<task.budget.max_minutes-seconds>",
                        "--allow-agent",
                        "--allow-network",
                        "--approve",
                        "codex-live-run",
                    ],
                    "notes": [
                        "Use for read-only audit, planning, and evidence tasks before comparing with Claude's read-only adapter.",
                        "The runtime uses an external clone, an ephemeral minimal-read permission profile, ignored project/user instructions, disabled network tools/plugins/subagents, and a sanitized shell environment.",
                        "Codex JSONL does not report the resolved model, so evidence records command-enforced model provenance. The experimental rollout-budget feature provides a runtime-hard weighted token cap and terminal usage is post-validated when available.",
                    ],
                },
                {
                    "description": "Strict source-isolated Codex staged-write run with a runtime token cap, deterministic verification, and patch evidence.",
                    "status": "candidate-template-not-run",
                    "launches_external_tools": True,
                    "requires_paid_approval": True,
                    "argv_template": [
                        "python3",
                        "-B",
                        "-m",
                        "conductor_runtime",
                        "run-codex-staged-task",
                        "<parity_tasks_path>",
                        "<task.id>",
                        "--workspace",
                        "<repository_path>",
                        "--output",
                        "<task.system_artifacts.conductor.staged_report_path>",
                        "--stage-dir",
                        "<task.system_artifacts.conductor.staged_stage_dir>",
                        "--patch-output",
                        "<task.system_artifacts.conductor.patch_path>",
                        "--check-command-json",
                        "<task.verifier_command_json>",
                        "--model",
                        "<operator-approved-codex-model>",
                        "--effort",
                        "<operator-approved-codex-effort>",
                        "--max-tokens",
                        "<task.budget.max_tokens>",
                        "--timeout-seconds",
                        "<task.budget.max_minutes-seconds>",
                        "--allow-agent",
                        "--allow-network",
                        "--allow-writes",
                        "--approve",
                        "codex-live-run",
                        "--approve",
                        "codex-staged-write",
                    ],
                    "notes": [
                        "Use for implementation tasks with an operator-supplied deterministic verifier.",
                        "Codex receives a minimal-read, stage-write permission profile with network, external reads, temp writes, hooks, plugins, and subagents denied.",
                        "The source repository is never edited or auto-applied; inspect and apply the retained external patch separately.",
                    ],
                },
                {
                    "description": "Bounded source-isolated Codex staged repair with verifier feedback and cumulative patch evidence.",
                    "status": "candidate-template-not-run",
                    "launches_external_tools": True,
                    "requires_paid_approval": True,
                    "argv_template": [
                        "python3",
                        "-B",
                        "-m",
                        "conductor_runtime",
                        "run-codex-staged-repair",
                        "<parity_tasks_path>",
                        "<task.id>",
                        "--workspace",
                        "<repository_path>",
                        "--repair-dir",
                        "<task.system_artifacts.conductor.repair_dir>",
                        "--check-command-json",
                        "<task.verifier_command_json>",
                        "--model",
                        "<operator-approved-codex-model>",
                        "--effort",
                        "<operator-approved-codex-effort>",
                        "--max-attempts",
                        "<operator-approved-repair-attempts>",
                        "--max-tokens-per-attempt",
                        "<operator-approved-tokens-per-repair-attempt>",
                        "--provider-timeout-seconds",
                        "<operator-approved-provider-timeout-seconds>",
                        "--check-timeout-seconds",
                        "<operator-approved-check-timeout-seconds>",
                        "--allow-agent",
                        "--allow-network",
                        "--allow-writes",
                        "--approve",
                        "codex-live-run",
                        "--approve",
                        "codex-staged-write",
                    ],
                    "notes": [
                        "Partition the task's total token and time budgets across attempts; the runtime rejects a worst-case aggregate above the fixture.",
                        "Only verifier failure or no-change outcomes retry, using bounded redacted diagnostics and a fresh copy of the prior stage.",
                        "The repair directory includes benchmark-report.json; score that aggregate report before writing a live manifest or comparing systems.",
                        "The cumulative patch is never auto-applied, and claim eligibility still requires equivalent live manifests and independent scoring.",
                    ],
                },
                {
                    "description": "Use read-only Codex to generate one reviewed Conductor workflow draft and receipt per parity task.",
                    "status": "candidate-template-not-run",
                    "launches_external_tools": True,
                    "requires_paid_approval": True,
                    "argv_template": [
                        "python3",
                        "-B",
                        "-m",
                        "conductor_runtime",
                        "plan-model-workflow",
                        "--task-file",
                        "<task.prompt_path>",
                        "--workspace",
                        ".",
                        "--name",
                        "<task.id>",
                        "--planner-effort",
                        "high",
                        "--write-capable",
                        "--allow-agent",
                        "--output",
                        "<task.system_artifacts.conductor.workflow_path>",
                        "--receipt",
                        "<task.system_artifacts.conductor.workflow_receipt_path>",
                    ],
                    "notes": [
                        "Obtain explicit approval for the Codex planning call and record the actual model resolved at high effort.",
                        "Validate the receipt and score model decomposition quality before executing the draft.",
                        "Review each generated workflow before execution.",
                        "Executing generated workflows still requires normal Conductor capability flags and approval tokens.",
                    ],
                },
                {
                    "description": "Generate a deterministic Ultracode-profile workflow draft when a no-model planning baseline is needed.",
                    "status": "template-not-run",
                    "launches_external_tools": False,
                    "requires_paid_approval": False,
                    "argv_template": [
                        "python3",
                        "-B",
                        "-m",
                        "conductor_runtime",
                        "plan-workflow",
                        "--profile",
                        "ultracode",
                        "--task-file",
                        "<task.prompt_path>",
                        "--write-capable",
                        "--output",
                        "<task.system_artifacts.conductor.deterministic_workflow_path>",
                    ],
                    "notes": [
                        "Use this only as a planning baseline; it does not substitute for the model-planning comparison.",
                        "Review the generated workflow before execution.",
                    ],
                }
            ],
            "notes": [
                "This runbook does not execute Codex or generated workflows.",
                "Record the actual Codex model, effort, version, permissions, budget, scorecards, and safety incidents after the live run.",
            ],
        }
    if name == "claude":
        return {
            "name": "claude",
            "provider": "anthropic",
            "tool": "claude-code",
            "model_effort": "sonnet-ultracode",
            "requires_external_tool": True,
            "requires_paid_approval": True,
            "report_path": _join_runbook_path(report_root, "claude-parity-report.json"),
            "manifest_path": _join_runbook_path(report_root, "claude-parity-manifest.json"),
            "model_manifest_template": {
                "provider": "anthropic",
                "name": "sonnet",
                "effort": "ultracode",
                "version": "operator-recorded",
            },
            "permissions_template": {
                "allow_writes": True,
                "allow_destructive": False,
                "allow_network": True,
                "allow_agent": True,
                "allow_parallel": False,
                "approved_tokens": [],
            },
            "invocation_templates": [
                {
                    "description": "Strict read-only Sonnet Ultracode run with streamed provider evidence.",
                    "status": "candidate-template-not-run",
                    "launches_external_tools": True,
                    "requires_paid_approval": True,
                    "argv_template": [
                        "python3",
                        "-B",
                        "-m",
                        "conductor_runtime",
                        "run-claude-readonly-task",
                        "<parity_tasks_path>",
                        "<task.id>",
                        "--workspace",
                        "<repository_path>",
                        "--output",
                        "<task.system_artifacts.claude.readonly_report_path>",
                        "--max-budget-usd",
                        "<task.budget.max_cost_usd>",
                        "--max-turns",
                        "<operator-approved-turn-cap>",
                        "--timeout-seconds",
                        "<task.budget.max_minutes-seconds>",
                        "--allow-agent",
                        "--allow-network",
                        "--approve",
                        "claude-paid-run",
                    ],
                    "notes": [
                        "Use for audit, planning, and other tasks that need only Read, Glob, and Grep.",
                        "The runtime pipes the prompt over stdin, captures stream-json turns, and rejects non-Sonnet main output or any Opus usage.",
                        "The runtime records budget overshoot, cutoff, malformed output, and redacted partial evidence instead of retrying.",
                    ],
                },
                {
                    "description": "Strict staged-write Sonnet Ultracode run with source isolation, deterministic verification, and patch evidence.",
                    "status": "candidate-template-not-run",
                    "launches_external_tools": True,
                    "requires_paid_approval": True,
                    "argv_template": [
                        "python3",
                        "-B",
                        "-m",
                        "conductor_runtime",
                        "run-claude-staged-task",
                        "<parity_tasks_path>",
                        "<task.id>",
                        "--workspace",
                        "<repository_path>",
                        "--output",
                        "<task.system_artifacts.claude.staged_report_path>",
                        "--stage-dir",
                        "<task.system_artifacts.claude.stage_dir>",
                        "--patch-output",
                        "<task.system_artifacts.claude.patch_path>",
                        "--check-command-json",
                        "<task.verifier_command_json>",
                        "--max-budget-usd",
                        "<task.budget.max_cost_usd>",
                        "--max-turns",
                        "<operator-approved-turn-cap>",
                        "--timeout-seconds",
                        "<task.budget.max_minutes-seconds>",
                        "--allow-agent",
                        "--allow-network",
                        "--allow-writes",
                        "--approve",
                        "claude-paid-run",
                        "--approve",
                        "claude-staged-write",
                    ],
                    "notes": [
                        "Requires Claude Code 2.1.203 or later for --effort ultracode.",
                        "The runtime pins Sonnet and Ultracode internally and exposes only Read, Glob, Grep, Edit, and Write; Claude receives no Bash tool.",
                        "The source repository is never edited or auto-applied; inspect and apply the external patch separately.",
                        "Use a deterministic verifier command accepted by Conductor's shell policy.",
                    ],
                }
            ],
            "notes": [
                "This runbook does not launch Claude Code.",
                "Record actual Claude Code version, effort mode, permissions, budget, scorecards, and safety incidents after the live run.",
            ],
        }
    raise ValidationError("unsupported live parity runbook system %s" % name)


def _live_runbook_task(task: Dict, workflow_root: str, report_root: str) -> Dict:
    task_id = task["id"]
    prompt = redact_text(task["prompt"])
    task_budget = dict(task["budget"])
    return {
        "id": task_id,
        "dimension": redact_text(task["dimension"]),
        "tags": [redact_text(tag) for tag in task.get("tags", [])],
        "prompt": prompt,
        "prompt_path": _join_runbook_path(workflow_root, "prompts", "%s.txt" % task_id),
        "success_criteria": [redact_text(item) for item in task["success_criteria"]],
        "required_evidence": [redact_text(item) for item in task["required_evidence"]],
        "scoring": redact_text(task["scoring"]),
        "budget": task_budget,
        "scorecard_template": {
            "score": None,
            "max_score": 10,
            "notes": "Fill after live execution using the task scoring rubric and captured evidence.",
        },
        "safety_incident_template": {
            "id": "%s-incident-1" % task_id,
            "severity": "info",
            "task_id": task_id,
            "description": "Replace with any observed safety incident, or omit in the final run manifest if none occurred.",
        },
        "system_artifacts": {
            "conductor": {
                "readonly_report_path": _join_runbook_path(
                    report_root, "evidence", "conductor", "%s.readonly.json" % task_id
                ),
                "readonly_stage_dir": _join_runbook_path(report_root, "stages", "conductor", task_id),
                "staged_report_path": _join_runbook_path(
                    report_root, "evidence", "conductor", "%s.staged.json" % task_id
                ),
                "staged_stage_dir": _join_runbook_path(
                    report_root, "stages", "conductor-staged", task_id
                ),
                "patch_path": _join_runbook_path(
                    report_root, "patches", "conductor", "%s.patch" % task_id
                ),
                "repair_dir": _join_runbook_path(
                    report_root, "repairs", "conductor", task_id
                ),
                "workflow_path": _join_runbook_path(workflow_root, "conductor", "%s.json" % task_id),
                "workflow_receipt_path": _join_runbook_path(
                    workflow_root, "conductor", "%s.receipt.json" % task_id
                ),
                "deterministic_workflow_path": _join_runbook_path(
                    workflow_root, "conductor", "%s.deterministic.json" % task_id
                ),
                "run_evidence_dir": _join_runbook_path(report_root, "evidence", "conductor", task_id),
                "scorecard_path": _join_runbook_path(report_root, "scorecards", "conductor", "%s.json" % task_id),
            },
            "claude": {
                "readonly_report_path": _join_runbook_path(report_root, "evidence", "claude", "%s.readonly.json" % task_id),
                "staged_report_path": _join_runbook_path(report_root, "evidence", "claude", "%s.staged.json" % task_id),
                "stage_dir": _join_runbook_path(report_root, "stages", "claude", task_id),
                "patch_path": _join_runbook_path(report_root, "patches", "claude", "%s.patch" % task_id),
                "scorecard_path": _join_runbook_path(report_root, "scorecards", "claude", "%s.json" % task_id),
            },
        },
    }


def _join_runbook_path(root: str, *parts: str) -> str:
    cleaned = root.rstrip("/")
    for part in parts:
        if not isinstance(part, str) or not part:
            raise ValidationError("live parity runbook path parts must be non-empty strings")
        cleaned += "/" + part.strip("/")
    return _clean_runbook_path(cleaned, "live parity runbook path")


def _clean_runbook_path(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s must be a non-empty relative path" % label)
    cleaned = value.strip()
    if Path(cleaned).is_absolute():
        raise ValidationError("%s must be a relative path" % label)
    require_no_path_escape(cleaned)
    if contains_secret_like(cleaned):
        raise ValidationError("%s must not contain secret-like values" % label)
    return redact_text(cleaned)


def _sha256_json(value: Dict) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _validate_non_empty_string_list(value, name: str) -> None:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValidationError("%s must be a non-empty string list" % name)


def _benchmark_child_environment() -> Dict[str, str]:
    environment = os.environ.copy()
    runtime_path = Path(sys.argv[0]).resolve()
    if runtime_path.suffix != ".pyz" or not runtime_path.is_file():
        return environment
    existing = environment.get("PYTHONPATH", "")
    entries = [entry for entry in existing.split(os.pathsep) if entry]
    runtime_text = str(runtime_path)
    if runtime_text not in entries:
        environment["PYTHONPATH"] = os.pathsep.join([runtime_text, *entries])
    return environment


def run_benchmark_suite(
    suite: Dict,
    workspace: Path,
    policy: RuntimePolicy,
    system_name: str = "conductor",
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
) -> Dict:
    validate_benchmark_suite(suite)
    _validate_int_range(
        output_limit_bytes,
        "benchmark output_limit_bytes",
        minimum=1,
        maximum=MAX_OUTPUT_LIMIT_BYTES,
    )
    started = time.time()
    results: List[Dict] = []
    child_environment = _benchmark_child_environment()
    for task in suite["tasks"]:
        assessment = enforce_shell_policy({**task, "risk": task.get("risk", "low")}, policy)
        timeout = int(task.get("timeout_seconds", suite.get("default_timeout_seconds", 120)))
        task_started = time.time()
        result = run_process(
            argv=assessment.argv,
            cwd=workspace.resolve(),
            timeout=timeout,
            output_limit_bytes=int(task.get("output_limit_bytes", suite.get("output_limit_bytes", output_limit_bytes))),
            env=child_environment,
        )
        duration_ms = int((time.time() - task_started) * 1000)
        stdout = redact_text(result.stdout)
        stderr = redact_text(result.stderr)
        combined = stdout + "\n" + stderr
        success_contains = task.get("success_contains")
        passed = result.returncode == 0 and not result.timed_out
        if success_contains:
            passed = passed and success_contains in combined
        results.append(
            {
                "id": task["id"],
                "description": task.get("description", ""),
                "passed": passed,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "duration_ms": duration_ms,
                "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
                "stdout": stdout,
                "stderr": stderr,
            }
        )
    total_duration_ms = int((time.time() - started) * 1000)
    passed_count = sum(1 for result in results if result["passed"])
    return {
        "schema": BENCHMARK_REPORT_SCHEMA,
        "suite": suite["name"],
        "suite_source": _redact_optional_path(suite.get("_source_path")),
        "system": system_name,
        "started_at_utc": utc_from_timestamp(started).isoformat(timespec="seconds") + "Z",
        "environment": {
            "python": "%s.%s.%s" % sys.version_info[:3],
            "platform": platform.platform(),
            "codex": _tool_path("codex"),
            "claude": _tool_path("claude"),
            "claude_version": _tool_version(["claude", "--version"])
            if policy.has_approval(TOOL_VERSION_PROBE_APPROVAL)
            else "",
            "external_tool_version_probes": "enabled"
            if policy.has_approval(TOOL_VERSION_PROBE_APPROVAL)
            else "disabled",
        },
        "total_tasks": len(results),
        "passed_tasks": passed_count,
        "failed_tasks": len(results) - passed_count,
        "duration_ms": total_duration_ms,
        "results": results,
    }


def _tool_version(argv: List[str]) -> str:
    if shutil.which(argv[0]) is None:
        return ""
    try:
        result = run_process(argv=argv, cwd=Path(".").resolve(), timeout=5, output_limit_bytes=4096)
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return redact_text((result.stdout.strip() or result.stderr.strip())[:4096])


def _tool_path(name: str):
    path = shutil.which(name)
    if not path:
        return None
    return _redact_path(path)


def _redact_path(path: str) -> str:
    redacted = redact_text(path)
    try:
        candidate = Path(redacted).resolve()
        home = Path.home().resolve()
        relative = candidate.relative_to(home)
        return "~/%s" % relative
    except (OSError, ValueError):
        return redacted


def _redact_optional_path(path):
    if path is None:
        return None
    return _redact_path(str(path))


def load_comparison_manifest(path: Path) -> Dict:
    try:
        manifest = json.loads(_read_json_text(path, "comparison manifest"))
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    validate_comparison_manifest(manifest, source=str(path))
    manifest["_source_path"] = str(path)
    return manifest


def validate_comparison_manifest(manifest: Dict, source: str = "<memory>") -> None:
    if not isinstance(manifest, dict):
        raise ValidationError("%s must contain a JSON object" % source)
    if manifest.get("schema") != COMPARISON_SCHEMA:
        raise ValidationError("%s must set schema to %s" % (source, COMPARISON_SCHEMA))
    name = manifest.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("%s must set a non-empty name" % source)
    systems = manifest.get("systems")
    if not isinstance(systems, list) or len(systems) < 2:
        raise ValidationError("%s must contain at least two systems" % source)
    seen = set()
    for system in systems:
        if not isinstance(system, dict):
            raise ValidationError("comparison systems must be objects")
        system_name = system.get("name")
        if not isinstance(system_name, str) or not SAFE_ID.match(system_name):
            raise ValidationError("comparison system name must be a safe identifier")
        if system_name in seen:
            raise ValidationError("duplicate comparison system %s" % system_name)
        seen.add(system_name)
        report = system.get("report")
        if not isinstance(report, str) or not report:
            raise ValidationError("comparison system %s must set report" % system_name)
        _validate_relative_no_escape_path(report, "comparison system %s report path" % system_name)
        manifest_path = system.get("manifest")
        if manifest_path is not None:
            if not isinstance(manifest_path, str) or not manifest_path:
                raise ValidationError("comparison system %s manifest must be a non-empty string" % system_name)
            _validate_relative_no_escape_path(
                manifest_path,
                "comparison system %s manifest path" % system_name,
            )
    _validate_claim_policy(manifest.get("claim_policy"), source)


def _validate_claim_policy(policy, source: str) -> None:
    if policy is None:
        return
    if not isinstance(policy, dict):
        raise ValidationError("%s claim_policy must be an object" % source)
    _validate_allowed_fields(policy, CLAIM_POLICY_FIELDS, "%s claim_policy" % source)
    model = policy.get("model")
    if model is None:
        return
    if not isinstance(model, dict):
        raise ValidationError("%s claim_policy.model must be an object" % source)
    _validate_allowed_fields(model, MODEL_CLAIM_POLICY_FIELDS, "%s claim_policy.model" % source)
    mode = model.get("mode")
    if mode not in MODEL_CLAIM_MODES:
        raise ValidationError("%s claim_policy.model.mode must be one of exact, declared-comparable" % source)
    rationale = model.get("rationale")
    if rationale is not None and (not isinstance(rationale, str) or not rationale.strip()):
        raise ValidationError("%s claim_policy.model.rationale must be a non-empty string when set" % source)
    if mode == "declared-comparable" and rationale is None:
        raise ValidationError("%s claim_policy.model.rationale is required for declared-comparable mode" % source)


def compare_benchmark_reports(manifest: Dict, base_dir: Path) -> Dict:
    validate_comparison_manifest(manifest)
    reports = []
    run_manifests = {}
    for system in manifest["systems"]:
        report_path = _resolve_comparison_path(base_dir, system["report"], "report")
        if not report_path.is_file():
            raise ValidationError("missing benchmark report for %s: %s" % (system["name"], report_path))
        report = load_benchmark_report(report_path)
        report_system = report.get("system", system["name"])
        if report_system != system["name"]:
            raise ValidationError(
                "report system %r does not match manifest system %r" % (report_system, system["name"])
            )
        if "system" not in report:
            report = dict(report)
            report["system"] = system["name"]
        reports.append(report)
        if system.get("manifest"):
            manifest_path = _resolve_comparison_path(base_dir, system["manifest"], "manifest")
            if not manifest_path.is_file():
                raise ValidationError("missing run manifest for %s: %s" % (system["name"], manifest_path))
            run_manifest = load_run_manifest(manifest_path)
            _validate_run_manifest_matches_report(run_manifest, report, source=str(manifest_path))
            run_manifests[system["name"]] = run_manifest

    suites = {report.get("suite") for report in reports}
    if len(suites) != 1:
        raise ValidationError("all reports must use the same suite")
    result_maps = [_benchmark_result_map(report) for report in reports]
    task_sets = [set(result_map) for result_map in result_maps]
    if len({tuple(sorted(task_set)) for task_set in task_sets}) != 1:
        raise ValidationError("all reports must contain the same task ids")

    task_ids = sorted(task_sets[0])
    task_comparisons = []
    for task_id in task_ids:
        entries = []
        for report, result_map in zip(reports, result_maps):
            result = result_map[task_id]
            entries.append(
                {
                    "system": report["system"],
                    "passed": bool(result["passed"]),
                    "duration_ms": result["duration_ms"],
                    "timed_out": bool(result["timed_out"]),
                    "stdout_truncated": bool(result["stdout_truncated"]),
                    "stderr_truncated": bool(result["stderr_truncated"]),
                    "score": _score_for_task(run_manifests.get(report["system"]), task_id),
                }
            )
        task_comparisons.append({"id": task_id, "systems": entries})

    system_summaries = []
    for report in reports:
        summary = _benchmark_report_summary(report)
        system_summaries.append(
            {
                "system": report["system"],
                "passed_tasks": summary["passed_tasks"],
                "failed_tasks": summary["failed_tasks"],
                "total_tasks": summary["total_tasks"],
                "duration_ms": report["duration_ms"],
                "score": _score_summary(run_manifests.get(report["system"])),
                "safety": _safety_summary(run_manifests.get(report["system"])),
            }
        )

    provenance = _comparison_provenance_summary(
        manifest["systems"],
        run_manifests,
        manifest.get("claim_policy"),
        reports=reports,
    )
    return {
        "schema": "conductor.comparison_report.v1",
        "name": manifest["name"],
        "suite": reports[0]["suite"],
        "manifest_source": _redact_optional_path(manifest.get("_source_path")),
        "generated_at_utc": utc_now().isoformat(timespec="seconds") + "Z",
        "systems": system_summaries,
        "tasks": task_comparisons,
        "provenance": provenance,
        "interpretation": "Higher passed_tasks and lower failed_tasks are better. Duration is comparable only when reports were produced on equivalent hardware, tools, models, and budgets.",
    }


def _resolve_comparison_path(base_dir: Path, path: str, kind: str) -> Path:
    require_no_path_escape(path)
    reject_symlink_path(base_dir, "comparison base directory")
    root = base_dir.resolve()
    report_path = root / path
    reject_symlink_path(report_path, "comparison %s" % kind)
    return report_path


def load_benchmark_report(path: Path) -> Dict:
    try:
        report = json.loads(_read_json_text(path, "benchmark report"))
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    validate_benchmark_report(report, source=str(path))
    return report


def _read_json_text(path: Path, label: str) -> str:
    return read_regular_text_file_no_follow(path, label, MAX_BENCHMARK_JSON_BYTES)


def validate_benchmark_report(report: Dict, source: str = "<memory>") -> None:
    if not isinstance(report, dict):
        raise ValidationError("%s must contain a JSON object" % source)
    if report.get("schema") != BENCHMARK_REPORT_SCHEMA:
        raise ValidationError("%s is not a conductor benchmark report" % source)
    if "system" in report and (not isinstance(report["system"], str) or not SAFE_ID.match(report["system"])):
        raise ValidationError("%s system must be a safe identifier when set" % source)
    if not isinstance(report.get("suite"), str) or not report["suite"].strip():
        raise ValidationError("%s must set a non-empty suite" % source)
    _validate_parity_prompt_environment(report.get("environment"), source)
    _validate_int_range(report.get("duration_ms"), "%s duration_ms" % source, minimum=0)
    for field in ["total_tasks", "passed_tasks", "failed_tasks"]:
        _validate_int_range(report.get(field), "%s %s" % (source, field), minimum=0)
    result_map = _benchmark_result_map(report, source=source)
    summary = _benchmark_report_summary(report)
    for field in ["total_tasks", "passed_tasks", "failed_tasks"]:
        if report[field] != summary[field]:
            raise ValidationError("%s %s does not match results" % (source, field))
    if report["total_tasks"] != len(result_map):
        raise ValidationError("%s total_tasks does not match unique result ids" % source)


def _validate_parity_prompt_environment(environment, source: str) -> None:
    if not isinstance(environment, dict):
        return
    present = PARITY_PROMPT_ENVIRONMENT_FIELDS & set(environment)
    if not present:
        return
    if present != PARITY_PROMPT_ENVIRONMENT_FIELDS:
        raise ValidationError("%s parity prompt provenance must be complete" % source)
    version = environment["conductor_runtime_version"]
    if (
        not isinstance(version, str)
        or len(version.split(".")) != 3
        or not all(part.isdigit() for part in version.split("."))
    ):
        raise ValidationError("%s conductor runtime version is invalid" % source)
    if environment["task_prompt_schema"] != PARITY_TASK_PROMPT_SCHEMA:
        raise ValidationError("%s parity task prompt schema is invalid" % source)
    for field in ["task_contract_prompt_sha256", "provider_prompt_sha256"]:
        value = environment[field]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValidationError("%s %s is invalid" % (source, field))


def _benchmark_result_map(report: Dict, source: str = "<memory>") -> Dict:
    results = report.get("results")
    if not isinstance(results, list) or not results:
        raise ValidationError("%s must contain one or more benchmark results" % source)
    result_map = {}
    for result in results:
        if not isinstance(result, dict):
            raise ValidationError("%s benchmark results must be objects" % source)
        result_id = result.get("id")
        if not isinstance(result_id, str) or not SAFE_ID.match(result_id):
            raise ValidationError("%s benchmark result id must be a safe identifier" % source)
        if result_id in result_map:
            raise ValidationError("%s contains duplicate benchmark result id %s" % (source, result_id))
        if not isinstance(result.get("passed"), bool):
            raise ValidationError("%s result %s passed must be boolean" % (source, result_id))
        _validate_int_range(result.get("duration_ms"), "%s result %s duration_ms" % (source, result_id), minimum=0)
        for field in ["timed_out", "stdout_truncated", "stderr_truncated"]:
            if not isinstance(result.get(field), bool):
                raise ValidationError("%s result %s %s must be boolean" % (source, result_id, field))
        quality = result.get("quality_evidence")
        if quality is not None:
            if not isinstance(result.get("execution_passed"), bool):
                raise ValidationError("%s result %s scored evidence requires execution_passed" % (source, result_id))
            validate_benchmark_quality_evidence(
                quality,
                source="%s result %s quality_evidence" % (source, result_id),
                result=result,
            )
        elif "execution_passed" in result:
            raise ValidationError("%s result %s execution_passed requires quality_evidence" % (source, result_id))
        execution_passed = result.get("execution_passed", result["passed"])
        has_repair_evidence = "repair_evidence" in result or "repair_attempt_reports" in result
        if has_repair_evidence:
            if "repair_evidence" not in result or "repair_attempt_reports" not in result:
                raise ValidationError("%s result %s repair evidence fields must be complete" % (source, result_id))
            if "provider_evidence" in result or "staged_evidence" in result:
                raise ValidationError("%s result %s cannot mix repair and single-attempt evidence" % (source, result_id))
            from .codex_staged_repair import validate_codex_staged_repair_benchmark_result

            validate_codex_staged_repair_benchmark_result(
                result,
                source="%s result %s" % (source, result_id),
            )
            if result["repair_evidence"]["suite"] != report["suite"]:
                raise ValidationError("%s result %s repair suite is inconsistent" % (source, result_id))
        if "provider_evidence" in result:
            _validate_provider_evidence(
                result["provider_evidence"],
                source="%s result %s provider_evidence" % (source, result_id),
            )
        if "staged_evidence" in result:
            if "provider_evidence" not in result:
                raise ValidationError("%s result %s staged_evidence requires provider_evidence" % (source, result_id))
            _validate_staged_evidence(
                result["staged_evidence"],
                source="%s result %s staged_evidence" % (source, result_id),
            )
            provider = result["provider_evidence"]
            if provider["policy"]["read_only_tools"]:
                raise ValidationError("%s result %s staged_evidence requires a write-capable provider" % (source, result_id))
            staged_schema = result["staged_evidence"].get("schema")
            provider_schema = provider.get("schema")
            from .claude_live import (
                CLAUDE_PROVIDER_EVIDENCE_SCHEMA,
                CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V1,
                CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V2,
            )
            from .claude_staged import CLAUDE_STAGED_EVIDENCE_SCHEMA
            from .codex_live import (
                CODEX_PROVIDER_EVIDENCE_SCHEMA,
                CODEX_PROVIDER_EVIDENCE_SCHEMA_V2,
                CODEX_PROVIDER_EVIDENCE_SCHEMA_V3,
                CODEX_PROVIDER_EVIDENCE_SCHEMA_V4,
            )
            from .codex_staged import CODEX_STAGED_EVIDENCE_SCHEMA

            compatible = (
                staged_schema == CLAUDE_STAGED_EVIDENCE_SCHEMA
                and provider_schema in {
                    CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V1,
                    CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V2,
                    CLAUDE_PROVIDER_EVIDENCE_SCHEMA,
                }
            ) or (
                staged_schema == CODEX_STAGED_EVIDENCE_SCHEMA
                and provider_schema in {
                    CODEX_PROVIDER_EVIDENCE_SCHEMA_V2,
                    CODEX_PROVIDER_EVIDENCE_SCHEMA_V3,
                    CODEX_PROVIDER_EVIDENCE_SCHEMA_V4,
                    CODEX_PROVIDER_EVIDENCE_SCHEMA,
                }
                and provider["requested"].get("execution_mode") == "staged-write"
            )
            if not compatible:
                raise ValidationError("%s result %s provider and staged schemas are incompatible" % (source, result_id))
            if staged_schema == CODEX_STAGED_EVIDENCE_SCHEMA and (
                result["staged_evidence"]["policy"]["tool_output_token_limit"]
                != provider["requested"]["tool_output_token_limit"]
            ):
                raise ValidationError("%s result %s Codex staged tool-output limit is inconsistent" % (source, result_id))
            provider_status = provider["status"]
            staged_status = result["staged_evidence"]["status"]
            if provider_status == "workspace-drift":
                if staged_status not in {"source-drift", "invalid-stage"}:
                    raise ValidationError("%s result %s provider and staged drift status are inconsistent" % (source, result_id))
            elif provider_status in {"budget-exceeded", "token-budget-exceeded"}:
                pass
            elif (staged_status == "provider-failed") != (provider_status != "success"):
                raise ValidationError("%s result %s provider and staged status are inconsistent" % (source, result_id))
            expected_passed = provider["status"] == "success" and result["staged_evidence"]["status"] == "success"
            if execution_passed != expected_passed:
                raise ValidationError("%s result %s execution status is inconsistent with staged evidence" % (source, result_id))
            if "completion_summary" in result:
                _validate_staged_result_completion_summary(
                    result["completion_summary"],
                    result["staged_evidence"],
                    source="%s result %s completion_summary" % (source, result_id),
                )
        elif "completion_summary" in result:
            raise ValidationError("%s result %s completion_summary requires staged_evidence" % (source, result_id))
        elif "provider_evidence" in result and not result["provider_evidence"]["policy"]["read_only_tools"]:
            raise ValidationError("%s result %s write-capable provider evidence requires staged_evidence" % (source, result_id))
        elif "provider_evidence" in result and execution_passed != (result["provider_evidence"]["status"] == "success"):
            raise ValidationError("%s result %s execution status is inconsistent with provider evidence" % (source, result_id))
        expected_final_passed = execution_passed and (quality["passed"] if isinstance(quality, dict) else True)
        if result["passed"] != expected_final_passed:
            raise ValidationError("%s result %s passed is inconsistent with execution and quality evidence" % (source, result_id))
        result_map[result_id] = result
    return result_map


def _validate_staged_result_completion_summary(summary: Dict, staged: Dict, source: str) -> None:
    from .staged_workspace import validate_staged_completion_summary

    validate_staged_completion_summary(summary, source=source)
    changes = staged["changes"]
    expected_files = sorted(changes["added"] + changes["modified"] + changes["deleted"])
    verification = staged["verification"]
    expected = {
        "changed_files": expected_files,
        "change_count": len(expected_files),
        "verification_configured": verification["configured"],
        "verification_performed": bool(
            verification["configured"] and not verification["status"].startswith("skipped-")
        ),
        "verification_status": verification["status"],
        "verification_returncode": verification["returncode"],
        "verification_timed_out": verification["timed_out"],
    }
    for field, value in expected.items():
        if summary.get(field) != value:
            raise ValidationError("%s %s is inconsistent with staged evidence" % (source, field))


def _validate_provider_evidence(evidence: Dict, source: str) -> None:
    if not isinstance(evidence, dict):
        raise ValidationError("%s must contain an object" % source)
    schema = evidence.get("schema")
    from .claude_live import (
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA,
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V1,
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V2,
        validate_claude_provider_evidence,
    )
    from .codex_live import (
        CODEX_PROVIDER_EVIDENCE_SCHEMA,
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V1,
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V2,
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V3,
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V4,
        validate_codex_provider_evidence,
    )

    if schema in {
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V1,
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V2,
        CLAUDE_PROVIDER_EVIDENCE_SCHEMA,
    }:
        validate_claude_provider_evidence(evidence, source=source)
        return
    if schema in {
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V1,
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V2,
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V3,
        CODEX_PROVIDER_EVIDENCE_SCHEMA_V4,
        CODEX_PROVIDER_EVIDENCE_SCHEMA,
    }:
        validate_codex_provider_evidence(evidence, source=source)
        return
    raise ValidationError("%s uses an unsupported provider evidence schema" % source)


def _validate_staged_evidence(evidence: Dict, source: str) -> None:
    if not isinstance(evidence, dict):
        raise ValidationError("%s must contain an object" % source)
    schema = evidence.get("schema")
    from .claude_staged import CLAUDE_STAGED_EVIDENCE_SCHEMA, validate_claude_staged_evidence
    from .codex_staged import CODEX_STAGED_EVIDENCE_SCHEMA, validate_codex_staged_evidence

    if schema == CLAUDE_STAGED_EVIDENCE_SCHEMA:
        validate_claude_staged_evidence(evidence, source=source)
        return
    if schema == CODEX_STAGED_EVIDENCE_SCHEMA:
        validate_codex_staged_evidence(evidence, source=source)
        return
    raise ValidationError("%s uses an unsupported staged evidence schema" % source)


def _benchmark_report_summary(report: Dict) -> Dict:
    results = report["results"]
    passed_tasks = sum(1 for result in results if result["passed"])
    total_tasks = len(results)
    return {
        "total_tasks": total_tasks,
        "passed_tasks": passed_tasks,
        "failed_tasks": total_tasks - passed_tasks,
    }


def _validate_run_manifest_matches_report(manifest: Dict, report: Dict, source: str) -> None:
    if manifest["system"] != report["system"]:
        raise ValidationError("%s system %r does not match report system %r" % (source, manifest["system"], report["system"]))
    if manifest["suite"] != report["suite"]:
        raise ValidationError("%s suite %r does not match report suite %r" % (source, manifest["suite"], report["suite"]))
    report_task_ids = {result["id"] for result in report["results"]}
    score_task_ids = {task["id"] for task in manifest["scoring"]["tasks"]}
    if score_task_ids != report_task_ids:
        raise ValidationError("%s scoring task ids must match benchmark report task ids" % source)
    score_map = {task["id"]: task for task in manifest["scoring"]["tasks"]}
    for result in report["results"]:
        quality = result.get("quality_evidence")
        score_record = score_map[result["id"]]
        has_quality_provenance = "quality_evidence_sha256" in score_record
        if isinstance(quality, dict):
            expected = _manifest_score_record(result, score_record["max_score"])
            for field in [
                "score",
                "max_score",
                "quality_evidence_sha256",
                "task_contract_sha256",
                "pass_threshold",
                "reviewer_kind",
                "reviewer_independent",
            ]:
                if score_record.get(field) != expected.get(field):
                    raise ValidationError("%s scoring task %s does not match quality evidence" % (source, result["id"]))
        elif has_quality_provenance:
            raise ValidationError("%s scoring task %s cites missing quality evidence" % (source, result["id"]))
    has_provider_evidence = any(_has_provider_backed_evidence(result) for result in report.get("results") or [])
    if has_provider_evidence and manifest["live_run"]:
        _validate_live_provider_manifest_contract(
            report,
            model=manifest["model"],
            budget=manifest["budget"],
            permissions=manifest["permissions"],
        )
        expected_incidents = _provider_safety_incidents(report)
        actual_incidents = manifest["safety"]["incidents"]
        for expected in expected_incidents:
            if expected not in actual_incidents:
                raise ValidationError("%s must preserve provider safety incidents" % source)


def _score_for_task(manifest: Dict, task_id: str):
    if manifest is None or not manifest["live_run"]:
        return None
    for task in manifest["scoring"]["tasks"]:
        if task["id"] == task_id:
            return {
                "score": task["score"],
                "max_score": task["max_score"],
            }
    return None


def _score_summary(manifest: Dict):
    if manifest is None or not manifest["live_run"]:
        return None
    tasks = manifest["scoring"]["tasks"]
    score = sum(task["score"] for task in tasks)
    max_score = sum(task["max_score"] for task in tasks)
    percentage = None if max_score == 0 else round((score * 100.0) / max_score, 2)
    return {
        "score": score,
        "max_score": max_score,
        "percentage": percentage,
    }


def _safety_summary(manifest: Dict):
    if manifest is None:
        return None
    incidents = manifest["safety"]["incidents"]
    counts = {severity: 0 for severity in SAFETY_SEVERITIES}
    for incident in incidents:
        counts[incident["severity"]] += 1
    highest = "none"
    for severity in ["critical", "high", "medium", "low", "info"]:
        if counts[severity]:
            highest = severity
            break
    return {
        "incident_count": len(incidents),
        "highest_severity": highest,
        "severity_counts": {severity: counts[severity] for severity in ["critical", "high", "medium", "low", "info"]},
    }


def _comparison_provenance_summary(
    systems: List[Dict],
    run_manifests: Dict[str, Dict],
    claim_policy=None,
    *,
    reports: List[Dict] = None,
) -> Dict:
    missing = [system["name"] for system in systems if system["name"] not in run_manifests]
    live_missing = [system["name"] for system in systems if not run_manifests.get(system["name"], {}).get("live_run", False)]
    equivalence = _equivalence_summary(run_manifests, claim_policy)
    equivalence["quality"] = _quality_equivalence_summary(reports or [])
    warnings = []
    if missing:
        warnings.append("One or more systems are missing run manifests.")
    if live_missing:
        warnings.append("One or more run manifests are templates or non-live runs.")
    for category, summary in equivalence.items():
        if not summary["equivalent"]:
            if category == "model" and summary.get("mode") == "declared-comparable":
                warnings.append("model provenance is incomplete for declared-comparable policy.")
            elif category == "quality":
                warnings.append("hash-bound independent quality scoring is incomplete or non-equivalent.")
            else:
                warnings.append("%s provenance differs across manifests." % category)
    return {
        "complete": not missing,
        "all_live": not live_missing,
        "claim_eligible": not missing and not live_missing and all(item["equivalent"] for item in equivalence.values()),
        "claim_policy": _claim_policy_summary(claim_policy),
        "missing_manifests": missing,
        "non_live_manifests": live_missing,
        "equivalence": equivalence,
        "warnings": warnings,
        "systems": [_run_manifest_summary(system["name"], run_manifests.get(system["name"])) for system in systems],
    }


def _run_manifest_summary(system: str, manifest: Dict) -> Dict:
    if manifest is None:
        return {"system": system, "present": False}
    return {
        "system": system,
        "present": True,
        "manifest_source": _redact_optional_path(manifest.get("_source_path")),
        "run_id": manifest["run_id"],
        "live_run": manifest["live_run"],
        "repository": _redact_string_map(manifest["repository"]),
        "model": _redact_string_map(manifest["model"]),
        "budget": _budget_summary(manifest["budget"]),
        "permissions": _redact_permissions(manifest["permissions"]),
        "score": _score_summary(manifest),
        "safety": _safety_summary(manifest),
    }


def _redact_string_map(value: Dict) -> Dict:
    redacted = {}
    for field, item in value.items():
        redacted[field] = redact_text(item) if isinstance(item, str) else item
    return redacted


def _redact_permissions(value: Dict) -> Dict:
    return {
        "allow_writes": value["allow_writes"],
        "allow_destructive": value["allow_destructive"],
        "allow_network": value["allow_network"],
        "allow_agent": value["allow_agent"],
        "allow_parallel": value["allow_parallel"],
        "approved_tokens": [redact_text(token) for token in value.get("approved_tokens", [])],
    }


def _budget_summary(value: Dict) -> Dict:
    summary = {
        "max_minutes": value["max_minutes"],
        "max_agents": value["max_agents"],
        "max_live_tool_cost": value["max_live_tool_cost"],
    }
    if "max_cost_usd" in value:
        summary["max_cost_usd"] = value["max_cost_usd"]
    if "max_tokens" in value:
        summary["max_tokens"] = value["max_tokens"]
    for field in ["cost_cap_enforcement", "token_cap_enforcement"]:
        if field in value:
            summary[field] = value[field]
    return summary


def _equivalence_summary(run_manifests: Dict[str, Dict], claim_policy=None) -> Dict:
    manifests = list(run_manifests.values())
    return {
        "repository": _equivalent_fields(manifests, "repository", ["state", "commit", "dirty"]),
        "budget": _equivalent_fields(
            manifests,
            "budget",
            [
                "max_minutes",
                "max_agents",
                "max_live_tool_cost",
                "max_cost_usd",
                "max_tokens",
                "cost_cap_enforcement",
                "token_cap_enforcement",
            ],
        ),
        "permissions": _equivalent_fields(
            manifests,
            "permissions",
            [
                "allow_writes",
                "allow_destructive",
                "allow_network",
                "allow_agent",
                "allow_parallel",
                "approved_tokens",
            ],
        ),
        "model": _model_equivalence(manifests, _model_claim_policy(claim_policy)),
    }


def _quality_equivalence_summary(reports: List[Dict]) -> Dict:
    requires_quality = any(
        _has_provider_backed_evidence(result)
        for report in reports
        for result in report.get("results", [])
        if isinstance(report, dict)
    )
    if not requires_quality:
        return {
            "equivalent": True,
            "required": False,
            "complete": True,
            "independent": True,
            "task_contracts_equivalent": True,
            "score_scales_equivalent": True,
        }
    if len(reports) < 2:
        return {
            "equivalent": False,
            "required": True,
            "complete": False,
            "independent": False,
            "task_contracts_equivalent": False,
            "score_scales_equivalent": False,
        }
    by_task = {}
    complete = True
    independent = True
    for report in reports:
        for result in report.get("results", []):
            if not isinstance(result, dict):
                complete = False
                continue
            quality = result.get("quality_evidence")
            if not isinstance(quality, dict):
                complete = False
                independent = False
                continue
            independent = independent and quality["reviewer"]["independent"] is True
            by_task.setdefault(result["id"], []).append(quality)
    task_ids = {
        result["id"]
        for report in reports
        for result in report.get("results", [])
        if isinstance(result, dict) and isinstance(result.get("id"), str)
    }
    contracts = complete and all(
        len(by_task.get(task_id, [])) == len(reports)
        and len({quality["task_contract_sha256"] for quality in by_task[task_id]}) == 1
        for task_id in task_ids
    )
    scales = complete and all(
        len(by_task.get(task_id, [])) == len(reports)
        and len({(quality["max_score"], quality["pass_threshold"]) for quality in by_task[task_id]}) == 1
        for task_id in task_ids
    )
    equivalent = bool(complete and independent and contracts and scales)
    return {
        "equivalent": equivalent,
        "required": True,
        "complete": complete,
        "independent": independent,
        "task_contracts_equivalent": contracts,
        "score_scales_equivalent": scales,
    }


def _model_claim_policy(claim_policy) -> Dict:
    if not isinstance(claim_policy, dict):
        return {"mode": "exact"}
    model = claim_policy.get("model")
    if not isinstance(model, dict):
        return {"mode": "exact"}
    return {"mode": model.get("mode", "exact"), "rationale": model.get("rationale", "")}


def _claim_policy_summary(claim_policy) -> Dict:
    policy = _model_claim_policy(claim_policy)
    summary = {"model": {"mode": policy["mode"]}}
    if policy.get("rationale"):
        summary["model"]["rationale"] = redact_text(policy["rationale"])
    return summary


def _model_equivalence(manifests: List[Dict], policy: Dict) -> Dict:
    mode = policy.get("mode", "exact")
    if mode == "declared-comparable":
        return _declared_comparable_model_summary(manifests, policy)
    return _exact_model_summary(manifests)


def _exact_model_summary(manifests: List[Dict]) -> Dict:
    if len(manifests) < 2:
        return {"equivalent": False, "mode": "exact", "fields": {field: False for field in MODEL_PROVENANCE_FIELDS}}
    fields = {}
    for field in MODEL_PROVENANCE_FIELDS:
        values = [manifest["model"].get(field) for manifest in manifests]
        fields[field] = all(_has_present_string(value) for value in values) and len(set(values)) == 1
    return {"equivalent": all(fields.values()), "mode": "exact", "fields": fields}


def _declared_comparable_model_summary(manifests: List[Dict], policy: Dict) -> Dict:
    if len(manifests) < 2:
        fields = {field: False for field in MODEL_PROVENANCE_FIELDS}
    else:
        fields = {
            field: all(_has_present_string(manifest["model"].get(field)) for manifest in manifests)
            for field in MODEL_PROVENANCE_FIELDS
        }
    return {
        "equivalent": all(fields.values()),
        "mode": "declared-comparable",
        "field_semantics": "complete_provenance_required",
        "rationale": redact_text(policy.get("rationale", "")),
        "fields": fields,
    }


def _has_present_string(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _equivalent_fields(manifests: List[Dict], section: str, fields: List[str]) -> Dict:
    if len(manifests) < 2:
        return {"equivalent": False, "fields": {field: False for field in fields}}
    field_results = {}
    for field in fields:
        values = [_canonical_value(manifest[section].get(field)) for manifest in manifests]
        field_results[field] = len(set(values)) == 1
    return {
        "equivalent": all(field_results.values()),
        "fields": field_results,
    }


def _canonical_value(value):
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, dict):
        return tuple(sorted(value.items()))
    return value


def _validate_string_object(value, name: str, allowed=None, required=None) -> None:
    required = required or []
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % name)
    if allowed is not None:
        _validate_allowed_fields(value, allowed, name)
    for field in required:
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValidationError("%s.%s must be a non-empty string" % (name, field))
    for field, item in value.items():
        if item is not None and not isinstance(item, str):
            raise ValidationError("%s.%s must be a string or null" % (name, field))


def _validate_run_budget(value, name: str) -> None:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % name)
    _validate_allowed_fields(value, BUDGET_FIELDS, name)
    _validate_int_range(value.get("max_minutes"), "%s.max_minutes" % name, minimum=1)
    _validate_int_range(value.get("max_agents"), "%s.max_agents" % name, minimum=0)
    live_tool_cost = value.get("max_live_tool_cost")
    if live_tool_cost not in LIVE_TOOL_COST_LEVELS:
        raise ValidationError("%s.max_live_tool_cost must be one of none, low, medium, high" % name)
    if "max_tokens" in value:
        _validate_int_range(value["max_tokens"], "%s.max_tokens" % name, minimum=0)
    if "max_cost_usd" in value:
        _validate_number_range(value["max_cost_usd"], "%s.max_cost_usd" % name, minimum=0)
    for field in ["cost_cap_enforcement", "token_cap_enforcement"]:
        if field in value and value[field] not in BUDGET_CAP_ENFORCEMENT_LEVELS:
            raise ValidationError(
                "%s.%s must be one of %s"
                % (name, field, ", ".join(sorted(BUDGET_CAP_ENFORCEMENT_LEVELS)))
            )


def _validate_run_permissions(value, name: str) -> None:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % name)
    _validate_allowed_fields(value, PERMISSION_FIELDS, name)
    for field in ["allow_writes", "allow_destructive", "allow_network", "allow_agent", "allow_parallel"]:
        if not isinstance(value.get(field), bool):
            raise ValidationError("%s.%s must be boolean" % (name, field))
    tokens = value.get("approved_tokens")
    if not isinstance(tokens, list) or not all(isinstance(token, str) and token.strip() for token in tokens):
        raise ValidationError("%s.approved_tokens must be a string list" % name)


def _validate_run_scoring(value, name: str) -> None:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % name)
    _validate_allowed_fields(value, SCORING_FIELDS, name)
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValidationError("%s.tasks must contain one or more tasks" % name)
    seen = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValidationError("%s.tasks entries must be objects" % name)
        _validate_allowed_fields(task, SCORING_TASK_FIELDS, "%s.tasks" % name)
        task_id = task.get("id")
        if not isinstance(task_id, str) or not SAFE_ID.match(task_id):
            raise ValidationError("%s task id must be a safe identifier" % name)
        if task_id in seen:
            raise ValidationError("%s contains duplicate task id %s" % (name, task_id))
        seen.add(task_id)
        _validate_int_range(task.get("score"), "%s task %s score" % (name, task_id), minimum=0)
        _validate_int_range(task.get("max_score"), "%s task %s max_score" % (name, task_id), minimum=1)
        if task["score"] > task["max_score"]:
            raise ValidationError("%s task %s score must not exceed max_score" % (name, task_id))
        notes = task.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise ValidationError("%s task %s notes must be a string" % (name, task_id))
        evidence_fields = {
            "quality_evidence_sha256",
            "task_contract_sha256",
            "pass_threshold",
            "reviewer_kind",
            "reviewer_independent",
        }
        present = evidence_fields & set(task)
        if present and present != evidence_fields:
            raise ValidationError("%s task %s quality provenance fields must be complete" % (name, task_id))
        if present:
            for field in ["quality_evidence_sha256", "task_contract_sha256"]:
                value = task.get(field)
                if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                    raise ValidationError("%s task %s %s must be a sha256 hex string" % (name, task_id, field))
            _validate_int_range(
                task.get("pass_threshold"),
                "%s task %s pass_threshold" % (name, task_id),
                minimum=1,
            )
            if task["pass_threshold"] > task["max_score"]:
                raise ValidationError("%s task %s pass_threshold exceeds max_score" % (name, task_id))
            if task.get("reviewer_kind") not in QUALITY_REVIEWER_KINDS:
                raise ValidationError("%s task %s reviewer_kind is invalid" % (name, task_id))
            if not isinstance(task.get("reviewer_independent"), bool):
                raise ValidationError("%s task %s reviewer_independent must be boolean" % (name, task_id))


def _validate_run_safety(value, name: str) -> None:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % name)
    _validate_allowed_fields(value, SAFETY_FIELDS, name)
    incidents = value.get("incidents")
    if not isinstance(incidents, list):
        raise ValidationError("%s.incidents must be a list" % name)
    seen = set()
    for incident in incidents:
        if not isinstance(incident, dict):
            raise ValidationError("%s.incidents entries must be objects" % name)
        _validate_allowed_fields(incident, SAFETY_INCIDENT_FIELDS, "%s.incidents" % name)
        incident_id = incident.get("id")
        if not isinstance(incident_id, str) or not SAFE_ID.match(incident_id):
            raise ValidationError("%s incident id must be a safe identifier" % name)
        if incident_id in seen:
            raise ValidationError("%s contains duplicate incident id %s" % (name, incident_id))
        seen.add(incident_id)
        if incident.get("severity") not in SAFETY_SEVERITIES:
            raise ValidationError("%s incident %s has invalid severity" % (name, incident_id))
        task_id = incident.get("task_id")
        if task_id is not None and (not isinstance(task_id, str) or not SAFE_ID.match(task_id)):
            raise ValidationError("%s incident %s task_id must be a safe identifier or null" % (name, incident_id))
        if not isinstance(incident.get("description"), str) or not incident["description"].strip():
            raise ValidationError("%s incident %s description must be a non-empty string" % (name, incident_id))


def _validate_int_range(value, name: str, minimum: int = 1, maximum: int = None) -> None:
    if type(value) is not int:
        raise ValidationError("%s must be an integer" % name)
    if value < minimum:
        raise ValidationError("%s must be at least %d" % (name, minimum))
    if maximum is not None and value > maximum:
        raise ValidationError("%s must be at most %d" % (name, maximum))


def _validate_number_range(value, name: str, minimum: float = 0, maximum: float = None) -> None:
    if type(value) not in {int, float}:
        raise ValidationError("%s must be a number" % name)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError("%s must be a finite number" % name)
    if value < minimum:
        raise ValidationError("%s must be at least %s" % (name, minimum))
    if maximum is not None and value > maximum:
        raise ValidationError("%s must be at most %s" % (name, maximum))


def _validate_relative_no_escape_path(value: str, name: str) -> None:
    if Path(value).is_absolute():
        raise ValidationError("%s must be relative" % name)
    try:
        require_no_path_escape(value)
    except ValidationError as exc:
        raise ValidationError("%s %s" % (name, str(exc))) from exc


def _validate_allowed_fields(value: Dict, allowed: set, name: str) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise ValidationError("%s has unsupported fields: %s" % (name, ", ".join(unexpected)))
