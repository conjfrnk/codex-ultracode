import hashlib
import json
import math
import os
import statistics
from pathlib import Path
from typing import Dict, List, Optional

from .benchmark import (
    COMPARISON_SCHEMA,
    compare_benchmark_reports,
    load_benchmark_report,
    load_run_manifest,
    validate_run_manifest_matches_report,
)
from .benchmark_quality import load_benchmark_score_input
from .errors import ConductorError, ValidationError
from .parity_campaign import PARITY_CAMPAIGN_SYSTEMS, load_parity_campaign
from .security import read_regular_file_bytes_no_follow, read_regular_text_file_no_follow, reject_symlink_path
from .staged_workspace import MAX_STAGED_PATCH_BYTES, snapshot_workspace


PARITY_CAMPAIGN_STATUS_SCHEMA = "conductor.parity_campaign_status.v5"
PROVIDER_NATIVE_GROSS_TOKEN_ACCOUNTING = "provider-native-gross-v1"
IMPLEMENTATION_EVALUATION_SCHEMA = "conductor.implementation_canary_evaluation.v1"
MAX_EVALUATION_BYTES = 4 * 1024 * 1024
MIN_SUPERIORITY_PAIRS = 10
MIN_SUPERIORITY_TASK_REPETITIONS = 2
IMPLEMENTATION_EVALUATOR_IDENTITIES = {
    "implementation-canary-evaluator-v2",
    "implementation-canary-evaluator-v3",
    "implementation-canary-evaluator-v4",
}
TERMINAL_ZERO_EVALUATOR_IDENTITIES = {
    "implementation-canary-evaluator-v3",
    "implementation-canary-evaluator-v4",
}
SAFETY_SEVERITY_RANK = {
    "none": 0,
    "info": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "critical": 5,
}
CAP_COHORT_ORDER = [
    "neither-native-cap-reached",
    "codex-token-cap-only",
    "claude-dollar-cap-only",
    "both-native-caps-reached",
]
EVALUATION_FIELDS = {
    "schema",
    "suite",
    "task_id",
    "report_sha256",
    "patch_sha256",
    "source_fixture_sha256",
    "held_out_fixture_sha256",
    "visible",
    "held_out",
    "scope",
    "structure",
    "reporting",
    "evaluation_sha256",
}
_T_CRITICAL_95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}


def campaign_status(campaign_path: Path) -> Dict:
    campaign_file = Path(campaign_path)
    campaign = load_parity_campaign(campaign_file)
    trials = [_trial_status(campaign_file, campaign, trial) for trial in campaign["trials"]]
    lifecycle = {
        status: sum(1 for trial in trials if trial["status"] == status)
        for status in ["pending", "partial", "completed", "invalid"]
    }
    paired_rows = [trial for trial in trials if trial["status"] == "completed"]
    release_cohort = _release_cohort_summary(paired_rows)
    system_summary = {
        system: _system_summary(paired_rows, system)
        for system in PARITY_CAMPAIGN_SYSTEMS
    }
    paired = _paired_summary(paired_rows)
    claim = _claim_summary(campaign, trials, paired_rows)
    inference = _inference_summary(campaign, trials, paired)
    dimensions = _dimension_summaries(paired_rows)
    enforcement = _enforcement_summary(campaign, trials, paired_rows)
    product_profile_inference = _product_profile_inference_summary(
        campaign,
        trials,
        paired,
        enforcement,
    )
    resources = _resource_summary(paired_rows)
    successful_outcome_resources = _successful_outcome_resource_summary(paired_rows)
    verified_delivery_resources = _successful_outcome_resource_summary(
        paired_rows,
        success_field="verified_delivery_passed",
        policy="verified-delivery-first-v1",
    )
    universal = _universal_superiority_summary(inference, dimensions, enforcement, resources)
    product_profile_universal = _product_profile_universal_summary(
        product_profile_inference,
        dimensions,
        enforcement,
        verified_delivery_resources,
    )
    if not release_cohort["claim_eligible"]:
        claim["eligible"] = False
        claim["reasons"].append("release-cohort-ineligible")
        inference["broad_superiority_supported"] = False
        inference["reasons"].append("release-cohort-ineligible")
        product_profile_inference["broad_superiority_supported"] = False
        product_profile_inference["reasons"].append("release-cohort-ineligible")
        universal["superiority_supported"] = False
        universal["reasons"].append("release-cohort-ineligible")
        product_profile_universal["superiority_supported"] = False
        product_profile_universal["reasons"].append("release-cohort-ineligible")
    return {
        "schema": PARITY_CAMPAIGN_STATUS_SCHEMA,
        "campaign": {
            "name": campaign["name"],
            "suite": campaign["source"]["suite"],
            "seed": campaign["design"]["seed"],
            "repetitions": campaign["design"]["repetitions"],
            "task_count": campaign["source"]["task_count"],
            "planned_trials": campaign["design"]["trial_count"],
            "source_sha256": campaign["source"]["parity_tasks_sha256"],
        },
        "lifecycle": {
            **lifecycle,
            "coverage": _ratio(lifecycle["completed"], campaign["design"]["trial_count"]),
        },
        "systems": system_summary,
        "paired": paired,
        "dimensions": dimensions,
        "enforcement": enforcement,
        "resources": resources,
        "successful_outcome_resources": successful_outcome_resources,
        "verified_delivery_resources": verified_delivery_resources,
        "release_cohort": release_cohort,
        "claim": claim,
        "inference": inference,
        "product_profile_inference": product_profile_inference,
        "universal": universal,
        "product_profile_universal": product_profile_universal,
        "trials": trials,
    }


def _trial_status(campaign_path: Path, campaign: Dict, trial: Dict) -> Dict:
    systems = {}
    for system in PARITY_CAMPAIGN_SYSTEMS:
        systems[system] = _system_evidence_status(campaign_path, campaign, trial, system)
    statuses = [systems[system]["status"] for system in PARITY_CAMPAIGN_SYSTEMS]
    result = {
        "id": trial["id"],
        "sequence": trial["sequence"],
        "task_id": trial["task_id"],
        "repetition": trial["repetition"],
        "provider_first": trial["system_order"][0],
        "status": "pending",
        "systems": systems,
        "pair": None,
    }
    if "invalid" in statuses:
        result["status"] = "invalid"
        return result
    if statuses == ["pending", "pending"]:
        return result
    if statuses != ["complete", "complete"]:
        result["status"] = "partial"
        return result
    try:
        pair = _pair_comparison(campaign_path, trial, systems)
    except (KeyError, TypeError, OSError, UnicodeError, ValueError, ConductorError):
        result["status"] = "invalid"
        result["pair"] = {"status": "invalid", "error_code": "pair-comparison-invalid"}
        return result
    result["status"] = "completed"
    result["pair"] = pair
    return result


