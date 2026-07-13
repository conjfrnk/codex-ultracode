import json
import os
import secrets
import signal
import socket
import stat
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX-only launch rejects first.
    fcntl = None

from .artifacts import utc_now, utc_stamp
from .background_run import runtime_self_command
from .errors import ConductorError, PolicyError, ValidationError
from .redaction import redact_text
from .routine_supervisor import supervise_routines, validate_supervise_routines_request
from .runner import DEFAULT_OUTPUT_LIMIT_BYTES, terminate_active_processes
from .security import (
    ensure_dir_no_follow,
    open_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    validate_approval_tokens,
    write_new_text_file_no_follow,
)


BACKGROUND_ROUTINE_SUPERVISOR_SCHEMA = "conductor.background_routine_supervisor.v1"
BACKGROUND_ROUTINE_SUPERVISOR_PAYLOAD_SCHEMA = "conductor.background_routine_supervisor_payload.v1"
BACKGROUND_ROUTINE_SUPERVISOR_STATUSES = {"launching", "active", "finished", "launch-failed"}
BACKGROUND_ROUTINE_SUPERVISOR_RESULT_STATUSES = {
    "not-started",
    "running",
    "completed",
    "completed-with-errors",
    "terminated",
    "failed",
}
BACKGROUND_ROUTINE_SUPERVISOR_FIELDS = {
    "schema",
    "status",
    "created_at_utc",
    "updated_at_utc",
    "heartbeat_at_utc",
    "finished_at_utc",
    "worker",
    "configuration",
    "policy",
    "handoff",
    "logs",
    "supervisor_status",
    "cycles_completed",
    "results_count",
    "completed_count",
    "blocked_count",
    "failed_count",
    "exit_code",
    "error_class",
}
MAX_BACKGROUND_ROUTINE_SUPERVISOR_BYTES = 512 * 1024
MAX_BACKGROUND_ROUTINE_SUPERVISOR_PAYLOAD_BYTES = 256 * 1024
MAX_BACKGROUND_ROUTINE_SUPERVISOR_WAIT_SECONDS = 7 * 24 * 60 * 60
BACKGROUND_ROUTINE_SUPERVISOR_HEARTBEAT_SECONDS = 1.0
BACKGROUND_ROUTINE_SUPERVISOR_STALE_SECONDS = 5
BACKGROUND_ROUTINE_SUPERVISOR_READY_SECONDS = 5
BACKGROUND_ROUTINE_SUPERVISOR_FORCE_GRACE_SECONDS = 2


@dataclass(frozen=True)
class BackgroundRoutineSupervisorResult:
    metadata_path: Path
    metadata: Dict
    pid: int


class _BackgroundRoutineSupervisorTermination(BaseException):
    pass


