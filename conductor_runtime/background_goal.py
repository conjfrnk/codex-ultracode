import hashlib
import json
import os
import secrets
import signal
import stat
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional

from .artifacts import utc_now, utc_stamp
from .background_run import runtime_self_command
from .errors import ConductorError, PolicyError, ValidationError
from .desktop_notification import send_artifact_desktop_notification
from .goal_loop import load_goal_state
from .model_goal_loop import (
    mark_model_goal_interrupted,
    model_goal_id,
    run_model_goal_loop,
    validate_model_goal_loop_request,
    validate_model_goal_resume,
)
from .model_planner import MAX_MODEL_WORKFLOW_ITEMS, MAX_MODEL_WORKFLOW_TASK_CHARS
from .redaction import redact_text
from .runner import DEFAULT_OUTPUT_LIMIT_BYTES, terminate_active_processes
from .security import (
    RuntimePolicy,
    ensure_dir_no_follow,
    open_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    validate_approval_tokens,
    write_new_text_file_no_follow,
)
from .workflow import SAFE_ID


BACKGROUND_MODEL_GOAL_SCHEMA = "conductor.background_model_goal.v1"
BACKGROUND_MODEL_GOAL_PAYLOAD_SCHEMA = "conductor.background_model_goal_payload.v1"
BACKGROUND_MODEL_GOAL_STATUSES = {"launching", "active", "finished", "launch-failed"}
BACKGROUND_MODEL_GOAL_FIELDS = {
    "schema",
    "status",
    "created_at_utc",
    "updated_at_utc",
    "heartbeat_at_utc",
    "finished_at_utc",
    "goal",
    "worker",
    "task",
    "verifier",
    "policy",
    "handoff",
    "logs",
    "goal_status",
    "iterations",
    "exit_code",
    "error_class",
}
MAX_BACKGROUND_MODEL_GOAL_BYTES = 512 * 1024
MAX_BACKGROUND_MODEL_GOAL_PAYLOAD_BYTES = 512 * 1024
MAX_BACKGROUND_MODEL_GOAL_WAIT_SECONDS = 7 * 24 * 60 * 60
BACKGROUND_MODEL_GOAL_HEARTBEAT_SECONDS = 1.0
BACKGROUND_MODEL_GOAL_STALE_SECONDS = 5
BACKGROUND_MODEL_GOAL_READY_SECONDS = 5
BACKGROUND_MODEL_GOAL_FORCE_GRACE_SECONDS = 2


@dataclass(frozen=True)
class BackgroundModelGoalResult:
    goal_id: str
    goal_path: Path
    metadata_path: Path
    metadata: Dict
    pid: int


class _BackgroundModelGoalTermination(BaseException):
    pass


