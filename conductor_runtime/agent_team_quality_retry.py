import hashlib
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from .agent_team import MAX_AGENT_TEAM_GENERATION, MAX_AGENT_TEAM_ROUNDS
from .codex_config import MIN_CODEX_RUNTIME_TOKEN_CAP
from .errors import ValidationError
from .security import (
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    require_no_path_escape,
    unlink_regular_file_no_follow,
    write_new_text_file_no_follow,
)


AGENT_TEAM_QUALITY_RETRY_SCHEMA = "conductor.agent_team_quality_retry.v1"
AGENT_TEAM_QUALITY_RETRY_STATUSES = {
    "active",
    "completed",
    "accepted",
    "rejected",
    "failed",
    "abandoned",
}
MAX_AGENT_TEAM_QUALITY_RETRIES = 4
MAX_AGENT_TEAM_QUALITY_RETRY_BYTES = 128 * 1024
MAX_AGENT_TEAM_QUALITY_RETRY_HISTORY = 8192
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
SAFE_ERROR_CLASS = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,199}$")
AGENT_TEAM_QUALITY_RETRY_FIELDS = {
    "schema",
    "status",
    "step_id",
    "workflow_fingerprint",
    "generation",
    "round",
    "member_id",
    "task_id",
    "event_task_id",
    "event",
    "hook_id",
    "hook_input_sha256",
    "retry_index",
    "max_retries",
    "hook_retry_index",
    "hook_max_retries",
    "max_tokens",
    "session_id_sha256",
    "feedback_sha256",
    "workspace_mode",
    "workspace_fingerprint_sha256",
    "rejected_output",
    "rejected_output_sha256",
    "retry_output",
    "retry_output_sha256",
    "started_at_utc",
    "updated_at_utc",
    "finished_at_utc",
    "error_class",
}


def build_agent_team_quality_retry(
    *,
    step_id: str,
    workflow_fingerprint: str,
    generation: int,
    round_number: int,
    member_id: str,
    task_id: str,
    event_task_id: str,
    event: str,
    hook_id: str,
    hook_input_sha256: str,
    retry_index: int,
    max_retries: int,
    hook_retry_index: int,
    hook_max_retries: int,
    max_tokens: int,
    session_id_sha256: str,
    feedback_sha256: str,
    workspace_mode: str,
    workspace_fingerprint_sha256: str,
    rejected_output: str,
    rejected_output_sha256: str,
    started_at_utc: str,
) -> Dict:
    checkpoint = {
        "schema": AGENT_TEAM_QUALITY_RETRY_SCHEMA,
        "status": "active",
        "step_id": step_id,
        "workflow_fingerprint": workflow_fingerprint,
        "generation": generation,
        "round": round_number,
        "member_id": member_id,
        "task_id": task_id,
        "event_task_id": event_task_id,
        "event": event,
        "hook_id": hook_id,
        "hook_input_sha256": hook_input_sha256,
        "retry_index": retry_index,
        "max_retries": max_retries,
        "hook_retry_index": hook_retry_index,
        "hook_max_retries": hook_max_retries,
        "max_tokens": max_tokens,
        "session_id_sha256": session_id_sha256,
        "feedback_sha256": feedback_sha256,
        "workspace_mode": workspace_mode,
        "workspace_fingerprint_sha256": workspace_fingerprint_sha256,
        "rejected_output": rejected_output,
        "rejected_output_sha256": rejected_output_sha256,
        "retry_output": None,
        "retry_output_sha256": None,
        "started_at_utc": started_at_utc,
        "updated_at_utc": started_at_utc,
        "finished_at_utc": None,
        "error_class": None,
    }
    validate_agent_team_quality_retry(checkpoint)
    return checkpoint


def complete_agent_team_quality_retry(
    checkpoint: Dict,
    *,
    retry_output: str,
    retry_output_sha256: str,
    finished_at_utc: str,
) -> Dict:
    validate_agent_team_quality_retry(checkpoint)
    if checkpoint["status"] != "active":
        raise ValidationError("only an active agent team quality retry can complete")
    value = dict(checkpoint)
    value.update(
        {
            "status": "completed",
            "retry_output": retry_output,
            "retry_output_sha256": retry_output_sha256,
            "updated_at_utc": finished_at_utc,
            "finished_at_utc": finished_at_utc,
        }
    )
    validate_agent_team_quality_retry(value)
    return value