def start_background_routine_supervisor(
    *,
    routines_dir: Path,
    approvals: Iterable[str],
    poll_seconds: int = 60,
    max_cycles: int = 0,
    max_routines: int = 50,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    self_command: Optional[List[str]] = None,
) -> BackgroundRoutineSupervisorResult:
    if os.name != "posix":
        raise ValidationError("background routine supervision currently requires POSIX session isolation")
    prepared = validate_supervise_routines_request(
        routines_dir=routines_dir,
        approvals=approvals,
        poll_seconds=poll_seconds,
        max_cycles=max_cycles,
        max_routines=max_routines,
        output_limit_bytes=output_limit_bytes,
        background=True,
    )
    routines_path = Path(prepared["routines_dir"])
    routines_fd = ensure_dir_no_follow(routines_path, "routines directory")
    os.close(routines_fd)
    supervisor_dir = routines_path / "_supervisor"
    supervisor_fd = ensure_dir_no_follow(supervisor_dir, "routine supervisor directory")
    os.close(supervisor_fd)
    workers_dir = supervisor_dir / "workers"
    workers_fd = ensure_dir_no_follow(workers_dir, "background routine supervisor workers directory")
    os.close(workers_fd)

    payload = _build_payload(prepared)
    payload_bytes = _encode_payload(payload)
    launch_id = "%s-%s" % (utc_stamp(), secrets.token_hex(4))
    metadata_path = workers_dir / (launch_id + ".json")
    stdout_name = launch_id + ".stdout.log"
    stderr_name = launch_id + ".stderr.log"
    command = list(self_command or runtime_self_command()) + [
        "_background-routine-supervisor-worker",
        str(metadata_path),
    ]
    metadata = _initial_metadata(
        command=command,
        prepared=prepared,
        stdout_name=stdout_name,
        stderr_name=stderr_name,
    )

    with _launch_lock(supervisor_dir):
        blocking = _blocking_background_routine_supervisor(routines_path)
        if blocking:
            raise PolicyError(
                "background routine supervisor is already active or requires operator recovery: %s"
                % blocking.get("metadata", "unknown")
            )
        _write_new_metadata(metadata_path, metadata)

        stdout_fd = _open_new_log(workers_dir / stdout_name, "background routine supervisor stdout log")
        stderr_fd = None
        read_fd = None
        write_fd = None
        pid = None
        try:
            stderr_fd = _open_new_log(workers_dir / stderr_name, "background routine supervisor stderr log")
            read_fd, write_fd = os.pipe()
            file_actions = [
                (os.POSIX_SPAWN_DUP2, read_fd, 0),
                (os.POSIX_SPAWN_DUP2, stdout_fd, 1),
                (os.POSIX_SPAWN_DUP2, stderr_fd, 2),
                (os.POSIX_SPAWN_CLOSE, write_fd),
                (os.POSIX_SPAWN_CLOSE, read_fd),
                (os.POSIX_SPAWN_CLOSE, stdout_fd),
                (os.POSIX_SPAWN_CLOSE, stderr_fd),
            ]
            pid = os.posix_spawn(
                command[0],
                command,
                os.environ.copy(),
                file_actions=file_actions,
                setsid=True,
            )
        except Exception as exc:
            if write_fd is not None:
                os.close(write_fd)
                write_fd = None
            if pid is not None:
                _stop_failed_child(pid)
            _finish_launch_failure(metadata_path, metadata, exc.__class__.__name__)
            raise ValidationError("failed to launch background routine supervisor: %s" % exc.__class__.__name__)
        finally:
            if read_fd is not None:
                os.close(read_fd)
            os.close(stdout_fd)
            if stderr_fd is not None:
                os.close(stderr_fd)

        metadata["status"] = "active"
        metadata["supervisor_status"] = "running"
        metadata["updated_at_utc"] = utc_now()
        metadata["heartbeat_at_utc"] = metadata["updated_at_utc"]
        metadata["worker"]["pid"] = pid
        metadata["worker"]["process_group_id"] = pid
        metadata["worker"]["session_id"] = pid
        _replace_metadata(metadata_path, metadata)
        try:
            if write_fd is None:
                raise BrokenPipeError("background routine supervisor policy pipe is unavailable")
            _write_pipe(write_fd, payload_bytes)
            os.close(write_fd)
            write_fd = None
            metadata = _wait_for_worker_ready(metadata_path, pid)
        except Exception as exc:
            if write_fd is not None:
                os.close(write_fd)
            _stop_failed_child(pid)
            _finish_launch_failure(metadata_path, metadata, exc.__class__.__name__)
            raise ValidationError("failed to hand off background routine supervisor policy: %s" % exc.__class__.__name__)

    return BackgroundRoutineSupervisorResult(
        metadata_path=metadata_path,
        metadata=metadata,
        pid=pid,
    )


