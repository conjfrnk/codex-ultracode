import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .clock import utc_now
from .dashboard import collect_goals, collect_reports, collect_runs, run_handle
from .errors import ConductorError, ValidationError
from .redaction import redact_json_value, redact_text
from .routines import iter_routine_manifest_paths, load_routine_manifest, routine_list_record
from .security import read_regular_text_file_no_follow, reject_symlink_path, write_new_text_file_no_follow


EVIDENCE_BUNDLE_SCHEMA = "conductor.evidence_bundle.v1"
MAX_EVIDENCE_BUNDLE_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_ITEMS = 500

TOP_LEVEL_FIELDS = {
    "schema",
    "generated_at_utc",
    "source",
    "limits",
    "counts",
    "claim",
    "runs",
    "goals",
    "routines",
    "reports",
}
SOURCE_FIELDS = {"runs_dir", "benchmarks_dir", "goals_dir", "routines_dir"}
LIMIT_FIELDS = {"max_runs", "max_reports", "max_goals", "max_routines"}
COUNT_FIELDS = {
    "runs",
    "evidence_complete_runs",
    "goals",
    "goals_needing_attention",
    "routines",
    "planned_routines",
    "reports",
    "reports_needing_attention",
    "benchmark_reports",
    "comparison_reports",
    "claim_eligible_reports",
    "invalid_reports",
}
CLAIM_FIELDS = {
    "status",
    "parity_claim_eligible",
    "superiority_claim_eligible",
    "reason",
    "eligible_reports",
    "blockers",
    "warnings",
}
CLAIM_STATUSES = {"claim-eligible", "claim-blocked"}
RUN_FIELDS = {
    "id",
    "run_handle",
    "status",
    "workflow",
    "evidence_complete",
    "started_at_utc",
    "finished_at_utc",
    "duration_ms",
    "mtime_utc",
}
GOAL_FIELDS = {
    "goal_id",
    "goal_handle",
    "status",
    "workflow",
    "iterations",
    "max_iterations",
    "updated_at_utc",
    "evidence_attention",
    "last_iteration",
    "last_check_status",
    "last_check_returncode",
    "error",
}
GOAL_LAST_ITERATION_FIELDS = {
    "iteration",
    "status",
    "run_id",
    "run_status",
    "started_at_utc",
    "finished_at_utc",
}
ROUTINE_FIELDS = {
    "name",
    "schedule",
    "target_kind",
    "workflow",
    "evidence_status",
    "no_process_started",
    "policy",
    "created_at_utc",
    "error",
}
ROUTINE_POLICY_FIELDS = {
    "allow_writes",
    "allow_destructive",
    "allow_network",
    "allow_agent",
    "allow_parallel",
    "approval_count",
}
REPORT_FIELDS = {
    "name",
    "kind",
    "schema",
    "suite",
    "system",
    "result",
    "failed_tasks",
    "duration_ms",
    "claim_eligible",
    "provenance_complete",
    "all_live",
    "warnings",
    "error",
    "evidence_status",
    "evidence_label",
    "evidence_attention",
    "mtime_utc",
    "source_sha256",
}


def build_evidence_bundle(
    *,
    runs_dir: Path,
    benchmarks_dir: Path,
    goals_dir: Path,
    routines_dir: Path,
    max_runs: int = 50,
    max_reports: int = 50,
    max_goals: int = 50,
    max_routines: int = 50,
) -> Dict:
    _validate_limit(max_runs, "max_runs")
    _validate_limit(max_reports, "max_reports")
    _validate_limit(max_goals, "max_goals")
    _validate_limit(max_routines, "max_routines")

    runs = [_run_record(run) for run in collect_runs(runs_dir, max_runs=max_runs)]
    goals = [_goal_record(goal) for goal in collect_goals(goals_dir, max_goals=max_goals)]
    reports = collect_evidence_report_records(benchmarks_dir, max_reports=max_reports)
    routines = _routine_records(routines_dir, max_routines=max_routines)
    counts = _counts(runs, goals, routines, reports)
    claim = _claim_summary(reports, counts)
    bundle = {
        "schema": EVIDENCE_BUNDLE_SCHEMA,
        "generated_at_utc": _utc_now(),
        "source": {
            "runs_dir": "configured",
            "benchmarks_dir": "configured",
            "goals_dir": "configured",
            "routines_dir": "configured",
        },
        "limits": {
            "max_runs": max_runs,
            "max_reports": max_reports,
            "max_goals": max_goals,
            "max_routines": max_routines,
        },
        "counts": counts,
        "claim": claim,
        "runs": runs,
        "goals": goals,
        "routines": routines,
        "reports": reports,
    }
    validate_evidence_bundle(bundle)
    return bundle


