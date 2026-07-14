import hashlib
import json
import math
import os
import re
import shlex
from pathlib import Path
from typing import Dict, Iterable, List, Optional  # noqa: F401 - compatibility re-export

from .agent_profiles import bind_agent_profile_static_resources
from .artifacts import utc_now
from .errors import ValidationError
from .redaction import redact_json_value, redact_text
from .security import (
    open_dir_no_follow,
    read_regular_file_bytes_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    write_new_text_file_no_follow,
)
from .workflow import MAX_AGENT_WORKERS, SAFE_ID, validate_workflow, workflow_fingerprint


ROUTINE_MANIFEST_SCHEMA = "conductor.routine_manifest.v1"
MAX_ROUTINE_MANIFEST_BYTES = 1024 * 1024
MAX_LIST_ROUTINES = 200
MAX_INTERVAL_MINUTES = 60 * 24 * 7
MAX_CRON_EXPRESSION_CHARS = 128
CRON_EXPRESSION_SHAPE_PATTERN = r"^[0-9*/,-]+(?: [0-9*/,-]+){4}$"
TIME_PATTERN = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
TIMEZONE_PATTERN = re.compile(r"^[A-Za-z0-9_./+-]{1,64}$")
UTC_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$")
CRON_FIELD_PATTERN = re.compile(r"^[0-9*/,\-]+$")
CRON_FIELD_SPECS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),
)
TOP_LEVEL_KEYS = {
    "schema",
    "name",
    "created_at_utc",
    "evidence_status",
    "schedule",
    "target",
    "launch",
    "budget",
    "external_supervisor_contract",
    "no_process_started",
    "process_model",
}
TARGET_BASE_KEYS = {"kind", "workflow", "workflow_fingerprint", "steps_planned"}
LAUNCH_REQUIRED_KEYS = {"command_argv", "command", "workspace", "runs_dir", "max_workers", "policy"}
LAUNCH_KEYS = LAUNCH_REQUIRED_KEYS | {"runtime_sha256"}
POLICY_KEYS = {
    "allow_writes",
    "allow_destructive",
    "allow_network",
    "allow_agent",
    "allow_parallel",
    "approval_count",
    "approval_values_persisted",
}
ROUTINE_CAPABILITY_FLAGS = {
    "allow_writes": "--allow-writes",
    "allow_destructive": "--allow-destructive",
    "allow_network": "--allow-network",
    "allow_agent": "--allow-agent",
    "allow_parallel": "--allow-parallel",
}
ROUTINE_LAUNCH_FLAGS = set(ROUTINE_CAPABILITY_FLAGS.values()) | {
    "--desktop-notify",
}
ROUTINE_LAUNCH_VALUE_OPTIONS = {"--workspace", "--runs-dir", "--max-workers"}
SAVED_ROUTINE_LAUNCH_FLAGS = {"--include-personal"}
SAVED_ROUTINE_LAUNCH_VALUE_OPTIONS = {
    "--project-root",
    "--personal-workflows-dir",
    "--arg",
}
EXTERNAL_SUPERVISOR_KEYS = {
    "may_run_command_argv",
    "must_not_assume_conductor_daemon",
    "must_not_start_without_operator_or_scheduler_approval",
    "must_record_actual_run_manifest_for_parity_claims",
    "approval_tokens_must_be_supplied_at_launch_if_required",
    "no_process_started_by_manifest_write",
}
BUDGET_KEYS = {"max_minutes", "max_cost_usd", "max_tokens"}
WEEKDAY_ALIASES = {
    "mon": "monday",
    "monday": "monday",
    "tue": "tuesday",
    "tues": "tuesday",
    "tuesday": "tuesday",
    "wed": "wednesday",
    "weds": "wednesday",
    "wednesday": "wednesday",
    "thu": "thursday",
    "thur": "thursday",
    "thurs": "thursday",
    "thursday": "thursday",
    "fri": "friday",
    "friday": "friday",
    "sat": "saturday",
    "saturday": "saturday",
    "sun": "sunday",
    "sunday": "sunday",
}