def run_background_routine_supervisor_worker(
    metadata_path: Path,
    input_stream: Optional[BinaryIO] = None,
) -> int:
    path = Path(metadata_path)
    routines_dir = _routines_dir_for_metadata(path)
    metadata = load_background_routine_supervisor(path)
    stream = input_stream if input_stream is not None else sys.stdin.buffer
    stop_heartbeat = threading.Event()
    metadata_lock = threading.Lock()
    heartbeat_thread = None
    exit_code = 1
    error_class = ""
    supervisor_status = "failed"
    previous_sigterm = None
    try:
        payload = _read_payload(stream)
        _validate_worker_identity(metadata)
        _validate_payload_matches_metadata(payload, metadata, routines_dir)
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def handle_sigterm(signum, frame):
            del frame
            terminate_active_processes()
            raise _BackgroundRoutineSupervisorTermination("received signal %d" % signum)

        signal.signal(signal.SIGTERM, handle_sigterm)
        with metadata_lock:
            metadata["worker"]["ready"] = True
            metadata["updated_at_utc"] = utc_now()
            metadata["heartbeat_at_utc"] = metadata["updated_at_utc"]
            _replace_metadata(path, metadata)
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(path, metadata, metadata_lock, stop_heartbeat),
            name="conductor-background-routine-supervisor-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()

        def record_cycle(cycle: int, results: List[Dict]) -> None:
            with metadata_lock:
                metadata["cycles_completed"] = cycle
                for result in results:
                    status = result.get("status") if isinstance(result, dict) else "failed"
                    metadata["results_count"] += 1
                    if status == "completed":
                        metadata["completed_count"] += 1
                    elif status == "blocked":
                        metadata["blocked_count"] += 1
                    else:
                        metadata["failed_count"] += 1
                metadata["updated_at_utc"] = utc_now()
                metadata["heartbeat_at_utc"] = metadata["updated_at_utc"]
                _replace_metadata(path, metadata)

        config = payload["configuration"]
        supervise_routines(
            routines_dir=Path(payload["routines_dir"]),
            approvals=payload["approvals"],
            poll_seconds=config["poll_seconds"],
            max_cycles=config["max_cycles"],
            max_routines=config["max_routines"],
            output_limit_bytes=config["output_limit_bytes"],
            background=True,
            cycle_callback=record_cycle,
        )
        has_errors = metadata["blocked_count"] > 0 or metadata["failed_count"] > 0
        supervisor_status = "completed-with-errors" if has_errors else "completed"
        exit_code = 1 if has_errors else 0
    except _BackgroundRoutineSupervisorTermination:
        error_class = "BackgroundRoutineSupervisorTermination"
        supervisor_status = "terminated"
        print("ERROR: background routine supervisor terminated by operator", file=sys.stderr)
    except ConductorError as exc:
        error_class = exc.__class__.__name__
        supervisor_status = "failed"
        print("ERROR: %s" % redact_text(str(exc)), file=sys.stderr)
    except Exception as exc:
        error_class = exc.__class__.__name__
        supervisor_status = "failed"
        print("ERROR: background routine supervisor failed: %s" % exc.__class__.__name__, file=sys.stderr)
    finally:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)
        terminate_active_processes()
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        timestamp = utc_now()
        with metadata_lock:
            metadata["status"] = "finished"
            metadata["supervisor_status"] = supervisor_status
            metadata["updated_at_utc"] = timestamp
            metadata["heartbeat_at_utc"] = timestamp
            metadata["finished_at_utc"] = timestamp
            metadata["exit_code"] = exit_code
            metadata["error_class"] = redact_text(error_class)
            _replace_metadata(path, metadata)
    return exit_code


def load_background_routine_supervisor(path: Path) -> Dict:
    metadata_path = Path(path)
    raw = read_regular_text_file_no_follow(
        metadata_path,
        "background routine supervisor metadata",
        MAX_BACKGROUND_ROUTINE_SUPERVISOR_BYTES,
    )
    try:
        metadata = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("background routine supervisor metadata is not strict JSON: %s" % exc)
    validate_background_routine_supervisor(metadata, source=str(metadata_path))
    return metadata


