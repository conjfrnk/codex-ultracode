import hashlib
import json
import math
import os
import uuid
from pathlib import Path
from typing import Dict, Tuple

from .errors import ValidationError
from .security import (
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    require_no_path_escape,
)


CODEX_STEP_CHECKPOINT_SCHEMA_V1 = "conductor.codex_step_checkpoint.v1"
CODEX_STEP_CHECKPOINT_SCHEMA = "conductor.codex_step_checkpoint.v2"
MAX_CODEX_STEP_CHECKPOINT_BYTES = 64 * 1024
MAX_CODEX_STEP_RESUMES = 100
_CHECKPOINT_FIELDS_V1 = {
    "schema",
    "status",
    "step_id",
    "workflow_fingerprint",
    "session_id",
    "prompt_sha256",
    "sandbox",
    "model",
    "output",
    "mode",
    "resume_count",
    "started_at_utc",
    "updated_at_utc",
    "finished_at_utc",
    "output_sha256",
    "error_class",
}
_CHECKPOINT_FIELDS = _CHECKPOINT_FIELDS_V1 | {
    "invocation_base_prompt_sha256",
    "invocation_effective_prompt_sha256",
    "lifecycle_context_receipt_sha256",
}


def codex_step_checkpoint_path(run, step_id: str) -> Path:
    relative = _codex_step_checkpoint_relative(step_id)
    path = run.resolve_artifact_path(relative)
    reject_symlink_path(path, "Codex step checkpoint")
    return path


def codex_step_checkpoint_path_from_run_dir(run_dir: Path, step_id: str) -> Path:
    relative = _codex_step_checkpoint_relative(step_id)
    path = Path(run_dir) / "artifacts" / relative
    reject_symlink_path(path, "Codex step checkpoint")
    return path


def _codex_step_checkpoint_relative(step_id: str) -> str:
    if not isinstance(step_id, str) or not step_id:
        raise ValidationError("Codex checkpoint step_id must be a non-empty string")
    name = hashlib.sha256(step_id.encode("utf-8")).hexdigest() + ".json"
    relative = ".codex-step-checkpoints/%s" % name
    require_no_path_escape(relative)
    return relative


def write_codex_step_checkpoint(run, checkpoint: Dict) -> Path:
    validate_codex_step_checkpoint(checkpoint)
    path = codex_step_checkpoint_path(run, checkpoint["step_id"])
    parent_fd = ensure_dir_no_follow(path.parent, "Codex step checkpoint parent")
    os.close(parent_fd)
    replace_text_file_no_follow(
        path,
        "Codex step checkpoint",
        json.dumps(checkpoint, indent=2, sort_keys=True) + "\n",
        ".codex-step-checkpoint-",
    )
    return path


def load_codex_step_checkpoint(run, step_id: str) -> Dict:
    checkpoint, _ = load_codex_step_checkpoint_with_sha256(run, step_id)
    return checkpoint


def load_codex_step_checkpoint_with_sha256(run, step_id: str) -> Tuple[Dict, str]:
    text = _read_codex_step_checkpoint_text(run, step_id)
    return _parse_codex_step_checkpoint(text, step_id)


def load_codex_step_checkpoint_from_run_dir(run_dir: Path, step_id: str) -> Dict:
    path = codex_step_checkpoint_path_from_run_dir(run_dir, step_id)
    text = read_regular_text_file_no_follow(
        path,
        "Codex step checkpoint",
        MAX_CODEX_STEP_CHECKPOINT_BYTES,
    )
    checkpoint, _ = _parse_codex_step_checkpoint(text, step_id)
    return checkpoint


def _parse_codex_step_checkpoint(text: str, step_id: str) -> Tuple[Dict, str]:
    try:
        checkpoint = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("Codex step checkpoint is invalid JSON: %s" % exc.__class__.__name__)
    validate_codex_step_checkpoint(checkpoint)
    if checkpoint["step_id"] != step_id:
        raise ValidationError("Codex step checkpoint step binding does not match its path")
    return checkpoint, hashlib.sha256(text.encode("utf-8")).hexdigest()


def codex_step_checkpoint_sha256(run, step_id: str) -> str:
    text = _read_codex_step_checkpoint_text(run, step_id)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_codex_step_checkpoint_text(run, step_id: str) -> str:
    path = codex_step_checkpoint_path(run, step_id)
    return read_regular_text_file_no_follow(
        path,
        "Codex step checkpoint",
        MAX_CODEX_STEP_CHECKPOINT_BYTES,
    )


