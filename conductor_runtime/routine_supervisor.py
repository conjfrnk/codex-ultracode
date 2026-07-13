import json
import calendar
import hashlib
import os
import signal
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback.
    fcntl = None

from .agent_profiles import bind_agent_profile_static_resources
from .desktop_notification import (
    desktop_notification_status_for_execution,
    send_run_desktop_notification,
)
from .errors import PolicyError, ValidationError
from .redaction import redact_json_value, redact_text
from .routines import (
    MAX_LIST_ROUTINES,
    iter_routine_manifest_paths,
    load_routine_manifest,
    parse_cron_expression,
    parse_routine_launch_command,
    replace_routine_schedule,
    routine_list_record,
    routine_schedule_label,
    validate_schedule,
)
from .runner import DEFAULT_OUTPUT_LIMIT_BYTES, ProcessResult, WorkflowRunner, run_process, terminate_active_processes
from .saved_workflows import apply_saved_workflow_args, parse_saved_workflow_args, resolve_saved_workflow
from .security import (
    ensure_dir_no_follow,
    read_regular_file_bytes_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    validate_approval_tokens,
    write_text_file_no_follow,
    RuntimePolicy,
)
from .workflow import SAFE_ID, load_workflow, workflow_fingerprint


ROUTINE_SUPERVISOR_SCHEMA = "conductor.routine_supervisor.v1"
ROUTINE_CONTROLS_SCHEMA = "conductor.routine_controls.v1"
MAX_ROUTINE_SUPERVISOR_STATE_BYTES = 2 * 1024 * 1024
MAX_ROUTINE_CONTROLS_BYTES = 1024 * 1024
MAX_ROUTINE_LAUNCH_SECONDS = 60 * 60 * 24 * 7
MAX_SUPERVISOR_CYCLES = 1000000
MAX_SUPERVISOR_POLL_SECONDS = 24 * 60 * 60
MAX_ROUTINE_CONTROL_EVENTS = 100
MAX_ROUTINE_HISTORY = 100
MAX_ROUTINE_REASON_CHARS = 512
MAX_ROUTINE_CATCHUP_DAYS = 7
ROUTINE_CONTROL_ACTIONS = {"paused", "resumed", "schedule-updated"}
ROUTINE_EXECUTION_STATUSES = {"running", "completed", "failed", "timed_out", "interrupted", "blocked", "skipped"}
ROUTINE_TRIGGERS = {"manual", "scheduled", "catch-up"}
ROUTINE_STATE_RECORD_FIELDS = {
    "manifest",
    "last_started_at_utc",
    "last_finished_at_utc",
    "last_status",
    "last_trigger",
    "last_scheduled_for_utc",
    "last_returncode",
    "last_duration_ms",
    "last_stdout_log",
    "last_stderr_log",
    "executions",
    "history",
    "approval_values_persisted",
    "owner_pid",
}
ROUTINE_HISTORY_FIELDS = {
    "started_at_utc",
    "finished_at_utc",
    "scheduled_for_utc",
    "trigger",
    "status",
    "returncode",
    "duration_ms",
    "stdout_log",
    "stderr_log",
    "detail",
}
FOREGROUND_SUPERVISOR_APPROVAL = "foreground-supervisor"
BACKGROUND_SUPERVISOR_APPROVAL = "background-supervisor"
SUPERVISOR_APPROVALS = {FOREGROUND_SUPERVISOR_APPROVAL, BACKGROUND_SUPERVISOR_APPROVAL}


def routine_launch_approval(name: str) -> str:
    return "routine:%s" % name


