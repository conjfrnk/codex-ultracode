import hashlib
import json
import os
import plistlib
import re
import signal
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - user services are POSIX-only.
    fcntl = None

from .artifacts import utc_now
from .errors import PolicyError, ValidationError
from .redaction import redact_text
from .routine_supervisor import (
    MAX_SUPERVISOR_POLL_SECONDS,
    run_due_routines,
)
from .routines import MAX_LIST_ROUTINES, load_routine_manifest
from .runner import DEFAULT_OUTPUT_LIMIT_BYTES
from .security import (
    RuntimePolicy,
    ensure_dir_no_follow,
    open_dir_no_follow,
    read_regular_file_bytes_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    write_new_text_file_no_follow,
    write_text_file_no_follow,
)


ROUTINE_SERVICE_GRANT_SCHEMA = "conductor.routine_service_grant.v1"
ROUTINE_SERVICE_STATE_SCHEMA = "conductor.routine_service_state.v1"
ROUTINE_SERVICE_INSTALL_APPROVAL = "routine-service-install"
ROUTINE_SERVICE_UPDATE_APPROVAL = "routine-service-update"
ROUTINE_SERVICE_UNINSTALL_APPROVAL = "routine-service-uninstall"
ROUTINE_SERVICE_PLATFORMS = {"launchd", "systemd"}
ROUTINE_SERVICE_STATUSES = {"starting", "running", "blocked", "expired", "stopping", "stopped", "failed"}
MAX_ROUTINE_SERVICE_BYTES = 2 * 1024 * 1024
MAX_RUNTIME_BYTES = 128 * 1024 * 1024
MAX_SERVICE_GRANT_DAYS = 365
MAX_SERVICE_ERROR_CHARS = 1024
MAX_MANAGER_OUTPUT_BYTES = 64 * 1024
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
SAFE_SERVICE_ID = re.compile(r"^routine-[a-f0-9]{20}$")
GRANT_KEYS = {
    "schema",
    "service_id",
    "created_at_utc",
    "expires_at_utc",
    "platform",
    "routines_dir",
    "runtime",
    "limits",
    "routines",
    "authorization",
}
RUNTIME_KEYS = {"path", "sha256", "python_path"}
LIMIT_KEYS = {"poll_seconds", "max_routines", "output_limit_bytes"}
ROUTINE_KEYS = {
    "name",
    "manifest_name",
    "manifest_sha256",
    "workflow_fingerprint",
    "approval_sha256",
}
AUTHORIZATION_KEYS = {
    "raw_approval_values_persisted",
    "wildcard_approval_allowed",
    "exact_manifest_binding",
    "exact_runtime_binding",
    "bounded_expiration",
    "unlisted_routines_allowed",
}
STATE_KEYS = {
    "schema",
    "service_id",
    "grant_sha256",
    "status",
    "pid",
    "started_at_utc",
    "updated_at_utc",
    "heartbeat_at_utc",
    "cycles_completed",
    "results_completed",
    "results_failed",
    "last_error",
    "raw_approval_values_persisted",
}
SERVICE_LIFECYCLE_APPROVALS = {
    ROUTINE_SERVICE_INSTALL_APPROVAL,
    ROUTINE_SERVICE_UPDATE_APPROVAL,
    ROUTINE_SERVICE_UNINSTALL_APPROVAL,
    "foreground-supervisor",
    "background-supervisor",
}


class RoutineServiceAlreadyActive(PolicyError):
    pass


