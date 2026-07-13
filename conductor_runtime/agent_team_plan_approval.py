import hashlib
import json
import os
import re
import stat
import uuid
try:
    import fcntl
except ImportError:  # pragma: no cover - Unix-only locking when available.
    fcntl = None
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, Optional

from .agent_team import (
    MAX_AGENT_TEAM_GENERATION,
    MAX_AGENT_TEAM_PLAN_CRITERIA_CHARS,
    MAX_AGENT_TEAM_PLAN_REVISIONS,
    MAX_AGENT_TEAM_PLAN_REVIEW_TIMEOUT_SECONDS,
    MAX_AGENT_TEAM_SUMMARY_CHARS,
)
from .codex_config import MIN_CODEX_RUNTIME_TOKEN_CAP
from .errors import ValidationError
from .redaction import redact_text
from .security import (
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    require_no_path_escape,
)


AGENT_TEAM_PLAN_SCHEMA = "conductor.agent_team_plan.v1"
AGENT_TEAM_PLAN_REVIEW_SCHEMA = "conductor.agent_team_plan_review.v1"
AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V1 = "conductor.agent_team_plan_approval.v1"
AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V2 = "conductor.agent_team_plan_approval.v2"
AGENT_TEAM_PLAN_APPROVAL_SCHEMA = "conductor.agent_team_plan_approval.v3"
AGENT_TEAM_PLAN_APPROVAL_MODERN_SCHEMAS = {
    AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V2,
    AGENT_TEAM_PLAN_APPROVAL_SCHEMA,
}
AGENT_TEAM_PLAN_REVIEWERS = {"lead", "operator"}
AGENT_TEAM_PLAN_APPROVAL_STATUSES = {
    "pending",
    "planning",
    "plan-ready",
    "reviewing",
    "revision-required",
    "approved",
    "rejected",
    "failed",
}
MAX_AGENT_TEAM_PLAN_APPROVAL_BYTES = 512 * 1024
MAX_AGENT_TEAM_PLAN_OUTPUT_BYTES = 256 * 1024
MAX_AGENT_TEAM_PLAN_ITEMS = 16
MAX_AGENT_TEAM_PLAN_ITEM_CHARS = 2000
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SAFE_ERROR_CLASS = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,199}$")

AGENT_TEAM_PLAN_FIELDS = {
    "schema",
    "task_id",
    "revision",
    "plan",
    "risks",
    "verification",
}
AGENT_TEAM_PLAN_REVIEW_FIELDS = {
    "schema",
    "task_id",
    "revision",
    "decision",
    "feedback",
}
AGENT_TEAM_PLAN_APPROVAL_FIELDS_V1 = {
    "schema",
    "status",
    "step_id",
    "workflow_fingerprint",
    "generation",
    "task_id",
    "member_id",
    "lead_member_id",
    "criteria_sha256",
    "max_revisions",
    "plan_max_tokens",
    "review_max_tokens",
    "revision",
    "planner_session_id",
    "planner_session_id_sha256",
    "lead_session_id",
    "lead_session_id_sha256",
    "attempts",
    "created_at_utc",
    "updated_at_utc",
    "error_class",
}
AGENT_TEAM_PLAN_APPROVAL_FIELDS_V2 = AGENT_TEAM_PLAN_APPROVAL_FIELDS_V1 | {
    "reviewer",
    "operator_reply_timeout_seconds",
}
AGENT_TEAM_PLAN_APPROVAL_FIELDS = set(AGENT_TEAM_PLAN_APPROVAL_FIELDS_V2)
AGENT_TEAM_PLAN_APPROVAL_ATTEMPT_FIELDS_V1 = {
    "revision",
    "plan_output",
    "plan_output_sha256",
    "review_output",
    "review_output_sha256",
    "decision",
    "feedback_sha256",
    "planner_session_id_sha256",
    "lead_session_id_sha256",
    "plan_max_tokens",
    "review_max_tokens",
    "plan_started_at_utc",
    "plan_finished_at_utc",
    "review_started_at_utc",
    "review_finished_at_utc",
    "plan_input_tokens",
    "plan_output_tokens",
    "plan_total_tokens",
    "review_input_tokens",
    "review_output_tokens",
    "review_total_tokens",
}
AGENT_TEAM_PLAN_APPROVAL_ATTEMPT_FIELDS_V2 = AGENT_TEAM_PLAN_APPROVAL_ATTEMPT_FIELDS_V1 | {
    "reviewer",
    "operator_decision_id",
}
AGENT_TEAM_PLAN_APPROVAL_ATTEMPT_FIELDS = AGENT_TEAM_PLAN_APPROVAL_ATTEMPT_FIELDS_V2 | {
    "plan_session_mode",
    "plan_base_prompt_sha256",
    "plan_effective_prompt_sha256",
    "plan_lifecycle_context_receipt_sha256",
    "review_session_mode",
    "review_base_prompt_sha256",
    "review_effective_prompt_sha256",
    "review_lifecycle_context_receipt_sha256",
}


def parse_agent_team_plan(text: str, *, task_id: str, revision: int) -> Dict:
    value = _parse_json(text, "agent team plan", MAX_AGENT_TEAM_PLAN_OUTPUT_BYTES)
    validate_agent_team_plan(value, task_id=task_id, revision=revision)
    clean = dict(value)
    clean["plan"] = redact_text(value["plan"])
    clean["risks"] = [redact_text(item) for item in value["risks"]]
    clean["verification"] = [redact_text(item) for item in value["verification"]]
    return clean


def load_agent_team_plan(path: Path) -> Dict:
    reject_symlink_path(path, "agent team plan")
    text = read_regular_text_file_no_follow(
        path,
        "agent team plan",
        MAX_AGENT_TEAM_PLAN_OUTPUT_BYTES,
    )
    raw = _parse_json(text, "agent team plan", MAX_AGENT_TEAM_PLAN_OUTPUT_BYTES)
    return parse_agent_team_plan(
        text,
        task_id=raw.get("task_id"),
        revision=raw.get("revision"),
    )