def load_evidence_bundle(path: Path) -> Dict:
    try:
        data = json.loads(read_regular_text_file_no_follow(path, "evidence bundle", MAX_EVIDENCE_BUNDLE_BYTES))
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    validate_evidence_bundle(data, source=str(path))
    return data


def write_evidence_bundle(bundle: Dict, destination: Path) -> Path:
    validate_evidence_bundle(bundle, source=str(destination))
    reject_symlink_path(destination, "evidence bundle output")
    reject_symlink_path(destination.parent, "evidence bundle output parent")
    if destination.exists() or destination.is_symlink():
        raise ValidationError("evidence bundle output already exists: %s" % destination)
    if destination.suffix != ".json":
        raise ValidationError("evidence bundle output must use .json: %s" % destination)
    text = json.dumps(redact_json_value(bundle), indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_EVIDENCE_BUNDLE_BYTES:
        raise ValidationError("evidence bundle must be at most %d bytes" % MAX_EVIDENCE_BUNDLE_BYTES)
    write_new_text_file_no_follow(destination, "evidence bundle output", text)
    return destination


def validate_evidence_bundle(bundle: Dict, source: str = "<memory>") -> None:
    if not isinstance(bundle, dict):
        raise ValidationError("%s must contain a JSON object" % source)
    _reject_unknown_keys(bundle, TOP_LEVEL_FIELDS, "%s evidence bundle" % source)
    if bundle.get("schema") != EVIDENCE_BUNDLE_SCHEMA:
        raise ValidationError("%s must set schema to %s" % (source, EVIDENCE_BUNDLE_SCHEMA))
    if _parse_utc(bundle.get("generated_at_utc")) is None:
        raise ValidationError("%s generated_at_utc must be an ISO UTC timestamp" % source)
    _validate_string_object(bundle.get("source"), SOURCE_FIELDS, "%s source" % source)
    _validate_limits_object(bundle.get("limits"), source)
    _validate_counts(bundle.get("counts"), source)
    _validate_claim(bundle.get("claim"), source)
    _validate_list(bundle.get("runs"), "%s runs" % source, _validate_run_record, RUN_FIELDS)
    _validate_list(bundle.get("goals"), "%s goals" % source, _validate_goal_record, GOAL_FIELDS)
    _validate_list(bundle.get("routines"), "%s routines" % source, _validate_routine_record, ROUTINE_FIELDS)
    _validate_list(bundle.get("reports"), "%s reports" % source, _validate_report_record, REPORT_FIELDS)
    _validate_bundle_consistency(bundle, source)


def collect_evidence_report_records(benchmarks_dir: Path, max_reports: int = 50) -> List[Dict]:
    _validate_limit(max_reports, "max_reports")
    return [_report_record(report) for report in collect_reports(benchmarks_dir, max_reports=max_reports)]


def require_claim_eligible(bundle: Dict, source_reports: Optional[List[Dict]] = None) -> None:
    validate_evidence_bundle(bundle)
    claim = bundle.get("claim") if isinstance(bundle.get("claim"), dict) else {}
    if claim.get("parity_claim_eligible") is not True:
        blockers = claim.get("blockers") if isinstance(claim.get("blockers"), list) else []
        detail = "; ".join(str(item) for item in blockers[:4]) or "no claim-eligible evidence bundle"
        raise ValidationError("evidence bundle is not claim-eligible: %s" % redact_text(detail))
    if source_reports is not None:
        _validate_list(source_reports, "source reports", _validate_report_record, REPORT_FIELDS)
        _require_source_backed_claim_reports(bundle, source_reports)


def _routine_records(routines_dir: Path, max_routines: int) -> List[Dict]:
    records = []
    for path in iter_routine_manifest_paths(routines_dir, max_routines=max_routines):
        try:
            record = routine_list_record(path, load_routine_manifest(path))
            records.append(_routine_record(record))
        except ConductorError as exc:
            records.append(
                _routine_record(
                    {
                        "name": path.stem,
                        "schedule": "unknown",
                        "target_kind": "unknown",
                        "workflow": "unknown",
                        "evidence_status": "invalid",
                        "no_process_started": False,
                        "policy": {},
                        "created_at_utc": "",
                        "error": exc.__class__.__name__,
                    }
                )
            )
    return records


def _run_record(run: Dict) -> Dict:
    record = _compact_record(
        run,
        RUN_FIELDS,
        defaults={
            "id": "unknown",
            "run_handle": "",
            "status": "unknown",
            "workflow": "unknown",
            "evidence_complete": False,
            "started_at_utc": "",
            "finished_at_utc": "",
            "duration_ms": None,
            "mtime_utc": "",
        },
    )
    if not record.get("run_handle") and record.get("id"):
        record["run_handle"] = run_handle(str(record["id"]))
    return record


def _goal_record(goal: Dict) -> Dict:
    return _compact_record(
        goal,
        GOAL_FIELDS,
        defaults={
            "goal_id": "unknown",
            "goal_handle": "",
            "status": "unknown",
            "workflow": "unknown",
            "iterations": 0,
            "max_iterations": None,
            "updated_at_utc": "",
            "evidence_attention": True,
            "last_iteration": {},
            "last_check_status": "",
            "last_check_returncode": None,
            "error": "",
        },
    )


def _routine_record(routine: Dict) -> Dict:
    record = _compact_record(
        routine,
        ROUTINE_FIELDS,
        defaults={
            "name": "unknown",
            "schedule": "unknown",
            "target_kind": "unknown",
            "workflow": "unknown",
            "evidence_status": "unknown",
            "no_process_started": False,
            "policy": {},
            "created_at_utc": "",
            "error": "",
        },
    )
    policy = record.get("policy") if isinstance(record.get("policy"), dict) else {}
    record["policy"] = _compact_record(
        policy,
        ROUTINE_POLICY_FIELDS,
        defaults={
            "allow_writes": False,
            "allow_destructive": False,
            "allow_network": False,
            "allow_agent": False,
            "allow_parallel": False,
            "approval_count": 0,
        },
    )
    return record


def _report_record(report: Dict) -> Dict:
    record = _compact_record(
        report,
        REPORT_FIELDS,
        defaults={
            "name": "unknown",
            "kind": "unknown",
            "schema": "unknown",
            "suite": "unknown",
            "system": "unknown",
            "result": "unknown",
            "failed_tasks": None,
            "duration_ms": None,
            "claim_eligible": None,
            "provenance_complete": None,
            "all_live": None,
            "warnings": [],
            "error": "",
            "evidence_status": "unknown-report",
            "evidence_label": "unknown report",
            "evidence_attention": True,
            "mtime_utc": "",
            "source_sha256": "",
        },
    )
    if not record.get("source_sha256"):
        record["source_sha256"] = _report_source_sha256(report)
    return record


def _report_source_sha256(report: Dict) -> str:
    if not isinstance(report, dict) or not isinstance(report.get("path"), Path):
        return ""
    try:
        text = read_regular_text_file_no_follow(report["path"], "evidence source report", MAX_EVIDENCE_BUNDLE_BYTES)
    except ConductorError:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compact_record(record: Dict, fields: set, defaults: Dict) -> Dict:
    result = {}
    raw = record if isinstance(record, dict) else {}
    for field in fields:
        if field in raw:
            result[field] = _public_value(raw[field])
        elif field in defaults:
            result[field] = _public_value(defaults[field])
    return {key: result[key] for key in sorted(result)}


def _public_value(value):
    if isinstance(value, Path):
        return redact_text(str(value))
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_public_value(item) for item in value[:MAX_EVIDENCE_ITEMS]]
    if isinstance(value, tuple):
        return [_public_value(item) for item in value[:MAX_EVIDENCE_ITEMS]]
    if isinstance(value, dict):
        public = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text == "path" or key_text.endswith("_path"):
                continue
            public[redact_text(key_text)] = _public_value(item)
        return public
    return value