def finalize_agent_team_quality_retry(
    checkpoint: Dict,
    status: str,
    *,
    timestamp: str,
    error_class: str = None,
    retry_output: str = None,
    retry_output_sha256: str = None,
) -> Dict:
    validate_agent_team_quality_retry(checkpoint)
    if status not in {"accepted", "rejected", "failed", "abandoned"}:
        raise ValidationError("agent team quality retry terminal status is invalid")
    value = dict(checkpoint)
    if retry_output is not None:
        value["retry_output"] = retry_output
        value["retry_output_sha256"] = retry_output_sha256
    value.update(
        {
            "status": status,
            "updated_at_utc": timestamp,
            "finished_at_utc": timestamp,
            "error_class": error_class if status in {"failed", "abandoned"} else None,
        }
    )
    validate_agent_team_quality_retry(value)
    return value


def agent_team_quality_retry_pending_path(run, step: Dict, member_id: str, task_id: str) -> Path:
    relative = _pending_relative(step, member_id, task_id)
    path = run.resolve_artifact_path(relative)
    reject_symlink_path(path, "agent team quality retry checkpoint")
    return path


def list_agent_team_quality_retry_pending(run, step: Dict) -> List[Path]:
    relative = "%s/quality-retries/pending" % step["capture_dir"]
    require_no_path_escape(relative)
    directory = run.resolve_artifact_path(relative)
    reject_symlink_path(directory, "agent team quality retry checkpoint directory")
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValidationError("agent team quality retry checkpoint directory is invalid")
    paths = sorted(directory.glob("*.json"))
    for path in paths:
        reject_symlink_path(path, "agent team quality retry checkpoint")
        if not path.is_file():
            raise ValidationError("agent team quality retry checkpoint must be a regular file")
    return paths


def write_agent_team_quality_retry_pending(run, step: Dict, checkpoint: Dict) -> Path:
    validate_agent_team_quality_retry(checkpoint)
    if checkpoint["step_id"] != step.get("id"):
        raise ValidationError("agent team quality retry step binding changed")
    path = agent_team_quality_retry_pending_path(
        run,
        step,
        checkpoint["member_id"],
        checkpoint["task_id"],
    )
    parent_fd = ensure_dir_no_follow(path.parent, "agent team quality retry checkpoint parent")
    os.close(parent_fd)
    replace_text_file_no_follow(
        path,
        "agent team quality retry checkpoint",
        _retry_json(checkpoint),
        ".agent-team-quality-retry-",
    )
    return path


def load_agent_team_quality_retry(path: Path) -> Tuple[Dict, str]:
    reject_symlink_path(path, "agent team quality retry checkpoint")
    text = read_regular_text_file_no_follow(
        path,
        "agent team quality retry checkpoint",
        MAX_AGENT_TEAM_QUALITY_RETRY_BYTES,
    )
    try:
        checkpoint = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError(
            "agent team quality retry checkpoint is invalid JSON: %s"
            % exc.__class__.__name__
        )
    validate_agent_team_quality_retry(checkpoint)
    return checkpoint, hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_agent_team_quality_retry_history(run, step: Dict, checkpoint: Dict) -> Path:
    validate_agent_team_quality_retry(checkpoint)
    if checkpoint["status"] not in {"accepted", "rejected", "failed", "abandoned"}:
        raise ValidationError("agent team quality retry history must be terminal")
    relative = (
        "%s/quality-retries/history/generation-%09d--round-%03d--%s--%s--retry-%02d.json"
        % (
            step["capture_dir"],
            checkpoint["generation"],
            checkpoint["round"],
            checkpoint["member_id"],
            checkpoint["task_id"],
            checkpoint["retry_index"],
        )
    )
    require_no_path_escape(relative)
    path = run.resolve_artifact_path(relative)
    reject_symlink_path(path, "agent team quality retry history")
    parent_fd = ensure_dir_no_follow(path.parent, "agent team quality retry history parent")
    os.close(parent_fd)
    serialized = _retry_json(checkpoint)
    try:
        write_new_text_file_no_follow(path, "agent team quality retry history", serialized)
    except FileExistsError:
        current = read_regular_text_file_no_follow(
            path,
            "agent team quality retry history",
            MAX_AGENT_TEAM_QUALITY_RETRY_BYTES,
        )
        if current != serialized:
            raise ValidationError("agent team quality retry history changed")
    return path


def list_agent_team_quality_retry_history(run, step: Dict) -> List[Dict]:
    relative = "%s/quality-retries/history" % step["capture_dir"]
    require_no_path_escape(relative)
    directory = run.resolve_artifact_path(relative)
    reject_symlink_path(directory, "agent team quality retry history directory")
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValidationError("agent team quality retry history directory is invalid")
    paths = sorted(directory.glob("*.json"))
    if len(paths) > MAX_AGENT_TEAM_QUALITY_RETRY_HISTORY:
        raise ValidationError("agent team quality retry history exceeds its limit")
    values = []
    for path in paths:
        reject_symlink_path(path, "agent team quality retry history")
        checkpoint, _file_sha256 = load_agent_team_quality_retry(path)
        if checkpoint["status"] not in {"accepted", "rejected", "failed", "abandoned"}:
            raise ValidationError("agent team quality retry history is not terminal")
        values.append(checkpoint)
    return values