def validate_agent_team_plan(value: Dict, *, task_id: str, revision: int) -> None:
    if not isinstance(value, dict) or set(value) != AGENT_TEAM_PLAN_FIELDS:
        raise ValidationError("agent team plan has invalid fields")
    if value.get("schema") != AGENT_TEAM_PLAN_SCHEMA:
        raise ValidationError("agent team plan schema is invalid")
    _validate_id(value.get("task_id"), "agent team plan task_id")
    _validate_int(
        value.get("revision"),
        1,
        MAX_AGENT_TEAM_PLAN_REVISIONS + 1,
        "plan revision",
    )
    if value.get("task_id") != task_id:
        raise ValidationError("agent team plan task binding changed")
    if value.get("revision") != revision:
        raise ValidationError("agent team plan revision binding changed")
    _validate_text(value.get("plan"), "agent team plan", MAX_AGENT_TEAM_SUMMARY_CHARS)
    for field in ("risks", "verification"):
        items = value.get(field)
        if (
            not isinstance(items, list)
            or len(items) > MAX_AGENT_TEAM_PLAN_ITEMS
            or not all(isinstance(item, str) and item.strip() for item in items)
        ):
            raise ValidationError("agent team plan %s are invalid" % field)
        for item in items:
            _validate_text(item, "agent team plan %s item" % field, MAX_AGENT_TEAM_PLAN_ITEM_CHARS)


def parse_agent_team_plan_review(text: str, *, task_id: str, revision: int) -> Dict:
    value = _parse_json(text, "agent team plan review", MAX_AGENT_TEAM_PLAN_OUTPUT_BYTES)
    validate_agent_team_plan_review(value, task_id=task_id, revision=revision)
    clean = dict(value)
    clean["feedback"] = redact_text(value["feedback"])
    return clean


def load_agent_team_plan_review(path: Path) -> Dict:
    reject_symlink_path(path, "agent team plan review")
    text = read_regular_text_file_no_follow(
        path,
        "agent team plan review",
        MAX_AGENT_TEAM_PLAN_OUTPUT_BYTES,
    )
    raw = _parse_json(text, "agent team plan review", MAX_AGENT_TEAM_PLAN_OUTPUT_BYTES)
    return parse_agent_team_plan_review(
        text,
        task_id=raw.get("task_id"),
        revision=raw.get("revision"),
    )


def validate_agent_team_plan_review(value: Dict, *, task_id: str, revision: int) -> None:
    if not isinstance(value, dict) or set(value) != AGENT_TEAM_PLAN_REVIEW_FIELDS:
        raise ValidationError("agent team plan review has invalid fields")
    if value.get("schema") != AGENT_TEAM_PLAN_REVIEW_SCHEMA:
        raise ValidationError("agent team plan review schema is invalid")
    _validate_id(value.get("task_id"), "agent team plan review task_id")
    _validate_int(
        value.get("revision"),
        1,
        MAX_AGENT_TEAM_PLAN_REVISIONS + 1,
        "plan review revision",
    )
    if value.get("task_id") != task_id:
        raise ValidationError("agent team plan review task binding changed")
    if value.get("revision") != revision:
        raise ValidationError("agent team plan review revision binding changed")
    if value.get("decision") not in {"approve", "reject"}:
        raise ValidationError("agent team plan review decision is invalid")
    _validate_text(
        value.get("feedback"),
        "agent team plan review feedback",
        MAX_AGENT_TEAM_PLAN_CRITERIA_CHARS,
    )


def initial_agent_team_plan_approval(
    *,
    step: Dict,
    workflow_fingerprint: str,
    generation: int,
    task_id: str,
    member_id: str,
    lead_member_id: str,
    planner_session_id: Optional[str],
    lead_session_id: Optional[str],
    timestamp: str,
) -> Dict:
    policy = step["plan_approval"]
    reviewer = policy.get("reviewer", "lead")
    value = {
        "schema": AGENT_TEAM_PLAN_APPROVAL_SCHEMA,
        "status": "pending",
        "step_id": step["id"],
        "workflow_fingerprint": workflow_fingerprint,
        "generation": generation,
        "task_id": task_id,
        "member_id": member_id,
        "lead_member_id": lead_member_id,
        "criteria_sha256": _sha256_text(policy["criteria"]),
        "max_revisions": policy["max_revisions"],
        "plan_max_tokens": policy["plan_max_tokens"],
        "review_max_tokens": policy.get("review_max_tokens"),
        "reviewer": reviewer,
        "operator_reply_timeout_seconds": policy.get("reply_timeout_seconds"),
        "revision": 0,
        "planner_session_id": planner_session_id,
        "planner_session_id_sha256": _optional_sha256_text(planner_session_id),
        "lead_session_id": lead_session_id if reviewer == "lead" else None,
        "lead_session_id_sha256": (
            _optional_sha256_text(lead_session_id) if reviewer == "lead" else None
        ),
        "attempts": [],
        "created_at_utc": timestamp,
        "updated_at_utc": timestamp,
        "error_class": None,
    }
    validate_agent_team_plan_approval(
        value,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=generation,
    )
    return value


def begin_agent_team_plan(state: Dict, *, timestamp: str) -> Dict:
    validate_agent_team_plan_approval(state)
    if state["status"] not in {"pending", "revision-required"}:
        raise ValidationError("agent team plan approval cannot begin planning from its current state")
    revision = state["revision"] + 1
    if revision > state["max_revisions"] + 1:
        raise ValidationError("agent team plan approval revision limit exceeded")
    value = dict(state)
    value.update(
        {
            "status": "planning",
            "revision": revision,
            "updated_at_utc": timestamp,
            "error_class": None,
        }
    )
    validate_agent_team_plan_approval(value)
    return value


