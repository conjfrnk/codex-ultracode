import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .errors import ValidationError
from .clock import utc_now
from .redaction import contains_secret_like, redact_text
from .security import read_regular_text_file_no_follow
from .workflow import SAFE_ID


BENCHMARK_QUALITY_EVIDENCE_SCHEMA = "conductor.benchmark_quality_evidence.v1"
BENCHMARK_QUALITY_EVIDENCE_SCHEMA_V2 = "conductor.benchmark_quality_evidence.v2"
BENCHMARK_SCORE_INPUT_SCHEMA = "conductor.benchmark_score_input.v1"
QUALITY_REVIEWER_KINDS = {"human", "deterministic", "model"}
QUALITY_EVIDENCE_FIELDS = {
    "schema",
    "suite",
    "task_id",
    "task_contract_sha256",
    "output_sha256",
    "provider_evidence_sha256",
    "staged_evidence_sha256",
    "reviewer",
    "criteria",
    "score",
    "max_score",
    "pass_threshold",
    "passed",
    "generated_at_utc",
}
QUALITY_EVIDENCE_FIELDS_V2 = QUALITY_EVIDENCE_FIELDS | {
    "repair_evidence_sha256",
    "repair_attempt_reports_sha256",
}
QUALITY_REVIEWER_FIELDS = {"kind", "identity", "independent"}
QUALITY_CRITERION_FIELDS = {"id", "score", "max_score", "notes"}
SCORE_INPUT_FIELDS = {"schema", "reviewer", "tasks"}
SCORE_INPUT_TASK_FIELDS = {"id", "pass_threshold", "criteria"}
MAX_QUALITY_EVIDENCE_BYTES = 512 * 1024
MAX_SCORE_INPUT_BYTES = 512 * 1024
MAX_QUALITY_TASKS = 1000
MAX_QUALITY_CRITERIA = 100
MAX_QUALITY_SCORE = 1000000
MAX_QUALITY_NOTES_CHARS = 4096


def score_benchmark_report(report: Dict, parity_tasks: Dict, score_input: Dict) -> Dict:
    if not isinstance(report, dict) or not isinstance(report.get("results"), list) or not report["results"]:
        raise ValidationError("benchmark report must contain results before scoring")
    if not isinstance(parity_tasks, dict) or not isinstance(parity_tasks.get("tasks"), list):
        raise ValidationError("parity task fixture must contain tasks before scoring")
    clean_input = validate_benchmark_score_input(score_input)
    report_ids = [result.get("id") for result in report["results"]]
    if any(not isinstance(task_id, str) or not SAFE_ID.match(task_id) for task_id in report_ids):
        raise ValidationError("benchmark report contains an invalid task id")
    if len(set(report_ids)) != len(report_ids):
        raise ValidationError("benchmark report contains duplicate task ids")
    task_map = {task.get("id"): task for task in parity_tasks["tasks"] if isinstance(task, dict)}
    score_map = {task["id"]: task for task in clean_input["tasks"]}
    if set(report_ids) != set(score_map):
        raise ValidationError("score input task ids must exactly match benchmark report task ids")
    missing_contracts = sorted(set(report_ids) - set(task_map))
    if missing_contracts:
        raise ValidationError("parity task fixture is missing scored task ids: %s" % ", ".join(missing_contracts))
    if report.get("suite") != parity_tasks.get("name"):
        raise ValidationError("benchmark report suite must match the parity task fixture")

    scored = copy.deepcopy(report)
    generated_at = utc_now().isoformat(timespec="seconds") + "Z"
    for result in scored["results"]:
        if "quality_evidence" in result or "execution_passed" in result:
            raise ValidationError("benchmark result %s has already been scored" % result["id"])
        score_task = score_map[result["id"]]
        criteria = copy.deepcopy(score_task["criteria"])
        score = sum(item["score"] for item in criteria)
        max_score = sum(item["max_score"] for item in criteria)
        threshold = score_task["pass_threshold"]
        quality = {
            "schema": (
                BENCHMARK_QUALITY_EVIDENCE_SCHEMA_V2
                if isinstance(result.get("repair_evidence"), dict)
                else BENCHMARK_QUALITY_EVIDENCE_SCHEMA
            ),
            "suite": report["suite"],
            "task_id": result["id"],
            "task_contract_sha256": _sha256_json(task_map[result["id"]]),
            "output_sha256": _sha256_text(str(result.get("stdout", ""))),
            "provider_evidence_sha256": _optional_json_sha256(result.get("provider_evidence")),
            "staged_evidence_sha256": _optional_json_sha256(result.get("staged_evidence")),
            "reviewer": copy.deepcopy(clean_input["reviewer"]),
            "criteria": criteria,
            "score": score,
            "max_score": max_score,
            "pass_threshold": threshold,
            "passed": score >= threshold,
            "generated_at_utc": generated_at,
        }
        if quality["schema"] == BENCHMARK_QUALITY_EVIDENCE_SCHEMA_V2:
            quality["repair_evidence_sha256"] = _optional_json_sha256(result.get("repair_evidence"))
            quality["repair_attempt_reports_sha256"] = _optional_json_sha256(
                result.get("repair_attempt_reports")
            )
        validate_benchmark_quality_evidence(quality, result=result)
        result["execution_passed"] = result["passed"]
        result["quality_evidence"] = quality
        result["passed"] = bool(result["execution_passed"] and quality["passed"])
    passed = sum(1 for result in scored["results"] if result["passed"])
    scored["passed_tasks"] = passed
    scored["failed_tasks"] = len(scored["results"]) - passed
    return scored


