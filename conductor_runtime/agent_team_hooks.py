import hashlib
import json
import re
from typing import Dict, Optional

from .errors import ValidationError
from .agent_team import MAX_AGENT_TEAM_GENERATION, MAX_AGENT_TEAM_ROUNDS
from .security import require_no_path_escape
from .staged_workspace import (
    StagedWorkspaceSnapshot,
    MAX_STAGED_CHANGES,
    build_workspace_delta,
)


AGENT_TEAM_HOOK_INPUT_SCHEMA = "conductor.agent_team_hook_input.v1"
AGENT_TEAM_HOOK_EVENTS = {
    "team_task_created",
    "team_task_completed",
    "team_member_idle",
}
MAX_AGENT_TEAM_HOOK_CHANGED_FILES = 512
MAX_AGENT_TEAM_HOOK_INPUT_BYTES = 512 * 1024
AGENT_TEAM_HOOK_INPUT_FIELDS = {
    "schema",
    "event",
    "workflow_fingerprint",
    "step_id",
    "generation",
    "round",
    "member_id",
    "task_id",
    "task_origin",
    "task_assignee",
    "task_description_sha256",
    "turn_output_sha256",
    "turn_summary_sha256",
    "turn_status",
    "workspace_mode",
    "workspace_base_sha256",
    "workspace_result_sha256",
    "changed_file_count",
    "changed_files",
    "changed_files_truncated",
}
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def build_agent_team_hook_input(
    *,
    event: str,
    workflow_fingerprint: str,
    step_id: str,
    generation: int,
    round_number: int,
    member_id: str,
    task: Dict,
    turn_output_sha256: str,
    turn_summary: str,
    turn_status: str,
    workspace_mode: str,
    workspace_base_snapshot: Optional[StagedWorkspaceSnapshot] = None,
    workspace_result_snapshot: Optional[StagedWorkspaceSnapshot] = None,
) -> Dict:
    if (workspace_base_snapshot is None) != (workspace_result_snapshot is None):
        raise ValidationError("agent team hook workspace snapshots must be supplied together")
    changed_files = []
    workspace_base_sha256 = None
    workspace_result_sha256 = None
    if workspace_base_snapshot is not None and workspace_result_snapshot is not None:
        delta = build_workspace_delta(workspace_base_snapshot, workspace_result_snapshot)
        all_changed_files = list(delta["changed_files"])
        changed_files = all_changed_files[:MAX_AGENT_TEAM_HOOK_CHANGED_FILES]
        workspace_base_sha256 = workspace_base_snapshot.tracked_fingerprint_sha256
        workspace_result_sha256 = workspace_result_snapshot.tracked_fingerprint_sha256
    else:
        all_changed_files = []
    payload = {
        "schema": AGENT_TEAM_HOOK_INPUT_SCHEMA,
        "event": event,
        "workflow_fingerprint": workflow_fingerprint,
        "step_id": step_id,
        "generation": generation,
        "round": round_number,
        "member_id": member_id,
        "task_id": task.get("id"),
        "task_origin": task.get("origin", "static"),
        "task_assignee": task.get("assignee"),
        "task_description_sha256": _sha256_text(task.get("description")),
        "turn_output_sha256": turn_output_sha256,
        "turn_summary_sha256": _sha256_text(turn_summary),
        "turn_status": turn_status,
        "workspace_mode": workspace_mode,
        "workspace_base_sha256": workspace_base_sha256,
        "workspace_result_sha256": workspace_result_sha256,
        "changed_file_count": len(all_changed_files),
        "changed_files": changed_files,
        "changed_files_truncated": len(all_changed_files) > len(changed_files),
    }
    validate_agent_team_hook_input(payload)
    if len(agent_team_hook_input_json(payload).encode("utf-8")) > MAX_AGENT_TEAM_HOOK_INPUT_BYTES:
        raise ValidationError("agent team hook input exceeds its byte limit")
    return payload