def _system_evidence_status(campaign_path: Path, campaign: Dict, trial: Dict, system: str) -> Dict:
    paths = {
        key: _resolve_artifact_path(campaign_path, value)
        for key, value in trial["artifacts"][system].items()
    }
    evaluation_json = paths["evaluation"] / "evaluation.json"
    score_input_path = paths["evaluation"] / "score-input.json"
    presence_paths = [
        paths["report"],
        paths["scored_report"],
        paths["manifest"],
        paths["patch"],
        paths["stage"],
        paths["evaluation"],
        evaluation_json,
        score_input_path,
    ]
    present = [path.exists() or path.is_symlink() for path in presence_paths]
    if not any(present):
        return {"status": "pending"}
    required = [
        paths["report"],
        paths["scored_report"],
        paths["manifest"],
        paths["evaluation"],
        evaluation_json,
        score_input_path,
    ]
    if not all(path.exists() or path.is_symlink() for path in required):
        return {"status": "partial"}
    try:
        raw_report = load_benchmark_report(paths["report"])
        scored_report = load_benchmark_report(paths["scored_report"])
        manifest = load_run_manifest(paths["manifest"])
        validate_run_manifest_matches_report(manifest, scored_report, source="campaign system evidence")
        if manifest.get("live_run") is not True:
            raise ValidationError("campaign completed evidence requires a live manifest")
        raw_result = _single_result(raw_report, campaign, trial)
        scored_result = _single_result(scored_report, campaign, trial)
        _validate_system_provider(campaign, trial, system, raw_report, raw_result)
        _validate_scored_result_matches_raw(raw_result, scored_result)
        _validate_campaign_budget(trial, manifest)
        _validate_task_contract(trial, manifest, scored_result)
        evaluation = _load_evaluation(evaluation_json)
        score_input = load_benchmark_score_input(score_input_path)
        _validate_evaluation_chain(
            campaign,
            trial,
            paths,
            raw_report,
            raw_result,
            scored_result,
            manifest,
            evaluation,
            score_input,
        )
        _validate_persisted_stage(paths["stage"], raw_result)
    except (KeyError, TypeError, OSError, UnicodeError, ValueError, ConductorError):
        return {"status": "invalid", "error_code": "evidence-chain-invalid"}
    quality = scored_result["quality_evidence"]
    provider = scored_result.get("provider_evidence") or {}
    staged = scored_result.get("staged_evidence") or {}
    observed = provider.get("observed") or {}
    safety = _safety_summary(manifest)
    cost_usd = observed.get("cost_usd")
    if isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float)):
        cost_usd = None
    total_tokens = observed.get("total_tokens")
    if isinstance(total_tokens, bool) or not isinstance(total_tokens, int):
        total_tokens = None
    token_accounting = observed.get("token_accounting")
    if total_tokens is not None and str(provider.get("schema", "")).startswith(
        "conductor.codex_provider_evidence."
    ):
        token_accounting = PROVIDER_NATIVE_GROSS_TOKEN_ACCOUNTING
    if token_accounting != PROVIDER_NATIVE_GROSS_TOKEN_ACCOUNTING:
        token_accounting = None
    return {
        "status": "complete",
        "report_system": scored_report.get("system"),
        "provider_status": provider.get("status"),
        "staged_status": staged.get("status"),
        "execution_passed": scored_result.get("execution_passed", scored_result["passed"]),
        "quality_passed": quality["passed"],
        "final_passed": scored_result["passed"],
        "verified_delivery_passed": _verified_delivery_passed(scored_result),
        "score": quality["score"],
        "max_score": quality["max_score"],
        "duration_ms": scored_result["duration_ms"],
        "native_cap_reached": _native_cap_reached(system, provider.get("status")),
        "safety_incident_count": safety["incident_count"],
        "highest_safety_severity": safety["highest_severity"],
        "cost_usd": _round(cost_usd) if cost_usd is not None else None,
        "total_tokens": total_tokens,
        "token_accounting": token_accounting,
        "manifest_live": manifest["live_run"],
        "conductor_runtime_version": _reported_runtime_version(raw_report),
    }


def _verified_delivery_passed(result: Dict) -> bool:
    quality = result.get("quality_evidence") or {}
    staged = result.get("staged_evidence") or {}
    verification = staged.get("verification") or {}
    completion = result.get("completion_summary") or {}
    return bool(
        quality.get("passed") is True
        and staged.get("status") == "success"
        and verification.get("configured") is True
        and verification.get("status") == "passed"
        and verification.get("returncode") == 0
        and verification.get("timed_out") is False
        and completion.get("verification_performed") is True
        and completion.get("verification_status") == "passed"
        and completion.get("verification_returncode") == 0
        and completion.get("verification_timed_out") is False
    )


def _reported_runtime_version(report: Dict) -> Optional[str]:
    environment = report.get("environment")
    if not isinstance(environment, dict):
        return None
    version = environment.get("conductor_runtime_version")
    if not isinstance(version, str) or not version.strip():
        return None
    return version


def _single_result(report: Dict, campaign: Dict, trial: Dict) -> Dict:
    if report.get("suite") != campaign["source"]["suite"] or len(report.get("results", [])) != 1:
        raise ValidationError("campaign report suite or result count is invalid")
    result = report["results"][0]
    if result.get("id") != trial["task_id"]:
        raise ValidationError("campaign report task id is invalid")
    return result


def _validate_system_provider(campaign: Dict, trial: Dict, system: str, report: Dict, result: Dict) -> None:
    provider = result.get("provider_evidence")
    staged = result.get("staged_evidence")
    if not isinstance(provider, dict) or not isinstance(staged, dict):
        raise ValidationError("campaign implementation evidence must be staged")
    requested = provider.get("requested") or {}
    expected = next(item for item in campaign["systems"] if item["name"] == system)
    if system == "codex":
        expected_effort = trial.get("codex_effort", expected["effort"])
        if (
            report.get("system") != "codex-isolated-staged"
            or requested.get("model") != expected["model"]
            or requested.get("effort") != expected_effort
            or not str(provider.get("schema", "")).startswith("conductor.codex_provider_evidence.")
        ):
            raise ValidationError("campaign Codex provider evidence is inconsistent")
    else:
        observed = provider.get("observed") or {}
        if (
            report.get("system") != "claude-sonnet-ultracode-staged"
            or requested.get("model") != "sonnet"
            or requested.get("effort") != "ultracode"
            or provider.get("schema") not in {
                "conductor.claude_provider_evidence.v1",
                "conductor.claude_provider_evidence.v2",
                "conductor.claude_provider_evidence.v3",
            }
            or observed.get("opus_observed") is not False
            or observed.get("sonnet_main_only") is not True
        ):
            raise ValidationError("campaign Claude evidence is not pinned Sonnet Ultracode")


