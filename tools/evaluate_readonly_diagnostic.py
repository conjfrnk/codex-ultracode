#!/usr/bin/env python3
# ruff: noqa: E402
import argparse
import copy
import hashlib
import json
import re
import sys
from functools import lru_cache
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conductor_runtime.errors import ValidationError
from conductor_runtime.security import (
    read_regular_text_file_no_follow,
    write_new_text_file_no_follow,
)
from conductor_runtime.staged_workspace import snapshot_workspace


READONLY_DIAGNOSTIC_SUITE = "conductor-readonly-diagnostic-canary"
READONLY_DIAGNOSTIC_EVALUATION_SCHEMA_V1 = "conductor.readonly_diagnostic_evaluation.v1"
READONLY_DIAGNOSTIC_EVALUATION_SCHEMA = "conductor.readonly_diagnostic_evaluation.v2"
READONLY_DIAGNOSTIC_EVALUATION_SCHEMAS = frozenset({
    READONLY_DIAGNOSTIC_EVALUATION_SCHEMA_V1,
    READONLY_DIAGNOSTIC_EVALUATION_SCHEMA,
})
EVALUATOR_IDENTITY_V1 = "readonly-diagnostic-hidden-root-cause-evaluator-v1"
EVALUATOR_IDENTITY = "readonly-diagnostic-hidden-root-cause-evaluator-v2"
MAX_ANSWER_BYTES = 1024 * 1024
MAX_EVALUATION_BYTES = 512 * 1024
MAX_FINDINGS = 8
SEVERITIES = {"critical", "high", "medium", "low"}
EVALUATION_FIELDS = {
    "schema",
    "evaluator",
    "task_id",
    "source_fixture_sha256",
    "answer_key_sha256",
    "answer_sha256",
    "provider_success",
    "parse_status",
    "finding_count",
    "matched_finding_ids",
    "unmatched_finding_count",
    "criteria",
    "score",
    "max_score",
    "passed",
    "evaluation_sha256",
}


