import hashlib
import json
import os
import re
from pathlib import Path
from typing import Dict, Optional

from .artifacts import utc_now
from .errors import ValidationError
from .security import (
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    require_no_path_escape,
    write_new_text_file_no_follow,
)
from .staged_workspace import (
    validate_workspace_merge_plan,
    workspace_snapshot_from_manifest,
)


DIRECT_WORKSPACE_TRANSACTION_SCHEMA = "conductor.direct_workspace_transaction.v1"
DIRECT_WORKSPACE_TRANSACTION_STATUSES = {
    "prepared",
    "merging",
    "merged",
    "conflict",
    "rejected",
}
DIRECT_WORKSPACE_TRANSACTION_FIELDS = {
    "schema",
    "status",
    "workflow_fingerprint",
    "step_id",
    "attempt",
    "source_workspace_sha256",
    "stage_relative",
    "base_manifest",
    "result_manifest",
    "source_before_manifest",
    "merge_plan",
    "invocation_sha256",
    "terminal_sha256",
    "stop_gate_sha256",
    "created_at_utc",
    "updated_at_utc",
    "error_class",
    "transaction_sha256",
}
MAX_DIRECT_WORKSPACE_TRANSACTION_BYTES = 32 * 1024 * 1024
MAX_DIRECT_WORKSPACE_ATTEMPT = 999_999_999
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$")
ERROR_CLASS = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]{0,127}$")


def build_direct_workspace_transaction(
    *,
    workflow_fingerprint: str,
    step_id: str,
    attempt: int,
    source_workspace_sha256: str,
    base_manifest: Dict,
    created_at_utc: Optional[str] = None,
) -> Dict:
    timestamp = created_at_utc or utc_now()
    transaction = {
        "schema": DIRECT_WORKSPACE_TRANSACTION_SCHEMA,
        "status": "prepared",
        "workflow_fingerprint": workflow_fingerprint,
        "step_id": step_id,
        "attempt": attempt,
        "source_workspace_sha256": source_workspace_sha256,
        "stage_relative": direct_workspace_stage_relative(
            workflow_fingerprint,
            step_id,
            attempt,
        ),
        "base_manifest": json.loads(json.dumps(base_manifest)),
        "result_manifest": None,
        "source_before_manifest": None,
        "merge_plan": None,
        "invocation_sha256": None,
        "terminal_sha256": None,
        "stop_gate_sha256": None,
        "created_at_utc": timestamp,
        "updated_at_utc": timestamp,
        "error_class": None,
        "transaction_sha256": "0" * 64,
    }
    return _finalize(transaction)


def bind_direct_workspace_transaction_merge(
    transaction: Dict,
    *,
    result_manifest: Dict,
    source_before_manifest: Dict,
    merge_plan: Dict,
    invocation_sha256: str,
    terminal_sha256: str,
    stop_gate_sha256: str,
    updated_at_utc: Optional[str] = None,
) -> Dict:
    validate_direct_workspace_transaction(transaction)
    if transaction["status"] != "prepared":
        raise ValidationError("direct workspace transaction is not prepared")
    base = workspace_snapshot_from_manifest(transaction["base_manifest"])
    result = workspace_snapshot_from_manifest(result_manifest)
    source_before = workspace_snapshot_from_manifest(source_before_manifest)
    validate_workspace_merge_plan(
        merge_plan,
        base_snapshot=base,
        incoming_snapshot=result,
    )
    if merge_plan["source_before_sha256"] != source_before.tracked_fingerprint_sha256:
        raise ValidationError("direct workspace transaction source-before binding changed")
    _validate_sha256(invocation_sha256, "direct workspace invocation digest")
    _validate_sha256(terminal_sha256, "direct workspace terminal digest")
    _validate_sha256(stop_gate_sha256, "direct workspace stop-gate digest")
    conflict = merge_plan["status"] == "conflict"
    candidate = dict(transaction)
    candidate.update(
        {
            "status": "conflict" if conflict else "merging",
            "result_manifest": json.loads(json.dumps(result_manifest)),
            "source_before_manifest": json.loads(json.dumps(source_before_manifest)),
            "merge_plan": json.loads(json.dumps(merge_plan)),
            "invocation_sha256": invocation_sha256,
            "terminal_sha256": terminal_sha256,
            "stop_gate_sha256": stop_gate_sha256,
            "updated_at_utc": updated_at_utc or utc_now(),
            "error_class": "WorkspaceMergeConflict" if conflict else None,
        }
    )
    return _finalize(candidate)


def mark_direct_workspace_transaction_merged(
    transaction: Dict,
    *,
    updated_at_utc: Optional[str] = None,
) -> Dict:
    validate_direct_workspace_transaction(transaction)
    if transaction["status"] != "merging":
        raise ValidationError("direct workspace transaction is not merging")
    candidate = dict(transaction)
    candidate.update(
        {
            "status": "merged",
            "updated_at_utc": updated_at_utc or utc_now(),
        }
    )
    return _finalize(candidate)


