import json
import os
import platform
import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

from .artifacts import RunArtifacts, utc_now
from .errors import ValidationError
from .security import (
    ensure_dir_no_follow,
    open_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    write_new_text_file_no_follow,
)


DESKTOP_NOTIFICATION_SCHEMA = "conductor.desktop_notification.v1"
DESKTOP_NOTIFICATION_FILENAME_TEMPLATE = "desktop-notification-%s.json"
MAX_DESKTOP_NOTIFICATION_BYTES = 32 * 1024
DESKTOP_NOTIFICATION_TIMEOUT_SECONDS = 5
DESKTOP_NOTIFICATION_STATUSES = {
    "sending",
    "delivered",
    "failed",
    "unavailable",
    "uncertain",
}
RUN_NOTIFICATION_STATUSES = {"blocked", "completed", "failed", "stopped"}
DESKTOP_NOTIFICATION_BACKENDS = {"linux-notify-send", "macos-osascript", "unavailable"}
DESKTOP_NOTIFICATION_CONTENT_POLICY = "fixed-status-only-v1"
DESKTOP_NOTIFICATION_ERROR_CLASSES = {
    "",
    "InterruptedDelivery",
    "InvalidReturnCode",
    "NotificationCommandFailed",
    "NotificationLaunchError",
    "NotificationTimeout",
}
DESKTOP_NOTIFICATION_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
MIN_DESKTOP_NOTIFICATION_RETURNCODE = -255
MAX_DESKTOP_NOTIFICATION_RETURNCODE = 255
DESKTOP_NOTIFICATION_RECEIPT_FIELDS = {
    "schema",
    "status",
    "run_status",
    "backend",
    "content_policy",
    "requested_at_utc",
    "finished_at_utc",
    "returncode",
    "error_class",
}
_MESSAGE_BY_STATUS = {
    "blocked": "Workflow needs approval.",
    "completed": "Workflow completed.",
    "failed": "Workflow failed.",
    "stopped": "Workflow stopped.",
}
_BLOCKED_EXECUTION_STATUSES = {"blocked", "workflow_blocked"}
_STOPPED_EXECUTION_STATUSES = {
    "cancelled",
    "canceled",
    "interrupted",
    "paused",
    "stopped",
    "terminated",
    "workflow_paused",
    "workflow_stopped",
    "workflow_terminated",
}
_NONTERMINAL_EXECUTION_STATUSES = {
    "active",
    "created",
    "launching",
    "not-started",
    "planned",
    "routing",
    "running",
}


def send_run_desktop_notification(
    run: RunArtifacts,
    run_status: str,
    *,
    system_name: Optional[str] = None,
    execute: Callable = subprocess.run,
) -> Dict:
    return send_desktop_notification(
        run.run_dir,
        run_status,
        system_name=system_name,
        execute=execute,
    )


def send_artifact_desktop_notification(
    artifact_path: Path,
    execution_status: str,
    *,
    system_name: Optional[str] = None,
    execute: Callable = subprocess.run,
) -> Dict:
    receipt_dir = desktop_notification_directory_for_artifact(artifact_path)
    directory_fd = ensure_dir_no_follow(
        receipt_dir,
        "desktop notification receipt directory",
    )
    os.close(directory_fd)
    return send_desktop_notification(
        receipt_dir,
        desktop_notification_status_for_execution(execution_status),
        system_name=system_name,
        execute=execute,
    )


def send_desktop_notification(
    receipt_dir: Path,
    run_status: str,
    *,
    system_name: Optional[str] = None,
    execute: Callable = subprocess.run,
) -> Dict:
    if run_status not in RUN_NOTIFICATION_STATUSES:
        raise ValidationError("desktop notification run status is invalid")
    target = Path(receipt_dir)
    directory_fd = open_dir_no_follow(
        target,
        "desktop notification receipt directory",
    )
    os.close(directory_fd)
    existing = _load_existing_receipt(target, run_status)
    if existing is not None:
        return _existing_receipt_result(target, run_status, existing)

    requested_at = utc_now()
    backend, command = _notification_command(system_name or platform.system(), run_status)
    if command is None:
        receipt = _receipt(
            status="unavailable",
            run_status=run_status,
            backend=backend,
            requested_at=requested_at,
            finished_at=utc_now(),
            returncode=None,
            error_class="",
        )
        _claimed, stored = _claim_receipt(target, run_status, receipt)
        return stored

    sending = _receipt(
        status="sending",
        run_status=run_status,
        backend=backend,
        requested_at=requested_at,
        finished_at=None,
        returncode=None,
        error_class="",
    )
    claimed, stored = _claim_receipt(target, run_status, sending)
    if not claimed:
        return _existing_receipt_result(target, run_status, stored)
    try:
        result = execute(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DESKTOP_NOTIFICATION_TIMEOUT_SECONDS,
            check=False,
        )
        returncode = result.returncode
        if not isinstance(returncode, int) or isinstance(returncode, bool):
            raise ValidationError("desktop notification return code is invalid")
        status = "delivered" if returncode == 0 else "failed"
        error_class = "" if status == "delivered" else "NotificationCommandFailed"
    except subprocess.TimeoutExpired:
        returncode = None
        status = "failed"
        error_class = "NotificationTimeout"
    except OSError:
        returncode = None
        status = "failed"
        error_class = "NotificationLaunchError"
    except ValidationError:
        returncode = None
        status = "failed"
        error_class = "InvalidReturnCode"
    receipt = _receipt(
        status=status,
        run_status=run_status,
        backend=backend,
        requested_at=requested_at,
        finished_at=utc_now(),
        returncode=returncode,
        error_class=error_class,
    )
    _replace_receipt(target, run_status, receipt)
    return receipt