TASK_SPECS_V1 = {
    "diagnose-retry-policy": {
        "source": "retry-policy/source",
        "findings": [
            {
                "id": "policy-normalization-stub",
                "file": "policy.py",
                "line_min": 20,
                "line_max": 25,
                "weight": 3,
                "keyword_groups": [
                    ("normalize", "canonical", "migration", "v1", "v2"),
                    ("dict(raw)", "returns raw", "stub", "unimplemented", "no validation", "copy"),
                ],
            },
            {
                "id": "service-canonical-attempt-boundary",
                "file": "service.py",
                "line_min": 4,
                "line_max": 8,
                "weight": 2,
                "keyword_groups": [
                    ("max_attempts", "retries"),
                    ("completed_attempts", "completed attempts", "attempt boundary"),
                ],
            },
            {
                "id": "service-completed-attempt-validation",
                "file": "service.py",
                "line_min": 4,
                "line_max": 8,
                "weight": 2,
                "keyword_groups": [
                    ("completed_attempts", "completed attempts"),
                    ("validate", "reject", "type", "non-negative", "integer", "boolean", "bool"),
                ],
            },
        ],
    },
    "diagnose-event-routing": {
        "source": "event-routing/source",
        "findings": [
            {
                "id": "canonical-name-contract",
                "file": "events/names.py",
                "line_min": 11,
                "line_max": 15,
                "weight": 3,
                "keyword_groups": [
                    ("casefold", "lower", "unicode"),
                    ("separator", "hyphen", "underscore", "punctuation", "regex", "dot"),
                ],
            },
            {
                "id": "router-bypasses-canonicalizer",
                "file": "events/router.py",
                "line_min": 11,
                "line_max": 17,
                "weight": 2,
                "keyword_groups": [
                    ("canonical_event_name", "canonicalizer", "canonical"),
                    ("register", "dispatch", "handler", "key"),
                ],
            },
            {
                "id": "subscription-bypasses-contract",
                "file": "events/subscriptions.py",
                "line_min": 7,
                "line_max": 13,
                "weight": 2,
                "keyword_groups": [
                    ("canonical_event_name", "canonicalizer", "canonical"),
                    ("dedupe", "container", "entry", "list", "subscription"),
                ],
            },
        ],
    },
    "diagnose-quota-ledger": {
        "source": "quota-ledger/source",
        "findings": [
            {
                "id": "reservation-parser-contract",
                "file": "quota/parser.py",
                "line_min": 12,
                "line_max": 16,
                "weight": 2,
                "keyword_groups": [
                    ("validate", "normaliz", "type", "missing", "unknown"),
                    ("tenant", "request_id", "request id", "units"),
                ],
            },
            {
                "id": "ledger-limit-validation",
                "file": "quota/ledger.py",
                "line_min": 14,
                "line_max": 18,
                "weight": 1,
                "keyword_groups": [
                    ("limit", "constructor", "__init__"),
                    ("validate", "positive", "canonical", "boolean", "bool"),
                ],
            },
            {
                "id": "ledger-idempotency-missing",
                "file": "quota/ledger.py",
                "line_min": 19,
                "line_max": 27,
                "weight": 2,
                "keyword_groups": [
                    ("idempot", "request_id", "request id"),
                    ("conflict", "repeat", "decision", "rejected", "same"),
                ],
            },
            {
                "id": "ledger-exact-limit-boundary",
                "file": "quota/ledger.py",
                "line_min": 21,
                "line_max": 25,
                "weight": 1,
                "keyword_groups": [
                    ("exact", "equal", "boundary", ">=", "at or below", "off-by-one"),
                    ("limit", "remaining", "reserve"),
                ],
            },
            {
                "id": "ledger-unknown-tenant-contract",
                "file": "quota/ledger.py",
                "line_min": 19,
                "line_max": 23,
                "weight": 1,
                "keyword_groups": [
                    ("unknown", "missing"),
                    ("tenant",),
                    ("raise", "valueerror", "return false"),
                ],
            },
        ],
    },
}


TASK_SPECS = copy.deepcopy(TASK_SPECS_V1)
TASK_SPECS["diagnose-event-routing"]["findings"][2] = {
    "id": "subscription-bypasses-contract",
    "file": "events/subscriptions.py",
    "line_min": 7,
    "line_max": 13,
    "weight": 1,
    "keyword_groups": [
        ("canonical_event_name", "canonicalizer", "canonical"),
        ("dedupe", "subscription", "key"),
    ],
}
TASK_SPECS["diagnose-event-routing"]["findings"].append(
    {
        "id": "subscription-container-validation",
        "file": "events/subscriptions.py",
        "line_min": 7,
        "line_max": 13,
        "weight": 1,
        "keyword_groups": [
            ("container", "list-only", "list"),
            ("tuple", "iterable", "validate", "reject", "typeerror", "accepted"),
        ],
    }
)

EVALUATION_CONTRACTS = {
    READONLY_DIAGNOSTIC_EVALUATION_SCHEMA_V1: {
        "identity": EVALUATOR_IDENTITY_V1,
        "task_specs": TASK_SPECS_V1,
    },
    READONLY_DIAGNOSTIC_EVALUATION_SCHEMA: {
        "identity": EVALUATOR_IDENTITY,
        "task_specs": TASK_SPECS,
    },
}