def validate_codex_step_checkpoint(checkpoint: Dict) -> None:
    if not isinstance(checkpoint, dict):
        raise ValidationError("Codex step checkpoint has an invalid shape")
    schema = checkpoint.get("schema")
    expected_fields = (
        _CHECKPOINT_FIELDS
        if schema == CODEX_STEP_CHECKPOINT_SCHEMA
        else _CHECKPOINT_FIELDS_V1
        if schema == CODEX_STEP_CHECKPOINT_SCHEMA_V1
        else None
    )
    if expected_fields is None:
        raise ValidationError("Codex step checkpoint has an unsupported schema")
    if set(checkpoint) != expected_fields:
        raise ValidationError("Codex step checkpoint has an invalid shape")
    if checkpoint.get("status") not in {"active", "completed", "failed"}:
        raise ValidationError("Codex step checkpoint status is invalid")
    if not _bounded_text(checkpoint.get("step_id"), 128):
        raise ValidationError("Codex step checkpoint step_id is invalid")
    if not _sha256(checkpoint.get("workflow_fingerprint")):
        raise ValidationError("Codex step checkpoint workflow fingerprint is invalid")
    if not _canonical_uuid(checkpoint.get("session_id")):
        raise ValidationError("Codex step checkpoint session_id is invalid")
    if not _sha256(checkpoint.get("prompt_sha256")):
        raise ValidationError("Codex step checkpoint prompt hash is invalid")
    if schema == CODEX_STEP_CHECKPOINT_SCHEMA:
        if not _sha256(checkpoint.get("invocation_base_prompt_sha256")):
            raise ValidationError("Codex step checkpoint invocation base prompt is invalid")
        if not _sha256(checkpoint.get("invocation_effective_prompt_sha256")):
            raise ValidationError("Codex step checkpoint invocation prompt is invalid")
        receipt_sha256 = checkpoint.get("lifecycle_context_receipt_sha256")
        if receipt_sha256 is None:
            if (
                checkpoint["invocation_effective_prompt_sha256"]
                != checkpoint["invocation_base_prompt_sha256"]
            ):
                raise ValidationError(
                    "Codex step checkpoint invocation prompt changed without a context receipt"
                )
        elif not _sha256(receipt_sha256):
            raise ValidationError(
                "Codex step checkpoint lifecycle context receipt is invalid"
            )
        elif (
            checkpoint["invocation_effective_prompt_sha256"]
            == checkpoint["invocation_base_prompt_sha256"]
        ):
            raise ValidationError(
                "Codex step checkpoint context receipt did not change the prompt"
            )
    if checkpoint.get("sandbox") not in {"read-only", "workspace-write"}:
        raise ValidationError("Codex step checkpoint sandbox is invalid")
    model = checkpoint.get("model")
    if model is not None and not _bounded_text(model, 200):
        raise ValidationError("Codex step checkpoint model is invalid")
    output = checkpoint.get("output")
    if not _bounded_text(output, 500):
        raise ValidationError("Codex step checkpoint output is invalid")
    require_no_path_escape(output)
    if checkpoint.get("mode") not in {"started", "resumed"}:
        raise ValidationError("Codex step checkpoint mode is invalid")
    resume_count = checkpoint.get("resume_count")
    if (
        not isinstance(resume_count, int)
        or isinstance(resume_count, bool)
        or not 0 <= resume_count <= MAX_CODEX_STEP_RESUMES
    ):
        raise ValidationError("Codex step checkpoint resume_count is invalid")
    for field in ("started_at_utc", "updated_at_utc"):
        if not _bounded_text(checkpoint.get(field), 64):
            raise ValidationError("Codex step checkpoint %s is invalid" % field)
    finished = checkpoint.get("finished_at_utc")
    output_hash = checkpoint.get("output_sha256")
    error_class = checkpoint.get("error_class")
    if checkpoint["status"] == "active":
        if finished is not None or output_hash is not None or error_class is not None:
            raise ValidationError("active Codex step checkpoint has terminal fields")
    elif checkpoint["status"] == "completed":
        if not _bounded_text(finished, 64) or not _sha256(output_hash) or error_class is not None:
            raise ValidationError("completed Codex step checkpoint terminal fields are invalid")
    else:
        if not _bounded_text(finished, 64) or output_hash is not None or not _bounded_text(error_class, 200):
            raise ValidationError("failed Codex step checkpoint terminal fields are invalid")


def _canonical_uuid(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return str(parsed) == value


def _sha256(value) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _bounded_text(value, limit: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= limit


def _reject_duplicate_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


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