def complete_agent_team_plan(
    state: Dict,
    *,
    plan_output: str,
    plan_output_sha256: str,
    planner_session_id: str,
    started_at_utc: str,
    finished_at_utc: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    total_tokens: Optional[int],
    session_mode: Optional[str] = None,
    base_prompt_sha256: Optional[str] = None,
    effective_prompt_sha256: Optional[str] = None,
    lifecycle_context_receipt_sha256: Optional[str] = None,
) -> Dict:
    validate_agent_team_plan_approval(state)
    if state["status"] != "planning":
        raise ValidationError("agent team plan approval is not planning")
    _validate_output(plan_output, plan_output_sha256, "agent team plan output")
    _validate_session(planner_session_id, "agent team planner session")
    expected = state["planner_session_id"]
    if expected is not None and expected != planner_session_id:
        raise ValidationError("agent team planner session changed")
    _validate_usage(input_tokens, output_tokens, total_tokens, "agent team plan")
    if state["schema"] == AGENT_TEAM_PLAN_APPROVAL_SCHEMA:
        _validate_provider_prompt_evidence(
            session_mode=session_mode,
            expected_resume=state["planner_session_id"] is not None,
            base_prompt_sha256=base_prompt_sha256,
            effective_prompt_sha256=effective_prompt_sha256,
            lifecycle_context_receipt_sha256=lifecycle_context_receipt_sha256,
            label="agent team plan",
        )
    attempt = {
        "revision": state["revision"],
        "plan_output": plan_output,
        "plan_output_sha256": plan_output_sha256,
        "review_output": None,
        "review_output_sha256": None,
        "decision": None,
        "feedback_sha256": None,
        "planner_session_id_sha256": _sha256_text(planner_session_id),
        "lead_session_id_sha256": None,
        "plan_max_tokens": state["plan_max_tokens"],
        "review_max_tokens": state["review_max_tokens"],
        "plan_started_at_utc": started_at_utc,
        "plan_finished_at_utc": finished_at_utc,
        "review_started_at_utc": None,
        "review_finished_at_utc": None,
        "plan_input_tokens": input_tokens,
        "plan_output_tokens": output_tokens,
        "plan_total_tokens": total_tokens,
        "review_input_tokens": None,
        "review_output_tokens": None,
        "review_total_tokens": None,
    }
    if state["schema"] in AGENT_TEAM_PLAN_APPROVAL_MODERN_SCHEMAS:
        attempt.update(
            {
                "reviewer": state["reviewer"],
                "operator_decision_id": None,
            }
        )
    if state["schema"] == AGENT_TEAM_PLAN_APPROVAL_SCHEMA:
        attempt.update(
            {
                "plan_session_mode": session_mode,
                "plan_base_prompt_sha256": base_prompt_sha256,
                "plan_effective_prompt_sha256": effective_prompt_sha256,
                "plan_lifecycle_context_receipt_sha256": (
                    lifecycle_context_receipt_sha256
                ),
                "review_session_mode": None,
                "review_base_prompt_sha256": None,
                "review_effective_prompt_sha256": None,
                "review_lifecycle_context_receipt_sha256": None,
            }
        )
    value = dict(state)
    value.update(
        {
            "status": "plan-ready",
            "planner_session_id": planner_session_id,
            "planner_session_id_sha256": _sha256_text(planner_session_id),
            "attempts": list(state["attempts"]) + [attempt],
            "updated_at_utc": finished_at_utc,
        }
    )
    validate_agent_team_plan_approval(value)
    return value


def begin_agent_team_plan_review(state: Dict, *, timestamp: str) -> Dict:
    validate_agent_team_plan_approval(state)
    if _approval_reviewer(state) != "lead":
        raise ValidationError("operator-reviewed plans do not start a provider review")
    if state["status"] != "plan-ready":
        raise ValidationError("agent team plan approval is not ready for review")
    value = dict(state)
    value.update({"status": "reviewing", "updated_at_utc": timestamp})
    validate_agent_team_plan_approval(value)
    return value


def complete_agent_team_plan_review(
    state: Dict,
    *,
    review_output: str,
    review_output_sha256: str,
    lead_session_id: str,
    decision: str,
    feedback: str,
    started_at_utc: str,
    finished_at_utc: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    total_tokens: Optional[int],
    session_mode: Optional[str] = None,
    base_prompt_sha256: Optional[str] = None,
    effective_prompt_sha256: Optional[str] = None,
    lifecycle_context_receipt_sha256: Optional[str] = None,
) -> Dict:
    return _complete_agent_team_plan_review(
        state,
        review_output=review_output,
        review_output_sha256=review_output_sha256,
        lead_session_id=lead_session_id,
        operator_decision_id=None,
        decision=decision,
        feedback=feedback,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        session_mode=session_mode,
        base_prompt_sha256=base_prompt_sha256,
        effective_prompt_sha256=effective_prompt_sha256,
        lifecycle_context_receipt_sha256=lifecycle_context_receipt_sha256,
        reviewer="lead",
    )


def complete_agent_team_operator_plan_review(
    state: Dict,
    *,
    review_output: str,
    review_output_sha256: str,
    operator_decision_id: str,
    decision: str,
    feedback: str,
    timestamp: str,
) -> Dict:
    return _complete_agent_team_plan_review(
        state,
        review_output=review_output,
        review_output_sha256=review_output_sha256,
        lead_session_id=None,
        operator_decision_id=operator_decision_id,
        decision=decision,
        feedback=feedback,
        started_at_utc=timestamp,
        finished_at_utc=timestamp,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        session_mode=None,
        base_prompt_sha256=None,
        effective_prompt_sha256=None,
        lifecycle_context_receipt_sha256=None,
        reviewer="operator",
    )