def _counts(runs: List[Dict], goals: List[Dict], routines: List[Dict], reports: List[Dict]) -> Dict:
    return {
        "runs": len(runs),
        "evidence_complete_runs": sum(1 for run in runs if run.get("evidence_complete") is True),
        "goals": len(goals),
        "goals_needing_attention": sum(1 for goal in goals if goal.get("evidence_attention") is True),
        "routines": len(routines),
        "planned_routines": sum(1 for routine in routines if routine.get("evidence_status") == "planned-not-evidence"),
        "reports": len(reports),
        "reports_needing_attention": sum(1 for report in reports if report.get("evidence_attention") is True),
        "benchmark_reports": sum(1 for report in reports if report.get("kind") == "benchmark"),
        "comparison_reports": sum(1 for report in reports if report.get("kind") == "comparison"),
        "claim_eligible_reports": sum(1 for report in reports if _report_is_claim_eligible(report)),
        "invalid_reports": sum(1 for report in reports if report.get("evidence_status") == "invalid-report"),
    }


def _claim_summary(reports: List[Dict], counts: Dict) -> Dict:
    eligible_reports = _claim_eligible_report_names(reports)
    blockers = []
    warnings = []
    if not eligible_reports:
        blockers.append("no claim-eligible live comparison report found")
    if counts["comparison_reports"] == 0:
        blockers.append("no comparison reports found")
    if counts["benchmark_reports"] + counts["comparison_reports"] == 0:
        blockers.append("no benchmark or comparison reports found")
    if counts["reports_needing_attention"] > 0:
        warnings.append("%d report(s) need attention" % counts["reports_needing_attention"])
    if counts["goals_needing_attention"] > 0:
        warnings.append("%d goal(s) need attention" % counts["goals_needing_attention"])
    if counts["planned_routines"] > 0:
        warnings.append("%d routine manifest(s) are planned-not-evidence" % counts["planned_routines"])
    parity_eligible = bool(eligible_reports)
    return {
        "status": "claim-eligible" if parity_eligible else "claim-blocked",
        "parity_claim_eligible": parity_eligible,
        "superiority_claim_eligible": False,
        "reason": (
            "At least one comparison report declares complete live claim-eligible provenance."
            if parity_eligible
            else "Parity and superiority claims are blocked until an equivalent live comparison report is present."
        ),
        "eligible_reports": eligible_reports,
        "blockers": blockers,
        "warnings": warnings,
    }