def execute_routine_manifest(
    path: Path,
    approvals: Iterable[str],
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    in_process: bool = False,
    trigger: str = "manual",
    scheduled_for_utc: Optional[str] = None,
    durable_manifest_sha256: Optional[str] = None,
    durable_approval_sha256: Optional[Iterable[str]] = None,
) -> Dict:
    if trigger not in {"manual", "scheduled", "catch-up"}:
        raise ValidationError("routine trigger must be manual, scheduled, or catch-up")
    if scheduled_for_utc is not None and _parse_utc(scheduled_for_utc) is None:
        raise ValidationError("routine scheduled_for_utc must be an ISO timestamp")
    if trigger == "manual" and scheduled_for_utc is not None:
        raise ValidationError("manual routine execution cannot set scheduled_for_utc")
    if trigger != "manual" and scheduled_for_utc is None:
        raise ValidationError("scheduled routine execution requires scheduled_for_utc")
    manifest_path = Path(path)
    manifest = load_routine_manifest(manifest_path)
    durable = durable_manifest_sha256 is not None or durable_approval_sha256 is not None
    workflow_approval_hashes = set()
    if durable:
        if durable_manifest_sha256 is None or durable_approval_sha256 is None:
            raise ValidationError("durable routine authorization requires manifest and approval hash bindings")
        if approvals:
            raise ValidationError("durable routine authorization cannot be combined with transient approvals")
        if not in_process:
            raise ValidationError("durable routine authorization requires in-process execution")
        _validate_sha256(durable_manifest_sha256, "durable routine manifest sha256")
        if _file_sha256(manifest_path, "durable routine manifest") != durable_manifest_sha256:
            raise PolicyError("durable routine authorization no longer matches the manifest")
        workflow_approval_hashes = _validate_approval_sha256(durable_approval_sha256)
        workflow_approvals = []
    else:
        approval_set = set(approvals or [])
        launch_approval = routine_launch_approval(manifest["name"])
        if launch_approval not in approval_set:
            raise PolicyError("routine launch requires --approve %s" % launch_approval)
        workflow_approvals = sorted(approval_set - {launch_approval} - SUPERVISOR_APPROVALS)
    required_count = manifest["launch"]["policy"]["approval_count"]
    available_count = len(workflow_approval_hashes) if durable else len(workflow_approvals)
    if available_count < required_count:
        raise PolicyError(
            "routine launch requires at least %d workflow approval authorization(s)"
            % required_count
        )
    workflow = _validate_current_target(manifest)
    workspace = Path(manifest["launch"]["workspace"])
    reject_symlink_path(workspace, "routine workspace")
    if not workspace.is_dir():
        raise ValidationError("routine workspace does not exist: %s" % workspace)
    argv = list(manifest["launch"]["command_argv"])
    for approval in workflow_approvals:
        argv.extend(["--approve", approval])
    timeout = _routine_timeout_seconds(manifest)
    started = _utc_now()
    _reserve_routine_execution(
        manifest_path.parent,
        manifest,
        started,
        trigger=trigger,
        scheduled_for_utc=scheduled_for_utc,
    )
    monotonic_started = time.monotonic()
    try:
        if in_process:
            result = _execute_routine_in_process(
                manifest=manifest,
                workflow=workflow,
                workflow_approvals=workflow_approvals,
                workflow_approval_sha256=workflow_approval_hashes,
                timeout=timeout,
            )
        else:
            result = run_process(
                argv=argv,
                cwd=workspace,
                timeout=timeout,
                output_limit_bytes=output_limit_bytes,
            )
    except Exception as exc:
        finished = _utc_now()
        record = {
            "name": manifest["name"],
            "manifest": str(manifest_path),
            "started_at_utc": started,
            "finished_at_utc": finished,
            "duration_ms": max(0, int((time.monotonic() - monotonic_started) * 1000)),
            "status": "failed",
            "trigger": trigger,
            "scheduled_for_utc": scheduled_for_utc,
            "returncode": -1,
            "timed_out": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "approval_values_persisted": False,
        }
        _record_routine_execution(manifest_path.parent, manifest, record, "", str(exc))
        raise
    except BaseException as exc:
        finished = _utc_now()
        record = {
            "name": manifest["name"],
            "manifest": str(manifest_path),
            "started_at_utc": started,
            "finished_at_utc": finished,
            "duration_ms": max(0, int((time.monotonic() - monotonic_started) * 1000)),
            "status": "interrupted",
            "trigger": trigger,
            "scheduled_for_utc": scheduled_for_utc,
            "returncode": -1,
            "timed_out": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "approval_values_persisted": False,
        }
        _record_routine_execution(manifest_path.parent, manifest, record, "", str(exc))
        raise
    finished = _utc_now()
    record = {
        "name": manifest["name"],
        "manifest": str(manifest_path),
        "started_at_utc": started,
        "finished_at_utc": finished,
        "duration_ms": max(0, int((time.monotonic() - monotonic_started) * 1000)),
        "status": "timed_out" if result.timed_out else ("completed" if result.returncode == 0 else "failed"),
        "trigger": trigger,
        "scheduled_for_utc": scheduled_for_utc,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
        "approval_values_persisted": False,
    }
    _record_routine_execution(manifest_path.parent, manifest, record, result.stdout, result.stderr)
    return record


def run_due_routines(
    routines_dir: Path,
    approvals: Iterable[str],
    max_routines: int = 50,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    now: Optional[datetime] = None,
    in_process: bool = False,
    durable_authorizations: Optional[Dict[str, Dict]] = None,
) -> List[Dict]:
    directory = Path(routines_dir)
    state = load_supervisor_state(directory)
    controls = load_routine_controls(directory)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValidationError("routine supervisor current time must be timezone-aware")
    results = []
    if durable_authorizations is None:
        paths = list(reversed(iter_routine_manifest_paths(directory, max_routines=max_routines)))
    else:
        if not isinstance(durable_authorizations, dict) or len(durable_authorizations) > max_routines:
            raise ValidationError("durable routine authorizations must be a bounded object")
        paths = []
        for raw_path in sorted(durable_authorizations):
            path = Path(raw_path)
            if path.parent.resolve() != directory.resolve():
                raise ValidationError("durable routine manifest must be directly under the routines directory")
            paths.append(path)
    seen_names = {}
    for path in paths:
        try:
            candidate = load_routine_manifest(path)
        except (PolicyError, ValidationError):
            continue
        previous_path = seen_names.get(candidate["name"])
        if previous_path is not None:
            raise ValidationError(
                "routine names must be unique within a routines directory: %s (%s and %s)"
                % (candidate["name"], previous_path.name, path.name)
            )
        seen_names[candidate["name"]] = path
    for path in paths:
        manifest = None
        scheduled_for_utc = None
        try:
            manifest = load_routine_manifest(path)
            previous = state.get("routines", {}).get(manifest["name"], {})
            scheduled_for = routine_due_slot(manifest, previous, current)
            if scheduled_for is None:
                continue
            scheduled_for_utc = _format_utc(scheduled_for)
            control = routine_control_record(manifest["name"], controls)
            if control["paused"]:
                _record_scheduled_nonexecution(
                    directory,
                    manifest,
                    manifest_path=path,
                    scheduled_for_utc=scheduled_for_utc,
                    status="skipped",
                    detail="routine is paused",
                )
                state = load_supervisor_state(directory)
                continue
            lag_seconds = max(0.0, (current.astimezone(timezone.utc) - scheduled_for).total_seconds())
            trigger = "catch-up" if lag_seconds >= 60 else "scheduled"
            results.append(
                execute_routine_manifest(
                    path,
                    approvals=approvals,
                    output_limit_bytes=output_limit_bytes,
                    in_process=in_process,
                    trigger=trigger,
                    scheduled_for_utc=scheduled_for_utc,
                    durable_manifest_sha256=(
                        durable_authorizations[str(path)].get("manifest_sha256")
                        if durable_authorizations is not None
                        else None
                    ),
                    durable_approval_sha256=(
                        durable_authorizations[str(path)].get("approval_sha256")
                        if durable_authorizations is not None
                        else None
                    ),
                )
            )
            state = load_supervisor_state(directory)
        except (PolicyError, ValidationError) as exc:
            result = {
                "name": manifest["name"] if isinstance(manifest, dict) else path.stem,
                "manifest": str(path),
                "status": "blocked",
                "detail": redact_text(str(exc)),
                "approval_values_persisted": False,
            }
            if scheduled_for_utc is not None and isinstance(manifest, dict):
                result["scheduled_for_utc"] = scheduled_for_utc
                result["trigger"] = "scheduled"
                _record_scheduled_nonexecution(
                    directory,
                    manifest,
                    manifest_path=path,
                    scheduled_for_utc=scheduled_for_utc,
                    status="blocked",
                    detail=str(exc),
                )
                state = load_supervisor_state(directory)
            results.append(result)
        finally:
            manifest = None
            scheduled_for_utc = None
    return results