def _validate_scored_result_matches_raw(raw: Dict, scored: Dict) -> None:
    if "quality_evidence" in raw or "execution_passed" in raw:
        raise ValidationError("campaign raw report must be unscored")
    quality = scored.get("quality_evidence")
    if not isinstance(quality, dict) or not isinstance(scored.get("execution_passed"), bool):
        raise ValidationError("campaign scored report is missing quality evidence")
    for field in [
        "id",
        "duration_ms",
        "timed_out",
        "stdout_truncated",
        "stderr_truncated",
        "stdout",
        "stderr",
        "returncode",
        "provider_evidence",
        "staged_evidence",
        "completion_summary",
    ]:
        if raw.get(field) != scored.get(field):
            raise ValidationError("campaign scored report changed raw execution evidence")
    if scored["execution_passed"] != raw["passed"]:
        raise ValidationError("campaign scored execution status changed")


def _validate_campaign_budget(trial: Dict, manifest: Dict) -> None:
    for field, value in trial["budget"].items():
        if manifest["budget"].get(field) != value:
            raise ValidationError("campaign manifest budget differs from preregistration")


def _validate_task_contract(trial: Dict, manifest: Dict, result: Dict) -> None:
    scoring = manifest["scoring"]["tasks"]
    if len(scoring) != 1 or scoring[0]["id"] != trial["task_id"]:
        raise ValidationError("campaign manifest scoring task is invalid")
    quality = result["quality_evidence"]
    if (
        scoring[0].get("task_contract_sha256") != trial["task_contract_sha256"]
        or quality.get("task_contract_sha256") != trial["task_contract_sha256"]
    ):
        raise ValidationError("campaign task contract hash is inconsistent")


def _load_evaluation(path: Path) -> Dict:
    text = read_regular_text_file_no_follow(path, "campaign deterministic evaluation", max_bytes=MAX_EVALUATION_BYTES)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("campaign evaluation is not strict JSON: %s" % exc.__class__.__name__)
    if not isinstance(value, dict) or set(value) != EVALUATION_FIELDS:
        raise ValidationError("campaign evaluation fields are invalid")
    if value.get("schema") != IMPLEMENTATION_EVALUATION_SCHEMA:
        raise ValidationError("campaign evaluation schema is invalid")
    for field in [
        "report_sha256",
        "patch_sha256",
        "source_fixture_sha256",
        "held_out_fixture_sha256",
        "evaluation_sha256",
    ]:
        _require_sha256(value.get(field), "campaign evaluation %s" % field)
    unsigned = dict(value)
    observed_hash = unsigned.pop("evaluation_sha256")
    if _sha256_json(unsigned) != observed_hash:
        raise ValidationError("campaign evaluation hash is invalid")
    return value


def _validate_evaluation_chain(
    campaign: Dict,
    trial: Dict,
    paths: Dict,
    raw_report: Dict,
    raw_result: Dict,
    scored_result: Dict,
    manifest: Dict,
    evaluation: Dict,
    score_input: Dict,
) -> None:
    if evaluation.get("suite") != campaign["source"]["suite"] or evaluation.get("task_id") != trial["task_id"]:
        raise ValidationError("campaign evaluation task identity is invalid")
    report_bytes = read_regular_file_bytes_no_follow(
        paths["report"],
        "campaign raw report",
        max_bytes=32 * 1024 * 1024,
    )
    if hashlib.sha256(report_bytes).hexdigest() != evaluation["report_sha256"]:
        raise ValidationError("campaign evaluation report hash is invalid")
    staged = raw_result["staged_evidence"]
    changes = staged["changes"]
    patch_bytes = read_regular_file_bytes_no_follow(
        paths["patch"],
        "campaign retained patch",
        max_bytes=MAX_STAGED_PATCH_BYTES,
    )
    patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
    terminal_no_patch = changes.get("patch_written") is False
    if terminal_no_patch:
        if (
            staged.get("status") != "no-changes"
            or changes.get("change_count") != 0
            or changes.get("added") != []
            or changes.get("modified") != []
            or changes.get("deleted") != []
            or changes.get("binary") != []
            or changes.get("mode_changed") != []
            or changes.get("unpatchable") != []
            or changes.get("patch_name") is not None
            or changes.get("patch_sha256") is not None
            or changes.get("patch_bytes") != 0
            or changes.get("validation_error") is not None
            or changes.get("patch_error") is not None
            or staged.get("verification", {}).get("status") != "skipped-no-changes"
            or patch_bytes != b""
            or patch_sha256 != evaluation["patch_sha256"]
        ):
            raise ValidationError("campaign terminal no-change patch evidence is invalid")
        expected_false = [
            evaluation.get("visible", {}).get("passed"),
            evaluation.get("held_out", {}).get("passed"),
            evaluation.get("scope", {}).get("passed"),
            evaluation.get("structure", {}).get("maintainable"),
            evaluation.get("reporting", {}).get("passed"),
        ]
        if any(value is not False for value in expected_false):
            raise ValidationError("campaign terminal no-change evaluation must score zero")
    elif (
        changes.get("patch_written") is not True
        or patch_sha256 != changes.get("patch_sha256")
        or patch_sha256 != evaluation["patch_sha256"]
    ):
        raise ValidationError("campaign retained patch hash is invalid")
    tasks = score_input.get("tasks") or []
    if len(tasks) != 1 or tasks[0].get("id") != trial["task_id"]:
        raise ValidationError("campaign score input task is invalid")
    reviewer = score_input.get("reviewer") or {}
    if (
        reviewer.get("kind") != "deterministic"
        or reviewer.get("independent") is not True
        or reviewer.get("identity") not in IMPLEMENTATION_EVALUATOR_IDENTITIES
    ):
        raise ValidationError("campaign deterministic reviewer is invalid")
    if terminal_no_patch and reviewer.get("identity") not in TERMINAL_ZERO_EVALUATOR_IDENTITIES:
        raise ValidationError("campaign terminal no-change evaluation requires the current evaluator")
    expected_note = "Deterministic evaluation %s." % evaluation["evaluation_sha256"]
    expected_criteria = _expected_score_criteria(evaluation, trial["task_id"], expected_note)
    if tasks[0].get("pass_threshold") != 10 or tasks[0]["criteria"] != expected_criteria:
        raise ValidationError("campaign score input is not bound to the evaluation")
    quality = scored_result["quality_evidence"]
    if quality.get("reviewer") != reviewer or quality.get("criteria") != tasks[0]["criteria"]:
        raise ValidationError("campaign scored quality evidence differs from score input")
    manifest_score = manifest["scoring"]["tasks"][0]
    if (
        manifest_score.get("score") != quality.get("score")
        or manifest_score.get("max_score") != quality.get("max_score")
        or manifest_score.get("reviewer_kind") != "deterministic"
        or manifest_score.get("reviewer_independent") is not True
    ):
        raise ValidationError("campaign manifest score differs from deterministic evidence")


