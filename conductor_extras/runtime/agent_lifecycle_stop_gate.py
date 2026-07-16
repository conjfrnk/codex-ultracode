import hashlib
import json
import os
import re
from pathlib import Path
from typing import Dict, Iterable, Optional

from .agent_lifecycle_hooks import validate_agent_lifecycle_hook_input
from .artifacts import utc_now
from .errors import ValidationError
from .security import (
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    write_new_text_file_no_follow,
)


AGENT_LIFECYCLE_STOP_GATE_SCHEMA = "conductor.agent_lifecycle_stop_gate.v1"
AGENT_LIFECYCLE_STOP_GATE_STATUSES = {
    "pending",
    "ready",
    "running",
    "passed",
    "failed",
    "uncertain",
}
AGENT_LIFECYCLE_STOP_GATE_FIELDS = {
    "schema",
    "status",
    "workflow_fingerprint",
    "step_id",
    "scope",
    "invocation_sha256",
    "start_input",
    "start_input_sha256",
    "hook_ids",
    "hook_failure_modes",
    "hook_set_sha256",
    "hook_count",
    "stop_input",
    "stop_input_sha256",
    "next_hook_index",
    "active_hook_index",
    "hook_results",
    "warning_count",
    "created_at_utc",
    "updated_at_utc",
    "error_class",
    "gate_sha256",
}
AGENT_LIFECYCLE_STOP_GATE_RESULT_FIELDS = {
    "hook_id",
    "status",
    "stdout_sha256",
    "stdout_truncated",
}
AGENT_LIFECYCLE_STOP_GATE_RESULT_STATUSES = {
    "completed",
    "failed",
    "blocked",
    "planned",
}
MAX_AGENT_LIFECYCLE_STOP_GATE_HOOKS = 64
MAX_AGENT_LIFECYCLE_STOP_GATES = 4096
MAX_AGENT_LIFECYCLE_STOP_GATE_BYTES = 256 * 1024
_AGENT_LIFECYCLE_STOP_GATE_TEMP_DIR = ".agent-lifecycle-stop-gate-temporary"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$")
ERROR_CLASS = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]{0,127}$")


def agent_lifecycle_stop_hook_binding(hooks: Iterable[Dict]) -> Dict:
    hook_list = list(hooks)
    if not 1 <= len(hook_list) <= MAX_AGENT_LIFECYCLE_STOP_GATE_HOOKS:
        raise ValidationError("agent lifecycle stop gate hook count is invalid")
    hook_ids = []
    failure_modes = []
    for hook in hook_list:
        if not isinstance(hook, dict):
            raise ValidationError("agent lifecycle stop gate hook is invalid")
        hook_id = hook.get("id")
        if not isinstance(hook_id, str) or not SAFE_ID.fullmatch(hook_id):
            raise ValidationError("agent lifecycle stop gate hook id is invalid")
        hook_ids.append(hook_id)
        failure_modes.append(hook.get("on_failure", "warn"))
    if len(hook_ids) != len(set(hook_ids)):
        raise ValidationError("agent lifecycle stop gate hook ids must be unique")
    if any(mode not in {"warn", "block"} for mode in failure_modes):
        raise ValidationError("agent lifecycle stop gate hook failure mode is invalid")
    if "block" not in failure_modes:
        raise ValidationError("agent lifecycle stop gate requires a blocking hook")
    hook_set = [
        {"id": hook_id, "on_failure": mode}
        for hook_id, mode in zip(hook_ids, failure_modes)
    ]
    return {
        "hook_ids": hook_ids,
        "hook_failure_modes": failure_modes,
        "hook_set_sha256": _sha256_json(hook_set),
        "hook_count": len(hook_ids),
    }


def has_blocking_agent_lifecycle_stop_hooks(hooks: Iterable[Dict]) -> bool:
    return any(hook.get("on_failure", "warn") == "block" for hook in hooks)


