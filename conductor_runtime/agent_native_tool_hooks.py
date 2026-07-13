import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Dict, List, Optional

from .agent_lifecycle_hooks import (
    AGENT_LIFECYCLE_HOOK_INPUT_FIELDS,
    validate_agent_lifecycle_hook_input,
)
from .agent_tool_policy import command_policy_sha256
from .errors import ValidationError
from .security import (
    ensure_dir_no_follow,
    read_regular_file_bytes_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    require_no_path_escape,
    write_new_text_file_no_follow,
)


AGENT_NATIVE_TOOL_HOOK_EVENT = "agent_pre_tool"
AGENT_NATIVE_TOOL_HOOK_EVENTS = {AGENT_NATIVE_TOOL_HOOK_EVENT}
AGENT_NATIVE_TOOL_INPUT_SCHEMA = "conductor.agent_native_tool_hook_input.v1"
AGENT_NATIVE_TOOL_GATE_SCHEMA = "conductor.agent_native_tool_gate.v1"
AGENT_NATIVE_TOOL_RECEIPT_SCHEMA = "conductor.agent_native_tool_receipt.v1"
AGENT_NATIVE_TOOL_CONFIG_SCHEMA = "conductor.agent_native_tool_config.v1"
AGENT_NATIVE_TOOL_SUMMARY_SCHEMA = "conductor.agent_native_tool_summary.v1"

MAX_AGENT_NATIVE_TOOL_HOOKS = 64
MAX_AGENT_NATIVE_TOOL_NAMES = 32
MAX_AGENT_NATIVE_TOOL_NAME_CHARS = 256
MAX_AGENT_NATIVE_TOOL_INPUT_BYTES = 1024 * 1024
MAX_AGENT_NATIVE_TOOL_CONFIG_BYTES = 512 * 1024
MAX_AGENT_NATIVE_TOOL_GATE_BYTES = 512 * 1024
MAX_AGENT_NATIVE_TOOL_RECEIPT_BYTES = 256 * 1024
MAX_AGENT_NATIVE_TOOL_RECEIPTS = 4096
MAX_AGENT_NATIVE_TOOL_PROGRAM_BYTES = 4 * 1024 * 1024
MAX_AGENT_NATIVE_TOOL_TIMEOUT_SECONDS = 30

SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
SAFE_TOOL = re.compile(r"^(?:\*|[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,255})$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

INPUT_FIELDS = set(AGENT_LIFECYCLE_HOOK_INPUT_FIELDS) - {
    "schema",
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
INPUT_FIELDS |= {
    "schema",
    "event",
    "tool_name",
    "tool_use_id_sha256",
    "tool_input",
    "tool_input_sha256",
}

GATE_FIELDS = {
    "schema",
    "status",
    "start_input",
    "start_input_sha256",
    "workflow_fingerprint",
    "invocation_sha256",
    "step_id",
    "scope",
    "hook_ids",
    "hook_failure_modes",
    "hook_tools",
    "hook_program_sha256s",
    "hook_timeouts",
    "hook_set_sha256",
    "bridge_sha256",
    "config_sha256",
    "command_policy_sha256",
    "receipt_count",
    "receipt_sha256s",
    "passed_count",
    "denied_count",
    "warning_count",
    "created_at_utc",
    "updated_at_utc",
    "error_class",
    "gate_sha256",
}

RECEIPT_FIELDS = {
    "schema",
    "status",
    "workflow_fingerprint",
    "invocation_sha256",
    "step_id",
    "scope",
    "tool_name",
    "tool_use_id_sha256",
    "tool_input_sha256",
    "hook_ids",
    "hook_failure_modes",
    "hook_program_sha256s",
    "next_hook_index",
    "active_hook_index",
    "hook_results",
    "warning_count",
    "created_at_utc",
    "updated_at_utc",
    "error_class",
    "receipt_sha256",
}

RECEIPT_RESULT_FIELDS = {
    "hook_id",
    "status",
    "returncode",
    "timed_out",
}

CONFIG_FIELDS = {
    "schema",
    "start_input",
    "run_dir",
    "receipt_dir_relative",
    "hooks",
    "command_policy",
    "command_policy_sha256",
    "config_sha256",
}

CONFIG_HOOK_FIELDS = {
    "id",
    "on_failure",
    "tools",
    "program_relative",
    "program_sha256",
    "timeout_seconds",
}


def build_agent_native_tool_input(
    start_input: Dict,
    *,
    tool_name: str,
    tool_use_id: str,
    tool_input,
) -> Dict:
    validate_agent_lifecycle_hook_input(start_input)
    if start_input["event"] != "agent_start":
        raise ValidationError("agent native tool input requires agent_start identity")
    _validate_tool_name(tool_name)
    if not isinstance(tool_use_id, str) or not tool_use_id or len(tool_use_id) > 512:
        raise ValidationError("agent native tool use id is invalid")
    tool_input_sha256 = _sha256_json(tool_input)
    payload = {
        key: start_input[key]
        for key in sorted(INPUT_FIELDS & set(start_input))
    }
    payload.update(
        {
            "schema": AGENT_NATIVE_TOOL_INPUT_SCHEMA,
            "event": AGENT_NATIVE_TOOL_HOOK_EVENT,
            "tool_name": tool_name,
            "tool_use_id_sha256": _sha256_text(tool_use_id),
            "tool_input": json.loads(json.dumps(tool_input)),
            "tool_input_sha256": tool_input_sha256,
        }
    )
    validate_agent_native_tool_input(payload)
    return payload


def validate_agent_native_tool_input(payload: Dict) -> None:
    if not isinstance(payload, dict) or set(payload) != INPUT_FIELDS:
        raise ValidationError("agent native tool hook input has invalid fields")
    if payload.get("schema") != AGENT_NATIVE_TOOL_INPUT_SCHEMA:
        raise ValidationError("agent native tool hook input schema is invalid")
    if payload.get("event") != AGENT_NATIVE_TOOL_HOOK_EVENT:
        raise ValidationError("agent native tool hook input event is invalid")
    _validate_tool_name(payload.get("tool_name"))
    _validate_sha256(payload.get("tool_use_id_sha256"), "tool use id")
    _validate_sha256(payload.get("tool_input_sha256"), "tool input")
    if payload["tool_input_sha256"] != _sha256_json(payload.get("tool_input")):
        raise ValidationError("agent native tool input hash is invalid")
    if len(_canonical_bytes(payload)) > MAX_AGENT_NATIVE_TOOL_INPUT_BYTES:
        raise ValidationError("agent native tool hook input is too large")
    start = {
        key: payload.get(key)
        for key in AGENT_LIFECYCLE_HOOK_INPUT_FIELDS
    }
    start.update(
        {
            "schema": "conductor.agent_lifecycle_hook_input.v1",
            "event": "agent_start",
            "status": "starting",
            "returncode": None,
            "timed_out": False,
            "interrupted": False,
            "output_sha256": None,
            "provider_stdout_sha256": None,
            "provider_stderr_sha256": None,
            "error_class": None,
        }
    )
    validate_agent_lifecycle_hook_input(start)


def agent_native_tool_input_json(payload: Dict) -> str:
    validate_agent_native_tool_input(payload)
    return _canonical_bytes(payload).decode("utf-8") + "\n"


def build_agent_native_tool_config(
    *,
    start_input: Dict,
    run_dir: Path,
    hooks: List[Dict],
    command_policy: Optional[Dict] = None,
) -> Dict:
    validate_agent_lifecycle_hook_input(start_input)
    if start_input["event"] != "agent_start":
        raise ValidationError("agent native tool config requires agent_start identity")
    if not Path(run_dir).is_absolute():
        raise ValidationError("agent native tool run directory must be absolute")
    cleaned_hooks = []
    for hook in hooks:
        cleaned = {
            "id": hook["id"],
            "on_failure": hook.get("on_failure", "block"),
            "tools": list(hook["tools"]),
            "program_relative": hook["program_relative"],
            "program_sha256": hook["program_sha256"],
            "timeout_seconds": int(hook.get("timeout_seconds", 5)),
        }
        cleaned_hooks.append(cleaned)
    config = {
        "schema": AGENT_NATIVE_TOOL_CONFIG_SCHEMA,
        "start_input": json.loads(json.dumps(start_input)),
        "run_dir": str(Path(run_dir).resolve()),
        "receipt_dir_relative": _receipt_dir_relative(start_input["invocation_sha256"]),
        "hooks": cleaned_hooks,
        "command_policy": (
            json.loads(json.dumps(command_policy))
            if command_policy is not None
            else None
        ),
        "command_policy_sha256": (
            command_policy_sha256(command_policy)
            if command_policy is not None
            else None
        ),
        "config_sha256": "0" * 64,
    }
    config["config_sha256"] = _self_hash(config, "config_sha256")
    validate_agent_native_tool_config(config)
    return config


def validate_agent_native_tool_config(config: Dict) -> None:
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
        raise ValidationError("agent native tool config has invalid fields")
    if config.get("schema") != AGENT_NATIVE_TOOL_CONFIG_SCHEMA:
        raise ValidationError("agent native tool config schema is invalid")
    start = config.get("start_input")
    validate_agent_lifecycle_hook_input(start)
    if start["event"] != "agent_start":
        raise ValidationError("agent native tool config start identity is invalid")
    run_dir = config.get("run_dir")
    if not isinstance(run_dir, str) or not Path(run_dir).is_absolute() or "\x00" in run_dir:
        raise ValidationError("agent native tool config run directory is invalid")
    expected_receipts = _receipt_dir_relative(start["invocation_sha256"])
    if config.get("receipt_dir_relative") != expected_receipts:
        raise ValidationError("agent native tool config receipt directory changed")
    hooks = config.get("hooks")
    if not isinstance(hooks, list) or not hooks or len(hooks) > MAX_AGENT_NATIVE_TOOL_HOOKS:
        raise ValidationError("agent native tool config hooks are invalid")
    seen = set()
    for hook in hooks:
        if not isinstance(hook, dict) or set(hook) != CONFIG_HOOK_FIELDS:
            raise ValidationError("agent native tool config hook has invalid fields")
        hook_id = hook.get("id")
        if not isinstance(hook_id, str) or not SAFE_ID.fullmatch(hook_id) or hook_id in seen:
            raise ValidationError("agent native tool config hook id is invalid")
        seen.add(hook_id)
        if hook.get("on_failure") not in {"block", "warn"}:
            raise ValidationError("agent native tool config hook failure mode is invalid")
        validate_agent_native_tool_names(hook.get("tools"), "agent native tool config tools")
        relative = hook.get("program_relative")
        if not isinstance(relative, str) or not relative:
            raise ValidationError("agent native tool config program path is invalid")
        require_no_path_escape(relative)
        _validate_sha256(hook.get("program_sha256"), "agent native tool program")
        timeout = hook.get("timeout_seconds")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_AGENT_NATIVE_TOOL_TIMEOUT_SECONDS:
            raise ValidationError("agent native tool config timeout is invalid")
    policy = config.get("command_policy")
    policy_sha256 = config.get("command_policy_sha256")
    if policy is None:
        if policy_sha256 is not None:
            raise ValidationError("agent native tool command policy binding is invalid")
    else:
        if command_policy_sha256(policy) != policy_sha256:
            raise ValidationError("agent native tool command policy hash is invalid")
    if config.get("config_sha256") != _self_hash(config, "config_sha256"):
        raise ValidationError("agent native tool config hash is invalid")
    if len(_canonical_bytes(config)) > MAX_AGENT_NATIVE_TOOL_CONFIG_BYTES:
        raise ValidationError("agent native tool config is too large")


def write_agent_native_tool_config(run, config: Dict) -> Path:
    validate_agent_native_tool_config(config)
    path = agent_native_tool_config_path(run, config["start_input"]["invocation_sha256"])
    write_new_text_file_no_follow(
        path,
        "agent native tool config",
        _canonical_bytes(config).decode("utf-8") + "\n",
        mode=0o600,
        sync=True,
    )
    return path


def load_agent_native_tool_config(path: Path) -> Dict:
    config = _load_json(path, "agent native tool config", MAX_AGENT_NATIVE_TOOL_CONFIG_BYTES)
    validate_agent_native_tool_config(config)
    return config


def build_agent_native_tool_gate(
    *,
    start_input: Dict,
    config: Dict,
    bridge_sha256: str,
    created_at_utc: str,
) -> Dict:
    validate_agent_native_tool_config(config)
    if config["start_input"] != start_input:
        raise ValidationError("agent native tool gate config identity changed")
    _validate_sha256(bridge_sha256, "agent native tool bridge")
    hooks = config["hooks"]
    gate = {
        "schema": AGENT_NATIVE_TOOL_GATE_SCHEMA,
        "status": "prepared",
        "start_input": json.loads(json.dumps(start_input)),
        "start_input_sha256": _sha256_json(start_input),
        "workflow_fingerprint": start_input["workflow_fingerprint"],
        "invocation_sha256": start_input["invocation_sha256"],
        "step_id": start_input["step_id"],
        "scope": start_input["scope"],
        "hook_ids": [hook["id"] for hook in hooks],
        "hook_failure_modes": [hook["on_failure"] for hook in hooks],
        "hook_tools": [list(hook["tools"]) for hook in hooks],
        "hook_program_sha256s": [hook["program_sha256"] for hook in hooks],
        "hook_timeouts": [hook["timeout_seconds"] for hook in hooks],
        "hook_set_sha256": _sha256_json(hooks),
        "bridge_sha256": bridge_sha256,
        "config_sha256": config["config_sha256"],
        "command_policy_sha256": config["command_policy_sha256"],
        "receipt_count": 0,
        "receipt_sha256s": [],
        "passed_count": 0,
        "denied_count": 0,
        "warning_count": 0,
        "created_at_utc": created_at_utc,
        "updated_at_utc": created_at_utc,
        "error_class": None,
        "gate_sha256": "0" * 64,
    }
    gate["gate_sha256"] = _self_hash(gate, "gate_sha256")
    validate_agent_native_tool_gate(gate)
    return gate


def validate_agent_native_tool_gate(gate: Dict) -> None:
    if not isinstance(gate, dict) or set(gate) != GATE_FIELDS:
        raise ValidationError("agent native tool gate has invalid fields")
    if gate.get("schema") != AGENT_NATIVE_TOOL_GATE_SCHEMA:
        raise ValidationError("agent native tool gate schema is invalid")
    if gate.get("status") not in {"prepared", "settled", "failed"}:
        raise ValidationError("agent native tool gate status is invalid")
    start = gate.get("start_input")
    validate_agent_lifecycle_hook_input(start)
    if start["event"] != "agent_start":
        raise ValidationError("agent native tool gate start identity is invalid")
    expected = {
        "start_input_sha256": _sha256_json(start),
        "workflow_fingerprint": start["workflow_fingerprint"],
        "invocation_sha256": start["invocation_sha256"],
        "step_id": start["step_id"],
        "scope": start["scope"],
    }
    if any(gate.get(key) != value for key, value in expected.items()):
        raise ValidationError("agent native tool gate invocation binding changed")
    count = len(gate.get("hook_ids", []))
    if not 1 <= count <= MAX_AGENT_NATIVE_TOOL_HOOKS:
        raise ValidationError("agent native tool gate hook count is invalid")
    arrays = (
        gate.get("hook_failure_modes"),
        gate.get("hook_tools"),
        gate.get("hook_program_sha256s"),
        gate.get("hook_timeouts"),
    )
    if any(not isinstance(value, list) or len(value) != count for value in arrays):
        raise ValidationError("agent native tool gate hook arrays are inconsistent")
    if len(set(gate["hook_ids"])) != count or not all(
        isinstance(value, str) and SAFE_ID.fullmatch(value) for value in gate["hook_ids"]
    ):
        raise ValidationError("agent native tool gate hook ids are invalid")
    if not all(value in {"block", "warn"} for value in gate["hook_failure_modes"]):
        raise ValidationError("agent native tool gate failure modes are invalid")
    for tools in gate["hook_tools"]:
        validate_agent_native_tool_names(tools, "agent native tool gate tools")
    for digest in gate["hook_program_sha256s"]:
        _validate_sha256(digest, "agent native tool gate program")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_AGENT_NATIVE_TOOL_TIMEOUT_SECONDS
        for value in gate["hook_timeouts"]
    ):
        raise ValidationError("agent native tool gate timeouts are invalid")
    for field in ("hook_set_sha256", "bridge_sha256", "config_sha256", "gate_sha256"):
        _validate_sha256(gate.get(field), "agent native tool gate %s" % field)
    command_policy_digest = gate.get("command_policy_sha256")
    if command_policy_digest is not None:
        _validate_sha256(command_policy_digest, "agent native tool gate command policy")
    receipt_count = gate.get("receipt_count")
    if not isinstance(receipt_count, int) or isinstance(receipt_count, bool) or not 0 <= receipt_count <= MAX_AGENT_NATIVE_TOOL_RECEIPTS:
        raise ValidationError("agent native tool gate receipt count is invalid")
    receipts = gate.get("receipt_sha256s")
    if not isinstance(receipts, list) or len(receipts) != receipt_count or receipts != sorted(receipts):
        raise ValidationError("agent native tool gate receipt hashes are invalid")
    if len(set(receipts)) != len(receipts):
        raise ValidationError("agent native tool gate receipt hashes are duplicated")
    for digest in receipts:
        _validate_sha256(digest, "agent native tool gate receipt")
    counts = [gate.get(name) for name in ("passed_count", "denied_count", "warning_count")]
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts):
        raise ValidationError("agent native tool gate counters are invalid")
    if gate["passed_count"] + gate["denied_count"] != receipt_count:
        raise ValidationError("agent native tool gate receipt counters are inconsistent")
    if gate["warning_count"] > count * max(1, receipt_count):
        raise ValidationError("agent native tool gate warning count is invalid")
    if gate["status"] == "prepared":
        if receipt_count or any(counts) or gate.get("error_class") is not None:
            raise ValidationError("prepared agent native tool gate has terminal evidence")
    elif gate["status"] == "settled":
        if gate.get("error_class") is not None:
            raise ValidationError("settled agent native tool gate has an error")
    else:
        if not isinstance(gate.get("error_class"), str) or not gate["error_class"] or len(gate["error_class"]) > 128:
            raise ValidationError("failed agent native tool gate lacks an error")
    if gate.get("gate_sha256") != _self_hash(gate, "gate_sha256"):
        raise ValidationError("agent native tool gate hash is invalid")
    if len(_canonical_bytes(gate)) > MAX_AGENT_NATIVE_TOOL_GATE_BYTES:
        raise ValidationError("agent native tool gate is too large")