def remove_agent_team_quality_retry_pending(path: Path) -> None:
    unlink_regular_file_no_follow(path, "agent team quality retry checkpoint")


def validate_agent_team_quality_retry(checkpoint: Dict) -> None:
    if not isinstance(checkpoint, dict) or set(checkpoint) != AGENT_TEAM_QUALITY_RETRY_FIELDS:
        raise ValidationError("agent team quality retry checkpoint has invalid fields")
    if checkpoint.get("schema") != AGENT_TEAM_QUALITY_RETRY_SCHEMA:
        raise ValidationError("agent team quality retry checkpoint schema is invalid")
    status = checkpoint.get("status")
    if status not in AGENT_TEAM_QUALITY_RETRY_STATUSES:
        raise ValidationError("agent team quality retry checkpoint status is invalid")
    for field in ("step_id", "member_id", "task_id", "event_task_id", "hook_id"):
        _validate_id(checkpoint.get(field), "agent team quality retry %s" % field)
    for field in (
        "workflow_fingerprint",
        "hook_input_sha256",
        "session_id_sha256",
        "feedback_sha256",
        "workspace_fingerprint_sha256",
        "rejected_output_sha256",
    ):
        _validate_sha256(checkpoint.get(field), "agent team quality retry %s" % field)
    _validate_int(checkpoint.get("generation"), 0, MAX_AGENT_TEAM_GENERATION, "generation")
    _validate_int(checkpoint.get("round"), 1, MAX_AGENT_TEAM_ROUNDS, "round")
    _validate_int(
        checkpoint.get("retry_index"),
        1,
        MAX_AGENT_TEAM_QUALITY_RETRIES,
        "retry_index",
    )
    _validate_int(
        checkpoint.get("max_retries"),
        1,
        MAX_AGENT_TEAM_QUALITY_RETRIES,
        "max_retries",
    )
    if checkpoint["retry_index"] > checkpoint["max_retries"]:
        raise ValidationError("agent team quality retry index exceeds its limit")
    _validate_int(
        checkpoint.get("hook_retry_index"),
        1,
        MAX_AGENT_TEAM_QUALITY_RETRIES,
        "hook_retry_index",
    )
    _validate_int(
        checkpoint.get("hook_max_retries"),
        1,
        MAX_AGENT_TEAM_QUALITY_RETRIES,
        "hook_max_retries",
    )
    if checkpoint["hook_retry_index"] > checkpoint["hook_max_retries"]:
        raise ValidationError("agent team quality hook retry index exceeds its limit")
    _validate_int(checkpoint.get("max_tokens"), MIN_CODEX_RUNTIME_TOKEN_CAP, 10**12, "max_tokens")
    if checkpoint.get("event") not in {
        "team_task_created",
        "team_task_completed",
        "team_member_idle",
    }:
        raise ValidationError("agent team quality retry event is invalid")
    if checkpoint["event"] == "team_task_created":
        if checkpoint["event_task_id"] == checkpoint["task_id"]:
            raise ValidationError("agent team quality retry created task must differ from retry task")
    elif checkpoint["event_task_id"] != checkpoint["task_id"]:
        raise ValidationError("agent team quality retry event task is inconsistent")
    if checkpoint.get("workspace_mode") not in {"read-only", "isolated-write"}:
        raise ValidationError("agent team quality retry workspace mode is invalid")
    rejected_output = checkpoint.get("rejected_output")
    if not _bounded_text(rejected_output, 1000):
        raise ValidationError("agent team quality retry rejected output is invalid")
    require_no_path_escape(rejected_output)
    for field in ("started_at_utc", "updated_at_utc"):
        _validate_timestamp(checkpoint.get(field), "agent team quality retry %s" % field)
    retry_output = checkpoint.get("retry_output")
    retry_output_sha256 = checkpoint.get("retry_output_sha256")
    finished = checkpoint.get("finished_at_utc")
    error_class = checkpoint.get("error_class")
    if status == "active":
        if any(value is not None for value in (retry_output, retry_output_sha256, finished, error_class)):
            raise ValidationError("active agent team quality retry has terminal fields")
    elif status in {"completed", "accepted", "rejected"}:
        if not _bounded_text(retry_output, 1000):
            raise ValidationError("completed agent team quality retry output is invalid")
        require_no_path_escape(retry_output)
        _validate_sha256(retry_output_sha256, "agent team quality retry output hash")
        _validate_timestamp(finished, "agent team quality retry finished_at_utc")
        if error_class is not None:
            raise ValidationError("completed agent team quality retry cannot record an error")
    else:
        if retry_output is not None:
            if not _bounded_text(retry_output, 1000):
                raise ValidationError("failed agent team quality retry output is invalid")
            require_no_path_escape(retry_output)
            _validate_sha256(retry_output_sha256, "agent team quality retry output hash")
        elif retry_output_sha256 is not None:
            raise ValidationError("failed agent team quality retry output hash is inconsistent")
        _validate_timestamp(finished, "agent team quality retry finished_at_utc")
        if not isinstance(error_class, str) or not SAFE_ERROR_CLASS.match(error_class):
            raise ValidationError("failed agent team quality retry error class is invalid")
    started_at = _parse_timestamp(checkpoint["started_at_utc"])
    updated_at = _parse_timestamp(checkpoint["updated_at_utc"])
    if updated_at < started_at:
        raise ValidationError("agent team quality retry timestamps are out of order")
    if finished is not None:
        finished_at = _parse_timestamp(finished)
        if finished_at < started_at or updated_at != finished_at:
            raise ValidationError("agent team quality retry terminal timestamps are inconsistent")