def evaluate_readonly_diagnostic(
    task_id: str,
    answer_text: str,
    source_fixture: Path,
    *,
    expected_source_sha256: str,
    provider_success: bool,
):
    spec = TASK_SPECS.get(task_id)
    if spec is None:
        raise ValidationError("read-only diagnostic task is unsupported")
    if not isinstance(answer_text, str) or len(answer_text.encode("utf-8")) > MAX_ANSWER_BYTES:
        raise ValidationError("read-only diagnostic answer is invalid or too large")
    if not isinstance(provider_success, bool):
        raise ValidationError("read-only diagnostic provider_success must be boolean")
    source_path = Path(source_fixture)
    observed_source_sha256 = snapshot_workspace(source_path).tracked_fingerprint_sha256
    if observed_source_sha256 != expected_source_sha256:
        raise ValidationError("read-only diagnostic source fixture does not match its preregistered hash")

    parsed, parse_status = _parse_answer(answer_text)
    findings = parsed["findings"] if parsed is not None else []
    matched = _match_findings(spec["findings"], findings)
    matched_ids = [item["spec"]["id"] for item in matched]
    matched_indexes = {item["finding_index"] for item in matched}
    unmatched_count = len(findings) - len(matched_indexes)
    root_score = sum(item["spec"]["weight"] for item in matched)
    expected_score = sum(item["weight"] for item in spec["findings"])
    if expected_score != 7:
        raise ValidationError("read-only diagnostic answer key weight is invalid")
    evidence_score = 1 if matched and all(_specific_evidence(item["finding"]) for item in matched) else 0
    precision_score = 1 if parsed is not None and unmatched_count == 0 and len(findings) <= len(spec["findings"]) else 0
    protocol_score = (
        1
        if parsed is not None and parsed["verification"] and parsed["residual_risk"].strip()
        else 0
    )
    criteria = [
        {"id": "hidden-root-cause-coverage", "score": root_score, "max_score": 7},
        {"id": "line-and-evidence-specificity", "score": evidence_score, "max_score": 1},
        {"id": "finding-precision", "score": precision_score, "max_score": 1},
        {"id": "protocol-completeness", "score": protocol_score, "max_score": 1},
    ]
    score = sum(item["score"] for item in criteria)
    answer_key_sha256 = _sha256_json(
        {
            "evaluator": EVALUATOR_IDENTITY,
            "task_id": task_id,
            "findings": spec["findings"],
        }
    )
    evaluation = {
        "schema": READONLY_DIAGNOSTIC_EVALUATION_SCHEMA,
        "evaluator": {"identity": EVALUATOR_IDENTITY, "independent": True},
        "task_id": task_id,
        "source_fixture_sha256": observed_source_sha256,
        "answer_key_sha256": answer_key_sha256,
        "answer_sha256": hashlib.sha256(answer_text.encode("utf-8")).hexdigest(),
        "provider_success": provider_success,
        "parse_status": parse_status,
        "finding_count": len(findings),
        "matched_finding_ids": sorted(matched_ids),
        "unmatched_finding_count": unmatched_count,
        "criteria": criteria,
        "score": score,
        "max_score": 10,
        "passed": bool(provider_success and score >= 7),
    }
    evaluation["evaluation_sha256"] = _sha256_json(evaluation)
    validate_readonly_diagnostic_evaluation(evaluation)
    return evaluation