def validate_agent_team_hook_input(payload: Dict) -> None:
    if not isinstance(payload, dict) or set(payload) != AGENT_TEAM_HOOK_INPUT_FIELDS:
        raise ValidationError("agent team hook input has invalid fields")
    if payload.get("schema") != AGENT_TEAM_HOOK_INPUT_SCHEMA:
        raise ValidationError("agent team hook input schema is invalid")
    if payload.get("event") not in AGENT_TEAM_HOOK_EVENTS:
        raise ValidationError("agent team hook event is invalid")
    for field in ("workflow_fingerprint", "task_description_sha256", "turn_output_sha256", "turn_summary_sha256"):
        _validate_sha256(payload.get(field), "agent team hook input %s" % field)
    for field in ("step_id", "member_id", "task_id"):
        _validate_id(payload.get(field), "agent team hook input %s" % field)
    for field in ("generation", "round", "changed_file_count"):
        value = payload.get(field)
        minimum = 1 if field == "round" else 0
        maximum = {
            "generation": MAX_AGENT_TEAM_GENERATION,
            "round": MAX_AGENT_TEAM_ROUNDS,
            "changed_file_count": MAX_STAGED_CHANGES,
        }[field]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not minimum <= value <= maximum
        ):
            raise ValidationError("agent team hook input %s is invalid" % field)
    if payload.get("task_origin") not in {
        "static",
        "proposed",
        "operator",
        "message",
        "operator-reply",
    }:
        raise ValidationError("agent team hook input task origin is invalid")
    if payload.get("task_assignee") is not None:
        _validate_id(payload["task_assignee"], "agent team hook input task assignee")
    if payload.get("turn_status") not in {"completed", "continue"}:
        raise ValidationError("agent team hook input turn status is invalid")
    if payload.get("workspace_mode") not in {"read-only", "isolated-write"}:
        raise ValidationError("agent team hook input workspace mode is invalid")
    base_sha256 = payload.get("workspace_base_sha256")
    result_sha256 = payload.get("workspace_result_sha256")
    if (base_sha256 is None) != (result_sha256 is None):
        raise ValidationError("agent team hook workspace fingerprints are inconsistent")
    if base_sha256 is not None:
        _validate_sha256(base_sha256, "agent team hook input workspace base")
        _validate_sha256(result_sha256, "agent team hook input workspace result")
    if payload["workspace_mode"] == "isolated-write" and base_sha256 is None:
        raise ValidationError("isolated-write team hook input requires workspace fingerprints")
    if payload["workspace_mode"] == "read-only" and base_sha256 is not None:
        raise ValidationError("read-only team hook input cannot claim write workspace fingerprints")
    changed_files = payload.get("changed_files")
    if not isinstance(changed_files, list) or len(changed_files) > MAX_AGENT_TEAM_HOOK_CHANGED_FILES:
        raise ValidationError("agent team hook changed files are invalid")
    for path in changed_files:
        if not isinstance(path, str) or not path:
            raise ValidationError("agent team hook changed file path is invalid")
        require_no_path_escape(path)
    if changed_files != sorted(set(changed_files)):
        raise ValidationError("agent team hook changed files are invalid")
    truncated = payload.get("changed_files_truncated")
    if not isinstance(truncated, bool):
        raise ValidationError("agent team hook changed-files truncation flag is invalid")
    if payload["changed_file_count"] < len(changed_files):
        raise ValidationError("agent team hook changed-file count is inconsistent")
    if truncated != (payload["changed_file_count"] > len(changed_files)):
        raise ValidationError("agent team hook changed-files truncation is inconsistent")
    if payload["workspace_mode"] == "read-only" and (
        payload["changed_file_count"] != 0 or changed_files or truncated
    ):
        raise ValidationError("read-only team hook input cannot claim changed files")


def agent_team_hook_input_json(payload: Dict) -> str:
    validate_agent_team_hook_input(payload)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if len(text.encode("utf-8")) > MAX_AGENT_TEAM_HOOK_INPUT_BYTES:
        raise ValidationError("agent team hook input exceeds its byte limit")
    return text


def agent_team_hook_input_sha256(payload: Dict) -> str:
    validate_agent_team_hook_input(payload)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_text(value) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("agent team hook text binding must be non-empty")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_id(value, label: str) -> None:
    if not isinstance(value, str) or not SAFE_ID.match(value):
        raise ValidationError("%s is invalid" % label)


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or not SHA256.match(value):
        raise ValidationError("%s is invalid" % label)