def validate_background_routine_supervisor(metadata: Dict, source: str = "<memory>") -> None:
    root = _exact_object(metadata, BACKGROUND_ROUTINE_SUPERVISOR_FIELDS, source)
    if root.get("schema") != BACKGROUND_ROUTINE_SUPERVISOR_SCHEMA:
        raise ValidationError("%s has unsupported background routine supervisor schema" % source)
    if root.get("status") not in BACKGROUND_ROUTINE_SUPERVISOR_STATUSES:
        raise ValidationError("%s has invalid background routine supervisor status" % source)
    if root.get("supervisor_status") not in BACKGROUND_ROUTINE_SUPERVISOR_RESULT_STATUSES:
        raise ValidationError("%s has invalid routine supervisor result status" % source)
    for key in ("created_at_utc", "updated_at_utc", "heartbeat_at_utc"):
        _validate_timestamp(root.get(key), "%s %s" % (source, key), nullable=False)
    _validate_timestamp(root.get("finished_at_utc"), "%s finished_at_utc" % source, nullable=True)

    worker = _exact_object(
        root.get("worker"),
        {"pid", "process_group_id", "session_id", "hostname", "detached_session", "ready", "argv"},
        "%s worker" % source,
    )
    if worker.get("detached_session") is not True or not isinstance(worker.get("ready"), bool):
        raise ValidationError("%s worker isolation or readiness metadata is invalid" % source)
    if not isinstance(worker.get("hostname"), str) or not worker["hostname"] or len(worker["hostname"]) > 255:
        raise ValidationError("%s worker hostname is invalid" % source)
    if not isinstance(worker.get("argv"), list) or not worker["argv"] or not all(
        isinstance(item, str) and item for item in worker["argv"]
    ):
        raise ValidationError("%s worker argv is invalid" % source)
    if any(item == "--approve" or item.startswith("--approve=") for item in worker["argv"]):
        raise ValidationError("%s worker argv must not persist approval values" % source)
    worker_ids = []
    for key in ("pid", "process_group_id", "session_id"):
        value = worker.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
            raise ValidationError("%s worker.%s is invalid" % (source, key))
        worker_ids.append(value)
    if any(value is None for value in worker_ids) and any(value is not None for value in worker_ids):
        raise ValidationError("%s worker process identity must be entirely present or absent" % source)
    if root["status"] == "launching" and any(value is not None for value in worker_ids):
        raise ValidationError("%s launching worker must not record process identifiers" % source)
    if root["status"] in {"active", "finished"} and any(value is None for value in worker_ids):
        raise ValidationError("%s active or finished worker requires process identifiers" % source)

    configuration = _exact_object(
        root.get("configuration"),
        {"poll_seconds", "max_cycles", "max_routines", "output_limit_bytes"},
        "%s configuration" % source,
    )
    validate_supervise_routines_request(
        routines_dir=Path("."),
        approvals={"background-supervisor"},
        poll_seconds=configuration.get("poll_seconds"),
        max_cycles=configuration.get("max_cycles"),
        max_routines=configuration.get("max_routines"),
        output_limit_bytes=configuration.get("output_limit_bytes"),
        background=True,
    )
    policy = _exact_object(
        root.get("policy"),
        {"approval_count", "approval_values_persisted"},
        "%s policy" % source,
    )
    if (
        not isinstance(policy.get("approval_count"), int)
        or isinstance(policy["approval_count"], bool)
        or not 1 <= policy["approval_count"] <= 1000
        or policy.get("approval_values_persisted") is not False
    ):
        raise ValidationError("%s policy metadata is invalid" % source)
    handoff = _exact_object(
        root.get("handoff"),
        {"preflight_completed", "transport", "payload_persisted", "daemon_installed", "restart_policy"},
        "%s handoff" % source,
    )
    if handoff != {
        "preflight_completed": True,
        "transport": "anonymous-stdin-pipe",
        "payload_persisted": False,
        "daemon_installed": False,
        "restart_policy": "never",
    }:
        raise ValidationError("%s handoff contract is invalid" % source)
    logs = _exact_object(root.get("logs"), {"stdout", "stderr"}, "%s logs" % source)
    _validate_log_name(logs.get("stdout"), ".stdout.log", "%s stdout log" % source)
    _validate_log_name(logs.get("stderr"), ".stderr.log", "%s stderr log" % source)

    counters = []
    for key in ("cycles_completed", "results_count", "completed_count", "blocked_count", "failed_count"):
        value = root.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValidationError("%s %s is invalid" % (source, key))
        counters.append(value)
    if root["results_count"] != root["completed_count"] + root["blocked_count"] + root["failed_count"]:
        raise ValidationError("%s result counters are inconsistent" % source)
    if root["results_count"] > root["cycles_completed"] * configuration["max_routines"]:
        raise ValidationError("%s result counters exceed configured cycle limits" % source)
    if configuration["max_cycles"] and root["cycles_completed"] > configuration["max_cycles"]:
        raise ValidationError("%s cycle count exceeds configured max_cycles" % source)
    if not isinstance(root.get("error_class"), str) or len(root["error_class"]) > 200:
        raise ValidationError("%s error_class is invalid" % source)

    if root["status"] in {"launching", "active"}:
        if root.get("finished_at_utc") is not None or root.get("exit_code") is not None:
            raise ValidationError("%s active metadata cannot be final" % source)
        expected = "not-started" if root["status"] == "launching" else "running"
        if root["supervisor_status"] != expected:
            raise ValidationError("%s active supervisor status is inconsistent" % source)
        if root["status"] == "launching" and worker["ready"]:
            raise ValidationError("%s launching worker cannot be ready" % source)
    else:
        if root.get("finished_at_utc") is None:
            raise ValidationError("%s final metadata requires finished_at_utc" % source)
        if not isinstance(root.get("exit_code"), int) or isinstance(root["exit_code"], bool):
            raise ValidationError("%s final metadata exit_code is invalid" % source)
        if root["supervisor_status"] in {"not-started", "running"}:
            raise ValidationError("%s final supervisor status is inconsistent" % source)
        if root["supervisor_status"] == "completed" and (root["exit_code"] != 0 or root["error_class"]):
            raise ValidationError("%s completed supervisor result is inconsistent" % source)
        if root["supervisor_status"] != "completed" and root["exit_code"] == 0:
            raise ValidationError("%s non-completed supervisor result requires a nonzero exit_code" % source)