def _validate_limit(value, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > MAX_EVIDENCE_ITEMS:
        raise ValidationError("%s must be an integer from 1 to %d" % (label, MAX_EVIDENCE_ITEMS))


def _validate_limits_object(limits, source: str) -> None:
    if not isinstance(limits, dict):
        raise ValidationError("%s limits must be an object" % source)
    _reject_unknown_keys(limits, LIMIT_FIELDS, "%s limits" % source)
    for field in LIMIT_FIELDS:
        _validate_limit(limits.get(field), "%s limits.%s" % (source, field))


def _validate_counts(counts, source: str) -> None:
    if not isinstance(counts, dict):
        raise ValidationError("%s counts must be an object" % source)
    _reject_unknown_keys(counts, COUNT_FIELDS, "%s counts" % source)
    for field in COUNT_FIELDS:
        _validate_non_negative_int(counts.get(field), "%s counts.%s" % (source, field))


def _validate_claim(claim, source: str) -> None:
    if not isinstance(claim, dict):
        raise ValidationError("%s claim must be an object" % source)
    _reject_unknown_keys(claim, CLAIM_FIELDS, "%s claim" % source)
    if claim.get("status") not in CLAIM_STATUSES:
        raise ValidationError("%s claim.status is invalid" % source)
    for field in ["parity_claim_eligible", "superiority_claim_eligible"]:
        if not isinstance(claim.get(field), bool):
            raise ValidationError("%s claim.%s must be boolean" % (source, field))
    for field in ["reason"]:
        if not isinstance(claim.get(field), str) or not claim[field].strip():
            raise ValidationError("%s claim.%s must be a non-empty string" % (source, field))
    for field in ["eligible_reports", "blockers", "warnings"]:
        if not isinstance(claim.get(field), list) or not all(isinstance(item, str) for item in claim[field]):
            raise ValidationError("%s claim.%s must be a string list" % (source, field))
    if claim["status"] == "claim-eligible" and claim["parity_claim_eligible"] is not True:
        raise ValidationError("%s claim.status conflicts with parity_claim_eligible" % source)
    if claim["status"] == "claim-blocked" and claim["parity_claim_eligible"] is True:
        raise ValidationError("%s claim.status conflicts with parity_claim_eligible" % source)
    if claim["parity_claim_eligible"] is True and not claim["eligible_reports"]:
        raise ValidationError("%s claim.eligible_reports is required when parity_claim_eligible is true" % source)
    if claim["superiority_claim_eligible"] is True:
        raise ValidationError("%s claim.superiority_claim_eligible is not supported by schema v1" % source)


def _validate_bundle_consistency(bundle: Dict, source: str) -> None:
    counts = bundle["counts"]
    runs = bundle["runs"]
    goals = bundle["goals"]
    routines = bundle["routines"]
    reports = bundle["reports"]
    expected = _counts(runs, goals, routines, reports)
    for field, value in expected.items():
        if counts.get(field) != value:
            raise ValidationError("%s counts.%s does not match bundle records" % (source, field))
    claim = bundle["claim"]
    eligible_reports = _claim_eligible_report_names(reports)
    if claim.get("eligible_reports") != eligible_reports:
        raise ValidationError("%s claim.eligible_reports does not match reports" % source)
    if claim.get("parity_claim_eligible") is not bool(eligible_reports):
        raise ValidationError("%s claim.parity_claim_eligible does not match reports" % source)


def _validate_list(items, label: str, validator, fields: set) -> None:
    if not isinstance(items, list):
        raise ValidationError("%s must be a list" % label)
    if len(items) > MAX_EVIDENCE_ITEMS:
        raise ValidationError("%s must contain at most %d items" % (label, MAX_EVIDENCE_ITEMS))
    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("%s items must be objects" % label)
        _reject_unknown_keys(item, fields, "%s item" % label)
        missing = sorted(fields - set(item))
        if missing:
            raise ValidationError("%s item missing field(s): %s" % (label, ", ".join(missing)))
        validator(item, label)


def _validate_run_record(record: Dict, label: str) -> None:
    for field in ["id", "status", "workflow"]:
        _validate_string(record.get(field), "%s.%s" % (label, field), allow_empty=False)
    _validate_string(record.get("run_handle"), "%s.run_handle" % label)
    _validate_bool(record.get("evidence_complete"), "%s.evidence_complete" % label)
    _validate_optional_int(record.get("duration_ms"), "%s.duration_ms" % label)


def _validate_goal_record(record: Dict, label: str) -> None:
    for field in ["goal_id", "status", "workflow"]:
        _validate_string(record.get(field), "%s.%s" % (label, field), allow_empty=False)
    _validate_string(record.get("goal_handle"), "%s.goal_handle" % label)
    _validate_non_negative_int(record.get("iterations"), "%s.iterations" % label)
    _validate_optional_int(record.get("max_iterations"), "%s.max_iterations" % label)
    _validate_bool(record.get("evidence_attention"), "%s.evidence_attention" % label)
    _validate_goal_last_iteration(record.get("last_iteration"), "%s.last_iteration" % label)


def _validate_routine_record(record: Dict, label: str) -> None:
    for field in ["name", "schedule", "target_kind", "workflow", "evidence_status"]:
        _validate_string(record.get(field), "%s.%s" % (label, field), allow_empty=False)
    _validate_bool(record.get("no_process_started"), "%s.no_process_started" % label)
    policy = record.get("policy")
    if not isinstance(policy, dict):
        raise ValidationError("%s.policy must be an object" % label)
    _reject_unknown_keys(policy, ROUTINE_POLICY_FIELDS, "%s.policy" % label)
    for field in ["allow_writes", "allow_destructive", "allow_network", "allow_agent", "allow_parallel"]:
        _validate_bool(policy.get(field), "%s.policy.%s" % (label, field))
    _validate_non_negative_int(policy.get("approval_count"), "%s.policy.approval_count" % label)


def _validate_report_record(record: Dict, label: str) -> None:
    for field in ["name", "kind", "schema", "suite", "system", "result", "evidence_status", "evidence_label"]:
        _validate_string(record.get(field), "%s.%s" % (label, field), allow_empty=False)
    _validate_string(record.get("source_sha256"), "%s.source_sha256" % label)
    if not _valid_sha256(record.get("source_sha256")):
        raise ValidationError("%s.source_sha256 must be a sha256 hex string" % label)
    _validate_bool(record.get("evidence_attention"), "%s.evidence_attention" % label)
    _validate_optional_int(record.get("failed_tasks"), "%s.failed_tasks" % label)
    _validate_optional_int(record.get("duration_ms"), "%s.duration_ms" % label)
    for field in ["claim_eligible", "provenance_complete", "all_live"]:
        _validate_optional_bool(record.get(field), "%s.%s" % (label, field))
    if not isinstance(record.get("warnings"), list) or not all(isinstance(item, str) for item in record["warnings"]):
        raise ValidationError("%s.warnings must be a string list" % label)
    if record.get("evidence_status") == "claim-eligible-report" and not _report_is_claim_eligible(record):
        raise ValidationError("%s claim-eligible report does not have complete live comparison provenance" % label)


def _validate_goal_last_iteration(value, label: str) -> None:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % label)
    if not value:
        return
    _reject_unknown_keys(value, GOAL_LAST_ITERATION_FIELDS, label)
    missing = sorted(GOAL_LAST_ITERATION_FIELDS - set(value))
    if missing:
        raise ValidationError("%s missing field(s): %s" % (label, ", ".join(missing)))
    _validate_optional_int(value.get("iteration"), "%s.iteration" % label)
    for field in ["status", "run_id", "run_status", "started_at_utc", "finished_at_utc"]:
        _validate_string(value.get(field), "%s.%s" % (label, field))