def build_routine_service_grant(
    *,
    routines_dir: Path,
    routine_manifests: Iterable[Path],
    runtime_path: Path,
    policy: RuntimePolicy,
    allow_service_install: bool,
    platform: str = "auto",
    poll_seconds: int = 60,
    max_routines: int = 50,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    grant_days: int = 30,
    approval: str = ROUTINE_SERVICE_INSTALL_APPROVAL,
    python_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Dict:
    if allow_service_install is not True:
        raise PolicyError("routine service installation requires --allow-service-install")
    if approval not in SERVICE_LIFECYCLE_APPROVALS:
        raise ValidationError("routine service lifecycle approval is invalid")
    approvals = set(policy.approvals or set())
    if approval not in approvals:
        raise PolicyError("routine service operation requires --approve %s" % approval)
    if "all" in approvals:
        raise PolicyError("durable routine services do not accept the wildcard approval token")
    platform_name = resolve_routine_service_platform(platform)
    _validate_service_limits(poll_seconds, max_routines, output_limit_bytes, grant_days)
    directory_source = Path(routines_dir)
    reject_symlink_path(directory_source, "routine service routines directory")
    directory = directory_source.resolve()
    if not directory.is_dir():
        raise ValidationError("routine service routines directory does not exist: %s" % directory)
    runtime = _validated_runtime(runtime_path, python_path=python_path)
    paths = [Path(path) for path in routine_manifests]
    if not paths or len(paths) > max_routines:
        raise ValidationError("routine service requires from 1 to %d explicit routine manifests" % max_routines)
    normalized = []
    seen_paths = set()
    seen_names = set()
    routine_tokens = set()
    manifests = []
    for path in paths:
        resolved = path.resolve()
        reject_symlink_path(path, "routine service manifest")
        if resolved.parent != directory:
            raise ValidationError("routine service manifests must be direct children of the routines directory")
        if resolved in seen_paths:
            raise ValidationError("routine service manifest paths must be unique")
        seen_paths.add(resolved)
        manifest = load_routine_manifest(resolved)
        name = manifest["name"]
        if name in seen_names:
            raise ValidationError("routine service manifest names must be unique")
        seen_names.add(name)
        routine_token = "routine:%s" % name
        routine_tokens.add(routine_token)
        if routine_token not in approvals:
            raise PolicyError("routine service grant requires --approve %s" % routine_token)
        _require_policy_covers_manifest(policy, manifest)
        manifests.append((resolved, manifest))
    workflow_approvals = approvals - SERVICE_LIFECYCLE_APPROVALS - routine_tokens
    approval_hashes = sorted(_approval_sha256(value) for value in workflow_approvals)
    if _approval_sha256("all") in approval_hashes:
        raise PolicyError("durable routine services do not accept the wildcard approval token")
    for resolved, manifest in manifests:
        required_count = manifest["launch"]["policy"]["approval_count"]
        if len(approval_hashes) < required_count:
            raise PolicyError(
                "routine service grant for %s requires at least %d workflow approval token(s)"
                % (manifest["name"], required_count)
            )
        normalized.append(
            {
                "name": manifest["name"],
                "manifest_name": resolved.name,
                "manifest_sha256": _file_sha256(resolved, "routine service manifest", MAX_ROUTINE_SERVICE_BYTES),
                "workflow_fingerprint": manifest["target"]["workflow_fingerprint"],
                "approval_sha256": list(approval_hashes),
            }
        )
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValidationError("routine service grant time must be timezone-aware")
    current = current.astimezone(timezone.utc)
    grant = {
        "schema": ROUTINE_SERVICE_GRANT_SCHEMA,
        "service_id": routine_service_id(directory),
        "created_at_utc": _format_utc(current),
        "expires_at_utc": _format_utc(current + timedelta(days=grant_days)),
        "platform": platform_name,
        "routines_dir": str(directory),
        "runtime": runtime,
        "limits": {
            "poll_seconds": poll_seconds,
            "max_routines": max_routines,
            "output_limit_bytes": output_limit_bytes,
        },
        "routines": sorted(normalized, key=lambda item: item["name"]),
        "authorization": {
            "raw_approval_values_persisted": False,
            "wildcard_approval_allowed": False,
            "exact_manifest_binding": True,
            "exact_runtime_binding": True,
            "bounded_expiration": True,
            "unlisted_routines_allowed": False,
        },
    }
    validate_routine_service_grant(grant)
    return grant


def validate_routine_service_grant(grant: Dict, source: str = "<memory>") -> None:
    if not isinstance(grant, dict):
        raise ValidationError("%s must contain an object" % source)
    _exact_keys(grant, GRANT_KEYS, "%s routine service grant" % source)
    if grant.get("schema") != ROUTINE_SERVICE_GRANT_SCHEMA:
        raise ValidationError("%s must set schema to %s" % (source, ROUTINE_SERVICE_GRANT_SCHEMA))
    service_id = grant.get("service_id")
    if not isinstance(service_id, str) or not SAFE_SERVICE_ID.match(service_id):
        raise ValidationError("%s service_id is invalid" % source)
    created = _parse_utc(grant.get("created_at_utc"), "%s created_at_utc" % source)
    expires = _parse_utc(grant.get("expires_at_utc"), "%s expires_at_utc" % source)
    if expires <= created or expires - created > timedelta(days=MAX_SERVICE_GRANT_DAYS):
        raise ValidationError("%s expiration must be after creation and within %d days" % (source, MAX_SERVICE_GRANT_DAYS))
    if grant.get("platform") not in ROUTINE_SERVICE_PLATFORMS:
        raise ValidationError("%s platform must be launchd or systemd" % source)
    routines_dir = _absolute_path(grant.get("routines_dir"), "%s routines_dir" % source)
    if routine_service_id(routines_dir) != service_id:
        raise ValidationError("%s service_id must match routines_dir" % source)
    runtime = grant.get("runtime")
    if not isinstance(runtime, dict):
        raise ValidationError("%s runtime must be an object" % source)
    _exact_keys(runtime, RUNTIME_KEYS, "%s runtime" % source)
    _absolute_path(runtime.get("path"), "%s runtime.path" % source)
    _absolute_path(runtime.get("python_path"), "%s runtime.python_path" % source)
    _sha256(runtime.get("sha256"), "%s runtime.sha256" % source)
    limits = grant.get("limits")
    if not isinstance(limits, dict):
        raise ValidationError("%s limits must be an object" % source)
    _exact_keys(limits, LIMIT_KEYS, "%s limits" % source)
    _validate_service_limits(
        limits.get("poll_seconds"),
        limits.get("max_routines"),
        limits.get("output_limit_bytes"),
        max(1, int((expires - created).total_seconds() // 86400)),
    )
    routines = grant.get("routines")
    if not isinstance(routines, list) or not routines or len(routines) > limits["max_routines"]:
        raise ValidationError("%s routines must be a non-empty bounded array" % source)
    names = set()
    manifest_names = set()
    for index, item in enumerate(routines):
        label = "%s routines[%d]" % (source, index)
        if not isinstance(item, dict):
            raise ValidationError("%s must be an object" % label)
        _exact_keys(item, ROUTINE_KEYS, label)
        name = item.get("name")
        if not isinstance(name, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", name):
            raise ValidationError("%s name is invalid" % label)
        manifest_name = item.get("manifest_name")
        if (
            not isinstance(manifest_name, str)
            or Path(manifest_name).name != manifest_name
            or not manifest_name.endswith(".json")
        ):
            raise ValidationError("%s manifest_name must be a safe JSON filename" % label)
        if name in names or manifest_name in manifest_names:
            raise ValidationError("%s routine names and manifest filenames must be unique" % source)
        names.add(name)
        manifest_names.add(manifest_name)
        _sha256(item.get("manifest_sha256"), "%s manifest_sha256" % label)
        _sha256(item.get("workflow_fingerprint"), "%s workflow_fingerprint" % label)
        approval_hashes = item.get("approval_sha256")
        if not isinstance(approval_hashes, list) or len(approval_hashes) > 1000 or len(set(approval_hashes)) != len(approval_hashes):
            raise ValidationError("%s approval_sha256 must be a unique bounded array" % label)
        for value in approval_hashes:
            _sha256(value, "%s approval_sha256" % label)
        if _approval_sha256("all") in approval_hashes:
            raise ValidationError("%s must not authorize the wildcard approval" % label)
    authorization = grant.get("authorization")
    if not isinstance(authorization, dict):
        raise ValidationError("%s authorization must be an object" % source)
    _exact_keys(authorization, AUTHORIZATION_KEYS, "%s authorization" % source)
    expected = {
        "raw_approval_values_persisted": False,
        "wildcard_approval_allowed": False,
        "exact_manifest_binding": True,
        "exact_runtime_binding": True,
        "bounded_expiration": True,
        "unlisted_routines_allowed": False,
    }
    if authorization != expected:
        raise ValidationError("%s authorization contract is invalid" % source)


def load_routine_service_grant(path: Path) -> Dict:
    grant_path = Path(path)
    reject_symlink_path(grant_path, "routine service grant")
    try:
        data = json.loads(read_regular_text_file_no_follow(grant_path, "routine service grant", MAX_ROUTINE_SERVICE_BYTES))
    except json.JSONDecodeError as exc:
        raise ValidationError("routine service grant is not valid JSON: %s" % exc)
    validate_routine_service_grant(data, source=str(grant_path))
    return data


def verify_routine_service_grant(
    grant: Dict,
    *,
    now: Optional[datetime] = None,
    running_runtime_path: Optional[Path] = None,
) -> Dict[str, Dict]:
    validate_routine_service_grant(grant)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValidationError("routine service verification time must be timezone-aware")
    if current.astimezone(timezone.utc) >= _parse_utc(grant["expires_at_utc"], "expires_at_utc"):
        raise PolicyError("routine service grant has expired")
    runtime_path = Path(grant["runtime"]["path"])
    reject_symlink_path(runtime_path, "routine service runtime")
    runtime_sha256 = _file_sha256(runtime_path, "routine service runtime", MAX_RUNTIME_BYTES)
    if runtime_sha256 != grant["runtime"]["sha256"]:
        raise PolicyError("routine service runtime changed after authorization")
    if running_runtime_path is not None and Path(running_runtime_path).resolve() != runtime_path.resolve():
        raise PolicyError("routine service worker is not running from the authorized runtime")
    directory = Path(grant["routines_dir"])
    reject_symlink_path(directory, "routine service routines directory")
    if not directory.is_dir():
        raise ValidationError("routine service routines directory is unavailable")
    authorizations = {}
    for item in grant["routines"]:
        path = directory / item["manifest_name"]
        if _file_sha256(path, "routine service manifest", MAX_ROUTINE_SERVICE_BYTES) != item["manifest_sha256"]:
            raise PolicyError("routine service manifest changed after authorization: %s" % item["manifest_name"])
        manifest = load_routine_manifest(path)
        if manifest["name"] != item["name"]:
            raise PolicyError("routine service manifest name changed after authorization")
        if manifest["target"]["workflow_fingerprint"] != item["workflow_fingerprint"]:
            raise PolicyError("routine service target fingerprint changed after authorization")
        if len(item["approval_sha256"]) < manifest["launch"]["policy"]["approval_count"]:
            raise PolicyError("routine service approval authorization is incomplete")
        authorizations[str(path)] = {
            "manifest_sha256": item["manifest_sha256"],
            "approval_sha256": list(item["approval_sha256"]),
        }
    return authorizations


def install_routine_service(
    grant: Dict,
    *,
    home: Optional[Path] = None,
    start: bool = True,
    replace: bool = False,
    manager_runner: Optional[Callable[[List[str], bool], None]] = None,
) -> Dict:
    validate_routine_service_grant(grant)
    verify_routine_service_grant(grant)
    paths = routine_service_paths(grant, home=home)
    grant_path = paths["grant"]
    descriptor_path = paths["descriptor"] if start else paths["staged_descriptor"]
    if not replace and (
        grant_path.exists()
        or grant_path.is_symlink()
        or paths["descriptor"].exists()
        or paths["descriptor"].is_symlink()
        or paths["staged_descriptor"].exists()
        or paths["staged_descriptor"].is_symlink()
    ):
        raise ValidationError("routine service is already installed; use update-routine-service")
    runner = manager_runner or _run_manager_command
    if replace:
        existing_grant = load_routine_service_grant(grant_path)
        if existing_grant["platform"] != grant["platform"]:
            raise ValidationError("routine service platform changes require uninstall followed by install")
        existing_paths = routine_service_paths(existing_grant, home=home)
        if existing_paths["descriptor"].exists() or existing_paths["descriptor"].is_symlink():
            _stop_service(existing_grant, existing_paths, runner, ignore_missing=True)
        _unlink_regular_file(existing_paths["descriptor"], "routine service descriptor", missing_ok=True)
        _unlink_regular_file(existing_paths["staged_descriptor"], "staged routine service descriptor", missing_ok=True)
    descriptor = render_routine_service_descriptor(grant, paths)
    if replace:
        if descriptor_path.exists() and not descriptor_path.is_symlink():
            replace_text_file_no_follow(descriptor_path, "routine service descriptor", descriptor, ".routine-service-descriptor-", mode=0o600)
        else:
            write_new_text_file_no_follow(descriptor_path, "routine service descriptor", descriptor, mode=0o600, sync=True)
        replace_text_file_no_follow(grant_path, "routine service grant", _json_text(grant), ".routine-service-grant-", mode=0o600)
    else:
        write_new_text_file_no_follow(descriptor_path, "routine service descriptor", descriptor, mode=0o600, sync=True)
        write_new_text_file_no_follow(grant_path, "routine service grant", _json_text(grant), mode=0o600, sync=True)
    obsolete_descriptor = paths["staged_descriptor"] if start else paths["descriptor"]
    _unlink_regular_file(obsolete_descriptor, "obsolete routine service descriptor", missing_ok=True)
    _unlink_regular_file(paths["state"], "prior routine service state", missing_ok=True)
    if start:
        try:
            _start_service(grant, paths, runner)
        except (PolicyError, ValidationError) as exc:
            failed_state = _new_service_state(
                grant,
                _file_sha256(grant_path, "routine service grant", MAX_ROUTINE_SERVICE_BYTES),
                utc_now(),
            )
            failed_state["status"] = "failed"
            failed_state["last_error"] = _public_error(exc)
            _touch_service_state(paths["state"], failed_state)
            raise
    return {
        "service_id": grant["service_id"],
        "platform": grant["platform"],
        "grant_path": str(grant_path),
        "descriptor_path": str(descriptor_path),
        "started": bool(start),
        "expires_at_utc": grant["expires_at_utc"],
    }


def uninstall_routine_service(
    routines_dir: Path,
    *,
    approvals: Iterable[str],
    allow_service_install: bool,
    home: Optional[Path] = None,
    manager_runner: Optional[Callable[[List[str], bool], None]] = None,
) -> Dict:
    if allow_service_install is not True:
        raise PolicyError("routine service removal requires --allow-service-install")
    approval_set = set(approvals or [])
    if ROUTINE_SERVICE_UNINSTALL_APPROVAL not in approval_set:
        raise PolicyError("routine service removal requires --approve %s" % ROUTINE_SERVICE_UNINSTALL_APPROVAL)
    reject_symlink_path(Path(routines_dir), "routine service routines directory")
    grant_path = routine_service_grant_path(Path(routines_dir))
    grant = load_routine_service_grant(grant_path)
    paths = routine_service_paths(grant, home=home)
    runner = manager_runner or _run_manager_command
    if paths["descriptor"].exists() or paths["descriptor"].is_symlink():
        _stop_service(grant, paths, runner, ignore_missing=True)
    removed_descriptor = _unlink_regular_file(paths["descriptor"], "routine service descriptor", missing_ok=True)
    removed_staged_descriptor = _unlink_regular_file(
        paths["staged_descriptor"],
        "staged routine service descriptor",
        missing_ok=True,
    )
    removed_grant = _unlink_regular_file(paths["grant"], "routine service grant", missing_ok=False)
    if grant["platform"] == "systemd":
        runner(["systemctl", "--user", "daemon-reload"], False)
    return {
        "service_id": grant["service_id"],
        "platform": grant["platform"],
        "removed_descriptor": removed_descriptor or removed_staged_descriptor,
        "removed_grant": removed_grant,
    }


def status_routine_service(routines_dir: Path, *, home: Optional[Path] = None) -> Dict:
    directory_source = Path(routines_dir)
    reject_symlink_path(directory_source, "routine service routines directory")
    directory = directory_source.resolve()
    grant_path = routine_service_grant_path(directory)
    result = {
        "schema": "conductor.routine_service_status.v1",
        "routines_dir": str(directory),
        "installed": False,
        "effective_status": "not-installed",
        "needs_attention": False,
        "grant_path": str(grant_path),
        "descriptor_path": "",
        "staged_descriptor_path": "",
        "service_id": routine_service_id(directory),
        "platform": "",
        "expires_at_utc": "",
        "grant_valid": False,
        "bindings_valid": False,
        "descriptor_present": False,
        "descriptor_valid": False,
        "staged_descriptor_present": False,
        "staged_descriptor_valid": False,
        "state": {},
        "liveness": "not-running",
        "heartbeat_fresh": False,
        "error": "",
    }
    if not grant_path.exists() and not grant_path.is_symlink():
        return result
    try:
        grant = load_routine_service_grant(grant_path)
        result["grant_valid"] = True
        result["service_id"] = grant["service_id"]
        result["platform"] = grant["platform"]
        result["expires_at_utc"] = grant["expires_at_utc"]
        paths = routine_service_paths(grant, home=home)
        result["descriptor_path"] = str(paths["descriptor"])
        result["staged_descriptor_path"] = str(paths["staged_descriptor"])
        result["descriptor_present"] = paths["descriptor"].is_file() and not paths["descriptor"].is_symlink()
        if result["descriptor_present"]:
            actual_descriptor = read_regular_text_file_no_follow(
                paths["descriptor"],
                "routine service descriptor",
                MAX_ROUTINE_SERVICE_BYTES,
            )
            result["descriptor_valid"] = actual_descriptor == render_routine_service_descriptor(grant, paths)
        result["staged_descriptor_present"] = (
            paths["staged_descriptor"].is_file() and not paths["staged_descriptor"].is_symlink()
        )
        if result["staged_descriptor_present"]:
            staged_descriptor = read_regular_text_file_no_follow(
                paths["staged_descriptor"],
                "staged routine service descriptor",
                MAX_ROUTINE_SERVICE_BYTES,
            )
            result["staged_descriptor_valid"] = staged_descriptor == render_routine_service_descriptor(grant, paths)
        verify_routine_service_grant(grant)
        result["bindings_valid"] = True
        state = load_routine_service_state(paths["state"], missing_ok=True)
        result["state"] = state
        if state:
            grant_sha256 = _file_sha256(grant_path, "routine service grant", MAX_ROUTINE_SERVICE_BYTES)
            if state["service_id"] != grant["service_id"] or state["grant_sha256"] != grant_sha256:
                raise PolicyError("routine service state does not match the installed grant")
            active_state = state["status"] in {"starting", "running", "blocked", "stopping"}
            heartbeat = _parse_utc(state["heartbeat_at_utc"], "routine service heartbeat")
            freshness_seconds = max(60, grant["limits"]["poll_seconds"] * 3)
            heartbeat_age = datetime.now(timezone.utc) - heartbeat
            result["heartbeat_fresh"] = timedelta(0) <= heartbeat_age <= timedelta(seconds=freshness_seconds)
            if active_state and _pid_is_running(state["pid"]):
                result["liveness"] = "running" if result["heartbeat_fresh"] else "stale"
        result["installed"] = result["descriptor_present"]
        if result["staged_descriptor_present"] and result["staged_descriptor_valid"] and not result["descriptor_present"]:
            result["effective_status"] = "staged"
        elif not result["descriptor_present"] or not result["descriptor_valid"]:
            result["effective_status"] = "incomplete"
            result["needs_attention"] = True
        elif state and state["status"] in {"blocked", "failed", "expired"}:
            result["effective_status"] = state["status"]
            result["needs_attention"] = True
        elif state and state["status"] == "running" and result["liveness"] == "running":
            result["effective_status"] = "running"
        elif state and state["status"] in {"starting", "stopping", "stopped"}:
            result["effective_status"] = state["status"]
        else:
            result["effective_status"] = "installed"
    except (FileNotFoundError, PolicyError, ValidationError) as exc:
        result["effective_status"] = "expired" if "expired" in str(exc).lower() else "invalid"
        result["needs_attention"] = True
        result["error"] = _public_error(exc)
    return result


def run_routine_service_worker(
    grant_path: Path,
    *,
    running_runtime_path: Optional[Path] = None,
    max_cycles: int = 0,
) -> int:
    if not isinstance(max_cycles, int) or isinstance(max_cycles, bool) or max_cycles < 0:
        raise ValidationError("routine service worker max_cycles must be a non-negative integer")
    path = Path(grant_path)
    grant = load_routine_service_grant(path)
    expected_path = routine_service_grant_path(Path(grant["routines_dir"]))
    if path.resolve() != expected_path.resolve():
        raise PolicyError("routine service worker grant path is not the installed grant path")
    runtime_path = running_runtime_path if running_runtime_path is not None else Path(sys.argv[0])
    paths = routine_service_paths(grant)
    stop = threading.Event()

    def handle_stop(signum, frame):
        del signum, frame
        stop.set()

    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    started = utc_now()
    grant_sha256 = _file_sha256(path, "routine service grant", MAX_ROUTINE_SERVICE_BYTES)
    state = _new_service_state(grant, grant_sha256, started)
    try:
        with _service_lock(paths["lock"]):
            _write_service_state(paths["state"], state)
            while not stop.is_set():
                try:
                    if _file_sha256(path, "routine service grant", MAX_ROUTINE_SERVICE_BYTES) != grant_sha256:
                        raise PolicyError("routine service grant changed while the worker was active")
                    authorizations = verify_routine_service_grant(
                        grant,
                        running_runtime_path=runtime_path,
                    )
                except PolicyError as exc:
                    if "expired" in str(exc).lower():
                        state["status"] = "expired"
                        state["last_error"] = "routine service grant expired"
                        _touch_service_state(paths["state"], state)
                        return 0
                    state["status"] = "blocked"
                    state["last_error"] = _public_error(exc)
                    _touch_service_state(paths["state"], state)
                    if stop.wait(grant["limits"]["poll_seconds"]):
                        break
                    continue
                except (FileNotFoundError, ValidationError) as exc:
                    state["status"] = "blocked"
                    state["last_error"] = _public_error(exc)
                    _touch_service_state(paths["state"], state)
                    if stop.wait(grant["limits"]["poll_seconds"]):
                        break
                    continue
                state["status"] = "running"
                state["last_error"] = ""
                results = run_due_routines(
                    Path(grant["routines_dir"]),
                    approvals=[],
                    max_routines=grant["limits"]["max_routines"],
                    output_limit_bytes=grant["limits"]["output_limit_bytes"],
                    in_process=True,
                    durable_authorizations=authorizations,
                )
                state["cycles_completed"] += 1
                for result in results:
                    if result.get("status") == "completed":
                        state["results_completed"] += 1
                    else:
                        state["results_failed"] += 1
                _touch_service_state(paths["state"], state)
                if max_cycles and state["cycles_completed"] >= max_cycles:
                    break
                if stop.wait(grant["limits"]["poll_seconds"]):
                    break
            state["status"] = "stopping"
            _touch_service_state(paths["state"], state)
            state["status"] = "stopped"
            _touch_service_state(paths["state"], state)
            return 0
    except RoutineServiceAlreadyActive:
        return 0
    except (FileNotFoundError, PolicyError, ValidationError) as exc:
        state["status"] = "failed"
        state["last_error"] = _public_error(exc)
        _touch_service_state(paths["state"], state)
        return 2
    except Exception as exc:  # pragma: no cover - defensive service boundary.
        state["status"] = "failed"
        state["last_error"] = exc.__class__.__name__
        _touch_service_state(paths["state"], state)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def validate_routine_service_state(state: Dict, source: str = "<memory>") -> None:
    if not isinstance(state, dict):
        raise ValidationError("%s must contain an object" % source)
    _exact_keys(state, STATE_KEYS, "%s routine service state" % source)
    if state.get("schema") != ROUTINE_SERVICE_STATE_SCHEMA:
        raise ValidationError("%s schema is invalid" % source)
    if not isinstance(state.get("service_id"), str) or not SAFE_SERVICE_ID.match(state["service_id"]):
        raise ValidationError("%s service_id is invalid" % source)
    _sha256(state.get("grant_sha256"), "%s grant_sha256" % source)
    if state.get("status") not in ROUTINE_SERVICE_STATUSES:
        raise ValidationError("%s status is invalid" % source)
    if not isinstance(state.get("pid"), int) or isinstance(state.get("pid"), bool) or state["pid"] < 1:
        raise ValidationError("%s pid must be positive" % source)
    started = _parse_utc(state.get("started_at_utc"), "%s started_at_utc" % source)
    updated = _parse_utc(state.get("updated_at_utc"), "%s updated_at_utc" % source)
    heartbeat = _parse_utc(state.get("heartbeat_at_utc"), "%s heartbeat_at_utc" % source)
    if updated < started or heartbeat < started or heartbeat > updated:
        raise ValidationError("%s service timestamps are inconsistent" % source)
    for key in ["cycles_completed", "results_completed", "results_failed"]:
        if not isinstance(state.get(key), int) or isinstance(state.get(key), bool) or state[key] < 0:
            raise ValidationError("%s %s must be a non-negative integer" % (source, key))
    error = state.get("last_error")
    if not isinstance(error, str) or len(error) > MAX_SERVICE_ERROR_CHARS:
        raise ValidationError("%s last_error is invalid" % source)
    if state.get("raw_approval_values_persisted") is not False:
        raise ValidationError("%s must not persist raw approval values" % source)


def load_routine_service_state(path: Path, *, missing_ok: bool = False) -> Dict:
    state_path = Path(path)
    if missing_ok and not state_path.exists() and not state_path.is_symlink():
        return {}
    try:
        data = json.loads(read_regular_text_file_no_follow(state_path, "routine service state", MAX_ROUTINE_SERVICE_BYTES))
    except json.JSONDecodeError as exc:
        raise ValidationError("routine service state is not valid JSON: %s" % exc)
    validate_routine_service_state(data, source=str(state_path))
    return data


def resolve_routine_service_platform(value: str = "auto") -> str:
    if value in ROUTINE_SERVICE_PLATFORMS:
        return value
    if value != "auto":
        raise ValidationError("routine service platform must be auto, launchd, or systemd")
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    raise ValidationError("routine services currently require macOS launchd or Linux systemd")


def routine_service_id(routines_dir: Path) -> str:
    path = Path(routines_dir)
    normalized = str(path if path.is_absolute() else path.resolve())
    return "routine-%s" % hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def routine_service_grant_path(routines_dir: Path) -> Path:
    return Path(routines_dir).resolve() / "_supervisor" / "service" / "grant.json"


def routine_service_paths(grant: Dict, *, home: Optional[Path] = None) -> Dict[str, Path]:
    validate_routine_service_grant(grant)
    directory = Path(grant["routines_dir"])
    service_dir = directory / "_supervisor" / "service"
    home_dir = Path(home).resolve() if home is not None else Path.home().resolve()
    if grant["platform"] == "launchd":
        label = "com.openai.codex-conductor.%s" % grant["service_id"]
        descriptor = home_dir / "Library" / "LaunchAgents" / (label + ".plist")
    else:
        label = "codex-conductor-%s" % grant["service_id"]
        descriptor = home_dir / ".config" / "systemd" / "user" / (label + ".service")
    return {
        "service_dir": service_dir,
        "grant": service_dir / "grant.json",
        "state": service_dir / "state.json",
        "lock": service_dir / "worker.lock",
        "stdout": service_dir / "stdout.log",
        "stderr": service_dir / "stderr.log",
        "descriptor": descriptor,
        "staged_descriptor": service_dir / ("launchd.plist" if grant["platform"] == "launchd" else "systemd.service"),
        "label": Path(label),
    }


def render_routine_service_descriptor(grant: Dict, paths: Dict[str, Path]) -> str:
    validate_routine_service_grant(grant)
    argv = [
        grant["runtime"]["python_path"],
        "-B",
        grant["runtime"]["path"],
        "_routine-service-worker",
        str(paths["grant"]),
    ]
    if grant["platform"] == "launchd":
        payload = {
            "Label": str(paths["label"]),
            "ProgramArguments": argv,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 30,
            "ProcessType": "Background",
            "StandardOutPath": str(paths["stdout"]),
            "StandardErrorPath": str(paths["stderr"]),
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")
    exec_start = " ".join(_systemd_quote(value) for value in argv)
    return (
        "[Unit]\n"
        "Description=Codex Conductor routine service %s\n\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=%s\n"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        "Environment=PYTHONUNBUFFERED=1\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ) % (grant["service_id"], exec_start)


def _validated_runtime(runtime_path: Path, *, python_path: Optional[Path]) -> Dict:
    runtime = Path(runtime_path).resolve()
    reject_symlink_path(Path(runtime_path), "routine service runtime")
    if runtime.suffix != ".pyz":
        raise ValidationError("routine service runtime must be a packaged .pyz artifact")
    digest = _file_sha256(runtime, "routine service runtime", MAX_RUNTIME_BYTES)
    interpreter = Path(python_path or sys.executable).resolve()
    reject_symlink_path(interpreter, "routine service Python interpreter")
    if not interpreter.is_file():
        raise ValidationError("routine service Python interpreter is unavailable")
    return {"path": str(runtime), "sha256": digest, "python_path": str(interpreter)}


def _require_policy_covers_manifest(policy: RuntimePolicy, manifest: Dict) -> None:
    launch_policy = manifest["launch"]["policy"]
    for field in ["allow_writes", "allow_destructive", "allow_network", "allow_agent", "allow_parallel"]:
        if launch_policy[field] and not getattr(policy, field):
            raise PolicyError("routine service grant requires --%s" % field.replace("_", "-"))


def _validate_service_limits(poll_seconds, max_routines, output_limit_bytes, grant_days) -> None:
    if not isinstance(poll_seconds, int) or isinstance(poll_seconds, bool) or not 1 <= poll_seconds <= MAX_SUPERVISOR_POLL_SECONDS:
        raise ValidationError("routine service poll_seconds must be from 1 to %d" % MAX_SUPERVISOR_POLL_SECONDS)
    if not isinstance(max_routines, int) or isinstance(max_routines, bool) or not 1 <= max_routines <= MAX_LIST_ROUTINES:
        raise ValidationError("routine service max_routines must be from 1 to %d" % MAX_LIST_ROUTINES)
    if (
        not isinstance(output_limit_bytes, int)
        or isinstance(output_limit_bytes, bool)
        or not 1 <= output_limit_bytes <= DEFAULT_OUTPUT_LIMIT_BYTES
    ):
        raise ValidationError("routine service output_limit_bytes must be from 1 to %d" % DEFAULT_OUTPUT_LIMIT_BYTES)
    if not isinstance(grant_days, int) or isinstance(grant_days, bool) or not 1 <= grant_days <= MAX_SERVICE_GRANT_DAYS:
        raise ValidationError("routine service grant_days must be from 1 to %d" % MAX_SERVICE_GRANT_DAYS)


def _new_service_state(grant: Dict, grant_sha256: str, started: str) -> Dict:
    state = {
        "schema": ROUTINE_SERVICE_STATE_SCHEMA,
        "service_id": grant["service_id"],
        "grant_sha256": grant_sha256,
        "status": "starting",
        "pid": os.getpid(),
        "started_at_utc": started,
        "updated_at_utc": started,
        "heartbeat_at_utc": started,
        "cycles_completed": 0,
        "results_completed": 0,
        "results_failed": 0,
        "last_error": "",
        "raw_approval_values_persisted": False,
    }
    validate_routine_service_state(state)
    return state


def _touch_service_state(path: Path, state: Dict) -> None:
    now = utc_now()
    state["updated_at_utc"] = now
    state["heartbeat_at_utc"] = now
    state["last_error"] = redact_text(str(state.get("last_error") or ""))[:MAX_SERVICE_ERROR_CHARS]
    _write_service_state(path, state)


def _write_service_state(path: Path, state: Dict) -> None:
    validate_routine_service_state(state)
    text = _json_text(state)
    if path.exists() and not path.is_symlink():
        replace_text_file_no_follow(path, "routine service state", text, ".routine-service-state-", mode=0o600)
    else:
        write_text_file_no_follow(path, "routine service state", text, mode=0o600, sync=True)


@contextmanager
def _service_lock(path: Path):
    if fcntl is None:
        raise ValidationError("routine services require POSIX file locking")
    reject_symlink_path(path, "routine service lock")
    parent_fd = ensure_dir_no_follow(path.parent, "routine service directory")
    fd = None
    try:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValidationError("routine service lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RoutineServiceAlreadyActive("routine service worker is already active")
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
        os.close(parent_fd)


def _start_service(grant: Dict, paths: Dict[str, Path], runner: Callable[[List[str], bool], None]) -> None:
    if grant["platform"] == "launchd":
        runner(["launchctl", "bootstrap", "gui/%d" % os.getuid(), str(paths["descriptor"])], False)
    else:
        runner(["systemctl", "--user", "daemon-reload"], False)
        runner(["systemctl", "--user", "enable", "--now", paths["descriptor"].name], False)


def _stop_service(
    grant: Dict,
    paths: Dict[str, Path],
    runner: Callable[[List[str], bool], None],
    *,
    ignore_missing: bool,
) -> None:
    if grant["platform"] == "launchd":
        runner(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), paths["label"])], ignore_missing)
    else:
        runner(["systemctl", "--user", "disable", "--now", paths["descriptor"].name], ignore_missing)


def _run_manager_command(argv: List[str], ignore_missing: bool) -> None:
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home())}
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError("routine service manager failed: %s" % exc.__class__.__name__)
    if completed.returncode != 0:
        detail = redact_text((completed.stderr or completed.stdout or "manager command failed")[:MAX_MANAGER_OUTPUT_BYTES])
        missing_markers = ["not loaded", "not found", "no such", "could not find", "does not exist"]
        if ignore_missing and any(marker in detail.lower() for marker in missing_markers):
            return
        raise ValidationError("routine service manager rejected the request: %s" % detail)


def _unlink_regular_file(path: Path, label: str, *, missing_ok: bool) -> bool:
    target = Path(path)
    reject_symlink_path(target, label)
    if missing_ok and not target.parent.exists() and not target.parent.is_symlink():
        return False
    parent_fd = open_dir_no_follow(target.parent, "%s parent" % label)
    try:
        try:
            read_regular_file_bytes_no_follow(target, label, max_bytes=MAX_ROUTINE_SERVICE_BYTES)
        except FileNotFoundError:
            if missing_ok:
                return False
            raise
        os.unlink(target.name, dir_fd=parent_fd)
        return True
    finally:
        os.close(parent_fd)


def _systemd_quote(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ValidationError("routine service command arguments must be non-empty printable strings")
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % escaped


def _exact_keys(value: Dict, expected: set, label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        details = []
        if missing:
            details.append("missing %s" % ", ".join(missing))
        if extra:
            details.append("unsupported %s" % ", ".join(extra))
        raise ValidationError("%s has invalid fields: %s" % (label, "; ".join(details)))


def _absolute_path(value, label: str) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or not Path(value).is_absolute()
        or "\x00" in value
        or ".." in Path(value).parts
    ):
        raise ValidationError("%s must be a non-empty absolute path" % label)
    return Path(value)


def _sha256(value, label: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.match(value):
        raise ValidationError("%s must be a lowercase sha256 hex string" % label)
    return value


def _approval_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha256(path: Path, label: str, max_bytes: int) -> str:
    payload = read_regular_file_bytes_no_follow(path, label, max_bytes=max_bytes)
    return hashlib.sha256(payload).hexdigest()


def _parse_utc(value, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError("%s must be an ISO UTC timestamp" % label)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValidationError("%s must be an ISO UTC timestamp" % label)
    if parsed.tzinfo is None:
        raise ValidationError("%s must be timezone-aware" % label)
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_text(value: Dict) -> str:
    text = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_ROUTINE_SERVICE_BYTES:
        raise ValidationError("routine service artifact exceeds the maximum size")
    return text


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _public_error(exc: BaseException) -> str:
    message = redact_text(str(exc))
    if "expired" in message.lower():
        return "routine service grant expired"
    if "changed after authorization" in message.lower():
        return "authorized artifact changed after grant creation"
    return exc.__class__.__name__