def build_schedule(
    *,
    manual: bool = False,
    interval_minutes: Optional[int] = None,
    daily_at: Optional[str] = None,
    weekdays_at: Optional[str] = None,
    weekly_at: Optional[str] = None,
    monthly_at: Optional[str] = None,
    once_at: Optional[str] = None,
    cron: Optional[str] = None,
    timezone: str = "UTC",
) -> Dict:
    supplied = sum(
        1
        for value in [
            bool(manual),
            interval_minutes is not None,
            daily_at is not None,
            weekdays_at is not None,
            weekly_at is not None,
            monthly_at is not None,
            once_at is not None,
            cron is not None,
        ]
        if value
    )
    if supplied != 1:
        raise ValidationError(
            "supply exactly one schedule: --manual, --interval-minutes, --daily-at, --weekdays-at, "
            "--weekly-at, --monthly-at, --once-at, or --cron"
        )
    if manual:
        return {"kind": "manual"}
    if interval_minutes is not None:
        if not isinstance(interval_minutes, int) or isinstance(interval_minutes, bool):
            raise ValidationError("interval_minutes must be an integer")
        if interval_minutes < 1 or interval_minutes > MAX_INTERVAL_MINUTES:
            raise ValidationError("interval_minutes must be from 1 to %d" % MAX_INTERVAL_MINUTES)
        return {"kind": "interval", "every_minutes": interval_minutes}
    if daily_at is not None:
        return {"kind": "daily", "time": _validate_time(daily_at, "daily_at"), "timezone": _validate_timezone(timezone)}
    if weekdays_at is not None:
        return {
            "kind": "weekdays",
            "time": _validate_time(weekdays_at, "weekdays_at"),
            "timezone": _validate_timezone(timezone),
        }
    if once_at is not None:
        return {"kind": "once", "at_utc": _validate_utc_timestamp(once_at, "once_at")}
    if cron is not None:
        parsed = parse_cron_expression(cron)
        return {
            "kind": "cron",
            "expression": parsed["expression"],
            "timezone": _validate_timezone(timezone),
        }
    if monthly_at is not None:
        day, time_value = _parse_monthly_at(monthly_at)
        return {
            "kind": "monthly",
            "day": day,
            "time": time_value,
            "timezone": _validate_timezone(timezone),
        }
    day, time_value = _parse_weekly_at(weekly_at)
    return {"kind": "weekly", "day": day, "time": time_value, "timezone": _validate_timezone(timezone)}


def build_routine_manifest(
    *,
    name: str,
    schedule: Dict,
    workflow: Dict,
    target: Dict,
    command_argv: List[str],
    workspace: Path,
    runs_dir: Path,
    policy,
    max_workers: Optional[int] = None,
    budget: Optional[Dict] = None,
    runtime_sha256: Optional[str] = None,
) -> Dict:
    _validate_name(name)
    validate_schedule(schedule)
    workspace = Path(workspace).resolve()
    workflow = bind_agent_profile_static_resources(workflow, workspace)
    validate_workflow(_public_workflow(workflow), source="routine workflow")
    command = _validate_command_argv(command_argv)
    _validate_max_workers(max_workers, "routine max_workers")
    budget_value = _validate_budget(budget or {})
    approval_count = len(getattr(policy, "approvals", set()) or set())
    public_command_argv = [redact_text(str(part)) for part in command]
    launch = {
        "command_argv": public_command_argv,
        "command": shlex.join(public_command_argv),
        "workspace": redact_text(str(workspace)),
        "runs_dir": redact_text(str(runs_dir)),
        "max_workers": max_workers,
        "policy": {
            "allow_writes": bool(getattr(policy, "allow_writes", False)),
            "allow_destructive": bool(getattr(policy, "allow_destructive", False)),
            "allow_network": bool(getattr(policy, "allow_network", False)),
            "allow_agent": bool(getattr(policy, "allow_agent", False)),
            "allow_parallel": bool(getattr(policy, "allow_parallel", False)),
            "approval_count": approval_count,
            "approval_values_persisted": False,
        },
    }
    if runtime_sha256 is not None:
        launch["runtime_sha256"] = _validate_sha256(runtime_sha256, "routine packaged runtime sha256")
    manifest = {
        "schema": ROUTINE_MANIFEST_SCHEMA,
        "name": name,
        "created_at_utc": utc_now(),
        "evidence_status": "planned-not-evidence",
        "schedule": schedule,
        "target": _build_target(target, workflow),
        "launch": launch,
        "budget": budget_value,
        "external_supervisor_contract": {
            "may_run_command_argv": public_command_argv,
            "must_not_assume_conductor_daemon": True,
            "must_not_start_without_operator_or_scheduler_approval": True,
            "must_record_actual_run_manifest_for_parity_claims": True,
            "approval_tokens_must_be_supplied_at_launch_if_required": approval_count > 0,
            "no_process_started_by_manifest_write": True,
        },
        "no_process_started": True,
        "process_model": "operator-owned external scheduler launch",
    }
    validate_routine_manifest(manifest, source=name)
    return redact_json_value(manifest)