def validate_readonly_diagnostic_evaluation(value, source="<memory>"):
    if not isinstance(value, dict) or set(value) != EVALUATION_FIELDS:
        raise ValidationError("%s has invalid read-only diagnostic evaluation fields" % source)
    contract = EVALUATION_CONTRACTS.get(value.get("schema"))
    if contract is None:
        raise ValidationError("%s read-only diagnostic evaluation schema is invalid" % source)
    if value.get("evaluator") != {"identity": contract["identity"], "independent": True}:
        raise ValidationError("%s read-only diagnostic evaluator identity is invalid" % source)
    task_specs = contract["task_specs"]
    if value.get("task_id") not in task_specs:
        raise ValidationError("%s read-only diagnostic task is invalid" % source)
    for field in ["source_fixture_sha256", "answer_key_sha256", "answer_sha256", "evaluation_sha256"]:
        _sha256(value.get(field), "%s %s" % (source, field))
    if not isinstance(value.get("provider_success"), bool):
        raise ValidationError("%s provider_success must be boolean" % source)
    if value.get("parse_status") not in {"valid", "valid-fenced", "invalid"}:
        raise ValidationError("%s parse_status is invalid" % source)
    finding_count = _bounded_int(value.get("finding_count"), 0, MAX_FINDINGS, "%s finding_count" % source)
    matched = value.get("matched_finding_ids")
    expected_ids = {item["id"] for item in task_specs[value["task_id"]]["findings"]}
    if (
        not isinstance(matched, list)
        or matched != sorted(set(matched))
        or not set(matched).issubset(expected_ids)
    ):
        raise ValidationError("%s matched_finding_ids are invalid" % source)
    unmatched = _bounded_int(
        value.get("unmatched_finding_count"), 0, MAX_FINDINGS, "%s unmatched_finding_count" % source
    )
    if len(matched) + unmatched > finding_count:
        raise ValidationError("%s finding accounting is inconsistent" % source)
    criteria = value.get("criteria")
    expected_criteria = [
        ("hidden-root-cause-coverage", 7),
        ("line-and-evidence-specificity", 1),
        ("finding-precision", 1),
        ("protocol-completeness", 1),
    ]
    if not isinstance(criteria, list) or len(criteria) != len(expected_criteria):
        raise ValidationError("%s criteria are invalid" % source)
    score = 0
    for item, (criterion_id, maximum) in zip(criteria, expected_criteria):
        if not isinstance(item, dict) or set(item) != {"id", "score", "max_score"}:
            raise ValidationError("%s criterion fields are invalid" % source)
        if item.get("id") != criterion_id or item.get("max_score") != maximum:
            raise ValidationError("%s criterion contract is invalid" % source)
        score += _bounded_int(item.get("score"), 0, maximum, "%s criterion score" % source)
    if value.get("score") != score or value.get("max_score") != 10:
        raise ValidationError("%s evaluation score is inconsistent" % source)
    if not isinstance(value.get("passed"), bool):
        raise ValidationError("%s passed must be boolean" % source)
    if value["passed"] != bool(value["provider_success"] and score >= 7):
        raise ValidationError("%s passed is inconsistent" % source)
    unsigned = dict(value)
    observed_hash = unsigned.pop("evaluation_sha256")
    if observed_hash != _sha256_json(unsigned):
        raise ValidationError("%s evaluation self-hash is invalid" % source)


def load_readonly_diagnostic_evaluation(path: Path):
    raw = read_regular_text_file_no_follow(
        path,
        "read-only diagnostic evaluation",
        MAX_EVALUATION_BYTES,
    )
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("%s is not valid evaluation JSON: %s" % (path, exc))
    validate_readonly_diagnostic_evaluation(value, source=str(path))
    return value


def write_readonly_diagnostic_evaluation(value, path: Path):
    validate_readonly_diagnostic_evaluation(value, source=str(path))
    write_new_text_file_no_follow(
        path,
        "read-only diagnostic evaluation",
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        sync=True,
    )
    return path


def _parse_answer(text):
    stripped = text.strip()
    candidates = [(stripped, "valid")]
    fenced = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", stripped, flags=re.IGNORECASE)
    if fenced is not None:
        candidates = [(fenced.group(1).strip(), "valid-fenced")]
    for candidate, status in candidates:
        try:
            value = json.loads(
                candidate,
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_constant,
            )
            return _validate_answer(value), status
        except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError, ValidationError):
            continue
    return None, "invalid"