def write_agent_native_tool_gate(run, gate: Dict) -> Path:
    validate_agent_native_tool_gate(gate)
    path = agent_native_tool_gate_path(run, gate["invocation_sha256"])
    write_new_text_file_no_follow(
        path,
        "agent native tool gate",
        _canonical_bytes(gate).decode("utf-8") + "\n",
        mode=0o600,
        sync=True,
    )
    return path


def replace_agent_native_tool_gate(run, gate: Dict) -> Path:
    validate_agent_native_tool_gate(gate)
    path = agent_native_tool_gate_path(run, gate["invocation_sha256"])
    replace_text_file_no_follow(
        path,
        "agent native tool gate",
        _canonical_bytes(gate).decode("utf-8") + "\n",
        ".agent-native-tool-gate-",
        mode=0o600,
        sync=True,
    )
    return path


def load_agent_native_tool_gate(path: Path) -> Dict:
    gate = _load_json(path, "agent native tool gate", MAX_AGENT_NATIVE_TOOL_GATE_BYTES)
    validate_agent_native_tool_gate(gate)
    return gate


def list_agent_native_tool_gates(run) -> List[Dict]:
    directory = run.artifacts_dir / ".agent-native-tool-hooks" / "gates"
    reject_symlink_path(directory, "agent native tool gate directory")
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValidationError("agent native tool gate directory is invalid")
    paths = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if (
                entry.is_symlink()
                or not entry.is_file(follow_symlinks=False)
                or not entry.name.endswith(".json")
            ):
                raise ValidationError(
                    "agent native tool gate directory contains an invalid entry"
                )
            paths.append(Path(entry.path))
            if len(paths) > MAX_AGENT_NATIVE_TOOL_RECEIPTS:
                raise ValidationError("agent native tool gate count exceeds its limit")
    gates = [load_agent_native_tool_gate(path) for path in sorted(paths)]
    for path, gate in zip(sorted(paths), gates):
        if path.name != gate["invocation_sha256"] + ".json":
            raise ValidationError("agent native tool gate filename changed")
    return gates


def summarize_agent_native_tool_gates(run) -> Dict:
    gates = list_agent_native_tool_gates(run)
    prepared_count = sum(gate["status"] == "prepared" for gate in gates)
    settled_count = sum(gate["status"] == "settled" for gate in gates)
    failed_count = sum(gate["status"] == "failed" for gate in gates)
    status = (
        "not-configured"
        if not gates
        else "failed"
        if failed_count
        else "prepared"
        if prepared_count
        else "settled"
    )
    summary = {
        "schema": AGENT_NATIVE_TOOL_SUMMARY_SCHEMA,
        "status": status,
        "gate_count": len(gates),
        "prepared_gate_count": prepared_count,
        "settled_gate_count": settled_count,
        "failed_gate_count": failed_count,
        "receipt_count": sum(gate["receipt_count"] for gate in gates),
        "passed_count": sum(gate["passed_count"] for gate in gates),
        "denied_count": sum(gate["denied_count"] for gate in gates),
        "warning_count": sum(gate["warning_count"] for gate in gates),
        "verifier_binding_count": sum(len(gate["hook_ids"]) for gate in gates),
        "command_policy_gate_count": sum(
            gate["command_policy_sha256"] is not None for gate in gates
        ),
        "gate_sha256s": sorted(gate["gate_sha256"] for gate in gates),
        "bridge_sha256s": sorted({gate["bridge_sha256"] for gate in gates}),
        "hook_set_sha256s": sorted({gate["hook_set_sha256"] for gate in gates}),
        "updated_at_utc": max(
            (gate["updated_at_utc"] for gate in gates),
            default="",
        ),
        "summary_sha256": "0" * 64,
    }
    summary["summary_sha256"] = _self_hash(summary, "summary_sha256")
    validate_agent_native_tool_summary(summary)
    return summary