def _expected_score_criteria(evaluation: Dict, task_id: str, note: str) -> List[Dict]:
    for field in ["visible", "held_out", "scope", "structure", "reporting"]:
        if not isinstance(evaluation.get(field), dict):
            raise ValidationError("campaign evaluation result sections must be objects")
    booleans = {
        "held": evaluation["held_out"].get("passed"),
        "visible": evaluation["visible"].get("passed"),
        "scope": evaluation["scope"].get("passed"),
        "maintainable": evaluation["structure"].get("maintainable"),
        "reporting": evaluation["reporting"].get("passed"),
    }
    if any(not isinstance(value, bool) for value in booleans.values()):
        raise ValidationError("campaign evaluation result booleans are invalid")
    held_id = "hidden-mutant-detection" if task_id == "backoff-test-authoring" else "held-out-correctness"
    visible_id = "baseline-test-correctness" if task_id == "backoff-test-authoring" else "visible-test-correctness"
    values = [
        (held_id, 5 if booleans["held"] else 0, 5),
        (visible_id, 2 if booleans["visible"] else 0, 2),
        ("scope-control", 1 if booleans["scope"] else 0, 1),
        ("maintainability", 1 if booleans["maintainable"] else 0, 1),
        ("final-reporting", 1 if booleans["reporting"] else 0, 1),
    ]
    return [
        {"id": criterion_id, "score": score, "max_score": maximum, "notes": note}
        for criterion_id, score, maximum in values
    ]


def _validate_persisted_stage(stage_path: Path, result: Dict) -> None:
    staged = result["staged_evidence"]
    stage = staged["stage"]
    if stage.get("persisted") is not True:
        raise ValidationError("campaign stage must be retained")
    reject_symlink_path(stage_path, "campaign retained stage")
    if not stage_path.is_dir() or stage_path.name != stage.get("directory_name"):
        raise ValidationError("campaign retained stage path is invalid")
    resolved = stage_path.resolve()
    if hashlib.sha256(str(resolved).encode("utf-8")).hexdigest() != stage.get("path_sha256"):
        raise ValidationError("campaign retained stage path hash is invalid")
    snapshot = snapshot_workspace(stage_path)
    if (
        snapshot.tracked_fingerprint_sha256 != stage.get("after_verification_sha256")
        or snapshot.file_count != stage.get("file_count")
        or snapshot.total_bytes != stage.get("total_bytes")
    ):
        raise ValidationError("campaign retained stage bytes drifted")


def _pair_comparison(campaign_path: Path, trial: Dict, systems: Dict) -> Dict:
    codex_report = _resolve_artifact_path(campaign_path, trial["artifacts"]["codex"]["scored_report"])
    claude_report = _resolve_artifact_path(campaign_path, trial["artifacts"]["claude"]["scored_report"])
    codex_manifest = _resolve_artifact_path(campaign_path, trial["artifacts"]["codex"]["manifest"])
    claude_manifest = _resolve_artifact_path(campaign_path, trial["artifacts"]["claude"]["manifest"])
    parents = {path.parent.resolve() for path in [codex_report, claude_report, codex_manifest, claude_manifest]}
    if len(parents) != 1:
        raise ValidationError("campaign pair artifacts must share one trial directory")
    base_dir = next(iter(parents))
    comparison_manifest = {
        "schema": COMPARISON_SCHEMA,
        "name": "campaign-%s" % trial["id"],
        "claim_policy": {
            "model": {
                "mode": "declared-comparable",
                "rationale": "Preregistered cross-vendor campaign row with identical task and source contracts.",
            }
        },
        "systems": [
            {
                "name": systems["codex"]["report_system"],
                "report": codex_report.name,
                "manifest": codex_manifest.name,
            },
            {
                "name": systems["claude"]["report_system"],
                "report": claude_report.name,
                "manifest": claude_manifest.name,
            },
        ],
    }
    comparison = compare_benchmark_reports(comparison_manifest, base_dir)
    scores = {
        item["system"]: item["score"]
        for item in comparison["systems"]
    }
    codex_score = scores[systems["codex"]["report_system"]]
    claude_score = scores[systems["claude"]["report_system"]]
    codex_percentage = _percentage(codex_score["score"], codex_score["max_score"])
    claude_percentage = _percentage(claude_score["score"], claude_score["max_score"])
    quality_winner = _winner(codex_percentage, claude_percentage)
    execution_winner = _winner(
        int(systems["codex"]["execution_passed"]),
        int(systems["claude"]["execution_passed"]),
    )
    final_winner = _winner(
        int(systems["codex"]["final_passed"]),
        int(systems["claude"]["final_passed"]),
    )
    verified_delivery_winner = _winner(
        int(systems["codex"]["verified_delivery_passed"]),
        int(systems["claude"]["verified_delivery_passed"]),
    )
    latency_winner = _winner(
        systems["codex"]["duration_ms"],
        systems["claude"]["duration_ms"],
        higher_is_better=False,
    )
    safety_winner = _safety_winner(systems["codex"], systems["claude"])
    dimension_winners = {
        "quality": quality_winner,
        "execution": execution_winner,
        "final_success": final_winner,
        "verified_delivery": verified_delivery_winner,
        "latency": latency_winner,
        "safety": safety_winner,
    }
    provenance = comparison["provenance"]
    stratification_eligible, provenance_class = _cap_stratification_eligibility(provenance)
    runtime_versions = {
        system: systems[system]["conductor_runtime_version"]
        for system in PARITY_CAMPAIGN_SYSTEMS
    }
    bound_runtime_versions = [
        version
        for version in runtime_versions.values()
        if isinstance(version, str) and version
    ]
    release_cohort_eligible = (
        len(bound_runtime_versions) == len(PARITY_CAMPAIGN_SYSTEMS)
        and len(set(bound_runtime_versions)) == 1
    )
    provenance_warnings = list(provenance.get("warnings", []))
    if len(bound_runtime_versions) != len(PARITY_CAMPAIGN_SYSTEMS):
        stratification_eligible = False
        provenance_class = "unbound-conductor-runtime-version"
        provenance_warnings.append(
            "Conductor runtime version is missing for one or both systems."
        )
    elif not release_cohort_eligible:
        stratification_eligible = False
        provenance_class = "mixed-conductor-runtime-versions"
        provenance_warnings.append("Conductor runtime versions differ within the pair.")
    return {
        "status": "valid",
        "codex_score": codex_score["score"],
        "claude_score": claude_score["score"],
        "max_score": codex_score["max_score"],
        "codex_percentage": codex_percentage,
        "claude_percentage": claude_percentage,
        "difference_percentage_points": _round(codex_percentage - claude_percentage),
        "winner": quality_winner,
        "dimension_winners": dimension_winners,
        "row_dominance": _row_dominance(dimension_winners),
        "codex_duration_ms": systems["codex"]["duration_ms"],
        "claude_duration_ms": systems["claude"]["duration_ms"],
        "codex_latency_advantage_ms": systems["claude"]["duration_ms"] - systems["codex"]["duration_ms"],
        "cap_cohort": _cap_cohort(systems),
        "claim_eligible": provenance["claim_eligible"] and release_cohort_eligible,
        "stratification_eligible": stratification_eligible,
        "provenance_class": provenance_class,
        "provenance_warnings": provenance_warnings,
        "release_cohort_eligible": release_cohort_eligible,
        "conductor_runtime_versions": runtime_versions,
    }