def background_routine_supervisor_status(path: Path) -> Dict:
    metadata = load_background_routine_supervisor(path)
    return {
        "schema": "conductor.background_routine_supervisor_status.v1",
        "metadata": str(Path(path)),
        "status": metadata["status"],
        "supervisor_status": metadata["supervisor_status"],
        "liveness": background_routine_supervisor_liveness(metadata),
        "pid": metadata["worker"]["pid"],
        "ready": metadata["worker"]["ready"],
        "heartbeat_at_utc": metadata["heartbeat_at_utc"],
        "cycles_completed": metadata["cycles_completed"],
        "results_count": metadata["results_count"],
        "completed_count": metadata["completed_count"],
        "blocked_count": metadata["blocked_count"],
        "failed_count": metadata["failed_count"],
        "exit_code": metadata["exit_code"],
        "error_class": metadata["error_class"],
    }


def background_routine_supervisor_routines_dir(path: Path) -> Path:
    return _routines_dir_for_metadata(Path(path))


def wait_for_background_routine_supervisor(
    path: Path,
    *,
    timeout_seconds: int,
    poll_interval_seconds: float = 0.25,
) -> Dict:
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 0 <= timeout_seconds <= MAX_BACKGROUND_ROUTINE_SUPERVISOR_WAIT_SECONDS
    ):
        raise ValidationError(
            "background routine supervisor wait timeout_seconds must be from 0 to %d"
            % MAX_BACKGROUND_ROUTINE_SUPERVISOR_WAIT_SECONDS
        )
    if (
        not isinstance(poll_interval_seconds, (int, float))
        or isinstance(poll_interval_seconds, bool)
        or not 0.05 <= poll_interval_seconds <= 60
    ):
        raise ValidationError("background routine supervisor poll interval must be from 0.05 to 60 seconds")
    deadline = time.monotonic() + timeout_seconds
    while True:
        metadata = load_background_routine_supervisor(path)
        if metadata["status"] in {"finished", "launch-failed"}:
            return metadata
        liveness = background_routine_supervisor_liveness(metadata)
        if liveness in {"dead", "stale"}:
            raise PolicyError("background routine supervisor worker is %s" % liveness)
        if time.monotonic() >= deadline:
            raise PolicyError("background routine supervisor did not finish within %d seconds" % timeout_seconds)
        time.sleep(poll_interval_seconds)


def background_routine_supervisor_liveness(metadata: Dict) -> str:
    if metadata.get("status") in {"finished", "launch-failed"}:
        return "finished"
    worker = metadata.get("worker") if isinstance(metadata.get("worker"), dict) else {}
    if worker.get("hostname") != _hostname():
        return "remote"
    pid = worker.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "unavailable"
    if not _pid_is_running(pid):
        return "dead"
    if _heartbeat_age(metadata) > BACKGROUND_ROUTINE_SUPERVISOR_STALE_SECONDS:
        return "stale"
    return "alive"


def latest_background_routine_supervisor(routines_dir: Path) -> Dict:
    workers_dir = Path(routines_dir) / "_supervisor" / "workers"
    for name in _supervisor_metadata_names(workers_dir):
        try:
            metadata = load_background_routine_supervisor(workers_dir / name)
        except (ConductorError, FileNotFoundError):
            continue
        return _supervisor_summary(name, metadata)
    return {}


def _blocking_background_routine_supervisor(routines_dir: Path) -> Dict:
    workers_dir = Path(routines_dir) / "_supervisor" / "workers"
    for name in _supervisor_metadata_names(workers_dir):
        try:
            metadata = load_background_routine_supervisor(workers_dir / name)
        except (ConductorError, FileNotFoundError):
            continue
        if metadata["status"] == "active" and background_routine_supervisor_liveness(metadata) != "dead":
            return _supervisor_summary(name, metadata)
    return {}