def supervise_routines(
    routines_dir: Path,
    approvals: Iterable[str],
    poll_seconds: int = 60,
    max_cycles: int = 1,
    max_routines: int = 50,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    background: bool = False,
    cycle_callback: Optional[Callable[[int, List[Dict]], None]] = None,
) -> List[Dict]:
    prepared = validate_supervise_routines_request(
        routines_dir=routines_dir,
        approvals=approvals,
        poll_seconds=poll_seconds,
        max_cycles=max_cycles,
        max_routines=max_routines,
        output_limit_bytes=output_limit_bytes,
        background=background,
    )
    if cycle_callback is not None and not callable(cycle_callback):
        raise ValidationError("cycle_callback must be callable")
    approval_set = prepared["approvals"]
    routines_dir = prepared["routines_dir"]
    all_results = []
    cycle = 0
    while max_cycles == 0 or cycle < max_cycles:
        cycle += 1
        cycle_results = run_due_routines(
            routines_dir,
            approvals=approval_set,
            max_routines=max_routines,
            output_limit_bytes=output_limit_bytes,
            in_process=background,
        )
        all_results.extend(cycle_results)
        if cycle_callback is not None:
            cycle_callback(cycle, cycle_results)
        if max_cycles != 0 and cycle >= max_cycles:
            break
        time.sleep(poll_seconds)
    return all_results


def validate_supervise_routines_request(
    *,
    routines_dir: Path,
    approvals: Iterable[str],
    poll_seconds: int,
    max_cycles: int,
    max_routines: int,
    output_limit_bytes: int,
    background: bool,
) -> Dict:
    if not isinstance(background, bool):
        raise ValidationError("background must be boolean")
    if not isinstance(poll_seconds, int) or isinstance(poll_seconds, bool) or not 1 <= poll_seconds <= MAX_SUPERVISOR_POLL_SECONDS:
        raise ValidationError("poll_seconds must be from 1 to %d" % MAX_SUPERVISOR_POLL_SECONDS)
    if not isinstance(max_cycles, int) or isinstance(max_cycles, bool) or not 0 <= max_cycles <= MAX_SUPERVISOR_CYCLES:
        raise ValidationError("max_cycles must be from 0 to %d" % MAX_SUPERVISOR_CYCLES)
    if not isinstance(max_routines, int) or isinstance(max_routines, bool) or not 1 <= max_routines <= MAX_LIST_ROUTINES:
        raise ValidationError("max_routines must be from 1 to %d" % MAX_LIST_ROUTINES)
    if (
        not isinstance(output_limit_bytes, int)
        or isinstance(output_limit_bytes, bool)
        or not 1 <= output_limit_bytes <= DEFAULT_OUTPUT_LIMIT_BYTES
    ):
        raise ValidationError("output_limit_bytes must be from 1 to %d" % DEFAULT_OUTPUT_LIMIT_BYTES)
    approval_set = validate_approval_tokens(approvals)
    required = BACKGROUND_SUPERVISOR_APPROVAL if background else FOREGROUND_SUPERVISOR_APPROVAL
    if background and required not in approval_set:
        raise PolicyError("background supervision requires --approve %s" % required)
    if not background and max_cycles != 1 and required not in approval_set:
        raise PolicyError("multi-cycle supervision requires --approve %s" % required)
    directory = Path(routines_dir)
    reject_symlink_path(directory, "routines directory")
    if directory.exists() and not directory.is_dir():
        raise ValidationError("routines directory is not a directory: %s" % directory)
    return {
        "routines_dir": directory.resolve(),
        "approvals": approval_set,
        "poll_seconds": poll_seconds,
        "max_cycles": max_cycles,
        "max_routines": max_routines,
        "output_limit_bytes": output_limit_bytes,
        "background": background,
    }


def routine_is_due(manifest: Dict, previous: Dict, now: datetime) -> bool:
    return routine_due_slot(manifest, previous, now) is not None