def _release_cohort_summary(rows: List[Dict]) -> Dict:
    runtime_versions = [
        row["systems"][system]["conductor_runtime_version"]
        for row in rows
        for system in PARITY_CAMPAIGN_SYSTEMS
    ]
    versions = sorted(
        {
            version
            for version in runtime_versions
            if isinstance(version, str) and version
        }
    )
    unbound_pairs = sum(
        1
        for row in rows
        if any(
            not isinstance(row["systems"][system]["conductor_runtime_version"], str)
            or not row["systems"][system]["conductor_runtime_version"]
            for system in PARITY_CAMPAIGN_SYSTEMS
        )
    )
    mixed_pairs = sum(
        1
        for row in rows
        if not row["pair"]["release_cohort_eligible"]
        and all(
            isinstance(row["systems"][system]["conductor_runtime_version"], str)
            and row["systems"][system]["conductor_runtime_version"]
            for system in PARITY_CAMPAIGN_SYSTEMS
        )
    )
    reasons = []
    if not rows:
        reasons.append("no-completed-pairs")
    if unbound_pairs:
        reasons.append("unbound-conductor-runtime-version")
    if mixed_pairs:
        reasons.append("within-pair-runtime-version-mismatch")
    if len(versions) > 1:
        reasons.append("mixed-conductor-runtime-versions")
    return {
        "completed_pairs": len(rows),
        "same_release_pairs": len(rows) - mixed_pairs - unbound_pairs,
        "mixed_release_pairs": mixed_pairs,
        "unbound_release_pairs": unbound_pairs,
        "versions": versions,
        "claim_eligible": not reasons,
        "reasons": reasons,
    }


def _system_summary(rows: List[Dict], system: str) -> Dict:
    records = [row["systems"][system] for row in rows]
    score = sum(record["score"] for record in records)
    max_score = sum(record["max_score"] for record in records)
    costs = [record["cost_usd"] for record in records if record["cost_usd"] is not None]
    tokens = [record["total_tokens"] for record in records if record["total_tokens"] is not None]
    highest_safety = max(
        (record["highest_safety_severity"] for record in records),
        key=lambda severity: SAFETY_SEVERITY_RANK[severity],
        default="none",
    )
    return {
        "completed_trials": len(records),
        "execution_passed": sum(1 for record in records if record["execution_passed"]),
        "quality_passed": sum(1 for record in records if record["quality_passed"]),
        "final_passed": sum(1 for record in records if record["final_passed"]),
        "verified_delivery_passed": sum(
            1 for record in records if record["verified_delivery_passed"]
        ),
        "score": score,
        "max_score": max_score,
        "percentage": _percentage(score, max_score) if max_score else None,
        "duration_ms": sum(record["duration_ms"] for record in records),
        "native_cap_reached": sum(1 for record in records if record["native_cap_reached"]),
        "safety_incidents": sum(record["safety_incident_count"] for record in records),
        "highest_safety_severity": highest_safety,
        "cost_observed_trials": len(costs),
        "observed_cost_usd": _round(sum(costs)) if costs else None,
        "token_observed_trials": len(tokens),
        "observed_total_tokens": sum(tokens) if tokens else None,
    }


def _paired_summary(rows: List[Dict]) -> Dict:
    differences = [row["pair"]["difference_percentage_points"] for row in rows]
    codex_wins = sum(1 for row in rows if row["pair"]["winner"] == "codex")
    claude_wins = sum(1 for row in rows if row["pair"]["winner"] == "claude")
    ties = len(rows) - codex_wins - claude_wins
    decisive = codex_wins + claude_wins
    return {
        "dimension": "quality",
        "completed_pairs": len(rows),
        "claim_eligible_pairs": sum(1 for row in rows if row["pair"]["claim_eligible"]),
        "codex_wins": codex_wins,
        "claude_wins": claude_wins,
        "ties": ties,
        "mean_difference_percentage_points": _round(statistics.mean(differences)) if differences else None,
        "median_difference_percentage_points": _round(statistics.median(differences)) if differences else None,
        "mean_difference_ci95": _mean_ci95(differences),
        "decisive_codex_win_rate": _ratio(codex_wins, decisive) if decisive else None,
        "decisive_codex_win_rate_ci95": _wilson_ci95(codex_wins, decisive) if decisive else None,
    }


def _dimension_summaries(rows: List[Dict]) -> Dict:
    quality_differences = [row["pair"]["difference_percentage_points"] for row in rows]
    latency_advantages = [row["pair"]["codex_latency_advantage_ms"] for row in rows]
    return {
        "quality": {
            **_winner_summary(rows, "quality"),
            "mean_difference_percentage_points": _round(statistics.mean(quality_differences))
            if quality_differences
            else None,
            "median_difference_percentage_points": _round(statistics.median(quality_differences))
            if quality_differences
            else None,
            "mean_difference_ci95": _mean_ci95(quality_differences),
        },
        "execution": _winner_summary(rows, "execution"),
        "final_success": _winner_summary(rows, "final_success"),
        "verified_delivery": _winner_summary(rows, "verified_delivery"),
        "latency": {
            **_winner_summary(rows, "latency"),
            "mean_codex_advantage_ms": _round(statistics.mean(latency_advantages))
            if latency_advantages
            else None,
            "median_codex_advantage_ms": _round(statistics.median(latency_advantages))
            if latency_advantages
            else None,
            "mean_codex_advantage_ci95": _mean_ci95(latency_advantages),
        },
        "safety": _winner_summary(rows, "safety"),
        "row_dominance": {
            "codex": sum(1 for row in rows if row["pair"]["row_dominance"] == "codex"),
            "claude": sum(1 for row in rows if row["pair"]["row_dominance"] == "claude"),
            "mixed": sum(1 for row in rows if row["pair"]["row_dominance"] == "mixed"),
            "tie": sum(1 for row in rows if row["pair"]["row_dominance"] == "tie"),
        },
    }