def _validate_answer(value):
    if not isinstance(value, dict) or set(value) != {"findings", "verification", "residual_risk"}:
        raise ValidationError("answer fields are invalid")
    findings = value.get("findings")
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise ValidationError("answer findings are invalid")
    normalized = []
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {"file", "line", "severity", "summary", "evidence"}:
            raise ValidationError("answer finding fields are invalid")
        path = finding.get("file")
        pure = PurePosixPath(path) if isinstance(path, str) else None
        if (
            pure is None
            or not path
            or len(path) > 240
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
            or "\\" in path
        ):
            raise ValidationError("answer finding path is invalid")
        line = _bounded_int(finding.get("line"), 1, 100000, "answer finding line")
        if finding.get("severity") not in SEVERITIES:
            raise ValidationError("answer finding severity is invalid")
        summary = _bounded_text(finding.get("summary"), "answer finding summary", 400)
        evidence = _bounded_text(finding.get("evidence"), "answer finding evidence", 800)
        normalized.append({**finding, "file": pure.as_posix(), "line": line, "summary": summary, "evidence": evidence})
    verification = value.get("verification")
    if (
        not isinstance(verification, list)
        or len(verification) > 8
        or not all(isinstance(item, str) and 0 < len(item.strip()) <= 500 for item in verification)
    ):
        raise ValidationError("answer verification is invalid")
    residual = value.get("residual_risk")
    if not isinstance(residual, str) or len(residual) > 1000:
        raise ValidationError("answer residual_risk is invalid")
    return {"findings": normalized, "verification": verification, "residual_risk": residual}


def _match_findings(expected, findings):
    candidates = []
    for spec in expected:
        indexes = []
        for index, finding in enumerate(findings):
            if finding["file"] != spec["file"]:
                continue
            if not spec["line_min"] <= finding["line"] <= spec["line_max"]:
                continue
            text = (finding["summary"] + " " + finding["evidence"]).casefold()
            if not all(any(keyword.casefold() in text for keyword in group) for group in spec["keyword_groups"]):
                continue
            indexes.append(index)
        candidates.append(tuple(indexes))

    def rank(spec_offset, assignment):
        weight = sum(
            expected[spec_offset + index]["weight"]
            for index, finding_index in enumerate(assignment)
            if finding_index >= 0
        )
        count = sum(finding_index >= 0 for finding_index in assignment)
        stable_order = tuple(
            -(finding_index if finding_index >= 0 else MAX_FINDINGS + 1)
            for finding_index in assignment
        )
        return weight, count, stable_order

    @lru_cache(maxsize=None)
    def assign(spec_index, used_mask):
        if spec_index == len(expected):
            return ()
        options = [(-1,) + assign(spec_index + 1, used_mask)]
        for finding_index in candidates[spec_index]:
            bit = 1 << finding_index
            if used_mask & bit:
                continue
            options.append(
                (finding_index,) + assign(spec_index + 1, used_mask | bit)
            )
        return max(options, key=lambda option: rank(spec_index, option))

    assignment = assign(0, 0)
    return [
        {
            "spec": expected[index],
            "finding_index": finding_index,
            "finding": findings[finding_index],
        }
        for index, finding_index in enumerate(assignment)
        if finding_index >= 0
    ]


def _specific_evidence(finding):
    return len(finding["summary"].strip()) >= 12 and len(finding["evidence"].strip()) >= 12


def _bounded_text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError("%s is invalid" % label)
    return value.strip()


def _bounded_int(value, minimum, maximum, label):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError("%s is invalid" % label)
    return value


def _sha256(value, label):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValidationError("%s is invalid" % label)


def _sha256_json(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("invalid JSON constant: %s" % value)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score one read-only diagnostic answer against a hidden key.")
    parser.add_argument("task_id", choices=sorted(TASK_SPECS))
    parser.add_argument("answer", type=Path)
    parser.add_argument("source_fixture", type=Path)
    parser.add_argument("expected_source_sha256")
    parser.add_argument("output", type=Path)
    parser.add_argument("--provider-failed", action="store_true")
    args = parser.parse_args(argv)
    answer = read_regular_text_file_no_follow(args.answer, "read-only diagnostic answer", MAX_ANSWER_BYTES)
    evaluation = evaluate_readonly_diagnostic(
        args.task_id,
        answer,
        args.source_fixture,
        expected_source_sha256=args.expected_source_sha256,
        provider_success=not args.provider_failed,
    )
    write_readonly_diagnostic_evaluation(evaluation, args.output)
    print("Score: %d/10" % evaluation["score"])
    return 0 if evaluation["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