def build_agent_lifecycle_stop_gate(
    start_input: Dict,
    hooks: Iterable[Dict],
    *,
    created_at_utc: Optional[str] = None,
) -> Dict:
    validate_agent_lifecycle_hook_input(start_input)
    if start_input["event"] != "agent_start":
        raise ValidationError("agent lifecycle stop gate requires an agent_start input")
    binding = agent_lifecycle_stop_hook_binding(hooks)
    timestamp = created_at_utc or utc_now()
    gate = {
        "schema": AGENT_LIFECYCLE_STOP_GATE_SCHEMA,
        "status": "pending",
        "workflow_fingerprint": start_input["workflow_fingerprint"],
        "step_id": start_input["step_id"],
        "scope": start_input["scope"],
        "invocation_sha256": start_input["invocation_sha256"],
        "start_input": start_input,
        "start_input_sha256": _sha256_json(start_input),
        **binding,
        "stop_input": None,
        "stop_input_sha256": None,
        "next_hook_index": 0,
        "active_hook_index": None,
        "hook_results": [],
        "warning_count": 0,
        "created_at_utc": timestamp,
        "updated_at_utc": timestamp,
        "error_class": None,
        "gate_sha256": "0" * 64,
    }
    gate["gate_sha256"] = agent_lifecycle_stop_gate_sha256(gate)
    validate_agent_lifecycle_stop_gate(gate)
    return gate


def bind_agent_lifecycle_stop_gate_result(
    gate: Dict,
    stop_input: Dict,
    *,
    updated_at_utc: Optional[str] = None,
) -> Dict:
    validate_agent_lifecycle_stop_gate(gate)
    validate_agent_lifecycle_hook_input(stop_input)
    if gate["status"] != "pending":
        raise ValidationError("agent lifecycle stop gate is not pending")
    if stop_input["event"] != "agent_stop":
        raise ValidationError("agent lifecycle stop gate requires an agent_stop input")
    _validate_stop_input_binding(gate, stop_input)
    candidate = dict(gate)
    candidate.update(
        {
            "status": "ready",
            "stop_input": stop_input,
            "stop_input_sha256": _sha256_json(stop_input),
            "updated_at_utc": updated_at_utc or utc_now(),
        }
    )
    return _finalize(candidate)


def claim_agent_lifecycle_stop_gate_hook(
    gate: Dict,
    *,
    updated_at_utc: Optional[str] = None,
) -> Dict:
    validate_agent_lifecycle_stop_gate(gate)
    if gate["status"] != "ready":
        raise ValidationError("agent lifecycle stop gate is not ready")
    candidate = dict(gate)
    candidate.update(
        {
            "status": "running",
            "active_hook_index": gate["next_hook_index"],
            "updated_at_utc": updated_at_utc or utc_now(),
        }
    )
    return _finalize(candidate)


def complete_agent_lifecycle_stop_gate_hook(
    gate: Dict,
    result: Dict,
    *,
    updated_at_utc: Optional[str] = None,
) -> Dict:
    validate_agent_lifecycle_stop_gate(gate)
    if gate["status"] != "running":
        raise ValidationError("agent lifecycle stop gate hook is not running")
    index = gate["active_hook_index"]
    entry = _build_hook_result(gate, index, result)
    results = list(gate["hook_results"])
    results.append(entry)
    next_index = index + 1
    candidate = dict(gate)
    candidate.update(
        {
            "status": "passed" if next_index == gate["hook_count"] else "ready",
            "next_hook_index": next_index,
            "active_hook_index": None,
            "hook_results": results,
            "warning_count": gate["warning_count"]
            + (1 if entry["status"] not in {"completed", "planned"} else 0),
            "updated_at_utc": updated_at_utc or utc_now(),
        }
    )
    return _finalize(candidate)


def fail_agent_lifecycle_stop_gate(
    gate: Dict,
    error_class: str,
    *,
    updated_at_utc: Optional[str] = None,
) -> Dict:
    return _terminal_failure(gate, "failed", error_class, updated_at_utc)