def validate_schedule(schedule: Dict) -> None:
    if not isinstance(schedule, dict):
        raise ValidationError("routine schedule must be an object")
    kind = schedule.get("kind")
    if kind == "manual":
        if sorted(schedule) != ["kind"]:
            raise ValidationError("manual routine schedule only supports kind")
        return
    if kind == "interval":
        if sorted(schedule) != ["every_minutes", "kind"]:
            raise ValidationError("interval routine schedule must contain kind and every_minutes")
        value = schedule.get("every_minutes")
        if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > MAX_INTERVAL_MINUTES:
            raise ValidationError("interval routine schedule every_minutes must be from 1 to %d" % MAX_INTERVAL_MINUTES)
        return
    if kind == "daily":
        if sorted(schedule) != ["kind", "time", "timezone"]:
            raise ValidationError("daily routine schedule must contain kind, time, and timezone")
        _validate_time(schedule.get("time"), "daily schedule time")
        _validate_timezone(schedule.get("timezone"))
        return
    if kind == "weekdays":
        if sorted(schedule) != ["kind", "time", "timezone"]:
            raise ValidationError("weekdays routine schedule must contain kind, time, and timezone")
        _validate_time(schedule.get("time"), "weekdays schedule time")
        _validate_timezone(schedule.get("timezone"))
        return
    if kind == "weekly":
        if sorted(schedule) != ["day", "kind", "time", "timezone"]:
            raise ValidationError("weekly routine schedule must contain kind, day, time, and timezone")
        day = schedule.get("day")
        if day not in set(WEEKDAY_ALIASES.values()):
            raise ValidationError("weekly routine schedule day is invalid")
        _validate_time(schedule.get("time"), "weekly schedule time")
        _validate_timezone(schedule.get("timezone"))
        return
    if kind == "monthly":
        if sorted(schedule) != ["day", "kind", "time", "timezone"]:
            raise ValidationError("monthly routine schedule must contain kind, day, time, and timezone")
        day = schedule.get("day")
        if not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= 31:
            raise ValidationError("monthly routine schedule day must be from 1 to 31")
        _validate_time(schedule.get("time"), "monthly schedule time")
        _validate_timezone(schedule.get("timezone"))
        return
    if kind == "once":
        if sorted(schedule) != ["at_utc", "kind"]:
            raise ValidationError("once routine schedule must contain kind and at_utc")
        _validate_utc_timestamp(schedule.get("at_utc"), "once schedule at_utc")
        return
    if kind == "cron":
        if sorted(schedule) != ["expression", "kind", "timezone"]:
            raise ValidationError("cron routine schedule must contain kind, expression, and timezone")
        parsed = parse_cron_expression(schedule.get("expression"))
        if parsed["expression"] != schedule.get("expression"):
            raise ValidationError("cron routine schedule expression must be canonical")
        _validate_timezone(schedule.get("timezone"))
        return
    raise ValidationError(
        "routine schedule kind must be manual, interval, daily, weekdays, weekly, monthly, once, or cron"
    )


def parse_cron_expression(value: str) -> Dict:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("cron expression must be a non-empty string")
    if len(value) > MAX_CRON_EXPRESSION_CHARS:
        raise ValidationError(
            "cron expression must be at most %d characters" % MAX_CRON_EXPRESSION_CHARS
        )
    parts = value.strip().split()
    if len(parts) != len(CRON_FIELD_SPECS):
        raise ValidationError("cron expression must contain exactly five fields")
    canonical = []
    fields = []
    wildcards = []
    for token, (label, minimum, maximum) in zip(parts, CRON_FIELD_SPECS):
        field, values = _parse_cron_field(token, label, minimum, maximum)
        canonical.append(field)
        fields.append(frozenset(values))
        wildcards.append(field.startswith("*"))
    return {
        "expression": " ".join(canonical),
        "fields": tuple(fields),
        "day_of_month_wildcard": wildcards[2],
        "day_of_week_wildcard": wildcards[4],
    }