def _supervisor_metadata_names(workers_dir: Path) -> List[str]:
    reject_symlink_path(workers_dir, "background routine supervisor workers directory")
    if not workers_dir.exists():
        return []
    directory_fd = open_dir_no_follow(workers_dir, "background routine supervisor workers directory")
    candidates = []
    try:
        for name in os.listdir(directory_fd):
            if not name.endswith(".json"):
                continue
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                candidates.append((info.st_mtime, name))
    finally:
        os.close(directory_fd)
    return [name for _mtime, name in sorted(candidates, reverse=True)[:1000]]


def _supervisor_summary(name: str, metadata: Dict) -> Dict:
    return {
        "metadata": name,
        "status": metadata["status"],
        "supervisor_status": metadata["supervisor_status"],
        "pid": metadata["worker"]["pid"],
        "liveness": background_routine_supervisor_liveness(metadata),
        "ready": metadata["worker"]["ready"],
        "updated_at_utc": metadata["updated_at_utc"],
        "cycles_completed": metadata["cycles_completed"],
        "results_count": metadata["results_count"],
        "completed_count": metadata["completed_count"],
        "blocked_count": metadata["blocked_count"],
        "failed_count": metadata["failed_count"],
        "exit_code": metadata["exit_code"],
        "error_class": metadata["error_class"],
    }


def terminate_background_routine_supervisor(path: Path, *, force: bool = False) -> Dict:
    if not isinstance(force, bool):
        raise ValidationError("force must be boolean")
    metadata = load_background_routine_supervisor(path)
    if metadata["status"] != "active":
        raise ValidationError("background routine supervisor worker is not active")
    worker = metadata["worker"]
    pid = worker["pid"]
    if worker["hostname"] != _hostname():
        raise ValidationError("background routine supervisor worker belongs to another host")
    if worker["process_group_id"] != pid or worker["session_id"] != pid:
        raise ValidationError("background routine supervisor worker is not session-isolated")
    if _heartbeat_age(metadata) > BACKGROUND_ROUTINE_SUPERVISOR_STALE_SECONDS:
        raise ValidationError("background routine supervisor heartbeat is too old for termination")
    if pid == os.getpid() or (hasattr(os, "getpgrp") and os.getpgrp() == pid) or (
        hasattr(os, "getsid") and os.getsid(0) == pid
    ):
        raise ValidationError("refusing to terminate the current process group or session")
    if not _pid_is_running(pid):
        raise ValidationError("background routine supervisor worker is not running")
    try:
        live_group = os.getpgid(pid)
        live_session = os.getsid(pid)
    except ProcessLookupError:
        raise ValidationError("background routine supervisor worker is not running")
    except OSError as exc:
        raise ValidationError("failed to inspect background routine supervisor identity: %s" % exc.__class__.__name__)
    if live_group != pid or live_session != pid:
        raise ValidationError("live background routine supervisor worker is not session-isolated")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        raise ValidationError("background routine supervisor worker is not running")
    except OSError as exc:
        raise ValidationError("failed to signal background routine supervisor: %s" % exc.__class__.__name__)
    signal_name = "TERM"
    if force:
        deadline = time.monotonic() + BACKGROUND_ROUTINE_SUPERVISOR_FORCE_GRACE_SECONDS
        while time.monotonic() < deadline:
            if not _pid_is_running(pid):
                break
            try:
                current = load_background_routine_supervisor(path)
            except (ConductorError, FileNotFoundError):
                current = {}
            if current.get("status") in {"finished", "launch-failed"}:
                break
            time.sleep(0.05)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
                signal_name = "TERM+KILL"
            except ProcessLookupError:
                pass
            except OSError as exc:
                raise ValidationError("failed to force background routine supervisor termination: %s" % exc.__class__.__name__)
    return {"action": "terminate-background-routine-supervisor", "pid": pid, "signal": signal_name}


def _build_payload(prepared: Dict) -> Dict:
    approvals = sorted(validate_approval_tokens(prepared["approvals"]))
    if len(approvals) > 1000:
        raise ValidationError("background routine supervisor approval set exceeds supported limits")
    return {
        "schema": BACKGROUND_ROUTINE_SUPERVISOR_PAYLOAD_SCHEMA,
        "routines_dir": str(prepared["routines_dir"]),
        "approvals": approvals,
        "configuration": {
            "poll_seconds": prepared["poll_seconds"],
            "max_cycles": prepared["max_cycles"],
            "max_routines": prepared["max_routines"],
            "output_limit_bytes": prepared["output_limit_bytes"],
        },
    }