def start_background_model_goal(
    *,
    task: str,
    workspace: Path,
    runs_dir: Path,
    goals_dir: Path,
    policy: RuntimePolicy,
    check_command: Optional[List[str]],
    check_prompt: Optional[str],
    max_iterations: int,
    dynamic_interval: bool = False,
    min_delay_minutes: Optional[int] = None,
    max_delay_minutes: Optional[int] = None,
    monitor_command: Optional[List[str]] = None,
    check_model: Optional[str] = None,
    planner_model: Optional[str] = None,
    planner_effort: str = "high",
    name: Optional[str] = None,
    write_capable: bool = False,
    max_workers: int = 1,
    max_items: int = MAX_MODEL_WORKFLOW_ITEMS,
    max_steps: int = 32,
    planning_timeout_seconds: int = 900,
    planning_output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    check_timeout_seconds: int = 120,
    check_output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    goal_id: Optional[str] = None,
    resume_goal: Optional[Path] = None,
    reuse_planner_session: bool = True,
    direct_plan: bool = False,
    progressive_plan: bool = False,
    self_command: Optional[List[str]] = None,
    desktop_notify: bool = False,
) -> BackgroundModelGoalResult:
    if os.name != "posix":
        raise ValidationError("background model goals currently require POSIX session isolation")
    if not isinstance(desktop_notify, bool):
        raise ValidationError("desktop_notify must be boolean")
    prepared = validate_model_goal_loop_request(
        task=task,
        workspace=workspace,
        policy=policy,
        check_command=check_command,
        check_prompt=check_prompt,
        check_model=check_model,
        planner_model=planner_model,
        planner_effort=planner_effort,
        name=name,
        write_capable=write_capable,
        max_iterations=max_iterations,
        dynamic_interval=dynamic_interval,
        min_delay_minutes=min_delay_minutes,
        max_delay_minutes=max_delay_minutes,
        monitor_command=monitor_command,
        max_workers=max_workers,
        max_items=max_items,
        max_steps=max_steps,
        planning_timeout_seconds=planning_timeout_seconds,
        planning_output_limit_bytes=planning_output_limit_bytes,
        check_timeout_seconds=check_timeout_seconds,
        check_output_limit_bytes=check_output_limit_bytes,
        foreground=False,
        reuse_planner_session=reuse_planner_session,
        direct_plan=direct_plan,
        progressive_plan=progressive_plan,
    )
    requested_goals_path = Path(goals_dir)
    reject_symlink_path(requested_goals_path, "background model goals directory")
    goals_path = requested_goals_path.resolve()
    runs_path = Path(runs_dir).resolve()
    workspace_path = Path(workspace).resolve()
    if resume_goal is not None:
        requested_goal_path = Path(resume_goal)
        reject_symlink_path(requested_goal_path, "background model goal resume artifact")
        goal_path = requested_goal_path.resolve()
        if goal_path.parent != goals_path:
            raise PolicyError("background model goal resume artifact must be inside goals_dir")
        state = validate_model_goal_resume(
            goal_path=goal_path,
            requested_goal_id=goal_id,
            task_sha256=prepared["task_sha256"],
            config_fingerprint=prepared["config_fingerprint"],
            verifier_fingerprint=prepared["verifier"]["fingerprint"],
            max_iterations=max_iterations,
        )
        goal_slug = str(state["goal_id"])
    else:
        goal_slug = model_goal_id(goal_id, prepared["name"])
        goal_path = goals_path / ("%s.json" % goal_slug)
        reject_symlink_path(goal_path, "model goal artifact")
        if goal_path.exists():
            raise ValidationError("model goal artifact already exists: %s" % goal_path)

    workers_dir = goals_path / "background-workers"
    workers_fd = ensure_dir_no_follow(workers_dir, "background model goal workers directory")
    os.close(workers_fd)
    launch_id = "%s-%s-%s" % (goal_slug, utc_stamp(), secrets.token_hex(4))
    metadata_path = workers_dir / ("%s.json" % launch_id)
    stdout_name = "%s.stdout.log" % launch_id
    stderr_name = "%s.stderr.log" % launch_id
    payload = _build_payload(
        prepared=prepared,
        workspace=workspace_path,
        runs_dir=runs_path,
        goals_dir=goals_path,
        goal_id=goal_slug,
        resume_goal=goal_path if resume_goal is not None else None,
        policy=policy,
        check_model=check_model,
        write_capable=write_capable,
        max_iterations=max_iterations,
        max_workers=max_workers,
        max_items=max_items,
        max_steps=max_steps,
        planning_timeout_seconds=planning_timeout_seconds,
        planning_output_limit_bytes=planning_output_limit_bytes,
        check_timeout_seconds=check_timeout_seconds,
        check_output_limit_bytes=check_output_limit_bytes,
        direct_plan=direct_plan,
        progressive_plan=progressive_plan,
    )
    payload_bytes = _encode_payload(payload)
    command = list(self_command or runtime_self_command()) + ["_background-model-goal-worker", str(metadata_path)]
    if desktop_notify:
        command.append("--desktop-notify")
    metadata = _initial_metadata(
        goal_id=goal_slug,
        goal_path=goal_path,
        resumed=resume_goal is not None,
        command=command,
        prepared=prepared,
        policy=policy,
        stdout_name=stdout_name,
        stderr_name=stderr_name,
    )
    _write_new_metadata(metadata_path, metadata)

    stdout_fd = _open_new_log(workers_dir / stdout_name, "background model goal stdout log")
    stderr_fd = None
    read_fd = None
    write_fd = None
    pid = None
    try:
        stderr_fd = _open_new_log(workers_dir / stderr_name, "background model goal stderr log")
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
        pid = os.posix_spawn(command[0], command, os.environ.copy(), file_actions=file_actions, setsid=True)
    except Exception as exc:
        if write_fd is not None:
            os.close(write_fd)
            write_fd = None
        if pid is not None:
            _stop_failed_child(pid)
        _finish_launch_failure(metadata_path, metadata, exc.__class__.__name__)
        raise ValidationError("failed to launch background model goal worker: %s" % exc.__class__.__name__)
    finally:
        if read_fd is not None:
            os.close(read_fd)
        os.close(stdout_fd)
        if stderr_fd is not None:
            os.close(stderr_fd)

    metadata["status"] = "active"
    metadata["updated_at_utc"] = utc_now()
    metadata["heartbeat_at_utc"] = metadata["updated_at_utc"]
    metadata["worker"]["pid"] = pid
    metadata["worker"]["process_group_id"] = pid
    metadata["worker"]["session_id"] = pid
    _replace_metadata(metadata_path, metadata)
    try:
        if write_fd is None:
            raise BrokenPipeError("background model goal policy pipe is unavailable")
        _write_pipe(write_fd, payload_bytes)
        os.close(write_fd)
        write_fd = None
        metadata = _wait_for_worker_ready(metadata_path, pid)
    except Exception as exc:
        if write_fd is not None:
            os.close(write_fd)
        _stop_failed_child(pid)
        _finish_launch_failure(metadata_path, metadata, exc.__class__.__name__)
        raise ValidationError("failed to hand off background model goal policy: %s" % exc.__class__.__name__)
    return BackgroundModelGoalResult(
        goal_id=goal_slug,
        goal_path=goal_path,
        metadata_path=metadata_path,
        metadata=metadata,
        pid=pid,
    )