def reject_direct_workspace_transaction(
    transaction: Dict,
    error_class: str,
    *,
    updated_at_utc: Optional[str] = None,
) -> Dict:
    validate_direct_workspace_transaction(transaction)
    if transaction["status"] != "prepared":
        raise ValidationError("only a prepared direct workspace transaction can be rejected")
    if not isinstance(error_class, str) or not ERROR_CLASS.fullmatch(error_class):
        raise ValidationError("direct workspace transaction error class is invalid")
    candidate = dict(transaction)
    candidate.update(
        {
            "status": "rejected",
            "updated_at_utc": updated_at_utc or utc_now(),
            "error_class": error_class,
        }
    )
    return _finalize(candidate)


def validate_direct_workspace_transaction(transaction: Dict) -> None:
    if not isinstance(transaction, dict) or set(transaction) != DIRECT_WORKSPACE_TRANSACTION_FIELDS:
        raise ValidationError("direct workspace transaction has invalid fields")
    if transaction.get("schema") != DIRECT_WORKSPACE_TRANSACTION_SCHEMA:
        raise ValidationError("direct workspace transaction schema is invalid")
    status = transaction.get("status")
    if status not in DIRECT_WORKSPACE_TRANSACTION_STATUSES:
        raise ValidationError("direct workspace transaction status is invalid")
    _validate_sha256(
        transaction.get("workflow_fingerprint"),
        "direct workspace workflow fingerprint",
    )
    step_id = transaction.get("step_id")
    if not isinstance(step_id, str) or not SAFE_ID.fullmatch(step_id):
        raise ValidationError("direct workspace transaction step id is invalid")
    attempt = transaction.get("attempt")
    if (
        not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or not 1 <= attempt <= MAX_DIRECT_WORKSPACE_ATTEMPT
    ):
        raise ValidationError("direct workspace transaction attempt is invalid")
    _validate_sha256(
        transaction.get("source_workspace_sha256"),
        "direct workspace source path digest",
    )
    stage_relative = transaction.get("stage_relative")
    if not isinstance(stage_relative, str) or not stage_relative:
        raise ValidationError("direct workspace transaction stage path is invalid")
    require_no_path_escape(stage_relative)
    expected_stage = direct_workspace_stage_relative(
        transaction["workflow_fingerprint"],
        step_id,
        attempt,
    )
    if stage_relative != expected_stage:
        raise ValidationError("direct workspace transaction stage path binding changed")
    workspace_snapshot_from_manifest(transaction.get("base_manifest"))
    for field in ("created_at_utc", "updated_at_utc"):
        value = transaction.get(field)
        if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
            raise ValidationError("direct workspace transaction %s is invalid" % field)
    if transaction["updated_at_utc"] < transaction["created_at_utc"]:
        raise ValidationError("direct workspace transaction timestamps are inconsistent")

    merge_fields = (
        "result_manifest",
        "source_before_manifest",
        "merge_plan",
        "invocation_sha256",
        "terminal_sha256",
        "stop_gate_sha256",
    )
    if status in {"prepared", "rejected"}:
        if any(transaction.get(field) is not None for field in merge_fields):
            raise ValidationError("unmerged direct workspace transaction contains merge evidence")
        if status == "prepared" and transaction.get("error_class") is not None:
            raise ValidationError("prepared direct workspace transaction contains an error")
        if status == "rejected":
            error_class = transaction.get("error_class")
            if not isinstance(error_class, str) or not ERROR_CLASS.fullmatch(error_class):
                raise ValidationError("rejected direct workspace transaction error is invalid")
    else:
        if transaction.get("error_class") is not None and status != "conflict":
            raise ValidationError("accepted direct workspace transaction contains an error")
        result = workspace_snapshot_from_manifest(transaction.get("result_manifest"))
        source_before = workspace_snapshot_from_manifest(
            transaction.get("source_before_manifest")
        )
        plan = transaction.get("merge_plan")
        base = workspace_snapshot_from_manifest(transaction["base_manifest"])
        validate_workspace_merge_plan(
            plan,
            base_snapshot=base,
            incoming_snapshot=result,
        )
        for field in ("invocation_sha256", "terminal_sha256", "stop_gate_sha256"):
            _validate_sha256(transaction.get(field), "direct workspace transaction %s" % field)
        if plan["source_before_sha256"] != source_before.tracked_fingerprint_sha256:
            raise ValidationError("direct workspace transaction source manifest changed")
        if status == "conflict":
            if plan["status"] != "conflict" or transaction.get("error_class") != "WorkspaceMergeConflict":
                raise ValidationError("direct workspace transaction conflict is inconsistent")
        elif plan["status"] == "conflict":
            raise ValidationError("accepted direct workspace transaction contains a conflict")

    if transaction.get("transaction_sha256") != direct_workspace_transaction_sha256(transaction):
        raise ValidationError("direct workspace transaction hash changed")