def validate_agent_native_tool_summary(summary: Dict) -> None:
    fields = {
        "schema",
        "status",
        "gate_count",
        "prepared_gate_count",
        "settled_gate_count",
        "failed_gate_count",
        "receipt_count",
        "passed_count",
        "denied_count",
        "warning_count",
        "verifier_binding_count",
        "command_policy_gate_count",
        "gate_sha256s",
        "bridge_sha256s",
        "hook_set_sha256s",
        "updated_at_utc",
        "summary_sha256",
    }
    if not isinstance(summary, dict) or set(summary) != fields:
        raise ValidationError("agent native tool summary has invalid fields")
    if summary.get("schema") != AGENT_NATIVE_TOOL_SUMMARY_SCHEMA:
        raise ValidationError("agent native tool summary schema is invalid")
    if summary.get("status") not in {
        "not-configured",
        "prepared",
        "settled",
        "failed",
    }:
        raise ValidationError("agent native tool summary status is invalid")
    counts = [
        summary.get(field)
        for field in fields
        if field.endswith("_count") or field == "gate_count"
    ]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in counts
    ):
        raise ValidationError("agent native tool summary count is invalid")
    gate_count = summary["gate_count"]
    if (
        summary["prepared_gate_count"]
        + summary["settled_gate_count"]
        + summary["failed_gate_count"]
        != gate_count
        or summary["passed_count"] + summary["denied_count"]
        != summary["receipt_count"]
        or summary["command_policy_gate_count"] > gate_count
        or summary["verifier_binding_count"]
        > gate_count * MAX_AGENT_NATIVE_TOOL_HOOKS
    ):
        raise ValidationError("agent native tool summary counters are inconsistent")
    expected_status = (
        "not-configured"
        if gate_count == 0
        else "failed"
        if summary["failed_gate_count"]
        else "prepared"
        if summary["prepared_gate_count"]
        else "settled"
    )
    if summary["status"] != expected_status:
        raise ValidationError("agent native tool summary status is inconsistent")
    for field in ("gate_sha256s", "bridge_sha256s", "hook_set_sha256s"):
        values = summary.get(field)
        if (
            not isinstance(values, list)
            or values != sorted(set(values))
            or len(values) > gate_count
        ):
            raise ValidationError("agent native tool summary hashes are invalid")
        for value in values:
            _validate_sha256(value, "agent native tool summary hash")
    if bool(gate_count) != bool(summary["bridge_sha256s"]):
        raise ValidationError("agent native tool summary bridge binding is invalid")
    if len(summary["gate_sha256s"]) != gate_count:
        raise ValidationError("agent native tool summary gate binding is invalid")
    updated_at = summary.get("updated_at_utc")
    if gate_count:
        if not isinstance(updated_at, str) or not updated_at:
            raise ValidationError("agent native tool summary timestamp is invalid")
    elif updated_at != "":
        raise ValidationError("agent native tool empty summary has a timestamp")
    if summary.get("summary_sha256") != _self_hash(summary, "summary_sha256"):
        raise ValidationError("agent native tool summary hash is invalid")


def validate_agent_native_tool_receipt(receipt: Dict) -> None:
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS:
        raise ValidationError("agent native tool receipt has invalid fields")
    if receipt.get("schema") != AGENT_NATIVE_TOOL_RECEIPT_SCHEMA:
        raise ValidationError("agent native tool receipt schema is invalid")
    if receipt.get("status") not in {"running", "passed", "denied"}:
        raise ValidationError("agent native tool receipt status is invalid")
    for field in (
        "workflow_fingerprint",
        "invocation_sha256",
        "tool_use_id_sha256",
        "tool_input_sha256",
        "receipt_sha256",
    ):
        _validate_sha256(receipt.get(field), "agent native tool receipt %s" % field)
    if not isinstance(receipt.get("step_id"), str) or not SAFE_ID.fullmatch(receipt["step_id"]):
        raise ValidationError("agent native tool receipt step id is invalid")
    if not isinstance(receipt.get("scope"), str) or not receipt["scope"]:
        raise ValidationError("agent native tool receipt scope is invalid")
    _validate_tool_name(receipt.get("tool_name"))
    count = len(receipt.get("hook_ids", []))
    if not 1 <= count <= MAX_AGENT_NATIVE_TOOL_HOOKS:
        raise ValidationError("agent native tool receipt hook count is invalid")
    for field in ("hook_failure_modes", "hook_program_sha256s"):
        if not isinstance(receipt.get(field), list) or len(receipt[field]) != count:
            raise ValidationError("agent native tool receipt hook arrays are inconsistent")
    if len(set(receipt["hook_ids"])) != count or not all(
        isinstance(value, str) and SAFE_ID.fullmatch(value) for value in receipt["hook_ids"]
    ):
        raise ValidationError("agent native tool receipt hook ids are invalid")
    if not all(value in {"block", "warn"} for value in receipt["hook_failure_modes"]):
        raise ValidationError("agent native tool receipt failure modes are invalid")
    for digest in receipt["hook_program_sha256s"]:
        _validate_sha256(digest, "agent native tool receipt program")
    next_index = receipt.get("next_hook_index")
    active = receipt.get("active_hook_index")
    if not isinstance(next_index, int) or isinstance(next_index, bool) or not 0 <= next_index <= count:
        raise ValidationError("agent native tool receipt next index is invalid")
    if active is not None and (
        not isinstance(active, int) or isinstance(active, bool) or not 0 <= active < count
    ):
        raise ValidationError("agent native tool receipt active index is invalid")
    results = receipt.get("hook_results")
    if not isinstance(results, list) or len(results) != next_index:
        raise ValidationError("agent native tool receipt results are invalid")
    for index, result in enumerate(results):
        if not isinstance(result, dict) or set(result) != RECEIPT_RESULT_FIELDS:
            raise ValidationError("agent native tool receipt result has invalid fields")
        if result.get("hook_id") != receipt["hook_ids"][index]:
            raise ValidationError("agent native tool receipt result order changed")
        if result.get("status") not in {"completed", "failed", "timed_out"}:
            raise ValidationError("agent native tool receipt result status is invalid")
        returncode = result.get("returncode")
        if returncode is not None and (not isinstance(returncode, int) or isinstance(returncode, bool)):
            raise ValidationError("agent native tool receipt return code is invalid")
        if not isinstance(result.get("timed_out"), bool):
            raise ValidationError("agent native tool receipt timeout flag is invalid")
        if (result["status"] == "timed_out") != result["timed_out"]:
            raise ValidationError("agent native tool receipt timeout result is inconsistent")
        if result["status"] == "completed" and returncode != 0:
            raise ValidationError("agent native tool completed result is invalid")
        if result["status"] == "failed" and (returncode is None or returncode == 0):
            raise ValidationError("agent native tool failed result is invalid")
        if result["status"] == "timed_out" and returncode is not None:
            raise ValidationError("agent native tool timed out result is invalid")
    warnings = receipt.get("warning_count")
    if not isinstance(warnings, int) or isinstance(warnings, bool) or not 0 <= warnings <= count:
        raise ValidationError("agent native tool receipt warning count is invalid")
    if receipt["status"] == "running":
        if active is None or next_index >= count or receipt.get("error_class") is not None:
            raise ValidationError("running agent native tool receipt is inconsistent")
    else:
        if active is not None:
            raise ValidationError("terminal agent native tool receipt has an active hook")
        if receipt["status"] == "passed" and next_index != count:
            raise ValidationError("passed agent native tool receipt is incomplete")
        if receipt["status"] == "denied" and not (
            0 < next_index <= count
            and receipt["hook_failure_modes"][next_index - 1] == "block"
            and results[-1]["status"] != "completed"
        ):
            raise ValidationError("denied agent native tool receipt is inconsistent")
        if receipt.get("error_class") is not None:
            raise ValidationError("terminal agent native tool receipt has an internal error")
    for field in ("created_at_utc", "updated_at_utc"):
        if not isinstance(receipt.get(field), str) or not receipt[field]:
            raise ValidationError("agent native tool receipt timestamp is invalid")
    if receipt.get("receipt_sha256") != _self_hash(receipt, "receipt_sha256"):
        raise ValidationError("agent native tool receipt hash is invalid")
    if len(_canonical_bytes(receipt)) > MAX_AGENT_NATIVE_TOOL_RECEIPT_BYTES:
        raise ValidationError("agent native tool receipt is too large")