def run_background_model_goal_worker(
    metadata_path: Path,
    input_stream: Optional[BinaryIO] = None,
    *,
    desktop_notify: bool = False,
) -> int:
    if not isinstance(desktop_notify, bool):
        raise ValidationError("desktop_notify must be boolean")
    path = Path(metadata_path)
    metadata = load_background_model_goal(path)
    persisted_notify = metadata["worker"]["argv"][-1] == "--desktop-notify"
    if desktop_notify is not persisted_notify:
        raise PolicyError("background model goal notification request does not match launch metadata")
    stream = input_stream if input_stream is not None else sys.stdin.buffer
    stop_heartbeat = threading.Event()
    metadata_lock = threading.Lock()
    heartbeat_thread = None
    payload = None
    goal_path = path.parent.parent / metadata["goal"]["artifact"]
    exit_code = 1
    error_class = ""
    goal_status = "failed"
    iterations = 0
    previous_sigterm = None
    try:
        payload = _read_payload(stream)
        _validate_worker_identity(metadata)
        _validate_payload_matches_metadata(payload, metadata)
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def handle_sigterm(signum, frame):
            del frame
            terminate_active_processes()
            raise _BackgroundModelGoalTermination("received signal %d" % signum)

        signal.signal(signal.SIGTERM, handle_sigterm)
        with metadata_lock:
            metadata["worker"]["ready"] = True
            metadata["updated_at_utc"] = utc_now()
            metadata["heartbeat_at_utc"] = metadata["updated_at_utc"]
            _replace_metadata(path, metadata)
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(path, metadata, metadata_lock, stop_heartbeat),
            name="conductor-background-model-goal-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        policy = _policy_from_payload(payload)
        verifier = payload["verifier"]
        config = payload["config"]
        dynamic_interval = config.get("dynamic_interval")
        result = run_model_goal_loop(
            task=payload["task"],
            workspace=Path(payload["workspace"]),
            runs_dir=Path(payload["runs_dir"]),
            goals_dir=Path(payload["goals_dir"]),
            policy=policy,
            check_command=verifier["command"],
            check_prompt=verifier["prompt"],
            check_model=config["check_model"],
            planner_model=config["planner_model"],
            planner_effort=config["planner_effort"],
            name=config["name"],
            write_capable=config["write_capable"],
            max_iterations=config["max_iterations"],
            dynamic_interval=dynamic_interval is not None,
            min_delay_minutes=(
                dynamic_interval["min_delay_minutes"]
                if dynamic_interval is not None
                else None
            ),
            max_delay_minutes=(
                dynamic_interval["max_delay_minutes"]
                if dynamic_interval is not None
                else None
            ),
            monitor_command=config.get("monitor_command"),
            max_workers=config["max_workers"],
            max_items=config["max_items"],
            max_steps=config["max_steps"],
            planning_timeout_seconds=config["planning_timeout_seconds"],
            planning_output_limit_bytes=config["planning_output_limit_bytes"],
            check_timeout_seconds=config["check_timeout_seconds"],
            check_output_limit_bytes=config["check_output_limit_bytes"],
            goal_id=payload["goal_id"],
            resume_goal=Path(payload["resume_goal"]) if payload["resume_goal"] else None,
            foreground=False,
            reuse_planner_session=config["reuse_planner_session"],
            direct_plan=config["direct_plan"],
            progressive_plan=config["progressive_plan"],
        )
        goal_status = result.status
        iterations = result.iterations
        exit_code = 0 if result.status == "completed" else 1
    except _BackgroundModelGoalTermination:
        error_class = "BackgroundModelGoalTermination"
        goal_status = "terminated"
        mark_model_goal_interrupted(goal_path, "terminated", error_class)
        print("ERROR: background model goal terminated by operator", file=sys.stderr)
    except ConductorError as exc:
        error_class = exc.__class__.__name__
        mark_model_goal_interrupted(goal_path, "failed", error_class)
        print("ERROR: %s" % redact_text(str(exc)), file=sys.stderr)
    except Exception as exc:
        error_class = exc.__class__.__name__
        mark_model_goal_interrupted(goal_path, "failed", error_class)
        print("ERROR: background model goal worker failed: %s" % exc.__class__.__name__, file=sys.stderr)
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=BACKGROUND_MODEL_GOAL_HEARTBEAT_SECONDS + 0.5)
        if goal_path.exists():
            try:
                state = load_goal_state(goal_path)
                goal_status = str(state.get("status") or goal_status)
                iterations_value = state.get("iterations")
                if isinstance(iterations_value, list):
                    iterations = len(iterations_value)
            except ConductorError:
                pass
        with metadata_lock:
            metadata["status"] = "finished"
            metadata["updated_at_utc"] = utc_now()
            metadata["heartbeat_at_utc"] = metadata["updated_at_utc"]
            metadata["finished_at_utc"] = metadata["updated_at_utc"]
            metadata["goal_status"] = goal_status
            metadata["iterations"] = iterations
            metadata["exit_code"] = exit_code
            metadata["error_class"] = redact_text(error_class)
            _replace_metadata(path, metadata)
        if desktop_notify:
            notification_artifact = goal_path if goal_path.is_file() else path
            try:
                receipt = send_artifact_desktop_notification(
                    notification_artifact,
                    goal_status,
                )
                print(
                    "Desktop notification: %s (%s)"
                    % (receipt["status"], receipt["backend"])
                )
            except Exception as exc:
                print(
                    "WARNING: desktop notification unavailable: %s"
                    % exc.__class__.__name__,
                    file=sys.stderr,
                )
    return exit_code