def mark_agent_lifecycle_stop_gate_uncertain(
    gate: Dict,
    error_class: str = "IndeterminateHookExecution",
    *,
    updated_at_utc: Optional[str] = None,
) -> Dict:
    return _terminal_failure(gate, "uncertain", error_class, updated_at_utc)


def validate_agent_lifecycle_stop_gate(gate: Dict) -> None:
    if not isinstance(gate, dict) or set(gate) != AGENT_LIFECYCLE_STOP_GATE_FIELDS:
        raise ValidationError("agent lifecycle stop gate has invalid fields")
    if gate.get("schema") != AGENT_LIFECYCLE_STOP_GATE_SCHEMA:
        raise ValidationError("agent lifecycle stop gate schema is invalid")
    if gate.get("status") not in AGENT_LIFECYCLE_STOP_GATE_STATUSES:
        raise ValidationError("agent lifecycle stop gate status is invalid")
    for field in (
        "workflow_fingerprint",
        "invocation_sha256",
        "start_input_sha256",
        "hook_set_sha256",
        "gate_sha256",
    ):
        _validate_sha256(gate.get(field), "agent lifecycle stop gate %s" % field)
    if gate.get("stop_input_sha256") is not None:
        _validate_sha256(
            gate["stop_input_sha256"],
            "agent lifecycle stop gate stop_input_sha256",
        )
    if not isinstance(gate.get("step_id"), str) or not SAFE_ID.fullmatch(gate["step_id"]):
        raise ValidationError("agent lifecycle stop gate step id is invalid")

    start_input = gate.get("start_input")
    validate_agent_lifecycle_hook_input(start_input)
    if start_input["event"] != "agent_start":
        raise ValidationError("agent lifecycle stop gate start input is invalid")
    if gate["start_input_sha256"] != _sha256_json(start_input):
        raise ValidationError("agent lifecycle stop gate start input hash changed")
    expected_identity = (
        start_input["workflow_fingerprint"],
        start_input["step_id"],
        start_input["scope"],
        start_input["invocation_sha256"],
    )
    observed_identity = (
        gate["workflow_fingerprint"],
        gate["step_id"],
        gate["scope"],
        gate["invocation_sha256"],
    )
    if observed_identity != expected_identity:
        raise ValidationError("agent lifecycle stop gate identity changed")

    hook_ids = gate.get("hook_ids")
    failure_modes = gate.get("hook_failure_modes")
    hook_count = gate.get("hook_count")
    if (
        not isinstance(hook_ids, list)
        or not isinstance(failure_modes, list)
        or not isinstance(hook_count, int)
        or isinstance(hook_count, bool)
        or not 1 <= hook_count <= MAX_AGENT_LIFECYCLE_STOP_GATE_HOOKS
        or len(hook_ids) != hook_count
        or len(failure_modes) != hook_count
    ):
        raise ValidationError("agent lifecycle stop gate hook binding is invalid")
    if (
        any(not isinstance(value, str) or not SAFE_ID.fullmatch(value) for value in hook_ids)
        or len(hook_ids) != len(set(hook_ids))
        or any(value not in {"warn", "block"} for value in failure_modes)
        or "block" not in failure_modes
    ):
        raise ValidationError("agent lifecycle stop gate hook binding is invalid")
    expected_hook_set_sha256 = _sha256_json(
        [
            {"id": hook_id, "on_failure": mode}
            for hook_id, mode in zip(hook_ids, failure_modes)
        ]
    )
    if gate["hook_set_sha256"] != expected_hook_set_sha256:
        raise ValidationError("agent lifecycle stop gate hook set hash changed")

    for field in ("next_hook_index", "warning_count"):
        value = gate.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValidationError("agent lifecycle stop gate %s is invalid" % field)
    if gate["next_hook_index"] > hook_count:
        raise ValidationError("agent lifecycle stop gate next hook index is invalid")
    active_index = gate.get("active_hook_index")
    if active_index is not None and (
        not isinstance(active_index, int)
        or isinstance(active_index, bool)
        or not 0 <= active_index < hook_count
    ):
        raise ValidationError("agent lifecycle stop gate active hook index is invalid")

    results = gate.get("hook_results")
    if not isinstance(results, list) or len(results) != gate["next_hook_index"]:
        raise ValidationError("agent lifecycle stop gate hook results are invalid")
    warning_count = 0
    for index, result in enumerate(results):
        _validate_hook_result(result)
        if result["hook_id"] != hook_ids[index]:
            raise ValidationError("agent lifecycle stop gate hook result order changed")
        if result["status"] not in {"completed", "planned"}:
            if failure_modes[index] != "warn":
                raise ValidationError("blocking stop hook cannot record an advisory failure")
            warning_count += 1
    if gate["warning_count"] != warning_count:
        raise ValidationError("agent lifecycle stop gate warning count changed")

    for field in ("created_at_utc", "updated_at_utc"):
        value = gate.get(field)
        if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
            raise ValidationError("agent lifecycle stop gate %s is invalid" % field)
    error_class = gate.get("error_class")
    if error_class is not None and (
        not isinstance(error_class, str) or not ERROR_CLASS.fullmatch(error_class)
    ):
        raise ValidationError("agent lifecycle stop gate error class is invalid")

    status = gate["status"]
    stop_input = gate.get("stop_input")
    if status == "pending":
        if (
            stop_input is not None
            or gate["stop_input_sha256"] is not None
            or gate["next_hook_index"] != 0
            or active_index is not None
            or results
            or gate["warning_count"] != 0
            or error_class is not None
        ):
            raise ValidationError("pending agent lifecycle stop gate is inconsistent")
    else:
        validate_agent_lifecycle_hook_input(stop_input)
        if stop_input["event"] != "agent_stop":
            raise ValidationError("agent lifecycle stop gate stop input is invalid")
        _validate_stop_input_binding(gate, stop_input)
        if gate["stop_input_sha256"] != _sha256_json(stop_input):
            raise ValidationError("agent lifecycle stop gate stop input hash changed")
        if status == "ready":
            if gate["next_hook_index"] >= hook_count or active_index is not None or error_class is not None:
                raise ValidationError("ready agent lifecycle stop gate is inconsistent")
        elif status == "running":
            if active_index != gate["next_hook_index"] or error_class is not None:
                raise ValidationError("running agent lifecycle stop gate is inconsistent")
        elif status == "passed":
            if gate["next_hook_index"] != hook_count or active_index is not None or error_class is not None:
                raise ValidationError("passed agent lifecycle stop gate is inconsistent")
        else:
            if active_index != gate["next_hook_index"] or error_class is None:
                raise ValidationError("terminal agent lifecycle stop gate is inconsistent")

    if gate["gate_sha256"] != agent_lifecycle_stop_gate_sha256(gate):
        raise ValidationError("agent lifecycle stop gate hash changed")


