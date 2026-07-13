import hashlib
import json
import re
from typing import Dict, Optional

from .errors import ValidationError


AGENT_LIFECYCLE_HOOK_INPUT_SCHEMA = "conductor.agent_lifecycle_hook_input.v1"
AGENT_LIFECYCLE_HOOK_EVENTS = {"agent_start", "agent_stop"}
AGENT_LIFECYCLE_SCOPES = {
    "codex_exec",
    "agent_map_packet",
    "agent_team_plan",
    "agent_team_review",
    "agent_team_turn",
    "agent_team_quality_retry",
}
AGENT_LIFECYCLE_STATUSES = {
    "starting",
    "returned",
    "failed",
    "timed_out",
    "interrupted",
    "runner_error",
}
AGENT_LIFECYCLE_HOOK_INPUT_FIELDS = {
    "schema",
    "event",
    "workflow_fingerprint",
    "invocation_sha256",
    "step_id",
    "step_kind",
    "scope",
    "agent_profile",
    "member_id",
    "task_id",
    "generation",
    "round",
    "attempt",
    "packet_index",
    "packet_generation",
    "quality_retry_index",
    "plan_revision",
    "model_sha256",
    "effort",
    "sandbox",
    "max_tokens",
    "prompt_sha256",
    "session_mode",
    "session_id_sha256",
    "status",
    "returncode",
    "timed_out",
    "interrupted",
    "output_sha256",
    "provider_stdout_sha256",
    "provider_stderr_sha256",
    "error_class",
}

SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STEP_KINDS = {"codex_exec", "agent_map", "agent_team"}
SESSION_MODES = {"new", "resume"}
SANDBOXES = {"read-only", "workspace-write"}


def build_agent_lifecycle_hook_input(
    *,
    event: str,
    workflow_fingerprint: str,
    step_id: str,
    step_kind: str,
    scope: str,
    prompt_sha256: str,
    attempt: int,
    sandbox: str,
    session_mode: str,
    agent_profile: Optional[str] = None,
    member_id: Optional[str] = None,
    task_id: Optional[str] = None,
    generation: Optional[int] = None,
    round_number: Optional[int] = None,
    packet_index: Optional[int] = None,
    packet_generation: Optional[int] = None,
    quality_retry_index: Optional[int] = None,
    plan_revision: Optional[int] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    max_tokens: Optional[int] = None,
    session_id: Optional[str] = None,
    status: str = "starting",
    returncode: Optional[int] = None,
    timed_out: bool = False,
    interrupted: bool = False,
    output_sha256: Optional[str] = None,
    provider_stdout_sha256: Optional[str] = None,
    provider_stderr_sha256: Optional[str] = None,
    error_class: Optional[str] = None,
) -> Dict:
    identity = {
        "workflow_fingerprint": workflow_fingerprint,
        "step_id": step_id,
        "step_kind": step_kind,
        "scope": scope,
        "agent_profile": agent_profile,
        "member_id": member_id,
        "task_id": task_id,
        "generation": generation,
        "round": round_number,
        "attempt": attempt,
        "packet_index": packet_index,
        "packet_generation": packet_generation,
        "quality_retry_index": quality_retry_index,
        "plan_revision": plan_revision,
        "prompt_sha256": prompt_sha256,
        "session_mode": session_mode,
        "session_id_sha256": _optional_sha256_text(session_id),
    }
    payload = {
        "schema": AGENT_LIFECYCLE_HOOK_INPUT_SCHEMA,
        "event": event,
        "workflow_fingerprint": workflow_fingerprint,
        "invocation_sha256": _sha256_json(identity),
        "step_id": step_id,
        "step_kind": step_kind,
        "scope": scope,
        "agent_profile": agent_profile,
        "member_id": member_id,
        "task_id": task_id,
        "generation": generation,
        "round": round_number,
        "attempt": attempt,
        "packet_index": packet_index,
        "packet_generation": packet_generation,
        "quality_retry_index": quality_retry_index,
        "plan_revision": plan_revision,
        "model_sha256": _optional_sha256_text(model),
        "effort": effort,
        "sandbox": sandbox,
        "max_tokens": max_tokens,
        "prompt_sha256": prompt_sha256,
        "session_mode": session_mode,
        "session_id_sha256": identity["session_id_sha256"],
        "status": status,
        "returncode": returncode,
        "timed_out": timed_out,
        "interrupted": interrupted,
        "output_sha256": output_sha256,
        "provider_stdout_sha256": provider_stdout_sha256,
        "provider_stderr_sha256": provider_stderr_sha256,
        "error_class": error_class,
    }
    validate_agent_lifecycle_hook_input(payload)
    return payload