def validate_benchmark_score_input(value: Dict, source: str = "<memory>") -> Dict:
    if not isinstance(value, dict):
        raise ValidationError("%s must contain an object" % source)
    _json_size(value, MAX_SCORE_INPUT_BYTES, source)
    _exact_fields(value, SCORE_INPUT_FIELDS, source)
    if value.get("schema") != BENCHMARK_SCORE_INPUT_SCHEMA:
        raise ValidationError("%s must set schema to %s" % (source, BENCHMARK_SCORE_INPUT_SCHEMA))
    reviewer = _strict_object(value.get("reviewer"), QUALITY_REVIEWER_FIELDS, "%s reviewer" % source)
    _validate_reviewer(reviewer, "%s reviewer" % source)
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_QUALITY_TASKS:
        raise ValidationError("%s tasks must be a bounded non-empty array" % source)
    seen = set()
    cleaned_tasks = []
    for task in tasks:
        task = _strict_object(task, SCORE_INPUT_TASK_FIELDS, "%s task" % source)
        task_id = task.get("id")
        if not isinstance(task_id, str) or not SAFE_ID.match(task_id) or task_id in seen:
            raise ValidationError("%s score input task ids must be unique safe identifiers" % source)
        seen.add(task_id)
        criteria = _validate_criteria(task.get("criteria"), "%s task %s" % (source, task_id))
        max_score = sum(item["max_score"] for item in criteria)
        threshold = _bounded_int(task.get("pass_threshold"), "%s task %s pass_threshold" % (source, task_id), 1, MAX_QUALITY_SCORE)
        if threshold > max_score:
            raise ValidationError("%s task %s pass_threshold exceeds max score" % (source, task_id))
        cleaned_tasks.append({"id": task_id, "pass_threshold": threshold, "criteria": criteria})
    return {
        "schema": BENCHMARK_SCORE_INPUT_SCHEMA,
        "reviewer": {
            "kind": reviewer["kind"],
            "identity": reviewer["identity"],
            "independent": reviewer["independent"],
        },
        "tasks": cleaned_tasks,
    }