def agent_lifecycle_stop_gate_sha256(gate: Dict) -> str:
    if not isinstance(gate, dict):
        raise ValidationError("agent lifecycle stop gate must be an object")
    payload = {key: value for key, value in gate.items() if key != "gate_sha256"}
    return _sha256_json(payload)


def write_agent_lifecycle_stop_gate(run, gate: Dict) -> Path:
    validate_agent_lifecycle_stop_gate(gate)
    path = agent_lifecycle_stop_gate_path(run, gate["invocation_sha256"])
    parent_fd = ensure_dir_no_follow(path.parent, "agent lifecycle stop gate parent")
    os.close(parent_fd)
    _validate_gate_directory_count(path.parent, path.name)
    serialized = json.dumps(gate, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    try:
        write_new_text_file_no_follow(
            path,
            "agent lifecycle stop gate",
            serialized,
            sync=True,
        )
    except FileExistsError:
        if load_agent_lifecycle_stop_gate(path) != gate:
            raise ValidationError("agent lifecycle stop gate already changed")
    return path


def replace_agent_lifecycle_stop_gate(run, gate: Dict) -> Path:
    validate_agent_lifecycle_stop_gate(gate)
    path = agent_lifecycle_stop_gate_path(run, gate["invocation_sha256"])
    if not path.exists() and not path.is_symlink():
        raise ValidationError("agent lifecycle stop gate is missing")
    load_agent_lifecycle_stop_gate(path)
    serialized = json.dumps(gate, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    replace_text_file_no_follow(
        path,
        "agent lifecycle stop gate",
        serialized,
        ".agent-stop-gate-",
        sync=True,
        temp_directory=_agent_lifecycle_stop_gate_temp_dir(run),
    )
    return path


def load_agent_lifecycle_stop_gate(path: Path) -> Dict:
    reject_symlink_path(path, "agent lifecycle stop gate")
    try:
        gate = json.loads(
            read_regular_text_file_no_follow(
                path,
                "agent lifecycle stop gate",
                MAX_AGENT_LIFECYCLE_STOP_GATE_BYTES,
            ),
            object_pairs_hook=_reject_duplicate_json_pairs,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError(
            "agent lifecycle stop gate is invalid JSON: %s" % exc.__class__.__name__
        )
    validate_agent_lifecycle_stop_gate(gate)
    if path.name != gate["invocation_sha256"] + ".json":
        raise ValidationError("agent lifecycle stop gate filename binding changed")
    return gate


def load_agent_lifecycle_stop_gate_for_invocation(run, invocation_sha256: str) -> Dict:
    return load_agent_lifecycle_stop_gate(
        agent_lifecycle_stop_gate_path(run, invocation_sha256)
    )


def list_agent_lifecycle_stop_gates(run) -> list[Dict]:
    directory = agent_lifecycle_stop_gate_dir(run)
    reject_symlink_path(directory, "agent lifecycle stop gate directory")
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValidationError("agent lifecycle stop gate path must be a directory")
    paths = sorted(directory.iterdir(), key=lambda value: value.name)
    if len(paths) > MAX_AGENT_LIFECYCLE_STOP_GATES:
        raise ValidationError("agent lifecycle stop gate count exceeds its limit")
    gates = []
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise ValidationError("agent lifecycle stop gate directory is invalid")
        gates.append(load_agent_lifecycle_stop_gate(path))
    return gates


def agent_lifecycle_stop_gate_path(run, invocation_sha256: str) -> Path:
    _validate_sha256(invocation_sha256, "agent lifecycle stop gate invocation digest")
    path = agent_lifecycle_stop_gate_dir(run) / (invocation_sha256 + ".json")
    reject_symlink_path(path, "agent lifecycle stop gate")
    return path


def agent_lifecycle_stop_gate_dir(run) -> Path:
    path = run.resolve_artifact_path(".agent-lifecycle-stop-gates")
    reject_symlink_path(path, "agent lifecycle stop gate directory")
    return path


def _agent_lifecycle_stop_gate_temp_dir(run) -> Path:
    path = run.resolve_artifact_path(_AGENT_LIFECYCLE_STOP_GATE_TEMP_DIR)
    reject_symlink_path(path, "agent lifecycle stop gate temporary directory")
    return path


def _build_hook_result(gate: Dict, index: int, result: Dict) -> Dict:
    if not isinstance(result, dict):
        raise ValidationError("agent lifecycle stop gate hook result is invalid")
    hook = result.get("hook")
    if not isinstance(hook, dict) or hook.get("id") != gate["hook_ids"][index]:
        raise ValidationError("agent lifecycle stop gate hook result identity changed")
    status = result.get("status")
    stdout = result.get("stdout")
    stdout_truncated = result.get("stdout_truncated")
    if status not in AGENT_LIFECYCLE_STOP_GATE_RESULT_STATUSES:
        raise ValidationError("agent lifecycle stop gate hook result status is invalid")
    if not isinstance(stdout, str) or not isinstance(stdout_truncated, bool):
        raise ValidationError("agent lifecycle stop gate hook result shape is invalid")
    entry = {
        "hook_id": hook["id"],
        "status": status,
        "stdout_sha256": _sha256_text(stdout),
        "stdout_truncated": stdout_truncated,
    }
    _validate_hook_result(entry)
    if status not in {"completed", "planned"} and gate["hook_failure_modes"][index] != "warn":
        raise ValidationError("blocking agent lifecycle stop hook did not pass")
    return entry


def _validate_hook_result(result: Dict) -> None:
    if not isinstance(result, dict) or set(result) != AGENT_LIFECYCLE_STOP_GATE_RESULT_FIELDS:
        raise ValidationError("agent lifecycle stop gate hook result has invalid fields")
    if not isinstance(result.get("hook_id"), str) or not SAFE_ID.fullmatch(result["hook_id"]):
        raise ValidationError("agent lifecycle stop gate hook result id is invalid")
    if result.get("status") not in AGENT_LIFECYCLE_STOP_GATE_RESULT_STATUSES:
        raise ValidationError("agent lifecycle stop gate hook result status is invalid")
    _validate_sha256(result.get("stdout_sha256"), "agent lifecycle stop gate stdout hash")
    if not isinstance(result.get("stdout_truncated"), bool):
        raise ValidationError("agent lifecycle stop gate hook truncation flag is invalid")


def _terminal_failure(
    gate: Dict,
    status: str,
    error_class: str,
    updated_at_utc: Optional[str],
) -> Dict:
    validate_agent_lifecycle_stop_gate(gate)
    if gate["status"] != "running":
        raise ValidationError("agent lifecycle stop gate hook is not running")
    if not isinstance(error_class, str) or not ERROR_CLASS.fullmatch(error_class):
        raise ValidationError("agent lifecycle stop gate error class is invalid")
    candidate = dict(gate)
    candidate.update(
        {
            "status": status,
            "error_class": error_class,
            "updated_at_utc": updated_at_utc or utc_now(),
        }
    )
    return _finalize(candidate)


def _validate_stop_input_binding(gate: Dict, stop_input: Dict) -> None:
    if stop_input.get("invocation_sha256") != gate["invocation_sha256"]:
        raise ValidationError("agent lifecycle stop gate invocation changed")
    start = gate["start_input"]
    excluded = {
        "event",
        "status",
        "returncode",
        "timed_out",
        "interrupted",
        "output_sha256",
        "provider_stdout_sha256",
        "provider_stderr_sha256",
        "error_class",
    }
    for field in start:
        if field not in excluded and stop_input.get(field) != start[field]:
            raise ValidationError("agent lifecycle stop gate stop identity changed")
    if stop_input.get("provider_stdout_sha256") is not None or stop_input.get("provider_stderr_sha256") is not None:
        raise ValidationError("blocking agent lifecycle stop gate cannot bind provider streams")


def _finalize(gate: Dict) -> Dict:
    candidate = dict(gate)
    candidate["gate_sha256"] = agent_lifecycle_stop_gate_sha256(candidate)
    validate_agent_lifecycle_stop_gate(candidate)
    return candidate


def _validate_gate_directory_count(directory: Path, candidate_name: str) -> None:
    reject_symlink_path(directory, "agent lifecycle stop gate directory")
    entries = list(directory.iterdir())
    if len(entries) > MAX_AGENT_LIFECYCLE_STOP_GATES or (
        len(entries) == MAX_AGENT_LIFECYCLE_STOP_GATES
        and all(path.name != candidate_name for path in entries)
    ):
        raise ValidationError("agent lifecycle stop gate count exceeds its limit")
    for path in entries:
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise ValidationError("agent lifecycle stop gate directory is invalid")


def _sha256_json(value) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return _sha256_text(serialized)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValidationError("%s must be a sha256 digest" % label)


def _reject_duplicate_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key %r" % key)
        result[key] = value
    return result