def build_agent_lifecycle_stop_input(
    start_input: Dict,
    *,
    status: str,
    returncode: Optional[int] = None,
    timed_out: bool = False,
    interrupted: bool = False,
    output_sha256: Optional[str] = None,
    provider_stdout_sha256: Optional[str] = None,
    provider_stderr_sha256: Optional[str] = None,
    error_class: Optional[str] = None,
) -> Dict:
    validate_agent_lifecycle_hook_input(start_input)
    if start_input["event"] != "agent_start":
        raise ValidationError("agent lifecycle stop input requires an agent_start input")
    payload = dict(start_input)
    payload.update(
        {
            "event": "agent_stop",
            "status": status,
            "returncode": returncode,
            "timed_out": timed_out,
            "interrupted": interrupted,
            "output_sha256": output_sha256,
            "provider_stdout_sha256": provider_stdout_sha256,
            "provider_stderr_sha256": provider_stderr_sha256,
            "error_class": error_class,
        }
    )
    validate_agent_lifecycle_hook_input(payload)
    if payload["invocation_sha256"] != start_input["invocation_sha256"]:
        raise ValidationError("agent lifecycle stop invocation changed")
    return payload


def validate_agent_lifecycle_hook_input(payload: Dict) -> None:
    if not isinstance(payload, dict) or set(payload) != AGENT_LIFECYCLE_HOOK_INPUT_FIELDS:
        raise ValidationError("agent lifecycle hook input has invalid fields")
    if payload.get("schema") != AGENT_LIFECYCLE_HOOK_INPUT_SCHEMA:
        raise ValidationError("agent lifecycle hook input schema is invalid")
    event = payload.get("event")
    if event not in AGENT_LIFECYCLE_HOOK_EVENTS:
        raise ValidationError("agent lifecycle hook event is invalid")
    for field in (
        "workflow_fingerprint",
        "invocation_sha256",
        "prompt_sha256",
    ):
        _validate_sha256(payload.get(field), "agent lifecycle hook input %s" % field)
    for field in (
        "model_sha256",
        "session_id_sha256",
        "output_sha256",
        "provider_stdout_sha256",
        "provider_stderr_sha256",
    ):
        value = payload.get(field)
        if value is not None:
            _validate_sha256(value, "agent lifecycle hook input %s" % field)
    for field in ("step_id", "agent_profile", "member_id", "task_id"):
        value = payload.get(field)
        if value is not None and not _safe_id(value):
            raise ValidationError("agent lifecycle hook input %s is invalid" % field)
    if payload.get("step_kind") not in STEP_KINDS:
        raise ValidationError("agent lifecycle hook step kind is invalid")
    scope = payload.get("scope")
    if scope not in AGENT_LIFECYCLE_SCOPES:
        raise ValidationError("agent lifecycle hook scope is invalid")
    if payload.get("sandbox") not in SANDBOXES:
        raise ValidationError("agent lifecycle hook sandbox is invalid")
    if payload.get("session_mode") not in SESSION_MODES:
        raise ValidationError("agent lifecycle hook session mode is invalid")
    if payload.get("effort") is not None and not _bounded_text(payload["effort"], 32):
        raise ValidationError("agent lifecycle hook effort is invalid")
    if payload.get("error_class") is not None and not _bounded_text(payload["error_class"], 128):
        raise ValidationError("agent lifecycle hook error class is invalid")
    _positive_int(payload.get("attempt"), "agent lifecycle hook attempt")
    for field in (
        "generation",
        "round",
        "packet_generation",
        "quality_retry_index",
        "plan_revision",
    ):
        value = payload.get(field)
        if value is not None:
            _non_negative_int(value, "agent lifecycle hook %s" % field)
    if payload.get("packet_index") is not None:
        _positive_int(payload["packet_index"], "agent lifecycle hook packet index")
    if payload.get("max_tokens") is not None:
        _positive_int(payload["max_tokens"], "agent lifecycle hook max tokens")
    returncode = payload.get("returncode")
    if returncode is not None and (not isinstance(returncode, int) or isinstance(returncode, bool)):
        raise ValidationError("agent lifecycle hook returncode is invalid")
    if not isinstance(payload.get("timed_out"), bool) or not isinstance(payload.get("interrupted"), bool):
        raise ValidationError("agent lifecycle hook outcome flags are invalid")

    _validate_scope_fields(payload)
    status = payload.get("status")
    if status not in AGENT_LIFECYCLE_STATUSES:
        raise ValidationError("agent lifecycle hook status is invalid")
    if event == "agent_start":
        if status != "starting":
            raise ValidationError("agent_start hook status must be starting")
        for field in (
            "returncode",
            "output_sha256",
            "provider_stdout_sha256",
            "provider_stderr_sha256",
            "error_class",
        ):
            if payload.get(field) is not None:
                raise ValidationError("agent_start hook %s must be null" % field)
        if payload["timed_out"] or payload["interrupted"]:
            raise ValidationError("agent_start hook outcome flags must be false")
        return

    if status == "runner_error":
        if payload.get("error_class") is None or returncode is not None:
            raise ValidationError("runner_error lifecycle outcome is incomplete")
    else:
        if returncode is None or payload.get("error_class") is not None:
            raise ValidationError("returned lifecycle outcome is incomplete")
    if status == "timed_out" and not payload["timed_out"]:
        raise ValidationError("timed_out lifecycle status requires timed_out")
    if status == "interrupted" and not payload["interrupted"]:
        raise ValidationError("interrupted lifecycle status requires interrupted")
    if payload["timed_out"] and status != "timed_out":
        raise ValidationError("timed_out lifecycle flag changed status")
    if payload["interrupted"] and status != "interrupted":
        raise ValidationError("interrupted lifecycle flag changed status")
    if status == "returned" and returncode != 0:
        raise ValidationError("returned lifecycle status requires exit code zero")
    if status == "failed" and returncode == 0:
        raise ValidationError("failed lifecycle status requires nonzero exit code")