def validate_benchmark_quality_evidence(
    evidence: Dict,
    source: str = "<memory>",
    *,
    result: Optional[Dict] = None,
) -> None:
    if not isinstance(evidence, dict):
        raise ValidationError("%s must contain an object" % source)
    _json_size(evidence, MAX_QUALITY_EVIDENCE_BYTES, source)
    schema = evidence.get("schema")
    if schema == BENCHMARK_QUALITY_EVIDENCE_SCHEMA:
        _exact_fields(evidence, QUALITY_EVIDENCE_FIELDS, source)
    elif schema == BENCHMARK_QUALITY_EVIDENCE_SCHEMA_V2:
        _exact_fields(evidence, QUALITY_EVIDENCE_FIELDS_V2, source)
    else:
        raise ValidationError("%s has an unsupported benchmark quality evidence schema" % source)
    if not isinstance(evidence.get("suite"), str) or not evidence["suite"].strip():
        raise ValidationError("%s suite must be non-empty text" % source)
    task_id = evidence.get("task_id")
    if not isinstance(task_id, str) or not SAFE_ID.match(task_id):
        raise ValidationError("%s task_id must be a safe identifier" % source)
    for field in ["task_contract_sha256", "output_sha256"]:
        _sha256(evidence.get(field), "%s %s" % (source, field))
    for field in ["provider_evidence_sha256", "staged_evidence_sha256"]:
        if evidence.get(field) is not None:
            _sha256(evidence[field], "%s %s" % (source, field))
    if schema == BENCHMARK_QUALITY_EVIDENCE_SCHEMA_V2:
        for field in ["repair_evidence_sha256", "repair_attempt_reports_sha256"]:
            _sha256(evidence.get(field), "%s %s" % (source, field))
    reviewer = _strict_object(evidence.get("reviewer"), QUALITY_REVIEWER_FIELDS, "%s reviewer" % source)
    _validate_reviewer(reviewer, "%s reviewer" % source)
    criteria = _validate_criteria(evidence.get("criteria"), source)
    score = sum(item["score"] for item in criteria)
    max_score = sum(item["max_score"] for item in criteria)
    if evidence.get("score") != score or evidence.get("max_score") != max_score:
        raise ValidationError("%s quality score totals are inconsistent" % source)
    threshold = _bounded_int(evidence.get("pass_threshold"), "%s pass_threshold" % source, 1, MAX_QUALITY_SCORE)
    if threshold > max_score:
        raise ValidationError("%s pass_threshold exceeds max_score" % source)
    if evidence.get("passed") is not (score >= threshold):
        raise ValidationError("%s passed is inconsistent with the quality score" % source)
    generated = evidence.get("generated_at_utc")
    if not isinstance(generated, str) or not generated.endswith("Z"):
        raise ValidationError("%s generated_at_utc must be a UTC timestamp" % source)
    try:
        datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("%s generated_at_utc must be a UTC timestamp" % source)
    if result is not None:
        if result.get("id") != task_id:
            raise ValidationError("%s task_id does not match the benchmark result" % source)
        if evidence["output_sha256"] != _sha256_text(str(result.get("stdout", ""))):
            raise ValidationError("%s output hash does not match the benchmark result" % source)
        if evidence["provider_evidence_sha256"] != _optional_json_sha256(result.get("provider_evidence")):
            raise ValidationError("%s provider evidence hash does not match the benchmark result" % source)
        if evidence["staged_evidence_sha256"] != _optional_json_sha256(result.get("staged_evidence")):
            raise ValidationError("%s staged evidence hash does not match the benchmark result" % source)
        has_repair = isinstance(result.get("repair_evidence"), dict) or isinstance(
            result.get("repair_attempt_reports"), list
        )
        if has_repair and schema != BENCHMARK_QUALITY_EVIDENCE_SCHEMA_V2:
            raise ValidationError("%s repair benchmark results require quality evidence v2" % source)
        if schema == BENCHMARK_QUALITY_EVIDENCE_SCHEMA_V2:
            if evidence["repair_evidence_sha256"] != _optional_json_sha256(result.get("repair_evidence")):
                raise ValidationError("%s repair evidence hash does not match the benchmark result" % source)
            if evidence["repair_attempt_reports_sha256"] != _optional_json_sha256(
                result.get("repair_attempt_reports")
            ):
                raise ValidationError("%s repair attempt reports hash does not match the benchmark result" % source)