def _parse_cron_field(token: str, label: str, minimum: int, maximum: int) -> tuple:
    if not isinstance(token, str) or not token or CRON_FIELD_PATTERN.fullmatch(token) is None:
        raise ValidationError("cron %s field is invalid" % label)
    pieces = token.split(",")
    if any(not piece for piece in pieces):
        raise ValidationError("cron %s field is invalid" % label)
    values = set()
    canonical_pieces = []
    for piece in pieces:
        if piece.count("/") > 1:
            raise ValidationError("cron %s field has invalid step syntax" % label)
        base, separator, step_text = piece.partition("/")
        if separator:
            step = _cron_integer(step_text, label)
            if step < 1 or step > maximum - minimum + 1:
                raise ValidationError("cron %s step is out of range" % label)
        else:
            step = 1
        if base == "*":
            start, end = minimum, maximum
            canonical_base = "*"
        elif "-" in base:
            if base.count("-") != 1:
                raise ValidationError("cron %s field has invalid range syntax" % label)
            start_text, end_text = base.split("-", 1)
            start = _cron_integer(start_text, label)
            end = _cron_integer(end_text, label)
            _validate_cron_bound(start, label, minimum, maximum)
            _validate_cron_bound(end, label, minimum, maximum)
            if start > end:
                raise ValidationError("cron %s range must be ascending" % label)
            canonical_base = "%d-%d" % (start, end)
        else:
            start = _cron_integer(base, label)
            _validate_cron_bound(start, label, minimum, maximum)
            end = maximum if separator else start
            canonical_base = str(start)
        canonical_pieces.append(
            "%s/%d" % (canonical_base, step) if separator else canonical_base
        )
        for item in range(start, end + 1, step):
            values.add(0 if label == "day-of-week" and item == 7 else item)
    if not values:
        raise ValidationError("cron %s field selects no values" % label)
    return ",".join(canonical_pieces), values


def _cron_integer(value: str, label: str) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise ValidationError("cron %s field must use decimal integers" % label)
    return int(value)


def _validate_cron_bound(value: int, label: str, minimum: int, maximum: int) -> None:
    if value < minimum or value > maximum:
        raise ValidationError(
            "cron %s value must be from %d to %d" % (label, minimum, maximum)
        )


def validate_routine_manifest(manifest: Dict, source: str = "<memory>") -> None:
    if not isinstance(manifest, dict):
        raise ValidationError("%s must contain a JSON object" % source)
    _reject_unknown_keys(manifest, TOP_LEVEL_KEYS, "%s routine manifest" % source)
    if manifest.get("schema") != ROUTINE_MANIFEST_SCHEMA:
        raise ValidationError("%s must set schema to %s" % (source, ROUTINE_MANIFEST_SCHEMA))
    _validate_name(manifest.get("name"))
    created_at = manifest.get("created_at_utc")
    if not isinstance(created_at, str) or not UTC_TIMESTAMP_PATTERN.match(created_at):
        raise ValidationError("%s created_at_utc must be an ISO UTC timestamp" % source)
    validate_schedule(manifest.get("schedule"))
    if manifest.get("evidence_status") != "planned-not-evidence":
        raise ValidationError("%s evidence_status must be planned-not-evidence" % source)
    target = manifest.get("target")
    if not isinstance(target, dict):
        raise ValidationError("%s target must be an object" % source)
    target_kind = target.get("kind")
    if target_kind not in {"workflow", "saved_workflow"}:
        raise ValidationError("%s target.kind must be workflow or saved_workflow" % source)
    expected_target_keys = set(TARGET_BASE_KEYS)
    if target_kind == "workflow":
        expected_target_keys.add("workflow_path")
    else:
        expected_target_keys.update({"saved_command", "saved_scope", "saved_path"})
    _reject_unknown_keys(target, expected_target_keys, "%s target" % source)
    for key in sorted(expected_target_keys - TARGET_BASE_KEYS):
        if not isinstance(target.get(key), str) or not target.get(key):
            raise ValidationError("%s target.%s must be a non-empty string" % (source, key))
    if not isinstance(target.get("workflow"), str) or not target.get("workflow"):
        raise ValidationError("%s target.workflow must be a non-empty string" % source)
    fingerprint = target.get("workflow_fingerprint")
    if not isinstance(fingerprint, str) or not re.match(r"^[a-f0-9]{64}$", fingerprint):
        raise ValidationError("%s target.workflow_fingerprint must be a sha256 hex string" % source)
    steps_planned = target.get("steps_planned")
    if not isinstance(steps_planned, int) or isinstance(steps_planned, bool) or steps_planned < 0:
        raise ValidationError("%s target.steps_planned must be a non-negative integer" % source)
    launch = manifest.get("launch")
    if not isinstance(launch, dict):
        raise ValidationError("%s launch must be an object" % source)
    _reject_unknown_keys(launch, LAUNCH_KEYS, "%s launch" % source)
    command_argv = _validate_command_argv(launch.get("command_argv"))
    command_details = parse_routine_launch_command(command_argv)
    notification_flags = [value for value in command_argv if value == "--desktop-notify"]
    if len(notification_flags) > 1 or any(
        value.startswith("--desktop-notify=") for value in command_argv
    ):
        raise ValidationError("%s launch command has an invalid desktop notification flag" % source)
    expected_command = shlex.join(command_argv)
    if launch.get("command") != expected_command:
        raise ValidationError("%s launch.command must match launch.command_argv" % source)
    if not isinstance(launch.get("workspace"), str) or not launch.get("workspace"):
        raise ValidationError("%s launch.workspace must be a non-empty string" % source)
    if not isinstance(launch.get("runs_dir"), str) or not launch.get("runs_dir"):
        raise ValidationError("%s launch.runs_dir must be a non-empty string" % source)
    _validate_max_workers(launch.get("max_workers"), "%s launch.max_workers" % source)
    policy = launch.get("policy")
    if not isinstance(policy, dict):
        raise ValidationError("%s launch.policy must be an object" % source)
    _reject_unknown_keys(policy, POLICY_KEYS, "%s launch.policy" % source)
    for key in ["allow_writes", "allow_destructive", "allow_network", "allow_agent", "allow_parallel"]:
        if not isinstance(policy.get(key), bool):
            raise ValidationError("%s launch.policy.%s must be boolean" % (source, key))
    if not isinstance(policy.get("approval_count"), int) or isinstance(policy.get("approval_count"), bool) or policy["approval_count"] < 0:
        raise ValidationError("%s launch.policy.approval_count must be a non-negative integer" % source)
    if policy.get("approval_values_persisted") is not False:
        raise ValidationError("%s launch.policy.approval_values_persisted must be false" % source)
    _validate_routine_launch_contract(
        target=target,
        launch=launch,
        policy=policy,
        command=command_details,
        source=source,
    )
    if not isinstance(manifest.get("budget"), dict):
        raise ValidationError("%s budget must be an object" % source)
    _validate_budget(manifest.get("budget"))
    external = manifest.get("external_supervisor_contract")
    if not isinstance(external, dict):
        raise ValidationError("%s external_supervisor_contract must be an object" % source)
    _reject_unknown_keys(external, EXTERNAL_SUPERVISOR_KEYS, "%s external_supervisor_contract" % source)
    if external.get("may_run_command_argv") != command_argv:
        raise ValidationError("%s external supervisor command must match launch.command_argv" % source)
    for key in [
        "must_not_assume_conductor_daemon",
        "must_not_start_without_operator_or_scheduler_approval",
        "must_record_actual_run_manifest_for_parity_claims",
    ]:
        if external.get(key) is not True:
            raise ValidationError("%s external supervisor contract %s must be true" % (source, key))
    if external.get("approval_tokens_must_be_supplied_at_launch_if_required") is not (policy["approval_count"] > 0):
        raise ValidationError("%s external supervisor approval-token flag must match approval_count" % source)
    if external.get("no_process_started_by_manifest_write") is not True:
        raise ValidationError("%s external supervisor contract must state no process was started" % source)
    if manifest.get("no_process_started") is not True:
        raise ValidationError("%s no_process_started must be true" % source)
    if manifest.get("process_model") != "operator-owned external scheduler launch":
        raise ValidationError("%s process_model must be operator-owned external scheduler launch" % source)