def _complete_agent_team_plan_review(
    state: Dict,
    *,
    review_output: str,
    review_output_sha256: str,
    lead_session_id: Optional[str],
    operator_decision_id: Optional[str],
    decision: str,
    feedback: str,
    started_at_utc: str,
    finished_at_utc: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    total_tokens: Optional[int],
    session_mode: Optional[str],
    base_prompt_sha256: Optional[str],
    effective_prompt_sha256: Optional[str],
    lifecycle_context_receipt_sha256: Optional[str],
    reviewer: str,
) -> Dict:
    validate_agent_team_plan_approval(state)
    if _approval_reviewer(state) != reviewer:
        raise ValidationError("agent team plan reviewer changed")
    expected_status = "reviewing" if reviewer == "lead" else "plan-ready"
    if state["status"] != expected_status:
        raise ValidationError("agent team plan approval is not ready for %s review" % reviewer)
    if decision not in {"approve", "reject"}:
        raise ValidationError("agent team plan decision is invalid")
    _validate_text(feedback, "agent team plan feedback", MAX_AGENT_TEAM_PLAN_CRITERIA_CHARS)
    _validate_output(review_output, review_output_sha256, "agent team plan review output")
    if reviewer == "lead":
        _validate_session(lead_session_id, "agent team lead session")
        expected = state["lead_session_id"]
        if expected is not None and expected != lead_session_id:
            raise ValidationError("agent team lead session changed")
        if operator_decision_id is not None:
            raise ValidationError("lead plan review cannot bind an operator decision")
        _validate_usage(input_tokens, output_tokens, total_tokens, "agent team plan review")
        if state["schema"] == AGENT_TEAM_PLAN_APPROVAL_SCHEMA:
            _validate_provider_prompt_evidence(
                session_mode=session_mode,
                expected_resume=state["lead_session_id"] is not None,
                base_prompt_sha256=base_prompt_sha256,
                effective_prompt_sha256=effective_prompt_sha256,
                lifecycle_context_receipt_sha256=(
                    lifecycle_context_receipt_sha256
                ),
                label="agent team plan review",
            )
    else:
        if state["schema"] not in AGENT_TEAM_PLAN_APPROVAL_MODERN_SCHEMAS:
            raise ValidationError("legacy plan approval cannot accept operator review")
        _validate_id(operator_decision_id, "agent team operator plan decision")
        if lead_session_id is not None or any(
            value is not None
            for value in (
                input_tokens,
                output_tokens,
                total_tokens,
                session_mode,
                base_prompt_sha256,
                effective_prompt_sha256,
                lifecycle_context_receipt_sha256,
            )
        ):
            raise ValidationError("operator plan review cannot contain provider evidence")
    attempts = [dict(attempt) for attempt in state["attempts"]]
    if not attempts or attempts[-1]["review_output"] is not None:
        raise ValidationError("agent team plan approval has no pending review attempt")
    attempts[-1].update(
        {
            "review_output": review_output,
            "review_output_sha256": review_output_sha256,
            "decision": decision,
            "feedback_sha256": _sha256_text(feedback),
            "lead_session_id_sha256": (
                _sha256_text(lead_session_id) if reviewer == "lead" else None
            ),
            "review_started_at_utc": started_at_utc,
            "review_finished_at_utc": finished_at_utc,
            "review_input_tokens": input_tokens,
            "review_output_tokens": output_tokens,
            "review_total_tokens": total_tokens,
        }
    )
    if state["schema"] in AGENT_TEAM_PLAN_APPROVAL_MODERN_SCHEMAS:
        attempts[-1].update(
            {
                "reviewer": reviewer,
                "operator_decision_id": operator_decision_id,
            }
        )
    if state["schema"] == AGENT_TEAM_PLAN_APPROVAL_SCHEMA:
        attempts[-1].update(
            {
                "review_session_mode": session_mode,
                "review_base_prompt_sha256": base_prompt_sha256,
                "review_effective_prompt_sha256": effective_prompt_sha256,
                "review_lifecycle_context_receipt_sha256": (
                    lifecycle_context_receipt_sha256
                ),
            }
        )
    status = "approved"
    if decision == "reject":
        status = (
            "revision-required"
            if state["revision"] <= state["max_revisions"]
            else "rejected"
        )
    value = dict(state)
    value.update({"status": status, "attempts": attempts, "updated_at_utc": finished_at_utc})
    if reviewer == "lead":
        value.update(
            {
                "lead_session_id": lead_session_id,
                "lead_session_id_sha256": _sha256_text(lead_session_id),
            }
        )
    validate_agent_team_plan_approval(value)
    return value


def fail_agent_team_plan_approval(state: Dict, *, error_class: str, timestamp: str) -> Dict:
    validate_agent_team_plan_approval(state)
    if not isinstance(error_class, str) or not SAFE_ERROR_CLASS.fullmatch(error_class):
        raise ValidationError("agent team plan approval error class is invalid")
    value = dict(state)
    value.update(
        {
            "status": "failed",
            "updated_at_utc": timestamp,
            "error_class": error_class,
        }
    )
    validate_agent_team_plan_approval(value)
    return value