def _winner_summary(rows: List[Dict], dimension: str) -> Dict:
    winners = [row["pair"]["dimension_winners"][dimension] for row in rows]
    return {
        "completed_pairs": len(rows),
        "codex_wins": winners.count("codex"),
        "claude_wins": winners.count("claude"),
        "ties": winners.count("tie"),
    }


def _enforcement_summary(campaign: Dict, trials: List[Dict], rows: List[Dict]) -> Dict:
    grouped = {cohort: [] for cohort in CAP_COHORT_ORDER}
    for row in rows:
        grouped[row["pair"]["cap_cohort"]].append(row)
    cohorts = []
    for cohort in CAP_COHORT_ORDER:
        members = grouped[cohort]
        if not members:
            continue
        differences = [row["pair"]["difference_percentage_points"] for row in members]
        cohorts.append(
            {
                "id": cohort,
                "completed_pairs": len(members),
                "stratification_eligible_pairs": sum(
                    1 for row in members if row["pair"]["stratification_eligible"]
                ),
                "quality": _winner_summary(members, "quality"),
                "execution": _winner_summary(members, "execution"),
                "final_success": _winner_summary(members, "final_success"),
                "verified_delivery": _winner_summary(members, "verified_delivery"),
                "mean_quality_difference_percentage_points": _round(statistics.mean(differences)),
                "mean_quality_difference_ci95": _mean_ci95(differences),
            }
        )
    reasons = []
    if len(rows) != campaign["design"]["trial_count"]:
        reasons.append("campaign-incomplete")
    if any(trial["status"] == "invalid" for trial in trials):
        reasons.append("invalid-evidence")
    eligible = sum(1 for row in rows if row["pair"]["stratification_eligible"])
    if eligible != len(rows):
        reasons.append("stratification-ineligible-pairs")
    if not rows:
        reasons.append("no-completed-pairs")
    elif any(cohort["completed_pairs"] < 2 for cohort in cohorts):
        reasons.append("sparse-observed-cap-cohorts")
    return {
        "requested_profile": {
            "codex": "runtime-hard-token",
            "claude": "hard-provider-dollar",
        },
        "completed_pairs": len(rows),
        "strict_claim_eligible_pairs": sum(1 for row in rows if row["pair"]["claim_eligible"]),
        "stratification_eligible_pairs": eligible,
        "cohorts": cohorts,
        "ready": not reasons and bool(rows),
        "reasons": reasons,
    }


def _resource_summary(rows: List[Dict]) -> Dict:
    codex_costs = [
        row["systems"]["codex"]["cost_usd"]
        for row in rows
        if row["systems"]["codex"]["cost_usd"] is not None
    ]
    claude_costs = [
        row["systems"]["claude"]["cost_usd"]
        for row in rows
        if row["systems"]["claude"]["cost_usd"] is not None
    ]
    codex_tokens = [
        row["systems"]["codex"]["total_tokens"]
        for row in rows
        if row["systems"]["codex"]["total_tokens"] is not None
    ]
    claude_tokens = [
        row["systems"]["claude"]["total_tokens"]
        for row in rows
        if row["systems"]["claude"]["total_tokens"] is not None
    ]
    cost_values = [
        (row["systems"]["codex"]["cost_usd"], row["systems"]["claude"]["cost_usd"])
        for row in rows
        if row["systems"]["codex"]["cost_usd"] is not None
        and row["systems"]["claude"]["cost_usd"] is not None
    ]
    token_observed = [
        row
        for row in rows
        if row["systems"]["codex"]["total_tokens"] is not None
        and row["systems"]["claude"]["total_tokens"] is not None
    ]
    token_values = [
        (row["systems"]["codex"]["total_tokens"], row["systems"]["claude"]["total_tokens"])
        for row in token_observed
        if row["systems"]["codex"].get("token_accounting") == PROVIDER_NATIVE_GROSS_TOKEN_ACCOUNTING
        and row["systems"]["claude"].get("token_accounting") == PROVIDER_NATIVE_GROSS_TOKEN_ACCOUNTING
    ]
    cost_comparison = _lower_resource_summary(cost_values)
    token_comparison = _lower_resource_summary(token_values)
    return {
        "completed_pairs": len(rows),
        "cost": {
            "codex_observed_trials": sum(
                1 for row in rows if row["systems"]["codex"]["cost_usd"] is not None
            ),
            "claude_observed_trials": sum(
                1 for row in rows if row["systems"]["claude"]["cost_usd"] is not None
            ),
            "comparable_pairs": len(cost_values),
            "complete": bool(rows) and len(cost_values) == len(rows),
            "codex_total_usd": _round(sum(codex_costs)) if codex_costs else None,
            "claude_total_usd": _round(sum(claude_costs)) if claude_costs else None,
            **cost_comparison,
        },
        "tokens": {
            "codex_observed_trials": sum(
                1 for row in rows if row["systems"]["codex"]["total_tokens"] is not None
            ),
            "claude_observed_trials": sum(
                1 for row in rows if row["systems"]["claude"]["total_tokens"] is not None
            ),
            "observed_pairs": len(token_observed),
            "comparable_pairs": len(token_values),
            "accounting": PROVIDER_NATIVE_GROSS_TOKEN_ACCOUNTING,
            "complete": bool(rows) and len(token_values) == len(rows),
            "codex_total_tokens": sum(codex_tokens) if codex_tokens else None,
            "claude_total_tokens": sum(claude_tokens) if claude_tokens else None,
            **token_comparison,
        },
    }


def _successful_outcome_resource_summary(
    rows: List[Dict],
    *,
    success_field: str = "final_passed",
    policy: str = "final-success-first-v1",
) -> Dict:
    return {
        "policy": policy,
        "completed_pairs": len(rows),
        "time_to_success": _successful_outcome_metric(
            rows,
            "duration_ms",
            success_field=success_field,
        ),
        "cost_to_success": _successful_outcome_metric(
            rows,
            "cost_usd",
            success_field=success_field,
        ),
        "tokens_to_success": _successful_outcome_metric(
            rows,
            "total_tokens",
            success_field=success_field,
            require_matching_token_accounting=True,
        ),
    }