def load_background_model_goal(path: Path) -> Dict:
    raw = read_regular_text_file_no_follow(
        Path(path),
        "background model goal metadata",
        MAX_BACKGROUND_MODEL_GOAL_BYTES,
    )
    try:
        data = json.loads(raw, object_pairs_hook=_object_without_duplicates, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("background model goal metadata is not strict JSON: %s" % exc)
    validate_background_model_goal(data, source=str(path))
    return data


def validate_background_model_goal(metadata: Dict, source: str = "<memory>") -> None:
    root = _exact_object(metadata, BACKGROUND_MODEL_GOAL_FIELDS, source)
    if root.get("schema") != BACKGROUND_MODEL_GOAL_SCHEMA:
        raise ValidationError("%s has unsupported background model goal schema" % source)
    if root.get("status") not in BACKGROUND_MODEL_GOAL_STATUSES:
        raise ValidationError("%s has invalid background model goal status" % source)
    for key in ("created_at_utc", "updated_at_utc", "heartbeat_at_utc"):
        _validate_timestamp(root.get(key), "%s %s" % (source, key), nullable=False)
    _validate_timestamp(root.get("finished_at_utc"), "%s finished_at_utc" % source, nullable=True)
    goal = _exact_object(root.get("goal"), {"id", "artifact", "resumed"}, "%s goal" % source)
    if not isinstance(goal.get("id"), str) or not SAFE_ID.fullmatch(goal["id"]):
        raise ValidationError("%s goal id is invalid" % source)
    if goal.get("artifact") != "%s.json" % goal["id"] or not isinstance(goal.get("resumed"), bool):
        raise ValidationError("%s goal artifact metadata is invalid" % source)
    worker = _exact_object(
        root.get("worker"),
        {"pid", "process_group_id", "session_id", "hostname", "detached_session", "ready", "argv"},
        "%s worker" % source,
    )
    if worker.get("detached_session") is not True or not isinstance(worker.get("hostname"), str) or not worker["hostname"]:
        raise ValidationError("%s worker isolation metadata is invalid" % source)
    if not isinstance(worker.get("ready"), bool):
        raise ValidationError("%s worker readiness metadata is invalid" % source)
    if not isinstance(worker.get("argv"), list) or not worker["argv"] or not all(isinstance(x, str) and x for x in worker["argv"]):
        raise ValidationError("%s worker argv is invalid" % source)
    if any(value == "--approve" or value.startswith("--approve=") for value in worker["argv"]):
        raise ValidationError("%s worker argv must not persist approval values" % source)
    notification_flags = [value for value in worker["argv"] if value == "--desktop-notify"]
    if (
        len(notification_flags) > 1
        or any(value.startswith("--desktop-notify=") for value in worker["argv"])
        or (notification_flags and worker["argv"][-1] != "--desktop-notify")
    ):
        raise ValidationError("%s worker argv has an invalid desktop notification flag" % source)
    for key in ("pid", "process_group_id", "session_id"):
        value = worker.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
            raise ValidationError("%s worker.%s is invalid" % (source, key))
    if root["status"] == "launching" and any(worker[key] is not None for key in ("pid", "process_group_id", "session_id")):
        raise ValidationError("%s launching worker must not record process identifiers" % source)
    if root["status"] == "launching" and worker["ready"]:
        raise ValidationError("%s launching worker cannot be ready" % source)
    if root["status"] in {"active", "finished"} and any(worker[key] is None for key in ("pid", "process_group_id", "session_id")):
        raise ValidationError("%s active or finished worker requires process identifiers" % source)
    task = _exact_object(root.get("task"), {"sha256", "chars", "raw_persisted"}, "%s task" % source)
    _validate_sha256(task.get("sha256"), "%s task.sha256" % source)
    if (
        not isinstance(task.get("chars"), int)
        or isinstance(task["chars"], bool)
        or not 1 <= task["chars"] <= MAX_MODEL_WORKFLOW_TASK_CHARS
        or task.get("raw_persisted") is not False
    ):
        raise ValidationError("%s task metadata is invalid" % source)
    verifier = _exact_object(root.get("verifier"), {"kind", "fingerprint"}, "%s verifier" % source)
    if verifier.get("kind") not in {"command", "model", "command+model"}:
        raise ValidationError("%s verifier kind is invalid" % source)
    _validate_sha256(verifier.get("fingerprint"), "%s verifier fingerprint" % source)
    policy = _exact_object(
        root.get("policy"),
        {"allow_writes", "allow_destructive", "allow_network", "allow_agent", "allow_parallel", "approval_count", "approval_values_persisted"},
        "%s policy" % source,
    )
    for key in ("allow_writes", "allow_destructive", "allow_network", "allow_agent", "allow_parallel"):
        if not isinstance(policy.get(key), bool):
            raise ValidationError("%s policy.%s must be boolean" % (source, key))
    if not isinstance(policy.get("approval_count"), int) or isinstance(policy["approval_count"], bool) or not 0 <= policy["approval_count"] <= 1000:
        raise ValidationError("%s policy approval_count is invalid" % source)
    if policy.get("approval_values_persisted") is not False:
        raise ValidationError("%s must not persist approval values" % source)
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
        raise ValidationError("%s background model goal handoff contract is invalid" % source)
    logs = _exact_object(root.get("logs"), {"stdout", "stderr"}, "%s logs" % source)
    if not all(
        isinstance(logs.get(key), str)
        and Path(logs[key]).name == logs[key]
        and logs[key] not in {".", ".."}
        and logs[key].endswith(".%s.log" % key)
        for key in ("stdout", "stderr")
    ):
        raise ValidationError("%s log metadata is invalid" % source)
    if not isinstance(root.get("goal_status"), str) or not root["goal_status"]:
        raise ValidationError("%s goal_status is invalid" % source)
    if not isinstance(root.get("iterations"), int) or isinstance(root["iterations"], bool) or root["iterations"] < 0:
        raise ValidationError("%s iterations is invalid" % source)
    if root["status"] in {"launching", "active"}:
        if root.get("finished_at_utc") is not None or root.get("exit_code") is not None:
            raise ValidationError("%s active background model goal cannot be final" % source)
    elif root.get("finished_at_utc") is None or not isinstance(root.get("exit_code"), int) or isinstance(root["exit_code"], bool):
        raise ValidationError("%s final background model goal metadata is incomplete" % source)
    if not isinstance(root.get("error_class"), str) or len(root["error_class"]) > 200:
        raise ValidationError("%s error_class is invalid" % source)


def wait_for_background_model_goal(path: Path, *, timeout_seconds: int, poll_interval_seconds: float = 0.25) -> Dict:
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 0 <= timeout_seconds <= MAX_BACKGROUND_MODEL_GOAL_WAIT_SECONDS:
        raise ValidationError("background model goal timeout_seconds is invalid")
    if not isinstance(poll_interval_seconds, (int, float)) or isinstance(poll_interval_seconds, bool) or not 0.05 <= poll_interval_seconds <= 60:
        raise ValidationError("background model goal poll interval is invalid")
    deadline = time.monotonic() + timeout_seconds
    while True:
        metadata = load_background_model_goal(path)
        if metadata["status"] in {"finished", "launch-failed"}:
            return metadata
        liveness = background_model_goal_liveness(metadata)
        if liveness in {"dead", "stale"}:
            raise PolicyError("background model goal worker is %s; inspect the goal artifact and resume explicitly" % liveness)
        if time.monotonic() >= deadline:
            raise PolicyError("background model goal did not finish within %d seconds" % timeout_seconds)
        time.sleep(poll_interval_seconds)


def background_model_goal_liveness(metadata: Dict) -> str:
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
    if _heartbeat_age(metadata) > BACKGROUND_MODEL_GOAL_STALE_SECONDS:
        return "stale"
    return "alive"


def latest_background_model_goal(goals_dir: Path, goal_id: str) -> Dict:
    workers_dir = Path(goals_dir) / "background-workers"
    reject_symlink_path(workers_dir, "background model goal workers directory")
    if not workers_dir.exists():
        return {}
    directory_fd = open_dir_no_follow(workers_dir, "background model goal workers directory")
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
    for _mtime, name in sorted(candidates, reverse=True)[:1000]:
        try:
            metadata = load_background_model_goal(workers_dir / name)
        except (ConductorError, FileNotFoundError):
            continue
        if metadata["goal"]["id"] != goal_id:
            continue
        return {
            "metadata": name,
            "status": metadata["status"],
            "goal_status": metadata["goal_status"],
            "iterations": metadata["iterations"],
            "pid": metadata["worker"]["pid"],
            "liveness": background_model_goal_liveness(metadata),
            "updated_at_utc": metadata["updated_at_utc"],
            "exit_code": metadata["exit_code"],
            "error_class": metadata["error_class"],
        }
    return {}


def terminate_background_model_goal(path: Path, *, force: bool = False) -> Dict:
    if not isinstance(force, bool):
        raise ValidationError("force must be boolean")
    metadata = load_background_model_goal(path)
    if metadata["status"] != "active":
        raise ValidationError("background model goal worker is not active")
    worker = metadata["worker"]
    pid = worker["pid"]
    if worker["hostname"] != _hostname():
        raise ValidationError("background model goal worker belongs to another host")
    if worker["process_group_id"] != pid or worker["session_id"] != pid:
        raise ValidationError("background model goal worker is not session-isolated")
    if _heartbeat_age(metadata) > BACKGROUND_MODEL_GOAL_STALE_SECONDS:
        raise ValidationError("background model goal heartbeat is too old for termination")
    if pid == os.getpid() or (hasattr(os, "getpgrp") and os.getpgrp() == pid) or (hasattr(os, "getsid") and os.getsid(0) == pid):
        raise ValidationError("refusing to terminate the current process group or session")
    if not _pid_is_running(pid):
        raise ValidationError("background model goal worker is not running")
    try:
        live_group = os.getpgid(pid)
        live_session = os.getsid(pid)
    except ProcessLookupError:
        raise ValidationError("background model goal worker is not running")
    except OSError as exc:
        raise ValidationError("failed to inspect background model goal worker identity: %s" % exc.__class__.__name__)
    if live_group != pid or live_session != pid:
        raise ValidationError("live background model goal worker is not session-isolated")
    signal_number = signal.SIGTERM
    try:
        os.killpg(pid, signal_number)
    except ProcessLookupError:
        raise ValidationError("background model goal worker is not running")
    except OSError as exc:
        raise ValidationError("failed to signal background model goal worker: %s" % exc.__class__.__name__)
    signal_name = "TERM"
    if force:
        deadline = time.monotonic() + BACKGROUND_MODEL_GOAL_FORCE_GRACE_SECONDS
        while time.monotonic() < deadline:
            if not _pid_is_running(pid):
                break
            try:
                current = load_background_model_goal(path)
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
                raise ValidationError("failed to force background model goal termination: %s" % exc.__class__.__name__)
    return {"action": "terminate-background-model-goal", "pid": pid, "signal": signal_name}


def _build_payload(
    *, prepared: Dict, workspace: Path, runs_dir: Path, goals_dir: Path, goal_id: str,
    resume_goal: Optional[Path], policy: RuntimePolicy, check_model: Optional[str], write_capable: bool,
    max_iterations: int, max_workers: int, max_items: int, max_steps: int,
    planning_timeout_seconds: int, planning_output_limit_bytes: int,
    check_timeout_seconds: int, check_output_limit_bytes: int,
    direct_plan: bool,
    progressive_plan: bool,
) -> Dict:
    verifier = prepared["verifier"]
    approvals = sorted(validate_approval_tokens(policy.approvals))
    return {
        "schema": BACKGROUND_MODEL_GOAL_PAYLOAD_SCHEMA,
        "task": prepared["task"],
        "workspace": str(workspace),
        "runs_dir": str(runs_dir),
        "goals_dir": str(goals_dir),
        "goal_id": goal_id,
        "resume_goal": str(resume_goal) if resume_goal is not None else None,
        "policy": {
            "allow_writes": bool(policy.allow_writes),
            "allow_destructive": bool(policy.allow_destructive),
            "allow_network": bool(policy.allow_network),
            "allow_agent": bool(policy.allow_agent),
            "allow_parallel": bool(policy.allow_parallel),
            "approvals": approvals,
        },
        "verifier": {
            "kind": verifier["kind"],
            "command": verifier.get("command"),
            "prompt": verifier.get("prompt"),
            "fingerprint": verifier["fingerprint"],
        },
        "config": {
            "check_model": verifier.get("model"),
            "planner_model": prepared["planner_model"],
            "planner_effort": prepared["planner_effort"],
            "name": prepared["name"],
            "write_capable": bool(write_capable),
            "max_iterations": max_iterations,
            "dynamic_interval": prepared["dynamic_interval"],
            "monitor": prepared["monitor"],
            "monitor_command": prepared["monitor_command"],
            "max_workers": max_workers,
            "max_items": max_items,
            "max_steps": max_steps,
            "planning_timeout_seconds": planning_timeout_seconds,
            "planning_output_limit_bytes": planning_output_limit_bytes,
            "check_timeout_seconds": check_timeout_seconds,
            "check_output_limit_bytes": check_output_limit_bytes,
            "reuse_planner_session": prepared["reuse_planner_session"],
            "direct_plan": bool(direct_plan),
            "progressive_plan": bool(progressive_plan),
        },
    }


def _read_payload(stream: BinaryIO) -> Dict:
    raw = stream.read(MAX_BACKGROUND_MODEL_GOAL_PAYLOAD_BYTES + 1)
    if len(raw) > MAX_BACKGROUND_MODEL_GOAL_PAYLOAD_BYTES:
        raise ValidationError("background model goal payload is too large")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_without_duplicates, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("background model goal payload is not strict JSON: %s" % exc)
    root = _exact_object(
        payload,
        {"schema", "task", "workspace", "runs_dir", "goals_dir", "goal_id", "resume_goal", "policy", "verifier", "config"},
        "background model goal payload",
    )
    if root.get("schema") != BACKGROUND_MODEL_GOAL_PAYLOAD_SCHEMA:
        raise ValidationError("background model goal payload schema is invalid")
    for key in ("workspace", "runs_dir", "goals_dir"):
        if not isinstance(root.get(key), str) or not Path(root[key]).is_absolute():
            raise ValidationError("background model goal payload %s is invalid" % key)
    if not isinstance(root.get("task"), str) or not root["task"] or not isinstance(root.get("goal_id"), str) or not root["goal_id"]:
        raise ValidationError("background model goal task or id is invalid")
    if root.get("resume_goal") is not None and (not isinstance(root["resume_goal"], str) or not Path(root["resume_goal"]).is_absolute()):
        raise ValidationError("background model goal resume path is invalid")
    policy = _exact_object(
        root.get("policy"),
        {"allow_writes", "allow_destructive", "allow_network", "allow_agent", "allow_parallel", "approvals"},
        "background model goal payload policy",
    )
    for key in ("allow_writes", "allow_destructive", "allow_network", "allow_agent", "allow_parallel"):
        if not isinstance(policy.get(key), bool):
            raise ValidationError("background model goal payload policy is invalid")
    if not isinstance(policy.get("approvals"), list) or len(policy["approvals"]) > 1000:
        raise ValidationError("background model goal approvals are invalid")
    validate_approval_tokens(policy["approvals"])
    verifier = _exact_object(root.get("verifier"), {"kind", "command", "prompt", "fingerprint"}, "background model goal payload verifier")
    if verifier.get("kind") == "command":
        if not isinstance(verifier.get("command"), list) or not verifier["command"] or verifier.get("prompt") is not None:
            raise ValidationError("background model goal command verifier is invalid")
    elif verifier.get("kind") == "model":
        if not isinstance(verifier.get("prompt"), str) or not verifier["prompt"] or verifier.get("command") is not None:
            raise ValidationError("background model goal model verifier is invalid")
    elif verifier.get("kind") == "command+model":
        if (
            not isinstance(verifier.get("command"), list)
            or not verifier["command"]
            or not isinstance(verifier.get("prompt"), str)
            or not verifier["prompt"]
        ):
            raise ValidationError("background model goal composite verifier is invalid")
    else:
        raise ValidationError("background model goal verifier kind is invalid")
    _validate_sha256(verifier.get("fingerprint"), "background model goal verifier fingerprint")
    config = _exact_object(
        root.get("config"),
        {"check_model", "planner_model", "planner_effort", "name", "write_capable", "max_iterations", "dynamic_interval", "monitor", "monitor_command", "max_workers", "max_items", "max_steps", "planning_timeout_seconds", "planning_output_limit_bytes", "check_timeout_seconds", "check_output_limit_bytes", "reuse_planner_session", "direct_plan", "progressive_plan"},
        "background model goal payload config",
    )
    dynamic_interval = config["dynamic_interval"]
    if dynamic_interval is not None:
        dynamic_interval = _exact_object(
            dynamic_interval,
            {
                "mode",
                "min_delay_minutes",
                "max_delay_minutes",
                "additional_provider_calls_per_selection",
            },
            "background model goal dynamic interval config",
        )
    monitor = config["monitor"]
    monitor_command = config["monitor_command"]
    if monitor is None:
        if monitor_command is not None:
            raise ValidationError("background model goal monitor command requires monitor config")
    else:
        monitor = _exact_object(
            monitor,
            {
                "mode",
                "argv_sha256",
                "argv_count",
                "raw_argv_persisted",
                "output_limit_bytes",
                "event_line_max_chars",
            },
            "background model goal monitor config",
        )
        if (
            not isinstance(monitor_command, list)
            or not monitor_command
            or not all(isinstance(value, str) and value for value in monitor_command)
        ):
            raise ValidationError("background model goal monitor command is invalid")
    prepared = validate_model_goal_loop_request(
        task=root["task"],
        workspace=Path(root["workspace"]),
        policy=_policy_from_payload(root),
        check_command=verifier["command"],
        check_prompt=verifier["prompt"],
        check_model=config["check_model"],
        planner_model=config["planner_model"],
        planner_effort=config["planner_effort"],
        name=config["name"],
        write_capable=config["write_capable"],
        max_iterations=config["max_iterations"],
        dynamic_interval=dynamic_interval is not None,
        min_delay_minutes=(
            dynamic_interval["min_delay_minutes"]
            if dynamic_interval is not None
            else None
        ),
        max_delay_minutes=(
            dynamic_interval["max_delay_minutes"]
            if dynamic_interval is not None
            else None
        ),
        monitor_command=monitor_command,
        max_workers=config["max_workers"],
        max_items=config["max_items"],
        max_steps=config["max_steps"],
        planning_timeout_seconds=config["planning_timeout_seconds"],
        planning_output_limit_bytes=config["planning_output_limit_bytes"],
        check_timeout_seconds=config["check_timeout_seconds"],
        check_output_limit_bytes=config["check_output_limit_bytes"],
        foreground=False,
        reuse_planner_session=config["reuse_planner_session"],
        direct_plan=config["direct_plan"],
        progressive_plan=config["progressive_plan"],
    )
    if prepared["verifier"]["fingerprint"] != verifier["fingerprint"]:
        raise PolicyError("background model goal verifier fingerprint changed during handoff")
    if prepared["dynamic_interval"] != dynamic_interval:
        raise PolicyError("background model goal dynamic interval changed during handoff")
    if prepared["monitor"] != monitor or prepared["monitor_command"] != monitor_command:
        raise PolicyError("background model goal monitor changed during handoff")
    return root


def _encode_payload(payload: Dict) -> bytes:
    data = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(data) > MAX_BACKGROUND_MODEL_GOAL_PAYLOAD_BYTES:
        raise ValidationError("background model goal payload is too large")
    return data


def _policy_from_payload(payload: Dict) -> RuntimePolicy:
    policy = payload["policy"]
    return RuntimePolicy(
        allow_writes=policy["allow_writes"],
        allow_destructive=policy["allow_destructive"],
        allow_network=policy["allow_network"],
        allow_agent=policy["allow_agent"],
        allow_parallel=policy["allow_parallel"],
        approvals=validate_approval_tokens(policy["approvals"]),
    )


def _initial_metadata(
    *, goal_id: str, goal_path: Path, resumed: bool, command: List[str], prepared: Dict,
    policy: RuntimePolicy, stdout_name: str, stderr_name: str,
) -> Dict:
    timestamp = utc_now()
    metadata = {
        "schema": BACKGROUND_MODEL_GOAL_SCHEMA,
        "status": "launching",
        "created_at_utc": timestamp,
        "updated_at_utc": timestamp,
        "heartbeat_at_utc": timestamp,
        "finished_at_utc": None,
        "goal": {"id": goal_id, "artifact": goal_path.name, "resumed": bool(resumed)},
        "worker": {
            "pid": None,
            "process_group_id": None,
            "session_id": None,
            "hostname": _hostname(),
            "detached_session": True,
            "ready": False,
            "argv": [redact_text(str(item)) for item in command],
        },
        "task": {"sha256": prepared["task_sha256"], "chars": len(prepared["task"]), "raw_persisted": False},
        "verifier": {"kind": prepared["verifier"]["kind"], "fingerprint": prepared["verifier"]["fingerprint"]},
        "policy": {
            "allow_writes": bool(policy.allow_writes),
            "allow_destructive": bool(policy.allow_destructive),
            "allow_network": bool(policy.allow_network),
            "allow_agent": bool(policy.allow_agent),
            "allow_parallel": bool(policy.allow_parallel),
            "approval_count": len(policy.approvals),
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
        "goal_status": "not-started",
        "iterations": 0,
        "exit_code": None,
        "error_class": "",
    }
    validate_background_model_goal(metadata)
    return metadata


def _validate_payload_matches_metadata(payload: Dict, metadata: Dict) -> None:
    if payload["goal_id"] != metadata["goal"]["id"]:
        raise PolicyError("background model goal id does not match launch metadata")
    if hashlib.sha256(payload["task"].encode("utf-8")).hexdigest() != metadata["task"]["sha256"]:
        raise PolicyError("background model goal task does not match launch metadata")
    if len(payload["task"]) != metadata["task"]["chars"]:
        raise PolicyError("background model goal task length does not match launch metadata")
    if payload["verifier"]["kind"] != metadata["verifier"]["kind"]:
        raise PolicyError("background model goal verifier kind does not match launch metadata")
    if payload["verifier"]["fingerprint"] != metadata["verifier"]["fingerprint"]:
        raise PolicyError("background model goal verifier does not match launch metadata")
    payload_policy = payload["policy"]
    metadata_policy = metadata["policy"]
    for key in ("allow_writes", "allow_destructive", "allow_network", "allow_agent", "allow_parallel"):
        if payload_policy[key] is not metadata_policy[key]:
            raise PolicyError("background model goal policy does not match launch metadata")
    if len(payload_policy["approvals"]) != metadata_policy["approval_count"]:
        raise PolicyError("background model goal approval count does not match launch metadata")


def _validate_worker_identity(metadata: Dict) -> None:
    if metadata.get("status") != "active":
        raise PolicyError("background model goal worker requires active metadata")
    worker = metadata["worker"]
    if worker.get("pid") != os.getpid() or worker.get("process_group_id") != os.getpid() or worker.get("session_id") != os.getpid():
        raise PolicyError("background model goal worker identity does not match metadata")
    if worker.get("hostname") != _hostname():
        raise PolicyError("background model goal worker host does not match metadata")
    if os.getpgrp() != os.getpid() or os.getsid(0) != os.getpid():
        raise PolicyError("background model goal worker is not session-isolated")


def _wait_for_worker_ready(path: Path, pid: int) -> Dict:
    deadline = time.monotonic() + BACKGROUND_MODEL_GOAL_READY_SECONDS
    while time.monotonic() < deadline:
        metadata = load_background_model_goal(path)
        if metadata["worker"]["ready"]:
            return metadata
        if metadata["status"] in {"finished", "launch-failed"} or not _pid_is_running(pid):
            raise ValidationError("background model goal worker exited before readiness")
        time.sleep(0.02)
    raise ValidationError("background model goal worker did not become ready")


def _heartbeat_loop(path: Path, metadata: Dict, lock: threading.Lock, stop: threading.Event) -> None:
    while not stop.wait(BACKGROUND_MODEL_GOAL_HEARTBEAT_SECONDS):
        try:
            with lock:
                metadata["updated_at_utc"] = utc_now()
                metadata["heartbeat_at_utc"] = metadata["updated_at_utc"]
                _replace_metadata(path, metadata)
        except Exception:
            return


def _write_new_metadata(path: Path, metadata: Dict) -> None:
    validate_background_model_goal(metadata)
    write_new_text_file_no_follow(path, "background model goal metadata", json.dumps(metadata, indent=2, sort_keys=True) + "\n")


def _replace_metadata(path: Path, metadata: Dict) -> None:
    validate_background_model_goal(metadata)
    replace_text_file_no_follow(path, "background model goal metadata", json.dumps(metadata, indent=2, sort_keys=True) + "\n", ".background-model-goal-", sync=False)


def _finish_launch_failure(path: Path, metadata: Dict, error_class: str) -> None:
    timestamp = utc_now()
    metadata["status"] = "launch-failed"
    metadata["updated_at_utc"] = timestamp
    metadata["heartbeat_at_utc"] = timestamp
    metadata["finished_at_utc"] = timestamp
    metadata["goal_status"] = "not-started"
    metadata["exit_code"] = 1
    metadata["error_class"] = redact_text(error_class)
    _replace_metadata(path, metadata)


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
            raise BrokenPipeError("background model goal policy pipe closed")
        offset += written


def _stop_failed_child(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited == pid:
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _heartbeat_age(metadata: Dict) -> float:
    try:
        heartbeat = datetime.fromisoformat(str(metadata["heartbeat_at_utc"]).replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    return max(0.0, (datetime.now(timezone.utc) - heartbeat).total_seconds())


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _hostname() -> str:
    try:
        return os.uname().nodename
    except AttributeError:
        return "unknown"


def _validate_timestamp(value, label: str, *, nullable: bool) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError("%s must use UTC Z notation" % label)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("%s must be an ISO timestamp" % label)


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValidationError("%s is invalid" % label)


def _exact_object(value, fields, label: str) -> Dict:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError("%s must contain exactly: %s" % (label, ", ".join(sorted(fields))))
    return value


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key %s" % key)
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant %s" % value)