def validate_agent_team_plan_approval(
    state: Dict,
    *,
    step: Optional[Dict] = None,
    workflow_fingerprint: Optional[str] = None,
    generation: Optional[int] = None,
) -> None:
    if not isinstance(state, dict):
        raise ValidationError("agent team plan approval has invalid fields")
    schema = state.get("schema")
    if schema == AGENT_TEAM_PLAN_APPROVAL_SCHEMA:
        expected_fields = AGENT_TEAM_PLAN_APPROVAL_FIELDS
    elif schema == AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V2:
        expected_fields = AGENT_TEAM_PLAN_APPROVAL_FIELDS_V2
    elif schema == AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V1:
        expected_fields = AGENT_TEAM_PLAN_APPROVAL_FIELDS_V1
    else:
        raise ValidationError("agent team plan approval schema is invalid")
    if set(state) != expected_fields:
        raise ValidationError("agent team plan approval has invalid fields")
    reviewer = _approval_reviewer(state)
    if schema in AGENT_TEAM_PLAN_APPROVAL_MODERN_SCHEMAS:
        if state.get("reviewer") not in AGENT_TEAM_PLAN_REVIEWERS:
            raise ValidationError("agent team plan approval reviewer is invalid")
        timeout = state.get("operator_reply_timeout_seconds")
        if reviewer == "operator":
            _validate_int(
                timeout,
                1,
                MAX_AGENT_TEAM_PLAN_REVIEW_TIMEOUT_SECONDS,
                "operator reply timeout",
            )
        elif timeout is not None:
            raise ValidationError("lead plan approval cannot bind an operator timeout")
    status = state.get("status")
    if status not in AGENT_TEAM_PLAN_APPROVAL_STATUSES:
        raise ValidationError("agent team plan approval status is invalid")
    for field in ("step_id", "task_id", "member_id", "lead_member_id"):
        _validate_id(state.get(field), "agent team plan approval %s" % field)
    _validate_sha256(state.get("workflow_fingerprint"), "agent team plan approval workflow")
    _validate_sha256(state.get("criteria_sha256"), "agent team plan approval criteria")
    _validate_int(state.get("generation"), 0, MAX_AGENT_TEAM_GENERATION, "generation")
    _validate_int(
        state.get("max_revisions"),
        0,
        MAX_AGENT_TEAM_PLAN_REVISIONS,
        "max_revisions",
    )
    _validate_int(
        state.get("revision"),
        0,
        state["max_revisions"] + 1,
        "revision",
    )
    _validate_int(
        state.get("plan_max_tokens"),
        MIN_CODEX_RUNTIME_TOKEN_CAP,
        10**12,
        "plan_max_tokens",
    )
    if reviewer == "lead":
        _validate_int(
            state.get("review_max_tokens"),
            MIN_CODEX_RUNTIME_TOKEN_CAP,
            10**12,
            "review_max_tokens",
        )
    elif state.get("review_max_tokens") is not None:
        raise ValidationError("operator plan approval cannot bind a review token cap")
    for raw_field, hash_field in (
        ("planner_session_id", "planner_session_id_sha256"),
        ("lead_session_id", "lead_session_id_sha256"),
    ):
        raw = state.get(raw_field)
        digest = state.get(hash_field)
        if raw is None:
            if digest is not None:
                raise ValidationError("agent team plan approval session hash has no session")
        else:
            _validate_session(raw, "agent team plan approval session")
            if digest != _sha256_text(raw):
                raise ValidationError("agent team plan approval session hash changed")
    if reviewer == "operator" and (
        state["lead_session_id"] is not None or state["lead_session_id_sha256"] is not None
    ):
        raise ValidationError("operator plan approval cannot bind a lead session")
    attempts = state.get("attempts")
    if not isinstance(attempts, list) or len(attempts) > state["max_revisions"] + 1:
        raise ValidationError("agent team plan approval attempts are invalid")
    for index, attempt in enumerate(attempts, start=1):
        _validate_attempt(attempt, state, index)
    if attempts and attempts[-1]["planner_session_id_sha256"] != state["planner_session_id_sha256"]:
        raise ValidationError("agent team plan approval planner session history changed")
    completed_reviews = [attempt for attempt in attempts if attempt["review_output"] is not None]
    if reviewer == "lead" and completed_reviews and (
        completed_reviews[-1]["lead_session_id_sha256"] != state["lead_session_id_sha256"]
    ):
        raise ValidationError("agent team plan approval lead session history changed")
    if reviewer == "operator" and any(
        attempt["lead_session_id_sha256"] is not None for attempt in completed_reviews
    ):
        raise ValidationError("operator plan approval contains lead review evidence")
    completed_attempts = sum(attempt["review_output"] is not None for attempt in attempts)
    if status == "pending" and (state["revision"] != 0 or attempts):
        raise ValidationError("pending agent team plan approval has attempt state")
    if status == "planning" and (
        state["revision"] != completed_attempts + 1 or len(attempts) != completed_attempts
    ):
        raise ValidationError("planning agent team plan approval history is inconsistent")
    if status in {"plan-ready", "reviewing"} and (
        len(attempts) != state["revision"]
        or completed_attempts != state["revision"] - 1
        or attempts[-1]["review_output"] is not None
    ):
        raise ValidationError("reviewing agent team plan approval history is inconsistent")
    if reviewer == "operator" and status == "reviewing":
        raise ValidationError("operator plan approval cannot have an in-flight provider review")
    if status in {"revision-required", "approved", "rejected"} and (
        len(attempts) != state["revision"] or completed_attempts != state["revision"]
    ):
        raise ValidationError("terminal agent team plan approval history is inconsistent")
    if status == "revision-required" and attempts[-1]["decision"] != "reject":
        raise ValidationError("agent team plan revision lacks a rejection")
    if status == "approved" and attempts[-1]["decision"] != "approve":
        raise ValidationError("approved agent team plan lacks approval evidence")
    if status == "rejected" and (
        attempts[-1]["decision"] != "reject"
        or state["revision"] != state["max_revisions"] + 1
    ):
        raise ValidationError("rejected agent team plan lacks exhausted evidence")
    if status == "failed":
        if not isinstance(state.get("error_class"), str) or not SAFE_ERROR_CLASS.fullmatch(
            state["error_class"]
        ):
            raise ValidationError("failed agent team plan approval lacks an error class")
    elif state.get("error_class") is not None:
        raise ValidationError("agent team plan approval error class is unexpected")
    for field in ("created_at_utc", "updated_at_utc"):
        _validate_timestamp(state.get(field), "agent team plan approval %s" % field)
    if workflow_fingerprint is not None and state["workflow_fingerprint"] != workflow_fingerprint:
        raise ValidationError("agent team plan approval workflow changed")
    if generation is not None and state["generation"] != generation:
        raise ValidationError("agent team plan approval generation changed")
    if step is not None:
        policy = step.get("plan_approval")
        if not isinstance(policy, dict) or state["task_id"] not in policy.get("task_ids", []):
            raise ValidationError("agent team plan approval policy binding changed")
        task = next((item for item in step["tasks"] if item["id"] == state["task_id"]), None)
        lead = next(member for member in step["members"] if member["lead"])
        policy_reviewer = policy.get("reviewer", "lead")
        if (
            state["step_id"] != step["id"]
            or task is None
            or state["member_id"] != task["assignee"]
            or state["lead_member_id"] != lead["id"]
            or state["criteria_sha256"] != _sha256_text(policy["criteria"])
            or state["max_revisions"] != policy["max_revisions"]
            or state["plan_max_tokens"] != policy["plan_max_tokens"]
            or state["review_max_tokens"] != policy.get("review_max_tokens")
            or reviewer != policy_reviewer
            or (
                schema in AGENT_TEAM_PLAN_APPROVAL_MODERN_SCHEMAS
                and state["operator_reply_timeout_seconds"]
                != policy.get("reply_timeout_seconds")
            )
            or (schema == AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V1 and policy_reviewer != "lead")
        ):
            raise ValidationError("agent team plan approval configuration changed")