def desktop_notification_directory_for_artifact(artifact_path: Path) -> Path:
    path = Path(artifact_path)
    reject_symlink_path(path, "desktop notification artifact")
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ValidationError(
            "desktop notification artifact is unavailable: %s" % exc.__class__.__name__
        )
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError("desktop notification artifact must be a regular file")
    return Path(str(path) + ".notifications")


def desktop_notification_status_for_execution(status: str) -> str:
    if not isinstance(status, str) or not status.strip() or len(status) > 200:
        raise ValidationError("desktop notification execution status is invalid")
    normalized = status.strip().lower()
    if normalized in _NONTERMINAL_EXECUTION_STATUSES:
        raise ValidationError("desktop notification requires a terminal execution status")
    if normalized == "completed":
        return "completed"
    if normalized in _BLOCKED_EXECUTION_STATUSES:
        return "blocked"
    if normalized in _STOPPED_EXECUTION_STATUSES:
        return "stopped"
    return "failed"


def load_desktop_notification_receipt(path: Path) -> Dict:
    raw = read_regular_text_file_no_follow(
        path,
        "desktop notification receipt",
        MAX_DESKTOP_NOTIFICATION_BYTES,
    )
    try:
        receipt = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("desktop notification receipt is not strict JSON") from exc
    validate_desktop_notification_receipt(receipt)
    return receipt


def validate_desktop_notification_receipt(receipt: Dict) -> None:
    if not isinstance(receipt, dict) or set(receipt) != DESKTOP_NOTIFICATION_RECEIPT_FIELDS:
        raise ValidationError("desktop notification receipt fields are invalid")
    if receipt.get("schema") != DESKTOP_NOTIFICATION_SCHEMA:
        raise ValidationError("desktop notification receipt schema is invalid")
    if receipt.get("status") not in DESKTOP_NOTIFICATION_STATUSES:
        raise ValidationError("desktop notification receipt status is invalid")
    if receipt.get("run_status") not in RUN_NOTIFICATION_STATUSES:
        raise ValidationError("desktop notification receipt run status is invalid")
    if receipt.get("backend") not in DESKTOP_NOTIFICATION_BACKENDS:
        raise ValidationError("desktop notification receipt backend is invalid")
    if receipt.get("content_policy") != DESKTOP_NOTIFICATION_CONTENT_POLICY:
        raise ValidationError("desktop notification content policy is invalid")
    requested_at = _validate_utc_timestamp(
        receipt.get("requested_at_utc"),
        "desktop notification requested timestamp",
    )
    finished = receipt.get("finished_at_utc")
    if receipt["status"] == "sending":
        if finished is not None:
            raise ValidationError("sending desktop notification cannot be final")
        finished_at = None
    else:
        finished_at = _validate_utc_timestamp(
            finished,
            "desktop notification finished timestamp",
        )
        if finished_at < requested_at:
            raise ValidationError("desktop notification finished before it was requested")
    returncode = receipt.get("returncode")
    if returncode is not None and (
        not isinstance(returncode, int)
        or isinstance(returncode, bool)
        or not MIN_DESKTOP_NOTIFICATION_RETURNCODE <= returncode <= MAX_DESKTOP_NOTIFICATION_RETURNCODE
    ):
        raise ValidationError("desktop notification return code is invalid")
    error_class = receipt.get("error_class")
    if error_class not in DESKTOP_NOTIFICATION_ERROR_CLASSES:
        raise ValidationError("desktop notification error class is invalid")
    status = receipt["status"]
    backend = receipt["backend"]
    if status == "sending":
        if backend == "unavailable" or returncode is not None or error_class:
            raise ValidationError("sending desktop notification fields are inconsistent")
    elif status == "delivered":
        if backend == "unavailable" or returncode != 0 or error_class:
            raise ValidationError("delivered desktop notification fields are inconsistent")
    elif status == "failed":
        if backend == "unavailable" or returncode == 0 or not error_class:
            raise ValidationError("failed desktop notification fields are inconsistent")
        if error_class == "NotificationCommandFailed" and returncode is None:
            raise ValidationError("failed desktop notification fields are inconsistent")
        if error_class != "NotificationCommandFailed" and returncode is not None:
            raise ValidationError("failed desktop notification fields are inconsistent")
    elif status == "unavailable":
        if backend != "unavailable" or returncode is not None or error_class:
            raise ValidationError("unavailable desktop notification fields are inconsistent")
    elif status == "uncertain":
        if backend == "unavailable" or returncode is not None or error_class != "InterruptedDelivery":
            raise ValidationError("uncertain desktop notification fields are inconsistent")