def load_agent_native_tool_receipt(path: Path) -> Dict:
    receipt = _load_json(path, "agent native tool receipt", MAX_AGENT_NATIVE_TOOL_RECEIPT_BYTES)
    validate_agent_native_tool_receipt(receipt)
    expected_name = _receipt_name(receipt["invocation_sha256"], receipt["tool_use_id_sha256"])
    if path.name != expected_name:
        raise ValidationError("agent native tool receipt filename changed")
    return receipt


def settle_agent_native_tool_gate(run, gate: Dict, *, updated_at_utc: str) -> Dict:
    validate_agent_native_tool_gate(gate)
    path = agent_native_tool_gate_path(run, gate["invocation_sha256"])
    current = load_agent_native_tool_gate(path)
    _validate_gate_binding(current, gate)
    config = load_agent_native_tool_config(
        agent_native_tool_config_path(run, gate["invocation_sha256"])
    )
    if (
        config["config_sha256"] != gate["config_sha256"]
        or config["start_input"] != gate["start_input"]
        or config["command_policy_sha256"] != gate["command_policy_sha256"]
    ):
        return _fail_gate(run, current, "ConfigBindingChanged", updated_at_utc)
    bridge_path = agent_native_tool_bridge_path(run)
    bridge_raw = read_regular_file_bytes_no_follow(
        bridge_path,
        "agent native tool bridge",
        max_bytes=MAX_AGENT_NATIVE_TOOL_PROGRAM_BYTES,
    )
    if hashlib.sha256(bridge_raw).hexdigest() != gate["bridge_sha256"]:
        return _fail_gate(run, current, "BridgeChanged", updated_at_utc)
    for hook in config["hooks"]:
        program = Path(config["run_dir"]) / hook["program_relative"]
        raw = read_regular_file_bytes_no_follow(
            program,
            "agent native tool program",
            max_bytes=MAX_AGENT_NATIVE_TOOL_PROGRAM_BYTES,
        )
        if hashlib.sha256(raw).hexdigest() != hook["program_sha256"]:
            return _fail_gate(run, current, "ProgramChanged", updated_at_utc)
    receipts = list_agent_native_tool_receipts(run, gate["invocation_sha256"])
    for receipt in receipts:
        _validate_receipt_gate_binding(receipt, gate)
        if receipt["status"] == "running":
            return _fail_gate(run, current, "ReceiptIndeterminate", updated_at_utc)
    receipt_hashes = sorted(receipt["receipt_sha256"] for receipt in receipts)
    passed = sum(receipt["status"] == "passed" for receipt in receipts)
    denied = sum(receipt["status"] == "denied" for receipt in receipts)
    warnings = sum(receipt["warning_count"] for receipt in receipts)
    expected_terminal = {
        "receipt_count": len(receipts),
        "receipt_sha256s": receipt_hashes,
        "passed_count": passed,
        "denied_count": denied,
        "warning_count": warnings,
    }
    if current["status"] == "settled":
        if any(current[key] != value for key, value in expected_terminal.items()):
            raise ValidationError("settled agent native tool gate receipts changed")
        return current
    if current["status"] == "failed":
        raise ValidationError("agent native tool gate is failed")
    settled = dict(current)
    settled.update(expected_terminal)
    settled["status"] = "settled"
    settled["updated_at_utc"] = updated_at_utc
    settled["error_class"] = None
    settled["gate_sha256"] = _self_hash(settled, "gate_sha256")
    replace_agent_native_tool_gate(run, settled)
    return settled


def require_settled_agent_native_tool_gate(run, expected: Dict) -> Dict:
    validate_agent_native_tool_gate(expected)
    gate = load_agent_native_tool_gate(
        agent_native_tool_gate_path(run, expected["invocation_sha256"])
    )
    _validate_gate_binding(gate, expected)
    if gate["status"] != "settled":
        raise ValidationError("agent native tool gate is not settled")
    return settle_agent_native_tool_gate(run, gate, updated_at_utc=gate["updated_at_utc"])


def list_agent_native_tool_receipts(run, invocation_sha256: str) -> List[Dict]:
    _validate_sha256(invocation_sha256, "agent native tool invocation")
    directory = run.run_dir / _receipt_dir_relative(invocation_sha256)
    reject_symlink_path(directory, "agent native tool receipt directory")
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValidationError("agent native tool receipt directory is invalid")
    paths = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".json"):
                raise ValidationError("agent native tool receipt directory contains an invalid entry")
            paths.append(Path(entry.path))
            if len(paths) > MAX_AGENT_NATIVE_TOOL_RECEIPTS:
                raise ValidationError("agent native tool receipt count exceeds its limit")
    receipts = [load_agent_native_tool_receipt(path) for path in sorted(paths)]
    if any(receipt["invocation_sha256"] != invocation_sha256 for receipt in receipts):
        raise ValidationError("agent native tool receipt invocation changed")
    return receipts


def validate_agent_native_tool_names(value, source: str) -> None:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_AGENT_NATIVE_TOOL_NAMES
        or len(value) != len(set(value))
        or not all(isinstance(item, str) and SAFE_TOOL.fullmatch(item) for item in value)
    ):
        raise ValidationError(
            "%s must contain 1 to %d unique native tool names or *"
            % (source, MAX_AGENT_NATIVE_TOOL_NAMES)
        )


def agent_native_tool_gate_path(run, invocation_sha256: str) -> Path:
    _validate_sha256(invocation_sha256, "agent native tool invocation")
    relative = ".agent-native-tool-hooks/gates/%s.json" % invocation_sha256
    return run.resolve_artifact_path(relative)


def agent_native_tool_config_path(run, invocation_sha256: str) -> Path:
    _validate_sha256(invocation_sha256, "agent native tool invocation")
    relative = ".agent-native-tool-hooks/configs/%s.json" % invocation_sha256
    return run.resolve_artifact_path(relative)


def agent_native_tool_bridge_path(run) -> Path:
    return run.resolve_artifact_path(".agent-native-tool-hooks/bridge.py")


def agent_native_tool_program_relative(digest: str) -> str:
    _validate_sha256(digest, "agent native tool program")
    relative = "artifacts/.agent-native-tool-hooks/programs/%s" % digest
    require_no_path_escape(relative)
    return relative


def agent_native_tool_program_path(run, digest: str) -> Path:
    relative = agent_native_tool_program_relative(digest)
    return run.run_dir / relative


def bridge_program_text() -> str:
    return _BRIDGE_PROGRAM + "\n"


def bridge_program_sha256() -> str:
    return hashlib.sha256(bridge_program_text().encode("utf-8")).hexdigest()


def write_agent_native_tool_bridge(run) -> Path:
    path = agent_native_tool_bridge_path(run)
    raw = bridge_program_text().encode("utf-8")
    if path.exists() or path.is_symlink():
        observed = read_regular_file_bytes_no_follow(
            path,
            "agent native tool bridge",
            max_bytes=MAX_AGENT_NATIVE_TOOL_PROGRAM_BYTES,
        )
        if observed != raw:
            raise ValidationError("agent native tool bridge changed")
        return path
    try:
        write_new_text_file_no_follow(
            path,
            "agent native tool bridge",
            raw.decode("utf-8"),
            mode=0o600,
            sync=True,
        )
    except FileExistsError:
        observed = read_regular_file_bytes_no_follow(
            path,
            "agent native tool bridge",
            max_bytes=MAX_AGENT_NATIVE_TOOL_PROGRAM_BYTES,
        )
        if observed != raw:
            raise ValidationError("agent native tool bridge changed")
    return path