def quality_evidence_sha256(evidence: Dict) -> str:
    validate_benchmark_quality_evidence(evidence)
    return _sha256_json(evidence)


def load_benchmark_score_input(path: Path) -> Dict:
    value = _load_strict_json(path, "benchmark score input", MAX_SCORE_INPUT_BYTES)
    return validate_benchmark_score_input(value, source=str(path))


def load_benchmark_quality_evidence(path: Path) -> Dict:
    value = _load_strict_json(path, "benchmark quality evidence", MAX_QUALITY_EVIDENCE_BYTES)
    validate_benchmark_quality_evidence(value, source=str(path))
    return value


def _validate_reviewer(reviewer: Dict, source: str) -> None:
    if reviewer.get("kind") not in QUALITY_REVIEWER_KINDS:
        raise ValidationError("%s kind is invalid" % source)
    identity = reviewer.get("identity")
    if (
        not isinstance(identity, str)
        or not identity.strip()
        or len(identity) > 200
        or contains_secret_like(identity)
        or redact_text(identity) != identity
    ):
        raise ValidationError("%s identity must be bounded non-secret text" % source)
    if not isinstance(reviewer.get("independent"), bool):
        raise ValidationError("%s independent must be boolean" % source)


def _validate_criteria(value, source: str):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_QUALITY_CRITERIA:
        raise ValidationError("%s criteria must be a bounded non-empty array" % source)
    cleaned = []
    seen = set()
    total_max = 0
    for criterion in value:
        criterion = _strict_object(criterion, QUALITY_CRITERION_FIELDS, "%s criterion" % source)
        criterion_id = criterion.get("id")
        if not isinstance(criterion_id, str) or not SAFE_ID.match(criterion_id) or criterion_id in seen:
            raise ValidationError("%s criterion ids must be unique safe identifiers" % source)
        seen.add(criterion_id)
        max_score = _bounded_int(
            criterion.get("max_score"),
            "%s criterion %s max_score" % (source, criterion_id),
            1,
            MAX_QUALITY_SCORE,
        )
        score = _bounded_int(
            criterion.get("score"),
            "%s criterion %s score" % (source, criterion_id),
            0,
            max_score,
        )
        notes = criterion.get("notes")
        if not isinstance(notes, str) or len(notes) > MAX_QUALITY_NOTES_CHARS or contains_secret_like(notes):
            raise ValidationError("%s criterion %s notes must be bounded non-secret text" % (source, criterion_id))
        cleaned.append({"id": criterion_id, "score": score, "max_score": max_score, "notes": redact_text(notes)})
        total_max += max_score
        if total_max > MAX_QUALITY_SCORE:
            raise ValidationError("%s total max score exceeds the supported limit" % source)
    return cleaned


def _load_strict_json(path: Path, label: str, max_bytes: int):
    text = read_regular_text_file_no_follow(Path(path), label, max_bytes=max_bytes)
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("%s is not strict JSON: %s" % (path, exc))


def _optional_json_sha256(value) -> Optional[str]:
    return _sha256_json(value) if isinstance(value, (dict, list)) else None


def _sha256_json(value) -> str:
    try:
        serialized = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("quality evidence input is not canonical JSON: %s" % exc.__class__.__name__)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256(value, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValidationError("%s must be a sha256 hex string" % label)
    return value


def _json_size(value, maximum: int, label: str) -> None:
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("%s is not JSON-compatible: %s" % (label, exc.__class__.__name__))
    if len(encoded) > maximum:
        raise ValidationError("%s exceeds the supported size" % label)


def _strict_object(value, fields: set, label: str) -> Dict:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % label)
    _exact_fields(value, fields, label)
    return value


def _exact_fields(value: Dict, fields: set, label: str) -> None:
    if set(value) != fields:
        raise ValidationError("%s must contain exactly the supported fields" % label)


def _bounded_int(value, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise ValidationError("%s must be an integer from %d to %d" % (label, minimum, maximum))
    return value


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key %s" % key)
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant %s" % value)