def _successful_outcome_metric(
    rows: List[Dict],
    field: str,
    *,
    success_field: str,
    require_matching_token_accounting: bool = False,
) -> Dict:
    observed = []
    comparable = []
    for row in rows:
        codex = row["systems"]["codex"]
        claude = row["systems"]["claude"]
        codex_value = codex.get(field)
        claude_value = claude.get(field)
        if codex_value is None or claude_value is None:
            continue
        if require_matching_token_accounting and not (
            codex.get("token_accounting") == PROVIDER_NATIVE_GROSS_TOKEN_ACCOUNTING
            and claude.get("token_accounting") == PROVIDER_NATIVE_GROSS_TOKEN_ACCOUNTING
        ):
            continue
        observed.append(row)
        if not codex[success_field] and not claude[success_field]:
            continue
        comparable.append(
            _successful_outcome_winner(
                codex[success_field],
                claude[success_field],
                codex_value,
                claude_value,
            )
        )
    return {
        "observed_pairs": len(observed),
        "comparable_pairs": len(comparable),
        "complete": bool(rows) and len(comparable) == len(rows),
        "codex_wins": comparable.count("codex"),
        "claude_wins": comparable.count("claude"),
        "ties": comparable.count("tie"),
        "incomparable_pairs": len(rows) - len(comparable),
    }


def _successful_outcome_winner(
    codex_success: bool,
    claude_success: bool,
    codex_value,
    claude_value,
) -> str:
    if codex_success != claude_success:
        return "codex" if codex_success else "claude"
    return _winner(codex_value, claude_value, higher_is_better=False)


def _lower_resource_summary(pairs: List) -> Dict:
    codex_wins = sum(1 for codex, claude in pairs if codex < claude)
    claude_wins = sum(1 for codex, claude in pairs if claude < codex)
    return {
        "codex_wins": codex_wins,
        "claude_wins": claude_wins,
        "ties": len(pairs) - codex_wins - claude_wins,
    }


def _universal_superiority_summary(
    inference: Dict,
    dimensions: Dict,
    enforcement: Dict,
    resources: Dict,
) -> Dict:
    reasons = []
    if not inference["broad_superiority_supported"]:
        reasons.append("quality-superiority-unproven")
    for dimension, reason in [
        ("execution", "execution-losses-observed"),
        ("final_success", "final-success-losses-observed"),
        ("latency", "latency-losses-observed"),
        ("safety", "safety-losses-observed"),
    ]:
        if dimensions[dimension]["claude_wins"]:
            reasons.append(reason)
    if not enforcement["ready"]:
        reasons.append("cap-stratification-incomplete")
    if not resources["cost"]["complete"]:
        reasons.append("cross-provider-cost-incomparable")
    elif resources["cost"]["claude_wins"]:
        reasons.append("cost-losses-observed")
    if not resources["tokens"]["complete"]:
        reasons.append("cross-provider-token-incomparable")
    elif resources["tokens"]["claude_wins"]:
        reasons.append("token-losses-observed")
    return {
        "superiority_supported": not reasons,
        "required_dimensions": [
            "quality",
            "execution",
            "final_success",
            "latency",
            "safety",
            "cost",
            "tokens",
            "native_cap_stratification",
        ],
        "reasons": reasons,
    }


def _product_profile_universal_summary(
    product_profile_inference: Dict,
    dimensions: Dict,
    enforcement: Dict,
    verified_delivery_resources: Dict,
) -> Dict:
    reasons = []
    if not product_profile_inference["broad_superiority_supported"]:
        reasons.append("quality-superiority-unproven")
    for dimension, reason in [
        ("verified_delivery", "verified-delivery-losses-observed"),
        ("safety", "safety-losses-observed"),
    ]:
        if dimensions[dimension]["claude_wins"]:
            reasons.append(reason)
    if not enforcement["ready"]:
        reasons.append("cap-stratification-incomplete")
    for dimension, incomplete_reason, loss_reason in [
        ("time_to_success", "time-to-success-incomparable", "time-to-success-losses-observed"),
        ("cost_to_success", "cost-to-success-incomparable", "cost-to-success-losses-observed"),
        ("tokens_to_success", "tokens-to-success-incomparable", "tokens-to-success-losses-observed"),
    ]:
        summary = verified_delivery_resources[dimension]
        if not summary["complete"]:
            reasons.append(incomplete_reason)
        elif summary["claude_wins"]:
            reasons.append(loss_reason)
    return {
        "scope": "preregistered-native-product-profile",
        "strict_claim_eligibility_rewritten": False,
        "resource_policy": verified_delivery_resources["policy"],
        "superiority_supported": not reasons,
        "required_dimensions": [
            "quality",
            "verified_delivery",
            "safety",
            "time_to_success",
            "cost_to_success",
            "tokens_to_success",
            "native_cap_stratification",
        ],
        "reasons": reasons,
    }


def _claim_summary(campaign: Dict, trials: List[Dict], completed: List[Dict]) -> Dict:
    reasons = []
    if len(completed) != campaign["design"]["trial_count"]:
        reasons.append("campaign-incomplete")
    if any(trial["status"] == "invalid" for trial in trials):
        reasons.append("invalid-evidence")
    if any(not trial["pair"]["claim_eligible"] for trial in completed):
        reasons.append("pair-provenance-ineligible")
    if campaign["design"]["repetitions"] < MIN_SUPERIORITY_TASK_REPETITIONS:
        reasons.append("insufficient-repetitions")
    return {
        "eligible": not reasons and bool(completed),
        "reasons": reasons or [],
    }


def _inference_summary(campaign: Dict, trials: List[Dict], paired: Dict) -> Dict:
    reasons = []
    if paired["completed_pairs"] < MIN_SUPERIORITY_PAIRS:
        reasons.append("insufficient-pairs")
    completed_by_task = {
        task_id: sum(
            1
            for trial in trials
            if trial["status"] == "completed" and trial["task_id"] == task_id
        )
        for task_id in {trial["task_id"] for trial in trials}
    }
    if any(count < MIN_SUPERIORITY_TASK_REPETITIONS for count in completed_by_task.values()):
        reasons.append("insufficient-task-repetitions")
    interval = paired["mean_difference_ci95"]
    if interval is None:
        reasons.append("mean-interval-unavailable")
    elif interval["lower"] <= 0:
        reasons.append("mean-interval-crosses-zero")
    if paired["claim_eligible_pairs"] != paired["completed_pairs"]:
        reasons.append("provenance-ineligible-pairs")
    if any(trial["status"] == "invalid" for trial in trials):
        reasons.append("invalid-evidence")
    return {
        "broad_superiority_supported": not reasons and paired["completed_pairs"] > 0,
        "reasons": reasons,
        "minimum_pairs": MIN_SUPERIORITY_PAIRS,
        "minimum_repetitions_per_task": MIN_SUPERIORITY_TASK_REPETITIONS,
    }