def snapshot_agent_native_tool_program(run, source: Path) -> Dict:
    raw = read_regular_file_bytes_no_follow(
        source,
        "agent native tool verifier",
        max_bytes=MAX_AGENT_NATIVE_TOOL_PROGRAM_BYTES,
    )
    digest = hashlib.sha256(raw).hexdigest()
    target = agent_native_tool_program_path(run, digest)
    if target.exists() or target.is_symlink():
        observed = read_regular_file_bytes_no_follow(
            target,
            "agent native tool verifier snapshot",
            max_bytes=MAX_AGENT_NATIVE_TOOL_PROGRAM_BYTES,
        )
        if observed != raw:
            raise ValidationError("agent native tool verifier snapshot changed")
    else:
        try:
            _write_new_bytes(target, raw, mode=0o500)
        except FileExistsError:
            observed = read_regular_file_bytes_no_follow(
                target,
                "agent native tool verifier snapshot",
                max_bytes=MAX_AGENT_NATIVE_TOOL_PROGRAM_BYTES,
            )
            if observed != raw:
                raise ValidationError("agent native tool verifier snapshot changed")
    return {
        "program_relative": agent_native_tool_program_relative(digest),
        "program_sha256": digest,
    }


def _fail_gate(run, gate: Dict, error_class: str, updated_at_utc: str) -> Dict:
    failed = dict(gate)
    failed["status"] = "failed"
    failed["updated_at_utc"] = updated_at_utc
    failed["error_class"] = error_class[:128]
    failed["gate_sha256"] = _self_hash(failed, "gate_sha256")
    replace_agent_native_tool_gate(run, failed)
    raise ValidationError("agent native tool gate failed: %s" % error_class)


def _validate_gate_binding(observed: Dict, expected: Dict) -> None:
    fields = {
        "start_input",
        "start_input_sha256",
        "workflow_fingerprint",
        "invocation_sha256",
        "step_id",
        "scope",
        "hook_ids",
        "hook_failure_modes",
        "hook_tools",
        "hook_program_sha256s",
        "hook_timeouts",
        "hook_set_sha256",
        "bridge_sha256",
        "config_sha256",
        "command_policy_sha256",
        "created_at_utc",
    }
    changed = sorted(field for field in fields if observed.get(field) != expected.get(field))
    if changed:
        raise ValidationError("agent native tool gate binding changed: %s" % ", ".join(changed))