def load_routine_manifest(path: Path) -> Dict:
    reject_symlink_path(path, "routine manifest")
    try:
        data = json.loads(read_regular_text_file_no_follow(path, "routine manifest", MAX_ROUTINE_MANIFEST_BYTES))
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    validate_routine_manifest(data, source=str(path))
    return data


def write_routine_manifest(manifest: Dict, destination: Path) -> Path:
    reject_symlink_path(destination, "routine manifest output")
    reject_symlink_path(destination.parent, "routine manifest output parent")
    if destination.exists() or destination.is_symlink():
        raise ValidationError("destination already exists: %s" % destination)
    if destination.suffix != ".json":
        raise ValidationError("routine manifest output must use .json: %s" % destination)
    validate_routine_manifest(manifest, source=str(destination))
    text = json.dumps(redact_json_value(manifest), indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_ROUTINE_MANIFEST_BYTES:
        raise ValidationError("routine manifest must be at most %d bytes" % MAX_ROUTINE_MANIFEST_BYTES)
    write_new_text_file_no_follow(destination, "routine manifest output", text)
    return destination


def replace_routine_schedule(path: Path, schedule: Dict) -> Dict:
    manifest_path = Path(path)
    manifest = load_routine_manifest(manifest_path)
    validate_schedule(schedule)
    updated = dict(manifest)
    updated["schedule"] = dict(schedule)
    validate_routine_manifest(updated, source=str(manifest_path))
    text = json.dumps(redact_json_value(updated), indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_ROUTINE_MANIFEST_BYTES:
        raise ValidationError("routine manifest must be at most %d bytes" % MAX_ROUTINE_MANIFEST_BYTES)
    replace_text_file_no_follow(
        manifest_path,
        "routine manifest",
        text,
        ".routine-manifest-",
    )
    return updated


def iter_routine_manifest_paths(routines_dir: Path, max_routines: int = 50) -> List[Path]:
    if not isinstance(max_routines, int) or isinstance(max_routines, bool) or max_routines < 1 or max_routines > MAX_LIST_ROUTINES:
        raise ValidationError("max_routines must be an integer from 1 to %d" % MAX_LIST_ROUTINES)
    directory = Path(routines_dir)
    reject_symlink_path(directory, "routines dir")
    if not directory.exists():
        return []
    directory_fd = open_dir_no_follow(directory, "routines dir")
    try:
        names = [name for name in os.listdir(directory_fd) if name.endswith(".json")]
    finally:
        os.close(directory_fd)
    paths = [directory / name for name in names]
    paths.sort(key=_routine_sort_key, reverse=True)
    return paths[:max_routines]


def routine_list_record(path: Path, manifest: Dict) -> Dict:
    schedule = manifest.get("schedule") if isinstance(manifest.get("schedule"), dict) else {}
    target = manifest.get("target") if isinstance(manifest.get("target"), dict) else {}
    launch = manifest.get("launch") if isinstance(manifest.get("launch"), dict) else {}
    policy = launch.get("policy") if isinstance(launch.get("policy"), dict) else {}
    return {
        "path": str(path),
        "name": str(manifest.get("name") or path.stem),
        "schedule": routine_schedule_label(schedule),
        "target_kind": str(target.get("kind") or "unknown"),
        "workflow": str(target.get("workflow") or "unknown"),
        "evidence_status": str(manifest.get("evidence_status") or "unknown"),
        "no_process_started": manifest.get("no_process_started") is True,
        "desktop_notify": "--desktop-notify" in launch.get("command_argv", []),
        "policy": {
            "allow_writes": bool(policy.get("allow_writes")),
            "allow_destructive": bool(policy.get("allow_destructive")),
            "allow_network": bool(policy.get("allow_network")),
            "allow_agent": bool(policy.get("allow_agent")),
            "allow_parallel": bool(policy.get("allow_parallel")),
            "approval_count": policy.get("approval_count") if isinstance(policy.get("approval_count"), int) else 0,
        },
        "created_at_utc": str(manifest.get("created_at_utc") or ""),
    }


def _build_target(target: Dict, workflow: Dict) -> Dict:
    if not isinstance(target, dict):
        raise ValidationError("routine target must be an object")
    kind = target.get("kind")
    if kind not in {"workflow", "saved_workflow"}:
        raise ValidationError("routine target kind must be workflow or saved_workflow")
    record = {
        "kind": kind,
        "workflow": redact_text(str(workflow.get("name") or "")),
        "workflow_fingerprint": workflow_fingerprint(workflow),
        "steps_planned": len(workflow.get("steps", [])) if isinstance(workflow.get("steps"), list) else 0,
    }
    for key in ["workflow_path", "saved_command", "saved_scope", "saved_path"]:
        if target.get(key) is not None:
            record[key] = redact_text(str(target[key]))
    return record


def _public_workflow(workflow: Dict) -> Dict:
    return {key: value for key, value in workflow.items() if not key.startswith("_")}


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not SAFE_ID.match(name):
        raise ValidationError("routine name must be a safe non-empty identifier")
    return name


def _validate_time(value, label: str) -> str:
    if not isinstance(value, str) or not TIME_PATTERN.match(value):
        raise ValidationError("%s must use HH:MM in 24-hour time" % label)
    return value


def _validate_timezone(value) -> str:
    if not isinstance(value, str) or not TIMEZONE_PATTERN.match(value) or ".." in value.split("/"):
        raise ValidationError("routine timezone must be a safe timezone identifier")
    return value


def _validate_utc_timestamp(value, label: str) -> str:
    if not isinstance(value, str) or not UTC_TIMESTAMP_PATTERN.match(value):
        raise ValidationError("%s must use an ISO UTC timestamp ending in Z" % label)
    return value


def _parse_weekly_at(value: Optional[str]) -> tuple:
    if not isinstance(value, str) or ":" not in value:
        raise ValidationError("weekly_at must use DAY:HH:MM")
    day_value, time_value = value.split(":", 1)
    day = WEEKDAY_ALIASES.get(day_value.strip().lower())
    if not day:
        raise ValidationError("weekly_at day is invalid")
    return day, _validate_time(time_value, "weekly_at time")


def _parse_monthly_at(value: Optional[str]) -> tuple:
    if not isinstance(value, str) or ":" not in value:
        raise ValidationError("monthly_at must use DAY:HH:MM")
    day_value, time_value = value.split(":", 1)
    try:
        day = int(day_value)
    except (TypeError, ValueError):
        raise ValidationError("monthly_at day must be from 1 to 31")
    if not 1 <= day <= 31 or str(day) != day_value.strip():
        raise ValidationError("monthly_at day must be from 1 to 31")
    return day, _validate_time(time_value, "monthly_at time")


def _validate_command_argv(argv) -> List[str]:
    parse_routine_launch_command(argv)
    return list(argv)


def parse_routine_launch_command(argv) -> Dict:
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
        raise ValidationError("routine launch command_argv must be a non-empty string array")
    for part in argv:
        if part == "--approve" or part.startswith("--approve="):
            raise ValidationError("routine launch command_argv must not persist approval token values")
    if argv.count("--desktop-notify") > 1 or any(
        part.startswith("--desktop-notify=") for part in argv
    ):
        raise ValidationError("routine launch command has an invalid desktop notification flag")
    subcommand_index = _routine_subcommand_index(argv)
    if subcommand_index + 1 >= len(argv):
        raise ValidationError("routine launch command_argv must include one target")
    subcommand = argv[subcommand_index]
    if subcommand not in {"run", "run-saved-workflow"}:
        raise ValidationError("routine launch command_argv must use run or run-saved-workflow")
    target = argv[subcommand_index + 1]
    if not target or target.startswith("--"):
        raise ValidationError("routine launch command target is invalid")
    flags = set(ROUTINE_LAUNCH_FLAGS)
    value_options = set(ROUTINE_LAUNCH_VALUE_OPTIONS)
    repeatable = set()
    if subcommand == "run-saved-workflow":
        flags.update(SAVED_ROUTINE_LAUNCH_FLAGS)
        value_options.update(SAVED_ROUTINE_LAUNCH_VALUE_OPTIONS)
        repeatable.add("--arg")
    parsed_flags = set()
    parsed_options = {}
    index = subcommand_index + 2
    while index < len(argv):
        option = argv[index]
        if "=" in option:
            raise ValidationError("routine launch command options must not use equals form")
        if option in flags:
            if option in parsed_flags:
                raise ValidationError("routine launch command option is duplicated: %s" % option)
            parsed_flags.add(option)
            index += 1
            continue
        if option not in value_options:
            raise ValidationError("routine launch command option is unsupported: %s" % option)
        if index + 1 >= len(argv) or not argv[index + 1]:
            raise ValidationError("routine launch command option requires a value: %s" % option)
        if option not in repeatable and option in parsed_options:
            raise ValidationError("routine launch command option is duplicated: %s" % option)
        parsed_options.setdefault(option, []).append(argv[index + 1])
        index += 2
    for required in ["--workspace", "--runs-dir"]:
        if len(parsed_options.get(required, [])) != 1:
            raise ValidationError("routine launch command must set %s exactly once" % required)
    return {
        "argv": list(argv),
        "subcommand": subcommand,
        "target": target,
        "flags": frozenset(parsed_flags),
        "options": {key: tuple(values) for key, values in parsed_options.items()},
        "runtime_path": str(Path(argv[1])) if subcommand_index == 2 else None,
    }


def _routine_subcommand_index(argv: List[str]) -> int:
    if len(argv) >= 5 and argv[1:3] == ["-B", "-m"] and argv[3] in {
        "conductor_runtime",
        "conductor_extras",
    }:
        if not _is_python_launcher(argv[0]):
            raise ValidationError("routine module launch must use a trusted Python command shape")
        return 4
    if len(argv) >= 3 and _is_python_launcher(argv[0]):
        runtime = Path(argv[1])
        if runtime.is_absolute() and runtime.suffix == ".pyz":
            return 2
    raise ValidationError(
        "routine launch command_argv must invoke the Conductor module or an absolute packaged .pyz"
    )


def _is_python_launcher(value: str) -> bool:
    if value == "python3":
        return True
    path = Path(value)
    if not path.is_absolute():
        return False
    return re.fullmatch(r"python(?:3(?:\.\d+)*)?(?:\.exe)?", path.name.lower()) is not None


def _validate_routine_launch_contract(*, target: Dict, launch: Dict, policy: Dict, command: Dict, source: str) -> None:
    options = command["options"]
    flags = command["flags"]
    workspace = Path(launch["workspace"])
    runs_dir = Path(launch["runs_dir"])
    command_workspace = _routine_effective_path(options["--workspace"][0], workspace)
    if command_workspace != workspace.resolve():
        raise ValidationError("%s launch command workspace must match launch.workspace" % source)
    command_runs_dir = _routine_effective_path(options["--runs-dir"][0], workspace)
    expected_runs_dir = runs_dir.resolve() if runs_dir.is_absolute() else (workspace / runs_dir).resolve()
    if command_runs_dir != expected_runs_dir:
        raise ValidationError("%s launch command runs directory must match launch.runs_dir" % source)
    if target["kind"] == "workflow":
        if command["subcommand"] != "run":
            raise ValidationError("%s workflow target must use the run launch command" % source)
        if _routine_effective_path(command["target"], workspace) != _routine_effective_path(
            target["workflow_path"], workspace
        ):
            raise ValidationError("%s launch command workflow must match target.workflow_path" % source)
    else:
        if command["subcommand"] != "run-saved-workflow" or command["target"] != target["saved_command"]:
            raise ValidationError("%s launch command must match the saved workflow target" % source)
    for field, flag in ROUTINE_CAPABILITY_FLAGS.items():
        if (flag in flags) is not policy[field]:
            raise ValidationError("%s launch command %s must match launch policy" % (source, field))
    raw_workers = options.get("--max-workers", ())
    expected_workers = launch["max_workers"]
    if expected_workers is None:
        if raw_workers:
            raise ValidationError("%s launch command max_workers must match launch.max_workers" % source)
    elif raw_workers != (str(expected_workers),):
        raise ValidationError("%s launch command max_workers must match launch.max_workers" % source)
    runtime_path = command["runtime_path"]
    runtime_sha256 = launch.get("runtime_sha256")
    if runtime_path is None:
        if runtime_sha256 is not None:
            raise ValidationError("%s module launch must not set runtime_sha256" % source)
        return
    expected_sha256 = _validate_sha256(runtime_sha256, "%s launch.runtime_sha256" % source)
    runtime_bytes = read_regular_file_bytes_no_follow(
        Path(runtime_path),
        "routine packaged runtime",
        max_bytes=25 * 1024 * 1024,
    )
    if hashlib.sha256(runtime_bytes).hexdigest() != expected_sha256:
        raise ValidationError("%s packaged runtime changed after the manifest was written" % source)


def _routine_effective_path(value: str, workspace: Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()


def _validate_sha256(value, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValidationError("%s must be a lowercase sha256 hex string" % label)
    return value


def _validate_budget(budget: Dict) -> Dict:
    if not isinstance(budget, dict):
        raise ValidationError("routine budget must be an object")
    _reject_unknown_keys(budget, BUDGET_KEYS, "routine budget")
    result = {}
    if budget.get("max_minutes") is not None:
        value = budget["max_minutes"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 60 * 24 * 7:
            raise ValidationError("routine budget max_minutes must be from 1 to 10080")
        result["max_minutes"] = value
    if budget.get("max_cost_usd") is not None:
        value = budget["max_cost_usd"]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValidationError("routine budget max_cost_usd must be a non-negative number")
        result["max_cost_usd"] = value
    if budget.get("max_tokens") is not None:
        value = budget["max_tokens"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValidationError("routine budget max_tokens must be a positive integer")
        result["max_tokens"] = value
    return result


def _validate_max_workers(value, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > MAX_AGENT_WORKERS:
        raise ValidationError("%s must be an integer from 1 to %d" % (label, MAX_AGENT_WORKERS))


def _reject_unknown_keys(values: Dict, allowed: set, label: str) -> None:
    unknown = sorted(str(key) for key in values if key not in allowed)
    if unknown:
        raise ValidationError("%s contains unsupported field(s): %s" % (label, ", ".join(unknown)))


def _routine_sort_key(path: Path) -> tuple:
    try:
        info = path.lstat()
        return (info.st_mtime, path.name)
    except OSError:
        return (0, path.name)


def routine_schedule_label(schedule: Dict) -> str:
    kind = schedule.get("kind")
    if kind == "manual":
        return "manual"
    if kind == "interval":
        minutes = schedule.get("every_minutes")
        return "every %s %s" % (minutes, "minute" if minutes == 1 else "minutes")
    if kind == "daily":
        return "daily %s %s" % (schedule.get("time"), schedule.get("timezone"))
    if kind == "weekdays":
        return "weekdays %s %s" % (schedule.get("time"), schedule.get("timezone"))
    if kind == "weekly":
        return "weekly %s %s %s" % (schedule.get("day"), schedule.get("time"), schedule.get("timezone"))
    if kind == "monthly":
        return "monthly day %s %s %s" % (schedule.get("day"), schedule.get("time"), schedule.get("timezone"))
    if kind == "once":
        return "once %s" % schedule.get("at_utc")
    if kind == "cron":
        return "cron %s %s" % (schedule.get("expression"), schedule.get("timezone"))
    return "invalid"