def agent_team_plan_approval_path(run, step: Dict, generation: int, task_id: str) -> Path:
    relative = "%s/plan-approvals/generation-%09d/%s/approval.json" % (
        step["capture_dir"],
        generation,
        task_id,
    )
    require_no_path_escape(relative)
    path = run.resolve_artifact_path(relative)
    reject_symlink_path(path, "agent team plan approval")
    return path


def agent_team_plan_output_relative(step: Dict, generation: int, task_id: str, revision: int) -> str:
    relative = "%s/plan-approvals/generation-%09d/%s/revision-%02d.plan.json" % (
        step["capture_dir"],
        generation,
        task_id,
        revision,
    )
    require_no_path_escape(relative)
    return relative


def agent_team_plan_review_output_relative(
    step: Dict,
    generation: int,
    task_id: str,
    revision: int,
) -> str:
    relative = "%s/plan-approvals/generation-%09d/%s/revision-%02d.review.json" % (
        step["capture_dir"],
        generation,
        task_id,
        revision,
    )
    require_no_path_escape(relative)
    return relative


def write_agent_team_plan_review(path: Path, value: Dict) -> str:
    validate_agent_team_plan_review(
        value,
        task_id=value.get("task_id"),
        revision=value.get("revision"),
    )
    reject_symlink_path(path, "agent team plan review")
    parent_fd = ensure_dir_no_follow(path.parent, "agent team plan review parent")
    os.close(parent_fd)
    text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if len(text.encode("utf-8")) > MAX_AGENT_TEAM_PLAN_OUTPUT_BYTES:
        raise ValidationError(
            "agent team plan review exceeds %d bytes" % MAX_AGENT_TEAM_PLAN_OUTPUT_BYTES
        )
    replace_text_file_no_follow(
        path,
        "agent team plan review",
        text,
        ".agent-team-plan-review-",
    )
    return _sha256_text(text)


@contextmanager
def agent_team_plan_approval_lock(path: Path) -> Iterator[None]:
    parent_fd = ensure_dir_no_follow(path.parent, "agent team plan approval parent")
    lock_name = ".approval.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = None
    try:
        lock_fd = os.open(lock_name, flags, 0o600, dir_fd=parent_fd)
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValidationError(
                "agent team plan approval lock must be a single-link regular file"
            )
        if fcntl is None:
            raise ValidationError("agent team plan approval locking is unavailable")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise ValidationError(
            "failed to lock agent team plan approval: %s" % exc.__class__.__name__
        )
    finally:
        if lock_fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(parent_fd)


def write_agent_team_plan_approval(run, step: Dict, state: Dict) -> Path:
    validate_agent_team_plan_approval(state, step=step)
    path = agent_team_plan_approval_path(run, step, state["generation"], state["task_id"])
    parent_fd = ensure_dir_no_follow(path.parent, "agent team plan approval parent")
    os.close(parent_fd)
    replace_text_file_no_follow(
        path,
        "agent team plan approval",
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        ".agent-team-plan-approval-",
    )
    return path


def load_agent_team_plan_approval(
    path: Path,
    *,
    step: Optional[Dict] = None,
    workflow_fingerprint: Optional[str] = None,
    generation: Optional[int] = None,
) -> Dict:
    reject_symlink_path(path, "agent team plan approval")
    text = read_regular_text_file_no_follow(
        path,
        "agent team plan approval",
        MAX_AGENT_TEAM_PLAN_APPROVAL_BYTES,
    )
    value = _parse_json(text, "agent team plan approval", MAX_AGENT_TEAM_PLAN_APPROVAL_BYTES)
    validate_agent_team_plan_approval(
        value,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=generation,
    )
    return value


def verify_agent_team_plan_approval_outputs(state: Dict, artifacts_dir: Path) -> None:
    validate_agent_team_plan_approval(state)
    for attempt in state["attempts"]:
        plan_path = artifacts_dir / attempt["plan_output"]
        plan_text = _verify_output_hash(
            plan_path,
            attempt["plan_output_sha256"],
            "agent team plan output",
        )
        plan = parse_agent_team_plan(
            plan_text,
            task_id=state["task_id"],
            revision=attempt["revision"],
        )
        if plan["revision"] != attempt["revision"]:
            raise ValidationError("agent team plan output revision changed")
        if attempt["review_output"] is None:
            continue
        review_path = artifacts_dir / attempt["review_output"]
        review_text = _verify_output_hash(
            review_path,
            attempt["review_output_sha256"],
            "agent team plan review output",
        )
        review = parse_agent_team_plan_review(
            review_text,
            task_id=state["task_id"],
            revision=attempt["revision"],
        )
        if (
            review["decision"] != attempt["decision"]
            or _sha256_text(review["feedback"]) != attempt["feedback_sha256"]
        ):
            raise ValidationError("agent team plan review output binding changed")


def approved_agent_team_plan(state: Dict, artifacts_dir: Path) -> Dict:
    validate_agent_team_plan_approval(state)
    if state["status"] != "approved":
        raise ValidationError("agent team plan is not approved")
    verify_agent_team_plan_approval_outputs(state, artifacts_dir)
    attempt = state["attempts"][-1]
    path = artifacts_dir / attempt["plan_output"]
    return parse_agent_team_plan(
        _verify_output_hash(
            path,
            attempt["plan_output_sha256"],
            "agent team approved plan output",
        ),
        task_id=state["task_id"],
        revision=attempt["revision"],
    )