def _read_payload(stream: BinaryIO) -> Dict:
    raw = stream.read(MAX_BACKGROUND_ROUTINE_SUPERVISOR_PAYLOAD_BYTES + 1)
    if len(raw) > MAX_BACKGROUND_ROUTINE_SUPERVISOR_PAYLOAD_BYTES:
        raise ValidationError("background routine supervisor payload is too large")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("background routine supervisor payload is not strict JSON: %s" % exc)
    root = _exact_object(
        payload,
        {"schema", "routines_dir", "approvals", "configuration"},
        "background routine supervisor payload",
    )
    if root.get("schema") != BACKGROUND_ROUTINE_SUPERVISOR_PAYLOAD_SCHEMA:
        raise ValidationError("background routine supervisor payload schema is invalid")
    if not isinstance(root.get("routines_dir"), str) or not Path(root["routines_dir"]).is_absolute():
        raise ValidationError("background routine supervisor payload routines_dir is invalid")
    if not isinstance(root.get("approvals"), list) or len(root["approvals"]) > 1000:
        raise ValidationError("background routine supervisor payload approvals are invalid")
    config = _exact_object(
        root.get("configuration"),
        {"poll_seconds", "max_cycles", "max_routines", "output_limit_bytes"},
        "background routine supervisor payload configuration",
    )
    prepared = validate_supervise_routines_request(
        routines_dir=Path(root["routines_dir"]),
        approvals=root["approvals"],
        poll_seconds=config["poll_seconds"],
        max_cycles=config["max_cycles"],
        max_routines=config["max_routines"],
        output_limit_bytes=config["output_limit_bytes"],
        background=True,
    )
    root["approvals"] = sorted(prepared["approvals"])
    root["routines_dir"] = str(prepared["routines_dir"])
    return root


def _encode_payload(payload: Dict) -> bytes:
    data = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(data) > MAX_BACKGROUND_ROUTINE_SUPERVISOR_PAYLOAD_BYTES:
        raise ValidationError("background routine supervisor payload is too large")
    return data


def _initial_metadata(
    *,
    command: List[str],
    prepared: Dict,
    stdout_name: str,
    stderr_name: str,
) -> Dict:
    timestamp = utc_now()
    metadata = {
        "schema": BACKGROUND_ROUTINE_SUPERVISOR_SCHEMA,
        "status": "launching",
        "created_at_utc": timestamp,
        "updated_at_utc": timestamp,
        "heartbeat_at_utc": timestamp,
        "finished_at_utc": None,
        "worker": {
            "pid": None,
            "process_group_id": None,
            "session_id": None,
            "hostname": _hostname(),
            "detached_session": True,
            "ready": False,
            "argv": [redact_text(str(item)) for item in command],
        },
        "configuration": {
            "poll_seconds": prepared["poll_seconds"],
            "max_cycles": prepared["max_cycles"],
            "max_routines": prepared["max_routines"],
            "output_limit_bytes": prepared["output_limit_bytes"],
        },
        "policy": {
            "approval_count": len(prepared["approvals"]),
            "approval_values_persisted": False,
        },
        "handoff": {
            "preflight_completed": True,
            "transport": "anonymous-stdin-pipe",
            "payload_persisted": False,
            "daemon_installed": False,
            "restart_policy": "never",
        },
        "logs": {"stdout": stdout_name, "stderr": stderr_name},
        "supervisor_status": "not-started",
        "cycles_completed": 0,
        "results_count": 0,
        "completed_count": 0,
        "blocked_count": 0,
        "failed_count": 0,
        "exit_code": None,
        "error_class": "",
    }
    validate_background_routine_supervisor(metadata)
    return metadata


def _validate_payload_matches_metadata(payload: Dict, metadata: Dict, routines_dir: Path) -> None:
    if Path(payload["routines_dir"]) != routines_dir.resolve():
        raise PolicyError("background routine supervisor directory does not match metadata location")
    if payload["configuration"] != metadata["configuration"]:
        raise PolicyError("background routine supervisor configuration does not match metadata")
    if len(payload["approvals"]) != metadata["policy"]["approval_count"]:
        raise PolicyError("background routine supervisor approval count does not match metadata")