def _load_existing_receipt(receipt_dir: Path, run_status: str) -> Optional[Dict]:
    path = receipt_dir / _receipt_filename(run_status)
    if not path.exists() and not path.is_symlink():
        return None
    return load_desktop_notification_receipt(path)


def _claim_receipt(receipt_dir: Path, run_status: str, receipt: Dict) -> tuple:
    path = receipt_dir / _receipt_filename(run_status)
    serialized = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    try:
        write_new_text_file_no_follow(
            path,
            "desktop notification receipt",
            serialized,
            sync=True,
        )
        return True, receipt
    except FileExistsError:
        return False, load_desktop_notification_receipt(path)


def _existing_receipt_result(receipt_dir: Path, run_status: str, receipt: Dict) -> Dict:
    if receipt["status"] != "sending":
        return receipt
    uncertain = dict(receipt)
    uncertain["status"] = "uncertain"
    uncertain["finished_at_utc"] = utc_now()
    uncertain["error_class"] = "InterruptedDelivery"
    validate_desktop_notification_receipt(uncertain)
    _replace_receipt(receipt_dir, run_status, uncertain)
    return uncertain


def _replace_receipt(receipt_dir: Path, run_status: str, receipt: Dict) -> None:
    validate_desktop_notification_receipt(receipt)
    serialized = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    replace_text_file_no_follow(
        receipt_dir / _receipt_filename(run_status),
        "desktop notification receipt",
        serialized,
        ".desktop-notification-",
        mode=0o600,
        sync=True,
    )


def _receipt_filename(run_status: str) -> str:
    if run_status not in RUN_NOTIFICATION_STATUSES:
        raise ValidationError("desktop notification run status is invalid")
    return DESKTOP_NOTIFICATION_FILENAME_TEMPLATE % run_status


def _notification_command(system_name: str, run_status: str) -> tuple:
    message = _MESSAGE_BY_STATUS[run_status]
    if system_name == "Darwin":
        executable = Path("/usr/bin/osascript")
        if _usable_executable(executable):
            script = 'display notification %s with title "Codex Conductor"' % json.dumps(message)
            return "macos-osascript", [str(executable), "-e", script]
        return "unavailable", None
    if system_name == "Linux":
        executable = Path("/usr/bin/notify-send")
        if _usable_executable(executable):
            return "linux-notify-send", [
                str(executable),
                "--app-name=Codex Conductor",
                "Codex Conductor",
                message,
            ]
        return "unavailable", None
    return "unavailable", None


def _usable_executable(path: Path) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and not path.is_symlink()
        and info.st_uid == 0
        and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        and os.access(str(path), os.X_OK)
    )


def _receipt(
    *,
    status: str,
    run_status: str,
    backend: str,
    requested_at: str,
    finished_at,
    returncode,
    error_class: str,
) -> Dict:
    receipt = {
        "schema": DESKTOP_NOTIFICATION_SCHEMA,
        "status": status,
        "run_status": run_status,
        "backend": backend,
        "content_policy": DESKTOP_NOTIFICATION_CONTENT_POLICY,
        "requested_at_utc": requested_at,
        "finished_at_utc": finished_at,
        "returncode": returncode,
        "error_class": error_class,
    }
    validate_desktop_notification_receipt(receipt)
    return receipt


def _validate_utc_timestamp(value, label: str) -> datetime:
    if not isinstance(value, str) or re.fullmatch(DESKTOP_NOTIFICATION_TIMESTAMP_PATTERN, value) is None:
        raise ValidationError("%s is invalid" % label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("%s is invalid" % label)
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValidationError("%s is invalid" % label)
    return parsed


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant: %s" % value)