def agent_team_plan_approval_summary(state: Dict) -> Dict:
    validate_agent_team_plan_approval(state)
    latest = state["attempts"][-1] if state["attempts"] else None
    reviewer = _approval_reviewer(state)
    operator_decision_id = (
        latest.get("operator_decision_id")
        if latest is not None
        and state["schema"] in AGENT_TEAM_PLAN_APPROVAL_MODERN_SCHEMAS
        else None
    )
    completed_reviews = sum(attempt["review_output"] is not None for attempt in state["attempts"])
    return {
        "schema": state["schema"],
        "status": state["status"],
        "step_id": state["step_id"],
        "generation": state["generation"],
        "task_id": state["task_id"],
        "member_id": state["member_id"],
        "lead_member_id": state["lead_member_id"],
        "reviewer": reviewer,
        "operator_reply_timeout_seconds": state.get("operator_reply_timeout_seconds"),
        "criteria_sha256": state["criteria_sha256"],
        "revision": state["revision"],
        "max_revisions": state["max_revisions"],
        "attempt_count": len(state["attempts"]),
        "rejection_count": sum(
            attempt["decision"] == "reject" for attempt in state["attempts"]
        ),
        "plan_max_tokens": state["plan_max_tokens"],
        "review_max_tokens": state["review_max_tokens"],
        "provider_call_count": len(state["attempts"]) + (
            completed_reviews if reviewer == "lead" else 0
        ),
        "operator_review_count": completed_reviews if reviewer == "operator" else 0,
        "planner_session_id_sha256": state["planner_session_id_sha256"],
        "lead_session_id_sha256": state["lead_session_id_sha256"],
        "latest_plan_output_sha256": latest["plan_output_sha256"] if latest else None,
        "latest_review_output_sha256": latest["review_output_sha256"] if latest else None,
        "latest_plan_effective_prompt_sha256": (
            latest.get("plan_effective_prompt_sha256") if latest else None
        ),
        "latest_plan_lifecycle_context_receipt_sha256": (
            latest.get("plan_lifecycle_context_receipt_sha256") if latest else None
        ),
        "latest_review_effective_prompt_sha256": (
            latest.get("review_effective_prompt_sha256") if latest else None
        ),
        "latest_review_lifecycle_context_receipt_sha256": (
            latest.get("review_lifecycle_context_receipt_sha256") if latest else None
        ),
        "latest_decision": latest["decision"] if latest else None,
        "latest_operator_decision_id_sha256": (
            _sha256_text(operator_decision_id) if operator_decision_id else None
        ),
        "error_class": state["error_class"],
        "updated_at_utc": state["updated_at_utc"],
    }


def _approval_reviewer(state: Dict) -> str:
    if state.get("schema") == AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V1:
        return "lead"
    return state.get("reviewer")


def _validate_attempt(attempt: Dict, state: Dict, index: int) -> None:
    expected_fields = (
        AGENT_TEAM_PLAN_APPROVAL_ATTEMPT_FIELDS
        if state["schema"] == AGENT_TEAM_PLAN_APPROVAL_SCHEMA
        else AGENT_TEAM_PLAN_APPROVAL_ATTEMPT_FIELDS_V2
        if state["schema"] == AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V2
        else AGENT_TEAM_PLAN_APPROVAL_ATTEMPT_FIELDS_V1
    )
    if not isinstance(attempt, dict) or set(attempt) != expected_fields:
        raise ValidationError("agent team plan approval attempt has invalid fields")
    reviewer = _approval_reviewer(state)
    if state["schema"] in AGENT_TEAM_PLAN_APPROVAL_MODERN_SCHEMAS:
        if attempt.get("reviewer") != reviewer:
            raise ValidationError("agent team plan approval attempt reviewer changed")
    if attempt.get("revision") != index:
        raise ValidationError("agent team plan approval attempt sequence is invalid")
    _validate_output(
        attempt.get("plan_output"),
        attempt.get("plan_output_sha256"),
        "agent team plan approval plan output",
    )
    _validate_sha256(
        attempt.get("planner_session_id_sha256"),
        "agent team plan approval planner session",
    )
    if attempt.get("plan_max_tokens") != state["plan_max_tokens"]:
        raise ValidationError("agent team plan approval plan token cap changed")
    if attempt.get("review_max_tokens") != state["review_max_tokens"]:
        raise ValidationError("agent team plan approval review token cap changed")
    for field in ("plan_started_at_utc", "plan_finished_at_utc"):
        _validate_timestamp(attempt.get(field), "agent team plan approval %s" % field)
    _validate_usage(
        attempt.get("plan_input_tokens"),
        attempt.get("plan_output_tokens"),
        attempt.get("plan_total_tokens"),
        "agent team plan approval plan",
    )
    if state["schema"] == AGENT_TEAM_PLAN_APPROVAL_SCHEMA:
        _validate_stored_prompt_evidence(
            session_mode=attempt.get("plan_session_mode"),
            base_prompt_sha256=attempt.get("plan_base_prompt_sha256"),
            effective_prompt_sha256=attempt.get("plan_effective_prompt_sha256"),
            lifecycle_context_receipt_sha256=attempt.get(
                "plan_lifecycle_context_receipt_sha256"
            ),
            label="agent team plan approval plan",
        )
    if attempt.get("review_output") is None:
        for field in (
            "review_output_sha256",
            "decision",
            "feedback_sha256",
            "lead_session_id_sha256",
            "review_started_at_utc",
            "review_finished_at_utc",
            "review_input_tokens",
            "review_output_tokens",
            "review_total_tokens",
        ):
            if attempt.get(field) is not None:
                raise ValidationError("agent team plan approval incomplete review has evidence")
        if state["schema"] == AGENT_TEAM_PLAN_APPROVAL_SCHEMA and any(
            attempt.get(field) is not None
            for field in (
                "review_session_mode",
                "review_base_prompt_sha256",
                "review_effective_prompt_sha256",
                "review_lifecycle_context_receipt_sha256",
            )
        ):
            raise ValidationError("incomplete plan review has prompt evidence")
        if (
            state["schema"] in AGENT_TEAM_PLAN_APPROVAL_MODERN_SCHEMAS
            and attempt.get("operator_decision_id") is not None
        ):
            raise ValidationError("incomplete plan review has an operator decision")
        return
    _validate_output(
        attempt["review_output"],
        attempt.get("review_output_sha256"),
        "agent team plan approval review output",
    )
    if attempt.get("decision") not in {"approve", "reject"}:
        raise ValidationError("agent team plan approval attempt decision is invalid")
    _validate_sha256(
        attempt.get("feedback_sha256"),
        "agent team plan approval feedback_sha256",
    )
    for field in ("review_started_at_utc", "review_finished_at_utc"):
        _validate_timestamp(attempt.get(field), "agent team plan approval %s" % field)
    if reviewer == "lead":
        _validate_sha256(
            attempt.get("lead_session_id_sha256"),
            "agent team plan approval lead_session_id_sha256",
        )
        if (
            state["schema"] in AGENT_TEAM_PLAN_APPROVAL_MODERN_SCHEMAS
            and attempt.get("operator_decision_id") is not None
        ):
            raise ValidationError("lead plan review contains an operator decision")
        _validate_usage(
            attempt.get("review_input_tokens"),
            attempt.get("review_output_tokens"),
            attempt.get("review_total_tokens"),
            "agent team plan approval review",
        )
        if state["schema"] == AGENT_TEAM_PLAN_APPROVAL_SCHEMA:
            _validate_stored_prompt_evidence(
                session_mode=attempt.get("review_session_mode"),
                base_prompt_sha256=attempt.get("review_base_prompt_sha256"),
                effective_prompt_sha256=attempt.get("review_effective_prompt_sha256"),
                lifecycle_context_receipt_sha256=attempt.get(
                    "review_lifecycle_context_receipt_sha256"
                ),
                label="agent team plan approval review",
            )
    else:
        if attempt.get("lead_session_id_sha256") is not None:
            raise ValidationError("operator plan review contains a lead session")
        _validate_id(
            attempt.get("operator_decision_id"),
            "agent team operator plan decision",
        )
        if any(
            attempt.get(field) is not None
            for field in (
                "review_input_tokens",
                "review_output_tokens",
                "review_total_tokens",
            )
        ):
            raise ValidationError("operator plan review contains provider usage")
        if state["schema"] == AGENT_TEAM_PLAN_APPROVAL_SCHEMA and any(
            attempt.get(field) is not None
            for field in (
                "review_session_mode",
                "review_base_prompt_sha256",
                "review_effective_prompt_sha256",
                "review_lifecycle_context_receipt_sha256",
            )
        ):
            raise ValidationError("operator plan review contains provider prompt evidence")