def agent_lifecycle_hook_input_json(payload: Dict) -> str:
    validate_agent_lifecycle_hook_input(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def _validate_scope_fields(payload: Dict) -> None:
    scope = payload["scope"]
    step_kind = payload["step_kind"]
    expected_kind = {
        "codex_exec": "codex_exec",
        "agent_map_packet": "agent_map",
        "agent_team_plan": "agent_team",
        "agent_team_review": "agent_team",
        "agent_team_turn": "agent_team",
        "agent_team_quality_retry": "agent_team",
    }[scope]
    if step_kind != expected_kind:
        raise ValidationError("agent lifecycle hook scope does not match its step kind")
    team_scope = scope.startswith("agent_team_")
    for field in ("member_id", "task_id"):
        if team_scope != (payload.get(field) is not None):
            raise ValidationError("agent lifecycle hook %s does not match its scope" % field)
    if scope == "agent_map_packet":
        if payload.get("packet_index") is None or payload.get("packet_generation") is None:
            raise ValidationError("agent_map lifecycle input requires packet identity")
    elif payload.get("packet_index") is not None or payload.get("packet_generation") is not None:
        raise ValidationError("non-map lifecycle input cannot set packet identity")
    if team_scope:
        if payload.get("generation") is None or payload.get("round") is None:
            raise ValidationError("agent team lifecycle input requires generation and round")
    elif payload.get("generation") is not None or payload.get("round") is not None:
        raise ValidationError("non-team lifecycle input cannot set team generation or round")
    if scope == "agent_team_quality_retry":
        if not payload.get("quality_retry_index"):
            raise ValidationError("quality-retry lifecycle input requires retry identity")
    elif payload.get("quality_retry_index") is not None:
        raise ValidationError("non-retry lifecycle input cannot set retry identity")
    if scope in {"agent_team_plan", "agent_team_review"}:
        if not payload.get("plan_revision"):
            raise ValidationError("plan lifecycle input requires a revision")
    elif payload.get("plan_revision") is not None:
        raise ValidationError("non-plan lifecycle input cannot set a plan revision")


def _optional_sha256_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValidationError("agent lifecycle hash source must be non-empty text")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Dict) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValidationError("%s must be a sha256 digest" % label)


def _safe_id(value) -> bool:
    return isinstance(value, str) and bool(SAFE_ID.fullmatch(value))


def _bounded_text(value, limit: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= limit and "\n" not in value


def _positive_int(value, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError("%s must be a positive integer" % label)


def _non_negative_int(value, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError("%s must be a non-negative integer" % label)