def _validate_receipt_gate_binding(receipt: Dict, gate: Dict) -> None:
    expected = {
        "workflow_fingerprint": gate["workflow_fingerprint"],
        "invocation_sha256": gate["invocation_sha256"],
        "step_id": gate["step_id"],
        "scope": gate["scope"],
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValidationError("agent native tool receipt gate binding changed")
    positions = [gate["hook_ids"].index(hook_id) for hook_id in receipt["hook_ids"]]
    if positions != sorted(positions):
        raise ValidationError("agent native tool receipt hook order changed")
    for index, position in enumerate(positions):
        if receipt["hook_failure_modes"][index] != gate["hook_failure_modes"][position]:
            raise ValidationError("agent native tool receipt failure mode changed")
        if receipt["hook_program_sha256s"][index] != gate["hook_program_sha256s"][position]:
            raise ValidationError("agent native tool receipt program changed")
        tools = gate["hook_tools"][position]
        if "*" not in tools and receipt["tool_name"] not in tools:
            raise ValidationError("agent native tool receipt tool filter changed")


def _receipt_dir_relative(invocation_sha256: str) -> str:
    _validate_sha256(invocation_sha256, "agent native tool invocation")
    relative = "artifacts/.agent-native-tool-hooks/receipts/%s" % invocation_sha256
    require_no_path_escape(relative)
    return relative


def _receipt_name(invocation_sha256: str, tool_use_id_sha256: str) -> str:
    _validate_sha256(invocation_sha256, "agent native tool invocation")
    _validate_sha256(tool_use_id_sha256, "agent native tool use id")
    return hashlib.sha256(
        (invocation_sha256 + "\x00" + tool_use_id_sha256).encode("ascii")
    ).hexdigest() + ".json"


def _write_new_bytes(path: Path, raw: bytes, mode: int) -> None:
    reject_symlink_path(path, "agent native tool verifier snapshot")
    parent_fd = ensure_dir_no_follow(path.parent, "agent native tool verifier parent")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = None
    try:
        fd = os.open(path.name, flags, mode, dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValidationError("agent native tool verifier snapshot must be regular")
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(fd)
        os.fsync(parent_fd)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def _load_json(path: Path, label: str, limit: int) -> Dict:
    text = read_regular_text_file_no_follow(path, label, limit)
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_constant)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("%s is invalid JSON: %s" % (label, exc.__class__.__name__))


def _canonical_bytes(value) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("agent native tool JSON value is invalid") from exc


def _self_hash(value: Dict, field: str) -> str:
    cleaned = dict(value)
    cleaned[field] = ""
    return hashlib.sha256(_canonical_bytes(cleaned)).hexdigest()


def _sha256_json(value) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValidationError("%s must be a sha256 digest" % label)


def _validate_tool_name(value) -> None:
    if not isinstance(value, str) or not value or len(value) > MAX_AGENT_NATIVE_TOOL_NAME_CHARS:
        raise ValidationError("agent native tool name is invalid")
    if any(ord(char) < 32 for char in value):
        raise ValidationError("agent native tool name is invalid")


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key %s" % key)
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-standard JSON constant %s" % value)


_BRIDGE_PROGRAM = r'''#!/usr/bin/env python3
import hashlib,json,os,re,shlex,stat,subprocess,sys,tempfile,time
from datetime import datetime,timezone

CONFIG_SCHEMA="conductor.agent_native_tool_config.v1"
INPUT_SCHEMA="conductor.agent_native_tool_hook_input.v1"
RECEIPT_SCHEMA="conductor.agent_native_tool_receipt.v1"
MAX_CONFIG=524288
MAX_INPUT=1048576
MAX_PROGRAM=4194304
MAX_RECEIPTS=4096

def canonical(value):
 return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode()
def digest(value): return hashlib.sha256(canonical(value)).hexdigest()
def self_hash(value,field):
 item=dict(value);item[field]="";return digest(item)
def now(): return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
def deny(reason):
 print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":reason}},separators=(",",":")))
 raise SystemExit(0)
def strict_json(raw):
 def pairs(items):
  out={}
  for key,value in items:
   if key in out: raise ValueError()
   out[key]=value
  return out
 return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda value:(_ for _ in ()).throw(ValueError()))
def load_json(path,limit):
 flags=os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)
 fd=os.open(path,flags)
 try:
  info=os.fstat(fd)
  if not stat.S_ISREG(info.st_mode) or info.st_size>limit: raise ValueError()
  raw=b""
  while len(raw)<=limit:
   chunk=os.read(fd,min(65536,limit+1-len(raw)))
   if not chunk: break
   raw+=chunk
  if len(raw)>limit: raise ValueError()
 finally: os.close(fd)
 return strict_json(raw)
def write_new(path,value):
 os.makedirs(os.path.dirname(path),mode=0o700,exist_ok=True)
 raw=canonical(value)+b"\n";flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0)
 fd=os.open(path,flags,0o600)
 try:
  if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError()
  offset=0
  while offset<len(raw): offset+=os.write(fd,raw[offset:])
  os.fsync(fd)
 finally: os.close(fd)
 parent=os.open(os.path.dirname(path),os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
 try: os.fsync(parent)
 finally: os.close(parent)
def replace(path,value):
 directory=os.path.dirname(path);raw=canonical(value)+b"\n"
 fd,temp=tempfile.mkstemp(prefix=".native-tool-",suffix=".tmp",dir=directory)
 try:
  os.fchmod(fd,0o600);offset=0
  while offset<len(raw): offset+=os.write(fd,raw[offset:])
  os.fsync(fd);os.close(fd);fd=None;os.replace(temp,path)
  parent=os.open(directory,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
  try: os.fsync(parent)
  finally: os.close(parent)
 finally:
  if fd is not None: os.close(fd)
  try:
   if os.path.exists(temp): os.unlink(temp)
  except OSError: pass
def regular_hash(path):
 flags=os.O_RDONLY|getattr(os,"O_NOFOLLOW",0);fd=os.open(path,flags)
 try:
  info=os.fstat(fd)
  if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_PROGRAM: raise ValueError()
  h=hashlib.sha256();total=0
  while True:
   chunk=os.read(fd,65536)
   if not chunk: break
   total+=len(chunk)
   if total>MAX_PROGRAM: raise ValueError()
   h.update(chunk)
  return h.hexdigest()
 finally: os.close(fd)
def validate_policy(policy,expected):
 if policy is None:
  if expected is not None: raise ValueError()
  return None
 schema=policy.get("schema") if isinstance(policy,dict) else None
 fields={"schema","command_allowlist","allow_apply_patch"}|({"tool_allowlist"} if schema=="conductor.agent_command_policy.v2" else set())
 if not isinstance(policy,dict) or set(policy)!=fields or schema not in {"conductor.agent_command_policy.v1","conductor.agent_command_policy.v2"} or not isinstance(policy.get("allow_apply_patch"),bool) or digest(policy)!=expected: raise ValueError()
 rules=policy.get("command_allowlist")
 if not isinstance(rules,list) or len(rules)>64: raise ValueError()
 seen=set()
 for rule in rules:
  if not isinstance(rule,dict) or len(rule)!=1 or next(iter(rule),None) not in {"argv","argv_prefix"}: raise ValueError()
  field=next(iter(rule));values=rule[field]
  if not isinstance(values,list) or not values or len(values)>64 or any(not isinstance(value,str) or not value or len(value)>2048 or "\x00" in value or "\r" in value or "\n" in value or any(ord(char)<32 and char!="\t" for char in value) for value in values): raise ValueError()
  identity=(field,tuple(values))
  if identity in seen: raise ValueError()
  seen.add(identity)
 tools=policy.get("tool_allowlist",[])
 mcp=re.compile(r"^mcp__[A-Za-z0-9][A-Za-z0-9_-]{0,63}__[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
 if not isinstance(tools,list) or len(tools)>64 or len(set(tools))!=len(tools) or any(not isinstance(name,str) or len(name)>256 or not mcp.fullmatch(name) for name in tools): raise ValueError()
 return policy
def validate_config(config,expected):
 fields={"schema","start_input","run_dir","receipt_dir_relative","hooks","command_policy","command_policy_sha256","config_sha256"}
 if not isinstance(config,dict) or set(config)!=fields or config.get("schema")!=CONFIG_SCHEMA or config.get("config_sha256")!=expected or self_hash(config,"config_sha256")!=expected: raise ValueError()
 start=config.get("start_input");hooks=config.get("hooks")
 if not isinstance(start,dict) or start.get("event")!="agent_start" or not isinstance(hooks,list) or not 1<=len(hooks)<=64: raise ValueError()
 if not isinstance(config.get("run_dir"),str) or not os.path.isabs(config["run_dir"]): raise ValueError()
 for hook in hooks:
  if not isinstance(hook,dict) or set(hook)!={"id","on_failure","tools","program_relative","program_sha256","timeout_seconds"}: raise ValueError()
  if hook["on_failure"] not in {"block","warn"} or not isinstance(hook["tools"],list) or not hook["tools"]: raise ValueError()
  if not isinstance(hook["timeout_seconds"],int) or not 1<=hook["timeout_seconds"]<=30: raise ValueError()
  if not isinstance(hook["program_relative"],str) or os.path.isabs(hook["program_relative"]) or ".." in hook["program_relative"].split("/"): raise ValueError()
  program=os.path.realpath(os.path.join(config["run_dir"],hook["program_relative"]))
  if os.path.commonpath([program,os.path.realpath(config["run_dir"])])!=os.path.realpath(config["run_dir"]): raise ValueError()
  if regular_hash(program)!=hook["program_sha256"]: raise ValueError()
 return start,hooks,validate_policy(config.get("command_policy"),config.get("command_policy_sha256"))
def parse_command(command):
 if not isinstance(command,str) or not command or len(command.encode())>65536: raise ValueError()
 bad=set(";&|<>()`$*?[]{}~!#");quote=None;escaped=False
 for char in command:
  if char in "\r\n\x00" or (ord(char)<32 and char!="\t"): raise ValueError()
  if escaped: escaped=False;continue
  if quote=="'":
   if char=="'": quote=None
   continue
  if quote=='"':
   if char=='"': quote=None
   elif char=="\\": escaped=True
   elif char in {"$","`"}: raise ValueError()
   continue
  if char=="\\": escaped=True
  elif char in {"'",'"'}: quote=char
  elif char in bad: raise ValueError()
 if quote is not None or escaped: raise ValueError()
 argv=shlex.split(command,comments=False,posix=True)
 if not argv or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=",argv[0]): raise ValueError()
 return argv
def enforce_command_policy(policy,event):
 if policy is None: return
 tool=event.get("tool_name")
 if tool=="apply_patch":
  if policy["allow_apply_patch"]: return
  deny("Conductor profile does not allow apply_patch.")
 if tool in policy.get("tool_allowlist",[]): return
 if tool!="Bash": deny("Conductor profile does not allow this tool.")
 item=event.get("tool_input");command=item.get("command") if isinstance(item,dict) else None
 try: argv=parse_command(command)
 except Exception: deny("Conductor profile allows only one expansion-free simple command.")
 for rule in policy["command_allowlist"]:
  if "argv" in rule and argv==rule["argv"]: return
  prefix=rule.get("argv_prefix")
  if prefix is not None and argv[:len(prefix)]==prefix: return
 deny("Bash command is outside the Conductor profile allowlist.")
def hook_input(start,event):
 tool=event.get("tool_name");tool_id=event.get("tool_use_id")
 if event.get("hook_event_name")!="PreToolUse" or not isinstance(tool,str) or not tool or len(tool)>256 or not isinstance(tool_id,str) or not tool_id or len(tool_id)>512: raise ValueError()
 item={key:value for key,value in start.items() if key not in {"schema","event","status","returncode","timed_out","interrupted","output_sha256","provider_stdout_sha256","provider_stderr_sha256","error_class"}}
 item.update({"schema":INPUT_SCHEMA,"event":"agent_pre_tool","tool_name":tool,"tool_use_id_sha256":hashlib.sha256(tool_id.encode()).hexdigest(),"tool_input":event.get("tool_input"),"tool_input_sha256":digest(event.get("tool_input"))})
 raw=canonical(item)+b"\n"
 if len(raw)>MAX_INPUT: raise ValueError()
 return item,raw,tool_id
def receipt_path(config,item,tool_id):
 directory=os.path.realpath(os.path.join(config["run_dir"],config["receipt_dir_relative"]))
 if os.path.commonpath([directory,os.path.realpath(config["run_dir"])])!=os.path.realpath(config["run_dir"]): raise ValueError()
 os.makedirs(directory,mode=0o700,exist_ok=True)
 if len([name for name in os.listdir(directory) if not name.startswith(".")])>=MAX_RECEIPTS: raise ValueError()
 name=hashlib.sha256((item["invocation_sha256"]+"\x00"+item["tool_use_id_sha256"]).encode()).hexdigest()+".json"
 return os.path.join(directory,name)
def validate_receipt(receipt,item,matching):
 fields={"schema","status","workflow_fingerprint","invocation_sha256","step_id","scope","tool_name","tool_use_id_sha256","tool_input_sha256","hook_ids","hook_failure_modes","hook_program_sha256s","next_hook_index","active_hook_index","hook_results","warning_count","created_at_utc","updated_at_utc","error_class","receipt_sha256"}
 if not isinstance(receipt,dict) or set(receipt)!=fields or receipt.get("schema")!=RECEIPT_SCHEMA or receipt.get("status") not in {"running","passed","denied"} or receipt.get("receipt_sha256")!=self_hash(receipt,"receipt_sha256"): raise ValueError()
 expected={"workflow_fingerprint":item["workflow_fingerprint"],"invocation_sha256":item["invocation_sha256"],"step_id":item["step_id"],"scope":item["scope"],"tool_name":item["tool_name"],"tool_use_id_sha256":item["tool_use_id_sha256"],"tool_input_sha256":item["tool_input_sha256"],"hook_ids":[hook["id"] for hook in matching],"hook_failure_modes":[hook["on_failure"] for hook in matching],"hook_program_sha256s":[hook["program_sha256"] for hook in matching]}
 if any(receipt.get(key)!=value for key,value in expected.items()): raise ValueError()
 count=len(matching);next_index=receipt.get("next_hook_index");active=receipt.get("active_hook_index");results=receipt.get("hook_results")
 if not isinstance(next_index,int) or isinstance(next_index,bool) or not 0<=next_index<=count or not isinstance(results,list) or len(results)!=next_index: raise ValueError()
 for index,result in enumerate(results):
  if not isinstance(result,dict) or set(result)!={"hook_id","status","returncode","timed_out"} or result.get("hook_id")!=matching[index]["id"] or result.get("status") not in {"completed","failed","timed_out"} or not isinstance(result.get("timed_out"),bool): raise ValueError()
  code=result.get("returncode");status=result["status"]
  if (code is not None and (not isinstance(code,int) or isinstance(code,bool))) or (status=="completed" and code!=0) or (status=="failed" and (code is None or code==0)) or (status=="timed_out" and code is not None) or ((status=="timed_out")!=result["timed_out"]): raise ValueError()
 warnings=sum(result["status"]!="completed" and matching[index]["on_failure"]=="warn" for index,result in enumerate(results))
 if receipt.get("warning_count")!=warnings or receipt.get("error_class") is not None or not isinstance(receipt.get("created_at_utc"),str) or not receipt["created_at_utc"] or not isinstance(receipt.get("updated_at_utc"),str) or not receipt["updated_at_utc"]: raise ValueError()
 if receipt["status"]=="running":
  if active!=next_index or next_index>=count: raise ValueError()
 elif active is not None: raise ValueError()
 elif receipt["status"]=="passed" and next_index!=count: raise ValueError()
 elif receipt["status"]=="denied" and not (0<next_index<=count and matching[next_index-1]["on_failure"]=="block" and results[-1]["status"]!="completed"): raise ValueError()
 return receipt
def decision_for(receipt):
 if receipt.get("status")=="passed": print("{}");raise SystemExit(0)
 deny("Conductor pre-tool gate denied this tool call.")
def replay(path,item,matching,wait_seconds):
 deadline=time.monotonic()+wait_seconds
 while True:
  receipt=validate_receipt(load_json(path,262144),item,matching)
  if receipt["status"]!="running": decision_for(receipt)
  if time.monotonic()>=deadline: deny("Conductor pre-tool receipt remained indeterminate.")
  time.sleep(0.05)
def main():
 if len(sys.argv)!=2 or len(sys.argv[1])!=64 or regular_hash(os.path.realpath(__file__))!=sys.argv[1]: deny("Conductor pre-tool gate bridge integrity check failed.")
 config_path=os.environ.get("CONDUCTOR_AGENT_PRE_TOOL_CONFIG");expected=os.environ.get("CONDUCTOR_AGENT_PRE_TOOL_CONFIG_SHA256")
 if not config_path or not expected or len(expected)!=64: deny("Conductor pre-tool gate configuration is unavailable.")
 config=load_json(config_path,MAX_CONFIG);start,hooks,command_policy=validate_config(config,expected)
 raw=sys.stdin.buffer.read(MAX_INPUT+1)
 if len(raw)>MAX_INPUT: deny("Conductor pre-tool gate input exceeded its limit.")
 event=strict_json(raw);item,payload,tool_id=hook_input(start,event);enforce_command_policy(command_policy,event)
 matching=[hook for hook in hooks if "*" in hook["tools"] or item["tool_name"] in hook["tools"]]
 if not matching: print("{}");return
 path=receipt_path(config,item,tool_id)
 wait_seconds=sum(hook["timeout_seconds"] for hook in matching)+2
 if os.path.exists(path): replay(path,item,matching,wait_seconds)
 created=now();receipt={"schema":RECEIPT_SCHEMA,"status":"running","workflow_fingerprint":item["workflow_fingerprint"],"invocation_sha256":item["invocation_sha256"],"step_id":item["step_id"],"scope":item["scope"],"tool_name":item["tool_name"],"tool_use_id_sha256":item["tool_use_id_sha256"],"tool_input_sha256":item["tool_input_sha256"],"hook_ids":[hook["id"] for hook in matching],"hook_failure_modes":[hook["on_failure"] for hook in matching],"hook_program_sha256s":[hook["program_sha256"] for hook in matching],"next_hook_index":0,"active_hook_index":0,"hook_results":[],"warning_count":0,"created_at_utc":created,"updated_at_utc":created,"error_class":None,"receipt_sha256":""}
 receipt["receipt_sha256"]=self_hash(receipt,"receipt_sha256")
 try: write_new(path,receipt)
 except FileExistsError: replay(path,item,matching,wait_seconds)
 safe_env={key:os.environ[key] for key in ("PATH","LANG","LC_ALL","NO_COLOR") if key in os.environ};safe_env.update({"PYTHONDONTWRITEBYTECODE":"1","PYTHONNOUSERSITE":"1"})
 for index,hook in enumerate(matching):
  receipt["active_hook_index"]=index;receipt["updated_at_utc"]=now();receipt["receipt_sha256"]=self_hash(receipt,"receipt_sha256");replace(path,receipt)
  program=os.path.join(config["run_dir"],hook["program_relative"]);status="completed";returncode=0;timed_out=False
  try:
   result=subprocess.run([program],input=payload,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,cwd=os.getcwd(),env=safe_env,timeout=hook["timeout_seconds"],check=False)
   returncode=result.returncode
   if returncode!=0: status="failed"
  except subprocess.TimeoutExpired: status="timed_out";returncode=None;timed_out=True
  except Exception: status="failed";returncode=255
  receipt["hook_results"].append({"hook_id":hook["id"],"status":status,"returncode":returncode,"timed_out":timed_out});receipt["next_hook_index"]=index+1
  if status!="completed" and hook["on_failure"]=="block":
   receipt["status"]="denied";receipt["active_hook_index"]=None;receipt["updated_at_utc"]=now();receipt["receipt_sha256"]=self_hash(receipt,"receipt_sha256");replace(path,receipt);decision_for(receipt)
  if status!="completed": receipt["warning_count"]+=1
 receipt["status"]="passed";receipt["active_hook_index"]=None;receipt["updated_at_utc"]=now();receipt["receipt_sha256"]=self_hash(receipt,"receipt_sha256");replace(path,receipt);print("{}")
try: main()
except SystemExit: raise
except Exception: deny("Conductor pre-tool gate failed closed.")
'''.strip()