def routine_due_slot(manifest: Dict, previous: Dict, now: datetime) -> Optional[datetime]:
    if now.tzinfo is None:
        raise ValidationError("routine current time must be timezone-aware")
    schedule = manifest["schedule"]
    kind = schedule["kind"]
    if kind == "manual":
        return None
    previous = previous if isinstance(previous, dict) else {}
    last_scheduled = _parse_utc(previous.get("last_scheduled_for_utc"))
    if last_scheduled is None and previous.get("last_trigger") != "manual":
        last_scheduled = _parse_utc(previous.get("last_started_at_utc"))
    current = now.astimezone(timezone.utc)
    created = _parse_utc(manifest.get("created_at_utc"))
    if kind == "interval":
        if last_scheduled is None:
            return current
        interval = timedelta(minutes=schedule["every_minutes"])
        if current < last_scheduled + interval:
            return None
        periods = max(1, int((current - last_scheduled) // interval))
        return last_scheduled + periods * interval
    if kind == "once":
        candidate = _parse_utc(schedule["at_utc"])
        if candidate is None or candidate > current:
            return None
        if last_scheduled is not None and last_scheduled >= candidate:
            return None
        if current - candidate > timedelta(days=MAX_ROUTINE_CATCHUP_DAYS):
            return None
        return candidate
    try:
        zone = ZoneInfo(schedule["timezone"])
    except ZoneInfoNotFoundError:
        raise ValidationError("routine timezone is not available: %s" % schedule["timezone"])
    if kind == "cron":
        return _latest_cron_slot(
            schedule,
            current=current,
            created=created,
            last_scheduled=last_scheduled,
            zone=zone,
        )
    local_now = current.astimezone(zone)
    hour, minute = [int(part) for part in schedule["time"].split(":")]
    if kind == "daily":
        scheduled = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled > local_now:
            scheduled -= timedelta(days=1)
    elif kind == "weekdays":
        scheduled = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled > local_now:
            scheduled -= timedelta(days=1)
        while scheduled.weekday() >= 5:
            scheduled -= timedelta(days=1)
    elif kind == "weekly":
        target_weekday = [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ].index(schedule["day"])
        days_since = (local_now.weekday() - target_weekday) % 7
        scheduled = (local_now - timedelta(days=days_since)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled > local_now:
            scheduled -= timedelta(days=7)
    else:
        scheduled = _latest_monthly_slot(local_now, schedule["day"], hour, minute)
    candidate = scheduled.astimezone(timezone.utc)
    if created is not None and candidate < created:
        return None
    if current - candidate > timedelta(days=MAX_ROUTINE_CATCHUP_DAYS):
        return None
    if last_scheduled is not None and last_scheduled >= candidate:
        return None
    return candidate


def _latest_cron_slot(
    schedule: Dict,
    *,
    current: datetime,
    created: Optional[datetime],
    last_scheduled: Optional[datetime],
    zone: ZoneInfo,
) -> Optional[datetime]:
    parsed = parse_cron_expression(schedule["expression"])
    candidate = current.replace(second=0, microsecond=0)
    lower_bound = current - timedelta(days=MAX_ROUTINE_CATCHUP_DAYS)
    if created is not None and created > lower_bound:
        lower_bound = created
    if last_scheduled is not None and last_scheduled > lower_bound:
        lower_bound = last_scheduled
    while candidate >= lower_bound:
        if last_scheduled is not None and candidate <= last_scheduled:
            return None
        if created is not None and candidate < created:
            return None
        if _cron_matches_local(parsed, candidate.astimezone(zone)):
            return candidate
        candidate -= timedelta(minutes=1)
    return None


def _cron_matches_local(parsed: Dict, local_time: datetime) -> bool:
    minute, hour, day_of_month, month, day_of_week = parsed["fields"]
    if (
        local_time.minute not in minute
        or local_time.hour not in hour
        or local_time.month not in month
    ):
        return False
    dom_matches = local_time.day in day_of_month
    cron_weekday = (local_time.weekday() + 1) % 7
    dow_matches = cron_weekday in day_of_week
    if parsed["day_of_month_wildcard"] or parsed["day_of_week_wildcard"]:
        return dom_matches and dow_matches
    return dom_matches or dow_matches


def _latest_monthly_slot(local_now: datetime, day: int, hour: int, minute: int) -> datetime:
    year = local_now.year
    month = local_now.month
    for _ in range(2):
        month_day = min(day, calendar.monthrange(year, month)[1])
        candidate = local_now.replace(
            year=year,
            month=month,
            day=month_day,
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
        if candidate <= local_now:
            return candidate
        if month == 1:
            year -= 1
            month = 12
        else:
            month -= 1
    raise ValidationError("failed to resolve monthly routine slot")


def load_routine_controls(routines_dir: Path) -> Dict:
    path = _routine_controls_path(Path(routines_dir))
    reject_symlink_path(path, "routine controls")
    if not path.exists():
        return {"schema": ROUTINE_CONTROLS_SCHEMA, "updated_at_utc": None, "controls": {}}
    return load_routine_controls_file(path)


def load_routine_controls_file(path: Path) -> Dict:
    path = Path(path)
    reject_symlink_path(path, "routine controls")
    raw = read_regular_text_file_no_follow(path, "routine controls", MAX_ROUTINE_CONTROLS_BYTES)
    try:
        controls = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("routine controls are not strict JSON: %s" % exc)
    validate_routine_controls(controls)
    return controls


def validate_routine_controls(document: Dict) -> None:
    if not isinstance(document, dict) or set(document) != {"schema", "updated_at_utc", "controls"}:
        raise ValidationError("routine controls must contain exactly schema, updated_at_utc, and controls")
    if document.get("schema") != ROUTINE_CONTROLS_SCHEMA:
        raise ValidationError("routine controls have an unsupported schema")
    updated = document.get("updated_at_utc")
    if updated is not None and _parse_utc(updated) is None:
        raise ValidationError("routine controls updated_at_utc is invalid")
    controls = document.get("controls")
    if not isinstance(controls, dict) or len(controls) > MAX_LIST_ROUTINES:
        raise ValidationError("routine controls mapping is invalid")
    for name, record in controls.items():
        if not isinstance(name, str) or not SAFE_ID.match(name):
            raise ValidationError("routine control name is invalid")
        _validate_routine_control_record(name, record)


def routine_control_record(name: str, controls: Optional[Dict] = None) -> Dict:
    document = controls if controls is not None else {"controls": {}}
    mapping = document.get("controls") if isinstance(document, dict) else {}
    record = mapping.get(name) if isinstance(mapping, dict) else None
    if isinstance(record, dict):
        return dict(record)
    return {
        "name": name,
        "paused": False,
        "updated_at_utc": None,
        "last_action": "",
        "reason": "",
        "events": [],
    }


def set_routine_paused(
    path: Path,
    *,
    paused: bool,
    approvals: Iterable[str] = (),
    reason: str = "",
) -> Dict:
    if not isinstance(paused, bool):
        raise ValidationError("paused must be boolean")
    _validate_routine_reason(reason)
    manifest_path = Path(path)
    manifest = load_routine_manifest(manifest_path)
    if not paused:
        _require_routine_approval(manifest["name"], approvals, "resume")
    routines_dir = manifest_path.parent
    with _supervisor_lock(routines_dir):
        document = load_routine_controls(routines_dir)
        previous = routine_control_record(manifest["name"], document)
        changed = previous["paused"] != paused
        if changed:
            action = "paused" if paused else "resumed"
            updated = _updated_routine_control(previous, action=action, paused=paused, reason=reason)
            document["controls"][manifest["name"]] = updated
            document["updated_at_utc"] = updated["updated_at_utc"]
            _write_routine_controls(routines_dir, document)
        else:
            updated = previous
    return {
        "schema": "conductor.routine_control_result.v1",
        "action": "pause-routine" if paused else "resume-routine",
        "changed": changed,
        "routine": manifest["name"],
        "manifest": str(manifest_path),
        "control": updated,
        "approval_values_persisted": False,
    }


def update_routine_schedule(
    path: Path,
    *,
    schedule: Dict,
    approvals: Iterable[str],
    reason: str = "",
) -> Dict:
    validate_schedule(schedule)
    _validate_routine_reason(reason)
    manifest_path = Path(path)
    manifest = load_routine_manifest(manifest_path)
    _require_routine_approval(manifest["name"], approvals, "schedule update")
    before = dict(manifest["schedule"])
    changed = before != schedule
    routines_dir = manifest_path.parent
    with _supervisor_lock(routines_dir):
        document = load_routine_controls(routines_dir)
        previous = routine_control_record(manifest["name"], document)
        if changed:
            updated_manifest = replace_routine_schedule(manifest_path, schedule)
            updated = _updated_routine_control(
                previous,
                action="schedule-updated",
                paused=previous["paused"],
                reason=reason,
                schedule_before=before,
                schedule_after=dict(schedule),
            )
            document["controls"][manifest["name"]] = updated
            document["updated_at_utc"] = updated["updated_at_utc"]
            _write_routine_controls(routines_dir, document)
        else:
            updated_manifest = manifest
            updated = previous
    return {
        "schema": "conductor.routine_control_result.v1",
        "action": "update-routine-schedule",
        "changed": changed,
        "routine": manifest["name"],
        "manifest": str(manifest_path),
        "schedule_before": routine_schedule_label(before),
        "schedule_after": routine_schedule_label(updated_manifest["schedule"]),
        "control": updated,
        "approval_values_persisted": False,
    }


def routine_status(path: Path) -> Dict:
    manifest_path = Path(path)
    manifest = load_routine_manifest(manifest_path)
    routines_dir = manifest_path.parent
    controls = load_routine_controls(routines_dir)
    state = load_supervisor_state(routines_dir)
    execution = state.get("routines", {}).get(manifest["name"], {})
    return {
        "schema": "conductor.routine_status.v1",
        "manifest": routine_list_record(manifest_path, manifest),
        "control": routine_control_record(manifest["name"], controls),
        "execution": execution if isinstance(execution, dict) else {},
    }


def _updated_routine_control(
    previous: Dict,
    *,
    action: str,
    paused: bool,
    reason: str,
    schedule_before: Optional[Dict] = None,
    schedule_after: Optional[Dict] = None,
) -> Dict:
    timestamp = _utc_now()
    event = {
        "action": action,
        "at_utc": timestamp,
        "reason": _clean_routine_reason(reason),
        "schedule_before": schedule_before,
        "schedule_after": schedule_after,
    }
    events = list(previous.get("events", [])) if isinstance(previous.get("events"), list) else []
    events.append(event)
    events = events[-MAX_ROUTINE_CONTROL_EVENTS:]
    return {
        "name": previous["name"],
        "paused": paused,
        "updated_at_utc": timestamp,
        "last_action": action,
        "reason": event["reason"],
        "events": events,
    }


def _validate_routine_control_record(name: str, record: Dict) -> None:
    fields = {"name", "paused", "updated_at_utc", "last_action", "reason", "events"}
    if not isinstance(record, dict) or set(record) != fields or record.get("name") != name:
        raise ValidationError("routine control record is invalid for %s" % name)
    if not isinstance(record.get("paused"), bool) or _parse_utc(record.get("updated_at_utc")) is None:
        raise ValidationError("routine control state is invalid for %s" % name)
    if record.get("last_action") not in ROUTINE_CONTROL_ACTIONS:
        raise ValidationError("routine control action is invalid for %s" % name)
    if not isinstance(record.get("reason"), str) or len(record["reason"]) > MAX_ROUTINE_REASON_CHARS:
        raise ValidationError("routine control reason is invalid for %s" % name)
    events = record.get("events")
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_ROUTINE_CONTROL_EVENTS:
        raise ValidationError("routine control events are invalid for %s" % name)
    for event in events:
        expected = {"action", "at_utc", "reason", "schedule_before", "schedule_after"}
        if not isinstance(event, dict) or set(event) != expected:
            raise ValidationError("routine control event is invalid for %s" % name)
        if event.get("action") not in ROUTINE_CONTROL_ACTIONS or _parse_utc(event.get("at_utc")) is None:
            raise ValidationError("routine control event metadata is invalid for %s" % name)
        if not isinstance(event.get("reason"), str) or len(event["reason"]) > MAX_ROUTINE_REASON_CHARS:
            raise ValidationError("routine control event reason is invalid for %s" % name)
        for key in ("schedule_before", "schedule_after"):
            value = event.get(key)
            if value is not None:
                validate_schedule(value)
        if event["action"] == "schedule-updated":
            if event["schedule_before"] is None or event["schedule_after"] is None:
                raise ValidationError("schedule-updated routine control event requires schedules")
        elif event["schedule_before"] is not None or event["schedule_after"] is not None:
            raise ValidationError("pause or resume routine control event cannot contain schedules")
    if events[-1]["action"] != record["last_action"] or events[-1]["at_utc"] != record["updated_at_utc"]:
        raise ValidationError("routine control latest event is inconsistent for %s" % name)


def _write_routine_controls(routines_dir: Path, document: Dict) -> None:
    validate_routine_controls(document)
    text = json.dumps(redact_json_value(document), indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_ROUTINE_CONTROLS_BYTES:
        raise ValidationError("routine controls exceed the supported size")
    replace_text_file_no_follow(
        _routine_controls_path(routines_dir, create=True),
        "routine controls",
        text,
        ".routine-controls-",
    )


def _require_routine_approval(name: str, approvals: Iterable[str], action: str) -> None:
    approval = routine_launch_approval(name)
    if approval not in validate_approval_tokens(approvals):
        raise PolicyError("routine %s requires --approve %s" % (action, approval))


def _validate_routine_reason(reason: str) -> None:
    if not isinstance(reason, str) or len(reason) > MAX_ROUTINE_REASON_CHARS:
        raise ValidationError("routine reason must be at most %d characters" % MAX_ROUTINE_REASON_CHARS)


def _clean_routine_reason(reason: str) -> str:
    return redact_text(" ".join(str(reason).split()))[:MAX_ROUTINE_REASON_CHARS]


def load_supervisor_state(routines_dir: Path) -> Dict:
    path = _supervisor_state_path(Path(routines_dir))
    reject_symlink_path(path, "routine supervisor state")
    if not path.exists():
        return {"schema": ROUTINE_SUPERVISOR_SCHEMA, "updated_at_utc": _utc_now(), "routines": {}}
    return load_supervisor_state_file(path)


def load_supervisor_state_file(path: Path) -> Dict:
    path = Path(path)
    reject_symlink_path(path, "routine supervisor state")
    try:
        state = json.loads(
            read_regular_text_file_no_follow(path, "routine supervisor state", MAX_ROUTINE_SUPERVISOR_STATE_BYTES),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("routine supervisor state is not strict JSON: %s" % exc)
    validate_supervisor_state(state)
    return state


def validate_supervisor_state(state: Dict) -> None:
    if not isinstance(state, dict) or set(state) != {"schema", "updated_at_utc", "routines"}:
        raise ValidationError("routine supervisor state has an unsupported shape")
    if state.get("schema") != ROUTINE_SUPERVISOR_SCHEMA or _parse_utc(state.get("updated_at_utc")) is None:
        raise ValidationError("routine supervisor state metadata is invalid")
    routines = state.get("routines")
    if not isinstance(routines, dict) or len(routines) > MAX_LIST_ROUTINES:
        raise ValidationError("routine supervisor state routines mapping is invalid")
    for name, record in routines.items():
        if not isinstance(name, str) or not SAFE_ID.match(name):
            raise ValidationError("routine supervisor state routine name is invalid")
        _validate_supervisor_routine_record(name, record)


def _validate_supervisor_routine_record(name: str, record: Dict) -> None:
    if not isinstance(record, dict) or not set(record).issubset(ROUTINE_STATE_RECORD_FIELDS):
        raise ValidationError("routine supervisor state record is invalid for %s" % name)
    for key in ("manifest", "last_stdout_log", "last_stderr_log"):
        if key in record and not isinstance(record[key], str):
            raise ValidationError("routine supervisor state %s is invalid for %s" % (key, name))
    for key in ("last_started_at_utc", "last_finished_at_utc", "last_scheduled_for_utc"):
        if key in record and record[key] is not None and _parse_utc(record[key]) is None:
            raise ValidationError("routine supervisor state %s is invalid for %s" % (key, name))
    if record.get("last_status") not in ROUTINE_EXECUTION_STATUSES:
        raise ValidationError("routine supervisor state status is invalid for %s" % name)
    if record.get("last_trigger") is not None and record["last_trigger"] not in ROUTINE_TRIGGERS:
        raise ValidationError("routine supervisor state trigger is invalid for %s" % name)
    returncode = record.get("last_returncode")
    if returncode is not None and (not isinstance(returncode, int) or isinstance(returncode, bool)):
        raise ValidationError("routine supervisor state returncode is invalid for %s" % name)
    duration = record.get("last_duration_ms")
    if duration is not None and (not isinstance(duration, int) or isinstance(duration, bool) or duration < 0):
        raise ValidationError("routine supervisor state duration is invalid for %s" % name)
    executions = record.get("executions", 0)
    if not isinstance(executions, int) or isinstance(executions, bool) or executions < 0:
        raise ValidationError("routine supervisor state executions is invalid for %s" % name)
    if record.get("approval_values_persisted") is not False:
        raise ValidationError("routine supervisor state must not persist approvals for %s" % name)
    owner_pid = record.get("owner_pid")
    if owner_pid is not None and (not isinstance(owner_pid, int) or isinstance(owner_pid, bool) or owner_pid <= 0):
        raise ValidationError("routine supervisor state owner_pid is invalid for %s" % name)
    if record["last_status"] == "running" and owner_pid is None:
        raise ValidationError("running routine supervisor state requires owner_pid for %s" % name)
    history = record.get("history", [])
    if not isinstance(history, list) or len(history) > MAX_ROUTINE_HISTORY:
        raise ValidationError("routine supervisor history is invalid for %s" % name)
    for entry in history:
        _validate_routine_history_entry(name, entry)


def _validate_routine_history_entry(name: str, entry: Dict) -> None:
    if not isinstance(entry, dict) or set(entry) != ROUTINE_HISTORY_FIELDS:
        raise ValidationError("routine history entry is invalid for %s" % name)
    if _parse_utc(entry.get("started_at_utc")) is None or _parse_utc(entry.get("finished_at_utc")) is None:
        raise ValidationError("routine history timestamps are invalid for %s" % name)
    if entry.get("scheduled_for_utc") is not None and _parse_utc(entry["scheduled_for_utc"]) is None:
        raise ValidationError("routine history schedule timestamp is invalid for %s" % name)
    if entry.get("trigger") not in ROUTINE_TRIGGERS or entry.get("status") not in ROUTINE_EXECUTION_STATUSES - {"running"}:
        raise ValidationError("routine history status or trigger is invalid for %s" % name)
    returncode = entry.get("returncode")
    if returncode is not None and (not isinstance(returncode, int) or isinstance(returncode, bool)):
        raise ValidationError("routine history returncode is invalid for %s" % name)
    if not isinstance(entry.get("duration_ms"), int) or isinstance(entry["duration_ms"], bool) or entry["duration_ms"] < 0:
        raise ValidationError("routine history duration is invalid for %s" % name)
    for key in ("stdout_log", "stderr_log", "detail"):
        if not isinstance(entry.get(key), str):
            raise ValidationError("routine history %s is invalid for %s" % (key, name))
    if len(entry["detail"]) > MAX_ROUTINE_REASON_CHARS:
        raise ValidationError("routine history detail is too long for %s" % name)


def _record_routine_execution(
    routines_dir: Path,
    manifest: Dict,
    record: Dict,
    stdout: str,
    stderr: str,
) -> None:
    supervisor_dir = _supervisor_dir(routines_dir)
    logs_dir = supervisor_dir / "logs"
    fd = ensure_dir_no_follow(logs_dir, "routine supervisor logs")
    os.close(fd)
    stamp = record["started_at_utc"].replace(":", "").replace("-", "").replace(".", "")
    prefix = "%s-%s" % (manifest["name"], stamp)
    stdout_path = logs_dir / (prefix + ".stdout.log")
    stderr_path = logs_dir / (prefix + ".stderr.log")
    write_text_file_no_follow(stdout_path, "routine stdout log", redact_text(stdout))
    write_text_file_no_follow(stderr_path, "routine stderr log", redact_text(stderr))
    public_record = dict(record)
    public_record["stdout_log"] = str(stdout_path)
    public_record["stderr_log"] = str(stderr_path)
    with _supervisor_lock(routines_dir):
        state = load_supervisor_state(routines_dir)
        routines = state.setdefault("routines", {})
        previous = routines.get(manifest["name"], {})
        executions = previous.get("executions", 0) if isinstance(previous, dict) else 0
        history = _routine_history(previous)
        history.append(
            {
                "started_at_utc": record["started_at_utc"],
                "finished_at_utc": record["finished_at_utc"],
                "scheduled_for_utc": record.get("scheduled_for_utc"),
                "trigger": record.get("trigger") or "manual",
                "status": record["status"],
                "returncode": record["returncode"],
                "duration_ms": record["duration_ms"],
                "stdout_log": str(stdout_path),
                "stderr_log": str(stderr_path),
                "detail": "",
            }
        )
        last_scheduled_for = record.get("scheduled_for_utc") or (
            previous.get("last_scheduled_for_utc") if isinstance(previous, dict) else None
        )
        routines[manifest["name"]] = {
            "manifest": str(manifest.get("_source_path") or record["manifest"]),
            "last_started_at_utc": record["started_at_utc"],
            "last_finished_at_utc": record["finished_at_utc"],
            "last_status": record["status"],
            "last_trigger": record.get("trigger") or "manual",
            "last_scheduled_for_utc": last_scheduled_for,
            "last_returncode": record["returncode"],
            "last_duration_ms": record["duration_ms"],
            "last_stdout_log": str(stdout_path),
            "last_stderr_log": str(stderr_path),
            "executions": executions + 1,
            "history": history[-MAX_ROUTINE_HISTORY:],
            "approval_values_persisted": False,
        }
        state["updated_at_utc"] = _utc_now()
        _write_supervisor_state(routines_dir, state)


def _record_scheduled_nonexecution(
    routines_dir: Path,
    manifest: Dict,
    *,
    manifest_path: Path,
    scheduled_for_utc: str,
    status: str,
    detail: str,
) -> None:
    if status not in {"blocked", "skipped"}:
        raise ValidationError("scheduled nonexecution status must be blocked or skipped")
    timestamp = _utc_now()
    with _supervisor_lock(routines_dir):
        state = load_supervisor_state(routines_dir)
        routines = state.setdefault("routines", {})
        previous = routines.get(manifest["name"], {})
        previous = previous if isinstance(previous, dict) else {}
        history = _routine_history(previous)
        history.append(
            {
                "started_at_utc": timestamp,
                "finished_at_utc": timestamp,
                "scheduled_for_utc": scheduled_for_utc,
                "trigger": "scheduled",
                "status": status,
                "returncode": None,
                "duration_ms": 0,
                "stdout_log": "",
                "stderr_log": "",
                "detail": _clean_routine_reason(detail),
            }
        )
        updated = dict(previous)
        if previous.get("last_status") != "running":
            updated.update(
                {
                    "manifest": str(manifest.get("_source_path") or ""),
                    "last_started_at_utc": timestamp,
                    "last_finished_at_utc": timestamp,
                    "last_status": status,
                    "last_trigger": "scheduled",
                    "last_returncode": None,
                    "last_duration_ms": 0,
                }
            )
        updated["last_scheduled_for_utc"] = scheduled_for_utc
        updated["manifest"] = str(manifest_path)
        updated["history"] = history[-MAX_ROUTINE_HISTORY:]
        updated["approval_values_persisted"] = False
        routines[manifest["name"]] = updated
        state["updated_at_utc"] = timestamp
        _write_supervisor_state(routines_dir, state)


def _routine_history(previous: Dict) -> List[Dict]:
    history = previous.get("history") if isinstance(previous, dict) else None
    return list(history[-MAX_ROUTINE_HISTORY:]) if isinstance(history, list) else []


def _reserve_routine_execution(
    routines_dir: Path,
    manifest: Dict,
    started: str,
    *,
    trigger: str,
    scheduled_for_utc: Optional[str],
) -> None:
    with _supervisor_lock(routines_dir):
        state = load_supervisor_state(routines_dir)
        routines = state.setdefault("routines", {})
        previous = routines.get(manifest["name"], {})
        if isinstance(previous, dict) and previous.get("last_status") == "running":
            owner_pid = previous.get("owner_pid")
            if isinstance(owner_pid, int) and owner_pid > 0 and _pid_is_running(owner_pid):
                raise PolicyError("routine %s is already running under supervisor pid %d" % (manifest["name"], owner_pid))
        updated = dict(previous) if isinstance(previous, dict) else {}
        updated.update(
            {
                "manifest": str(manifest.get("_source_path") or ""),
                "last_started_at_utc": started,
                "last_status": "running",
                "last_trigger": trigger,
                "last_scheduled_for_utc": scheduled_for_utc or updated.get("last_scheduled_for_utc"),
                "owner_pid": os.getpid(),
                "approval_values_persisted": False,
            }
        )
        routines[manifest["name"]] = updated
        state["updated_at_utc"] = _utc_now()
        _write_supervisor_state(routines_dir, state)


def _validate_current_target(manifest: Dict) -> Dict:
    target = manifest["target"]
    workspace = Path(manifest["launch"]["workspace"])
    if target["kind"] == "workflow":
        target_path = Path(target["workflow_path"])
        path = target_path if target_path.is_absolute() else workspace / target_path
        current = load_workflow(path)
    else:
        command = parse_routine_launch_command(manifest["launch"]["command_argv"])
        options = command["options"]
        saved = resolve_saved_workflow(
            target["saved_command"],
            workspace=workspace,
            project_root=(Path(options["--project-root"][0]) if options.get("--project-root") else None),
            include_personal="--include-personal" in command["flags"],
            personal_workflows_dir=(
                Path(options["--personal-workflows-dir"][0])
                if options.get("--personal-workflows-dir")
                else None
            ),
        )
        if saved.path.resolve() != Path(target["saved_path"]).resolve() or saved.scope != target["saved_scope"]:
            raise ValidationError("routine saved workflow resolution changed after the manifest was written")
        raw_args = list(options.get("--arg", ()))
        current = apply_saved_workflow_args(saved, parse_saved_workflow_args(raw_args)).workflow
    current = bind_agent_profile_static_resources(current, workspace)
    if workflow_fingerprint(current) != target["workflow_fingerprint"]:
        raise ValidationError("routine target changed after the manifest was written; write a new routine manifest")
    return current


def _execute_routine_in_process(
    *,
    manifest: Dict,
    workflow: Dict,
    workflow_approvals: List[str],
    workflow_approval_sha256: Optional[Iterable[str]] = None,
    timeout: int,
) -> ProcessResult:
    if os.name != "posix" or not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        raise ValidationError("in-process routine execution requires the POSIX main thread")
    launch = manifest["launch"]
    launch_policy = launch["policy"]
    policy = RuntimePolicy(
        allow_writes=launch_policy["allow_writes"],
        allow_destructive=launch_policy["allow_destructive"],
        allow_network=launch_policy["allow_network"],
        allow_agent=launch_policy["allow_agent"],
        allow_parallel=launch_policy["allow_parallel"],
        approvals=set(workflow_approvals),
        approval_sha256=set(workflow_approval_sha256 or []),
    )
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)

    def handle_timeout(signum, frame):
        del signum, frame
        terminate_active_processes()
        raise TimeoutError("routine exceeded its maximum runtime")

    signal.signal(signal.SIGALRM, handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    runner = None
    try:
        workspace = Path(launch["workspace"])
        runs_dir = Path(launch["runs_dir"])
        if not runs_dir.is_absolute():
            runs_dir = workspace / runs_dir
        runner = WorkflowRunner(
            workflow=workflow,
            workspace=workspace,
            base_run_dir=runs_dir,
            policy=policy,
            max_workers=launch["max_workers"],
        )
        run = runner.execute()
        signal.setitimer(signal.ITIMER_REAL, 0)
        state = run.read_state()
        status = state.get("status")
        _notify_routine_run_fail_open(run, manifest, status or "failed")
        return ProcessResult(
            returncode=0 if status == "completed" else 1,
            stdout="Run directory: %s\nStatus: %s\n" % (run.run_dir, status or "unknown"),
            stderr="",
        )
    except TimeoutError:
        if runner is not None:
            _notify_routine_run_fail_open(runner.run, manifest, "timed_out")
        return ProcessResult(returncode=-9, stdout="", stderr="routine timed out", timed_out=True)
    except BaseException:
        if runner is not None:
            _notify_routine_run_fail_open(runner.run, manifest, "interrupted")
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer != (0.0, 0.0):
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _notify_routine_run_fail_open(run, manifest: Dict, status: str) -> None:
    if "--desktop-notify" not in manifest["launch"]["command_argv"]:
        return
    try:
        send_run_desktop_notification(
            run,
            desktop_notification_status_for_execution(status),
        )
    except Exception:
        return


def _routine_timeout_seconds(manifest: Dict) -> int:
    minutes = manifest.get("budget", {}).get("max_minutes")
    if minutes is None:
        return 60 * 60
    return min(MAX_ROUTINE_LAUNCH_SECONDS, int(minutes) * 60)


def _validate_approval_sha256(values: Iterable[str]) -> set:
    if isinstance(values, (str, bytes)):
        raise ValidationError("durable approval sha256 values must be an array")
    result = set(values or [])
    if len(result) > 1000:
        raise ValidationError("durable approval sha256 values must contain at most 1000 entries")
    for value in result:
        _validate_sha256(value, "durable approval sha256")
    return result


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValidationError("%s must be a lowercase sha256 hex string" % label)


def _file_sha256(path: Path, label: str) -> str:
    payload = read_regular_file_bytes_no_follow(path, label, max_bytes=1024 * 1024)
    return hashlib.sha256(payload).hexdigest()


def _supervisor_dir(routines_dir: Path) -> Path:
    path = Path(routines_dir) / "_supervisor"
    fd = ensure_dir_no_follow(path, "routine supervisor directory")
    os.close(fd)
    return path


def _supervisor_state_path(routines_dir: Path) -> Path:
    return Path(routines_dir) / "_supervisor" / "state.json"


def _routine_controls_path(routines_dir: Path, *, create: bool = False) -> Path:
    parent = _supervisor_dir(routines_dir) if create else Path(routines_dir) / "_supervisor"
    return parent / "controls.json"


def _write_supervisor_state(routines_dir: Path, state: Dict) -> None:
    validate_supervisor_state(state)
    text = json.dumps(redact_json_value(state), indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_ROUTINE_SUPERVISOR_STATE_BYTES:
        raise ValidationError("routine supervisor state exceeds the supported size")
    _supervisor_dir(routines_dir)
    replace_text_file_no_follow(_supervisor_state_path(routines_dir), "routine supervisor state", text, ".routine-state-")


@contextmanager
def _supervisor_lock(routines_dir: Path):
    path = _supervisor_dir(routines_dir) / "state.lock"
    reject_symlink_path(path, "routine supervisor lock")
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _option_values(argv: List[str], option: str) -> List[str]:
    values = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == option:
            if index + 1 >= len(argv):
                raise ValidationError("routine launch command is missing a value for %s" % option)
            values.append(argv[index + 1])
            index += 2
            continue
        if value.startswith(option + "="):
            values.append(value.split("=", 1)[1])
        index += 1
    return values


def _parse_utc(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _object_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key %s" % key)
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant %s" % value)


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