def direct_workspace_transaction_sha256(transaction: Dict) -> str:
    if not isinstance(transaction, dict):
        raise ValidationError("direct workspace transaction must be an object")
    payload = {
        key: value
        for key, value in transaction.items()
        if key != "transaction_sha256"
    }
    return _sha256_json(payload)


def write_direct_workspace_transaction(run, transaction: Dict) -> Path:
    validate_direct_workspace_transaction(transaction)
    path = direct_workspace_transaction_path(
        run,
        transaction["workflow_fingerprint"],
        transaction["step_id"],
    )
    parent_fd = ensure_dir_no_follow(path.parent, "direct workspace transaction parent")
    os.close(parent_fd)
    serialized = json.dumps(transaction, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if len(serialized.encode("utf-8")) > MAX_DIRECT_WORKSPACE_TRANSACTION_BYTES:
        raise ValidationError("direct workspace transaction exceeds its byte limit")
    try:
        write_new_text_file_no_follow(
            path,
            "direct workspace transaction",
            serialized,
            sync=True,
        )
    except FileExistsError:
        if load_direct_workspace_transaction(path) != transaction:
            raise ValidationError("direct workspace transaction already changed")
    return path


def replace_direct_workspace_transaction(run, transaction: Dict) -> Path:
    validate_direct_workspace_transaction(transaction)
    path = direct_workspace_transaction_path(
        run,
        transaction["workflow_fingerprint"],
        transaction["step_id"],
    )
    if not path.exists() and not path.is_symlink():
        raise ValidationError("direct workspace transaction is missing")
    load_direct_workspace_transaction(path)
    serialized = json.dumps(transaction, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if len(serialized.encode("utf-8")) > MAX_DIRECT_WORKSPACE_TRANSACTION_BYTES:
        raise ValidationError("direct workspace transaction exceeds its byte limit")
    replace_text_file_no_follow(
        path,
        "direct workspace transaction",
        serialized,
        ".direct-workspace-transaction-",
        sync=True,
    )
    return path


def load_direct_workspace_transaction(path: Path) -> Dict:
    reject_symlink_path(path, "direct workspace transaction")
    try:
        transaction = json.loads(
            read_regular_text_file_no_follow(
                path,
                "direct workspace transaction",
                MAX_DIRECT_WORKSPACE_TRANSACTION_BYTES,
            ),
            object_pairs_hook=_reject_duplicate_json_pairs,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError(
            "direct workspace transaction is invalid JSON: %s"
            % exc.__class__.__name__
        )
    validate_direct_workspace_transaction(transaction)
    expected = direct_workspace_transaction_key(
        transaction["workflow_fingerprint"],
        transaction["step_id"],
    ) + ".json"
    if path.name != expected:
        raise ValidationError("direct workspace transaction filename binding changed")
    return transaction


def direct_workspace_transaction_path(run, workflow_fingerprint: str, step_id: str) -> Path:
    key = direct_workspace_transaction_key(workflow_fingerprint, step_id)
    path = run.resolve_artifact_path(".direct-workspace-transactions/%s.json" % key)
    reject_symlink_path(path, "direct workspace transaction")
    return path


def direct_workspace_transaction_key(workflow_fingerprint: str, step_id: str) -> str:
    _validate_sha256(workflow_fingerprint, "direct workspace workflow fingerprint")
    if not isinstance(step_id, str) or not SAFE_ID.fullmatch(step_id):
        raise ValidationError("direct workspace transaction step id is invalid")
    return hashlib.sha256(
        (workflow_fingerprint + "\0" + step_id).encode("utf-8")
    ).hexdigest()


def direct_workspace_stage_relative(
    workflow_fingerprint: str,
    step_id: str,
    attempt: int,
) -> str:
    if (
        not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or not 1 <= attempt <= MAX_DIRECT_WORKSPACE_ATTEMPT
    ):
        raise ValidationError("direct workspace transaction attempt is invalid")
    key = direct_workspace_transaction_key(workflow_fingerprint, step_id)
    return ".direct-workspaces/%s/attempt-%09d" % (key, attempt)


def _finalize(transaction: Dict) -> Dict:
    candidate = dict(transaction)
    candidate["transaction_sha256"] = direct_workspace_transaction_sha256(candidate)
    validate_direct_workspace_transaction(candidate)
    return candidate


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValidationError("%s is invalid" % label)


def _sha256_json(value) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _reject_duplicate_json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key %r" % key)
        value[key] = item
    return value