def _validate_worker_identity(metadata: Dict) -> None:
    if metadata.get("status") != "active":
        raise PolicyError("background routine supervisor worker requires active metadata")
    worker = metadata["worker"]
    if (
        worker.get("pid") != os.getpid()
        or worker.get("process_group_id") != os.getpid()
        or worker.get("session_id") != os.getpid()
    ):
        raise PolicyError("background routine supervisor worker identity does not match metadata")
    if worker.get("hostname") != _hostname():
        raise PolicyError("background routine supervisor worker host does not match metadata")
    if os.getpgrp() != os.getpid() or os.getsid(0) != os.getpid():
        raise PolicyError("background routine supervisor worker is not session-isolated")


def _routines_dir_for_metadata(path: Path) -> Path:
    resolved = Path(path).resolve()
    if resolved.parent.name != "workers" or resolved.parent.parent.name != "_supervisor":
        raise ValidationError("background routine supervisor metadata path is outside the supervisor workers directory")
    return resolved.parent.parent.parent


def _wait_for_worker_ready(path: Path, pid: int) -> Dict:
    deadline = time.monotonic() + BACKGROUND_ROUTINE_SUPERVISOR_READY_SECONDS
    while time.monotonic() < deadline:
        metadata = load_background_routine_supervisor(path)
        if metadata["worker"]["ready"]:
            return metadata
        if metadata["status"] in {"finished", "launch-failed"} or not _pid_is_running(pid):
            raise ValidationError("background routine supervisor worker exited before readiness")
        time.sleep(0.02)
    raise ValidationError("background routine supervisor worker did not become ready")


def _heartbeat_loop(path: Path, metadata: Dict, lock: threading.Lock, stop: threading.Event) -> None:
    while not stop.wait(BACKGROUND_ROUTINE_SUPERVISOR_HEARTBEAT_SECONDS):
        try:
            with lock:
                metadata["updated_at_utc"] = utc_now()
                metadata["heartbeat_at_utc"] = metadata["updated_at_utc"]
                _replace_metadata(path, metadata)
        except Exception:
            return


def _write_new_metadata(path: Path, metadata: Dict) -> None:
    validate_background_routine_supervisor(metadata)
    write_new_text_file_no_follow(
        path,
        "background routine supervisor metadata",
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
    )


def _replace_metadata(path: Path, metadata: Dict) -> None:
    validate_background_routine_supervisor(metadata)
    replace_text_file_no_follow(
        path,
        "background routine supervisor metadata",
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        ".background-routine-supervisor-",
        sync=False,
    )


def _finish_launch_failure(path: Path, metadata: Dict, error_class: str) -> None:
    timestamp = utc_now()
    metadata["status"] = "launch-failed"
    metadata["supervisor_status"] = "failed"
    metadata["updated_at_utc"] = timestamp
    metadata["heartbeat_at_utc"] = timestamp
    metadata["finished_at_utc"] = timestamp
    metadata["exit_code"] = 1
    metadata["error_class"] = redact_text(error_class)
    _replace_metadata(path, metadata)


@contextmanager
def _launch_lock(supervisor_dir: Path):
    path = Path(supervisor_dir) / "workers.lock"
    reject_symlink_path(path, "background routine supervisor launch lock")
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _open_new_log(path: Path, label: str) -> int:
    reject_symlink_path(path, label)
    parent_fd = open_dir_no_follow(path.parent, "%s parent" % label)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValidationError("%s must be a regular file" % label)
        return fd
    except FileExistsError:
        raise ValidationError("%s already exists: %s" % (label, path))
    finally:
        os.close(parent_fd)


def _write_pipe(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise BrokenPipeError("background routine supervisor payload pipe closed")
        offset += written


def _stop_failed_child(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited == pid:
            return
        time.sleep(0.02)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _validate_log_name(value, suffix: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value.endswith(suffix)
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or len(value) > 255
    ):
        raise ValidationError("%s is invalid" % label)


def _validate_timestamp(value, label: str, *, nullable: bool) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError("%s is invalid" % label)
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValidationError("%s is invalid" % label)


def _heartbeat_age(metadata: Dict) -> float:
    value = metadata.get("heartbeat_at_utc")
    if not isinstance(value, str):
        return float("inf")
    try:
        heartbeat = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    return max(0.0, (datetime.now(timezone.utc) - heartbeat.astimezone(timezone.utc)).total_seconds())


def _hostname() -> str:
    return socket.gethostname()


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _exact_object(value, fields, label: str) -> Dict:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError("%s must contain exactly: %s" % (label, ", ".join(sorted(fields))))
    return value


def _object_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key %s" % key)
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant %s" % value)