def _parse_json(text: str, label: str, limit: int) -> Dict:
    if not isinstance(text, str):
        raise ValidationError("%s must be text" % label)
    if len(text.encode("utf-8")) > limit:
        raise ValidationError("%s exceeds %d bytes" % (label, limit))
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3 and lines[0].strip() in {"```", "```json"} and lines[-1].strip() == "```":
            value = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("%s is invalid JSON: %s" % (label, exc.__class__.__name__))
    if not isinstance(parsed, dict):
        raise ValidationError("%s must be an object" % label)
    return parsed


def _verify_output_hash(path: Path, expected: str, label: str) -> str:
    reject_symlink_path(path, label)
    raw = read_regular_text_file_no_follow(path, label, MAX_AGENT_TEAM_PLAN_OUTPUT_BYTES)
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != expected:
        raise ValidationError("%s hash changed" % label)
    return raw


def _validate_output(relative: str, digest: str, label: str) -> None:
    if not isinstance(relative, str) or not relative:
        raise ValidationError("%s path is invalid" % label)
    require_no_path_escape(relative)
    _validate_sha256(digest, "%s hash" % label)


def _validate_usage(input_tokens, output_tokens, total_tokens, label: str) -> None:
    for value in (input_tokens, output_tokens, total_tokens):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 10**12
        ):
            raise ValidationError("%s usage is invalid" % label)
    if input_tokens is not None and output_tokens is not None:
        if total_tokens != input_tokens + output_tokens:
            raise ValidationError("%s usage is inconsistent" % label)


def _validate_provider_prompt_evidence(
    *,
    session_mode: Optional[str],
    expected_resume: bool,
    base_prompt_sha256: Optional[str],
    effective_prompt_sha256: Optional[str],
    lifecycle_context_receipt_sha256: Optional[str],
    label: str,
) -> None:
    expected_mode = "resume" if expected_resume else "new"
    if session_mode != expected_mode:
        raise ValidationError("%s session mode changed" % label)
    _validate_stored_prompt_evidence(
        session_mode=session_mode,
        base_prompt_sha256=base_prompt_sha256,
        effective_prompt_sha256=effective_prompt_sha256,
        lifecycle_context_receipt_sha256=lifecycle_context_receipt_sha256,
        label=label,
    )


def _validate_stored_prompt_evidence(
    *,
    session_mode: Optional[str],
    base_prompt_sha256: Optional[str],
    effective_prompt_sha256: Optional[str],
    lifecycle_context_receipt_sha256: Optional[str],
    label: str,
) -> None:
    if session_mode not in {"new", "resume"}:
        raise ValidationError("%s session mode is invalid" % label)
    _validate_sha256(base_prompt_sha256, "%s base prompt" % label)
    _validate_sha256(effective_prompt_sha256, "%s effective prompt" % label)
    if lifecycle_context_receipt_sha256 is None:
        if effective_prompt_sha256 != base_prompt_sha256:
            raise ValidationError("%s prompt changed without a context receipt" % label)
    else:
        _validate_sha256(
            lifecycle_context_receipt_sha256,
            "%s lifecycle context receipt" % label,
        )
        if effective_prompt_sha256 == base_prompt_sha256:
            raise ValidationError("%s context receipt did not change the prompt" % label)


def _validate_id(value, label: str) -> None:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValidationError("%s is invalid" % label)


def _validate_text(value, label: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError("%s must contain 1 to %d characters" % (label, maximum))


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ValidationError("%s must be a lowercase SHA-256 value" % label)


def _validate_int(value, minimum: int, maximum: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValidationError("agent team plan approval %s is invalid" % label)


def _validate_timestamp(value, label: str) -> None:
    if not isinstance(value, str):
        raise ValidationError("%s is invalid" % label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("%s is invalid" % label) from exc
    if parsed.tzinfo is None:
        raise ValidationError("%s must include a timezone" % label)


def _validate_session(value: str, label: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationError("%s is invalid" % label) from exc
    if str(parsed) != value.lower():
        raise ValidationError("%s is invalid" % label)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _optional_sha256_text(value: Optional[str]) -> Optional[str]:
    return _sha256_text(value) if value is not None else None


def _reject_duplicate_json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("non-finite number %s" % value)