def _validate_string_object(value, fields: set, label: str) -> None:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % label)
    _reject_unknown_keys(value, fields, label)
    for field in fields:
        _validate_string(value.get(field), "%s.%s" % (label, field), allow_empty=False)


def _validate_string(value, label: str, allow_empty: bool = True) -> None:
    if not isinstance(value, str):
        raise ValidationError("%s must be a string" % label)
    if not allow_empty and not value.strip():
        raise ValidationError("%s must be a non-empty string" % label)


def _validate_bool(value, label: str) -> None:
    if not isinstance(value, bool):
        raise ValidationError("%s must be boolean" % label)


def _validate_optional_bool(value, label: str) -> None:
    if value is not None:
        _validate_bool(value, label)


def _validate_non_negative_int(value, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError("%s must be a non-negative integer" % label)


def _validate_optional_int(value, label: str) -> None:
    if value is not None:
        _validate_non_negative_int(value, label)


def _reject_unknown_keys(value: Dict, allowed: set, label: str) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        raise ValidationError("%s has unsupported field(s): %s" % (label, ", ".join(extra)))


def _claim_eligible_report_names(reports: List[Dict]) -> List[str]:
    return [str(report.get("name") or "unknown") for report in reports if _report_is_claim_eligible(report)]


def _report_is_claim_eligible(report: Dict) -> bool:
    return (
        isinstance(report, dict)
        and report.get("schema") == "conductor.comparison_report.v1"
        and report.get("kind") == "comparison"
        and report.get("evidence_status") == "claim-eligible-report"
        and report.get("claim_eligible") is True
        and report.get("provenance_complete") is True
        and report.get("all_live") is True
        and report.get("evidence_attention") is False
        and report.get("failed_tasks") == 0
        and _valid_sha256(report.get("source_sha256"))
    )


def _require_source_backed_claim_reports(bundle: Dict, source_reports: List[Dict]) -> None:
    source_claims = {
        (report.get("name"), report.get("source_sha256"))
        for report in source_reports
        if _report_is_claim_eligible(report)
    }
    missing = []
    for report in bundle.get("reports", []):
        if not _report_is_claim_eligible(report):
            continue
        key = (report.get("name"), report.get("source_sha256"))
        if key not in source_claims:
            missing.append(str(report.get("name") or "unknown"))
    if missing:
        raise ValidationError(
            "evidence bundle claim is not backed by current source report(s): %s"
            % ", ".join(redact_text(name) for name in missing[:5])
        )


def _valid_sha256(value) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(char in "0123456789abcdef" for char in value)


def _utc_now() -> str:
    return utc_now().isoformat(timespec="seconds") + "Z"


def _parse_utc(value):
    if not isinstance(value, str) or not value:
        return None
    if not value.endswith("Z"):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