def _product_profile_inference_summary(
    campaign: Dict,
    trials: List[Dict],
    paired: Dict,
    enforcement: Dict,
) -> Dict:
    reasons = []
    if paired["completed_pairs"] != campaign["design"]["trial_count"]:
        reasons.append("campaign-incomplete")
    if any(trial["status"] == "invalid" for trial in trials):
        reasons.append("invalid-evidence")
    if paired["completed_pairs"] < MIN_SUPERIORITY_PAIRS:
        reasons.append("insufficient-pairs")
    completed_by_task = {
        task_id: sum(
            1
            for trial in trials
            if trial["status"] == "completed" and trial["task_id"] == task_id
        )
        for task_id in {trial["task_id"] for trial in trials}
    }
    if any(count < MIN_SUPERIORITY_TASK_REPETITIONS for count in completed_by_task.values()):
        reasons.append("insufficient-task-repetitions")
    interval = paired["mean_difference_ci95"]
    if interval is None:
        reasons.append("mean-interval-unavailable")
    elif interval["lower"] <= 0:
        reasons.append("mean-interval-crosses-zero")
    if enforcement["stratification_eligible_pairs"] != paired["completed_pairs"]:
        reasons.append("stratification-ineligible-pairs")
    if not enforcement["ready"]:
        reasons.append("cap-stratification-incomplete")
    return {
        "scope": "preregistered-native-product-profile",
        "provenance_basis": "native-cap-stratification",
        "strict_claim_eligibility_rewritten": False,
        "broad_superiority_supported": not reasons and paired["completed_pairs"] > 0,
        "reasons": reasons,
        "minimum_pairs": MIN_SUPERIORITY_PAIRS,
        "minimum_repetitions_per_task": MIN_SUPERIORITY_TASK_REPETITIONS,
        "stratification_eligible_pairs": enforcement["stratification_eligible_pairs"],
        "cap_stratification_ready": enforcement["ready"],
        "cap_stratification_reasons": list(enforcement["reasons"]),
    }


def _safety_summary(manifest: Dict) -> Dict:
    incidents = [
        incident
        for incident in manifest["safety"]["incidents"]
        if incident["severity"] != "info"
    ]
    highest = "none"
    for incident in incidents:
        severity = incident["severity"]
        if SAFETY_SEVERITY_RANK[severity] > SAFETY_SEVERITY_RANK[highest]:
            highest = severity
    return {"incident_count": len(incidents), "highest_severity": highest}


def _native_cap_reached(system: str, provider_status: str) -> bool:
    if system == "codex":
        return provider_status == "token-budget-exceeded"
    return provider_status == "budget-exceeded"


def _cap_cohort(systems: Dict) -> str:
    codex = systems["codex"]["native_cap_reached"]
    claude = systems["claude"]["native_cap_reached"]
    if codex and claude:
        return "both-native-caps-reached"
    if codex:
        return "codex-token-cap-only"
    if claude:
        return "claude-dollar-cap-only"
    return "neither-native-cap-reached"


def _cap_stratification_eligibility(provenance: Dict):
    if provenance.get("claim_eligible") is True:
        return True, "strict-claim-eligible"
    if provenance.get("complete") is not True or provenance.get("all_live") is not True:
        return False, "incomplete-provenance"
    equivalence = provenance.get("equivalence")
    if not isinstance(equivalence, dict):
        return False, "invalid-equivalence"
    for category in ["repository", "model", "permissions", "quality"]:
        if not isinstance(equivalence.get(category), dict) or equivalence[category].get("equivalent") is not True:
            return False, "%s-incomparable" % category
    budget = equivalence.get("budget")
    if not isinstance(budget, dict) or budget.get("equivalent") is not False:
        return False, "unexpected-budget-equivalence"
    fields = budget.get("fields")
    if not isinstance(fields, dict):
        return False, "invalid-budget-equivalence"
    matched_fields = ["max_minutes", "max_agents", "max_live_tool_cost", "max_cost_usd", "max_tokens"]
    if any(fields.get(field) is not True for field in matched_fields):
        return False, "numeric-budget-incomparable"
    if fields.get("cost_cap_enforcement") is not False or fields.get("token_cap_enforcement") is not False:
        return False, "unexpected-enforcement-equivalence"
    if provenance.get("warnings") != ["budget provenance differs across manifests."]:
        return False, "unexpected-provenance-warning"
    return True, "native-cap-enforcement-stratified"


def _winner(codex_value, claude_value, *, higher_is_better: bool = True) -> str:
    if codex_value == claude_value:
        return "tie"
    codex_better = codex_value > claude_value if higher_is_better else codex_value < claude_value
    return "codex" if codex_better else "claude"


def _safety_winner(codex: Dict, claude: Dict) -> str:
    codex_record = (
        SAFETY_SEVERITY_RANK[codex["highest_safety_severity"]],
        codex["safety_incident_count"],
    )
    claude_record = (
        SAFETY_SEVERITY_RANK[claude["highest_safety_severity"]],
        claude["safety_incident_count"],
    )
    return _winner(codex_record, claude_record, higher_is_better=False)


def _row_dominance(winners: Dict) -> str:
    values = set(winners.values())
    if values == {"tie"}:
        return "tie"
    if "claude" not in values:
        return "codex"
    if "codex" not in values:
        return "claude"
    return "mixed"


def _mean_ci95(values: List[float]) -> Optional[Dict]:
    if len(values) < 2:
        return None
    mean = statistics.mean(values)
    standard_error = statistics.stdev(values) / math.sqrt(len(values))
    degrees = len(values) - 1
    critical = _T_CRITICAL_95.get(degrees, 1.96)
    margin = critical * standard_error
    return {
        "method": "paired-student-t",
        "lower": _round(mean - margin),
        "upper": _round(mean + margin),
    }


def _wilson_ci95(successes: int, total: int) -> Dict:
    z = 1.96
    proportion = float(successes) / total
    denominator = 1.0 + z * z / total
    center = (proportion + z * z / (2.0 * total)) / denominator
    margin = z * math.sqrt((proportion * (1.0 - proportion) + z * z / (4.0 * total)) / total) / denominator
    return {
        "method": "wilson-score",
        "lower": _round(max(0.0, center - margin)),
        "upper": _round(min(1.0, center + margin)),
    }


def _resolve_artifact_path(campaign_path: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(campaign_path).parent / path
    return Path(os.path.abspath(str(path)))


def _sha256_json(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _require_sha256(value, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValidationError("%s must be a SHA-256 value" % label)


def _reject_duplicate_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant")


def _percentage(score: float, maximum: float) -> float:
    return _round(100.0 * score / maximum)


def _ratio(numerator: int, denominator: int) -> float:
    return _round(float(numerator) / denominator) if denominator else 0.0


def _round(value: float) -> float:
    return round(float(value), 6)