def agent_team_quality_retry_sha256(checkpoint: Dict) -> str:
    validate_agent_team_quality_retry(checkpoint)
    canonical = json.dumps(checkpoint, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def agent_team_quality_retry_summary(checkpoint: Dict) -> Dict:
    validate_agent_team_quality_retry(checkpoint)
    return {
        "schema": checkpoint["schema"],
        "status": checkpoint["status"],
        "step_id": checkpoint["step_id"],
        "generation": checkpoint["generation"],
        "round": checkpoint["round"],
        "member_id": checkpoint["member_id"],
        "task_id": checkpoint["task_id"],
        "event_task_id": checkpoint["event_task_id"],
        "event": checkpoint["event"],
        "hook_id": checkpoint["hook_id"],
        "retry_index": checkpoint["retry_index"],
        "max_retries": checkpoint["max_retries"],
        "hook_retry_index": checkpoint["hook_retry_index"],
        "hook_max_retries": checkpoint["hook_max_retries"],
        "max_tokens": checkpoint["max_tokens"],
        "workspace_mode": checkpoint["workspace_mode"],
        "hook_input_sha256": checkpoint["hook_input_sha256"],
        "workspace_fingerprint_sha256": checkpoint["workspace_fingerprint_sha256"],
        "rejected_output_sha256": checkpoint["rejected_output_sha256"],
        "retry_output_sha256": checkpoint["retry_output_sha256"],
        "feedback_sha256": checkpoint["feedback_sha256"],
        "started_at_utc": checkpoint["started_at_utc"],
        "finished_at_utc": checkpoint["finished_at_utc"],
        "error_class": checkpoint["error_class"],
        "automatic_provider_replay_allowed": False,
    }


def _pending_relative(step: Dict, member_id: str, task_id: str) -> str:
    _validate_id(step.get("id"), "agent team quality retry step id")
    _validate_id(member_id, "agent team quality retry member id")
    _validate_id(task_id, "agent team quality retry task id")
    relative = "%s/quality-retries/pending/%s--%s.json" % (
        step["capture_dir"],
        member_id,
        task_id,
    )
    require_no_path_escape(relative)
    return relative


def _retry_json(checkpoint: Dict) -> str:
    validate_agent_team_quality_retry(checkpoint)
    text = json.dumps(checkpoint, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if len(text.encode("utf-8")) > MAX_AGENT_TEAM_QUALITY_RETRY_BYTES:
        raise ValidationError("agent team quality retry checkpoint exceeds its byte limit")
    return text


def _validate_id(value, label: str) -> None:
    if not isinstance(value, str) or not SAFE_ID.match(value):
        raise ValidationError("%s is invalid" % label)


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValidationError("%s is invalid" % label)


def _validate_int(value, minimum: int, maximum: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValidationError("agent team quality retry %s is invalid" % label)


def _validate_timestamp(value, label: str) -> None:
    if not _bounded_text(value, 64) or not value.endswith("Z"):
        raise ValidationError("%s is invalid" % label)
    try:
        _parse_timestamp(value)
    except ValueError:
        raise ValidationError("%s is invalid" % label)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _bounded_text(value, limit: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= limit


def _reject_duplicate_json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(value):
    if value in {"NaN", "Infinity", "-Infinity"}:
        raise ValueError("non-finite JSON constant")
    try:
        number = float(value)
    except ValueError:
        raise ValueError("invalid JSON constant")
    if not math.isfinite(number):
        raise ValueError("non-finite JSON constant")
    return number
