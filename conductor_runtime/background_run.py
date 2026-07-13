import json
import hashlib
import os
import signal
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional

from .artifacts import RunArtifacts, utc_now
from .errors import ConductorError, PolicyError, ValidationError
from .desktop_notification import RUN_NOTIFICATION_STATUSES, send_run_desktop_notification
from .prepared_run import prepare_run
from .redaction import redact_text
from .runtime_self import runtime_self_command
from .runner import WorkflowRunner, terminate_active_processes
from .security import (
    RuntimePolicy,
    open_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    validate_approval_tokens,
)
from .workflow import MAX_AGENT_WORKERS, load_workflow


BACKGROUND_RUN_SCHEMA = "conductor.background_run.v1"
BACKGROUND_LAUNCH_PAYLOAD_SCHEMA = "conductor.background_launch_payload.v1"
BACKGROUND_RUN_FILENAME = "background.json"
BACKGROUND_STDOUT_LOG = "background-worker.stdout.log"
BACKGROUND_STDERR_LOG = "background-worker.stderr.log"
MAX_BACKGROUND_METADATA_BYTES = 512 * 1024
MAX_BACKGROUND_PAYLOAD_BYTES = 256 * 1024
MAX_BACKGROUND_WAIT_SECONDS = 7 * 24 * 60 * 60
COMPLETION_RECEIPT_KIND = "model-workflow-run-receipt"
BACKGROUND_STATUSES = {"launching", "active", "finished", "launch-failed"}
BACKGROUND_FIELDS = {
    "schema",
    "status",
    "created_at_utc",
    "updated_at_utc",
    "finished_at_utc",
    "run_status",
    "workflow",
    "worker",
    "policy",
    "handoff",
    "logs",
    "exit_code",
    "error_class",
}


@dataclass(frozen=True)
class BackgroundRunResult:
    run_dir: Path
    metadata: Dict
    pid: int


class _BackgroundWorkerTermination(BaseException):
    pass


def start_background_run(
    *,
    workflow: Dict,
    workflow_path: Path,
    workspace: Path,
    runs_dir: Path,
    policy: RuntimePolicy,
    run_id: Optional[str] = None,
    max_workers: Optional[int] = None,
    self_command: Optional[List[str]] = None,
    desktop_notify: bool = False,
) -> BackgroundRunResult:
    if os.name != "posix":
        raise ValidationError("background runs currently require POSIX session isolation")
    initial_payload = _launch_payload(
        workspace=workspace,
        runs_dir=runs_dir,
        policy=policy,
        max_workers=max_workers,
        completion_receipt=None,
    )
    _encode_launch_payload(initial_payload)
    prepared = prepare_run(
        workflow=workflow,
        workflow_path=workflow_path,
        workspace=workspace,
        runs_dir=runs_dir,
        policy=policy,
        run_id=run_id,
        max_workers=max_workers,
    )
    run_dir = Path(prepared["run_dir"])
    return start_prepared_background_run(
        run_dir=run_dir,
        workspace=workspace,
        runs_dir=runs_dir,
        policy=policy,
        max_workers=max_workers,
        self_command=self_command,
        desktop_notify=desktop_notify,
    )


def start_prepared_background_run(
    *,
    run_dir: Path,
    workspace: Path,
    runs_dir: Path,
    policy: RuntimePolicy,
    max_workers: Optional[int] = None,
    self_command: Optional[List[str]] = None,
    completion_receipt: Optional[Dict] = None,
    desktop_notify: bool = False,
) -> BackgroundRunResult:
    if os.name != "posix":
        raise ValidationError("background runs currently require POSIX session isolation")
    run_dir = Path(run_dir).resolve()
    run = RunArtifacts.resume(run_dir)
    launch = _load_prepared_launch(run_dir)
    workflow = load_workflow(run_dir / "workflow.json")
    effective_workers = launch["max_workers"]
    _validate_prepared_launch(
        launch=launch,
        run=run,
        workspace=workspace,
        runs_dir=runs_dir,
        policy=policy,
        max_workers=max_workers,
    )
    payload = _launch_payload(
        workspace=workspace,
        runs_dir=runs_dir,
        policy=policy,
        max_workers=effective_workers,
        completion_receipt=completion_receipt,
    )
    payload_bytes = _encode_launch_payload(payload)
    command = _background_worker_command(run_dir, self_command, desktop_notify)
    metadata = _background_metadata(
        run=run,
        workflow=workflow,
        policy=policy,
        command=command,
        max_workers=effective_workers,
    )
    _write_background_metadata(run, metadata)

    stdout_fd = _open_new_log(run.logs_dir / BACKGROUND_STDOUT_LOG, "background stdout log")
    stderr_fd = None
    read_fd = None
    write_fd = None
    pid = None
    try:
        stderr_fd = _open_new_log(run.logs_dir / BACKGROUND_STDERR_LOG, "background stderr log")
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
        metadata = _finish_launch_failure(run, metadata, exc.__class__.__name__)
        raise ValidationError("failed to launch background worker: %s" % exc.__class__.__name__)
    finally:
        if read_fd is not None:
            os.close(read_fd)
        os.close(stdout_fd)
        if stderr_fd is not None:
            os.close(stderr_fd)

    metadata["status"] = "active"
    metadata["updated_at_utc"] = utc_now()
    metadata["worker"]["pid"] = pid
    metadata["worker"]["process_group_id"] = pid
    metadata["worker"]["session_id"] = pid
    _write_background_metadata(run, metadata)
    _record_background_state(run, metadata)
    try:
        if write_fd is None:
            raise BrokenPipeError("background worker policy pipe is unavailable")
        _write_pipe(write_fd, payload_bytes)
        os.close(write_fd)
        write_fd = None
    except Exception as exc:
        if write_fd is not None:
            os.close(write_fd)
            write_fd = None
        _stop_failed_child(pid)
        _finish_launch_failure(run, metadata, exc.__class__.__name__)
        raise ValidationError("failed to hand off background launch policy: %s" % exc.__class__.__name__)
    return BackgroundRunResult(run_dir=run_dir, metadata=metadata, pid=pid)


def run_background_worker(
    run_dir: Path,
    input_stream: Optional[BinaryIO] = None,
    *,
    desktop_notify: bool = False,
) -> int:
    if not isinstance(desktop_notify, bool):
        raise ValidationError("desktop_notify must be boolean")
    run = RunArtifacts.resume(Path(run_dir))
    metadata = load_background_run(run.run_dir)
    stream = input_stream if input_stream is not None else sys.stdin.buffer
    exit_code = 1
    error_class = ""
    payload = None
    try:
        payload = _read_launch_payload(stream)
        _validate_worker_identity(metadata)
        _validate_payload_matches_metadata(payload, metadata)
        policy = _policy_from_payload(payload)
        workspace = Path(payload["workspace"])
        runs_dir = Path(payload["runs_dir"])
        if runs_dir.resolve() != run.run_dir.parent.resolve():
            raise ValidationError("background payload runs_dir does not match the prepared run")
        workflow = load_workflow(run.run_dir / "workflow.json")
        runner = WorkflowRunner(
            workflow=workflow,
            workspace=workspace,
            base_run_dir=runs_dir,
            policy=policy,
            dry_run=False,
            resume_dir=run.run_dir,
            max_workers=payload["max_workers"],
        )
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def handle_sigterm(signum, frame):
            del frame
            terminate_active_processes()
            raise _BackgroundWorkerTermination("received signal %d" % signum)

        signal.signal(signal.SIGTERM, handle_sigterm)
        try:
            runner.execute()
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
        run_status = str(run.read_state().get("status") or "failed")
        exit_code = 0 if run_status == "completed" else 1
    except _BackgroundWorkerTermination:
        error_class = "BackgroundWorkerTermination"
        print("ERROR: background worker terminated by operator", file=sys.stderr)
    except ConductorError as exc:
        error_class = exc.__class__.__name__
        print("ERROR: %s" % redact_text(str(exc)), file=sys.stderr)
    except Exception as exc:
        error_class = exc.__class__.__name__
        print("ERROR: background worker failed: %s" % exc.__class__.__name__, file=sys.stderr)
    finally:
        try:
            run_status = str(run.read_state().get("status") or "failed")
        except Exception:
            run_status = "failed"
        metadata["status"] = "finished"
        metadata["updated_at_utc"] = utc_now()
        metadata["finished_at_utc"] = metadata["updated_at_utc"]
        metadata["run_status"] = run_status
        metadata["exit_code"] = exit_code
        metadata["error_class"] = redact_text(error_class)
        if payload is not None and payload.get("completion_receipt") is not None:
            try:
                _finalize_completion_receipt(payload["completion_receipt"], run, run_status, error_class)
            except Exception as exc:
                exit_code = 1
                metadata["exit_code"] = 1
                metadata["error_class"] = "CompletionReceiptFinalizationError"
                print(
                    "ERROR: background completion receipt finalization failed: %s" % exc.__class__.__name__,
                    file=sys.stderr,
                )
        if desktop_notify:
            notification_status = run_status if run_status in RUN_NOTIFICATION_STATUSES else "failed"
            if notification_status == "completed" and exit_code != 0:
                notification_status = "failed"
            try:
                receipt = send_run_desktop_notification(run, notification_status)
                print(
                    "Desktop notification: %s (%s)"
                    % (receipt["status"], receipt["backend"])
                )
            except Exception as exc:
                print(
                    "WARNING: desktop notification unavailable: %s" % exc.__class__.__name__,
                    file=sys.stderr,
                )
        _record_background_state(run, metadata)
        _write_background_metadata(run, metadata)
    return exit_code


def _background_worker_command(
    run_dir: Path,
    self_command: Optional[List[str]],
    desktop_notify: bool,
) -> List[str]:
    if not isinstance(desktop_notify, bool):
        raise ValidationError("desktop_notify must be boolean")
    command = list(self_command or runtime_self_command())
    if not command or not all(isinstance(item, str) and item for item in command):
        raise ValidationError("background worker self command is invalid")
    if any(item == "--approve" or item.startswith("--approve=") for item in command):
        raise ValidationError("background worker self command must not persist approval values")
    command.extend(["_background-worker", str(Path(run_dir).resolve())])
    if desktop_notify:
        command.append("--desktop-notify")
    return command


def load_background_run(run_dir: Path) -> Dict:
    path = Path(run_dir) / BACKGROUND_RUN_FILENAME
    raw = read_regular_text_file_no_follow(path, "background run metadata", MAX_BACKGROUND_METADATA_BYTES)
    try:
        data = json.loads(raw, object_pairs_hook=_object_without_duplicates, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("background run metadata is not strict JSON: %s" % exc)
    validate_background_run(data, source=str(path))
    return data


def validate_background_run(metadata: Dict, source: str = "<memory>") -> None:
    root = _exact_object(metadata, BACKGROUND_FIELDS, source)
    if root.get("schema") != BACKGROUND_RUN_SCHEMA:
        raise ValidationError("%s has unsupported background run schema" % source)
    if root.get("status") not in BACKGROUND_STATUSES:
        raise ValidationError("%s has invalid background run status" % source)
    for key in ("created_at_utc", "updated_at_utc"):
        _validate_timestamp(root.get(key), "%s %s" % (source, key), nullable=False)
    _validate_timestamp(root.get("finished_at_utc"), "%s finished_at_utc" % source, nullable=True)
    if not isinstance(root.get("run_status"), str) or not root["run_status"]:
        raise ValidationError("%s run_status is invalid" % source)

    workflow = _exact_object(root.get("workflow"), {"name", "run_id"}, "%s workflow" % source)
    if not all(isinstance(workflow.get(key), str) and workflow[key] for key in workflow):
        raise ValidationError("%s workflow metadata is invalid" % source)
    worker = _exact_object(
        root.get("worker"),
        {"pid", "process_group_id", "session_id", "detached_session", "argv"},
        "%s worker" % source,
    )
    if worker.get("detached_session") is not True:
        raise ValidationError("%s worker must record detached session isolation" % source)
    if not isinstance(worker.get("argv"), list) or not worker["argv"] or not all(
        isinstance(item, str) and item for item in worker["argv"]
    ):
        raise ValidationError("%s worker argv is invalid" % source)
    if any(item == "--approve" or item.startswith("--approve=") for item in worker["argv"]):
        raise ValidationError("%s worker argv must not persist approval values" % source)
    for key in ("pid", "process_group_id", "session_id"):
        value = worker.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
            raise ValidationError("%s worker.%s is invalid" % (source, key))
    if root["status"] == "launching" and any(worker[key] is not None for key in ("pid", "process_group_id", "session_id")):
        raise ValidationError("%s launching worker must not record process identifiers" % source)
    if root["status"] in {"active", "finished"} and any(
        worker[key] is None for key in ("pid", "process_group_id", "session_id")
    ):
        raise ValidationError("%s active or finished worker requires process identifiers" % source)

    policy = _exact_object(
        root.get("policy"),
        {
            "allow_writes",
            "allow_destructive",
            "allow_network",
            "allow_agent",
            "allow_parallel",
            "approval_count",
            "approval_values_persisted",
        },
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
        {
            "prepared_preflight",
            "transport",
            "payload_persisted",
            "daemon_installed",
            "restart_policy",
            "max_workers",
        },
        "%s handoff" % source,
    )
    if handoff.get("prepared_preflight") is not True or handoff.get("payload_persisted") is not False:
        raise ValidationError("%s background handoff contract is invalid" % source)
    if handoff.get("transport") != "anonymous-stdin-pipe" or handoff.get("daemon_installed") is not False:
        raise ValidationError("%s background process contract is invalid" % source)
    if handoff.get("restart_policy") != "never":
        raise ValidationError("%s background restart policy is invalid" % source)
    if not isinstance(handoff.get("max_workers"), int) or isinstance(handoff["max_workers"], bool) or not 1 <= handoff["max_workers"] <= MAX_AGENT_WORKERS:
        raise ValidationError("%s background max_workers is invalid" % source)
    logs = _exact_object(root.get("logs"), {"stdout", "stderr"}, "%s logs" % source)
    if logs != {"stdout": BACKGROUND_STDOUT_LOG, "stderr": BACKGROUND_STDERR_LOG}:
        raise ValidationError("%s background log metadata is invalid" % source)
    if root["status"] in {"launching", "active"}:
        if root.get("finished_at_utc") is not None or root.get("exit_code") is not None:
            raise ValidationError("%s active background run cannot be final" % source)
    else:
        if root.get("finished_at_utc") is None:
            raise ValidationError("%s final background run requires finished_at_utc" % source)
        if not isinstance(root.get("exit_code"), int) or isinstance(root["exit_code"], bool):
            raise ValidationError("%s final background run exit_code is invalid" % source)
    if not isinstance(root.get("error_class"), str) or len(root["error_class"]) > 200:
        raise ValidationError("%s error_class is invalid" % source)


def wait_for_background_run(
    run_dir: Path,
    *,
    timeout_seconds: int,
    poll_interval_seconds: float = 0.25,
) -> Dict:
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 0 <= timeout_seconds <= MAX_BACKGROUND_WAIT_SECONDS:
        raise ValidationError("background wait timeout_seconds must be from 0 to %d" % MAX_BACKGROUND_WAIT_SECONDS)
    if not isinstance(poll_interval_seconds, (int, float)) or isinstance(poll_interval_seconds, bool) or not 0.05 <= poll_interval_seconds <= 60:
        raise ValidationError("background poll interval must be from 0.05 to 60 seconds")
    deadline = time.monotonic() + timeout_seconds
    while True:
        metadata = load_background_run(run_dir)
        if metadata["status"] in {"finished", "launch-failed"}:
            return metadata
        if time.monotonic() >= deadline:
            raise PolicyError("background run did not finish within %d seconds" % timeout_seconds)
        time.sleep(poll_interval_seconds)


def _background_metadata(
    *,
    run: RunArtifacts,
    workflow: Dict,
    policy: RuntimePolicy,
    command: List[str],
    max_workers: int,
) -> Dict:
    timestamp = utc_now()
    metadata = {
        "schema": BACKGROUND_RUN_SCHEMA,
        "status": "launching",
        "created_at_utc": timestamp,
        "updated_at_utc": timestamp,
        "finished_at_utc": None,
        "run_status": "needs_resume",
        "workflow": {"name": redact_text(str(workflow["name"])), "run_id": redact_text(run.run_dir.name)},
        "worker": {
            "pid": None,
            "process_group_id": None,
            "session_id": None,
            "detached_session": True,
            "argv": [redact_text(str(item)) for item in command],
        },
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
            "prepared_preflight": True,
            "transport": "anonymous-stdin-pipe",
            "payload_persisted": False,
            "daemon_installed": False,
            "restart_policy": "never",
            "max_workers": max_workers,
        },
        "logs": {"stdout": BACKGROUND_STDOUT_LOG, "stderr": BACKGROUND_STDERR_LOG},
        "exit_code": None,
        "error_class": "",
    }
    validate_background_run(metadata)
    return metadata


def _launch_payload(
    *,
    workspace: Path,
    runs_dir: Path,
    policy: RuntimePolicy,
    max_workers: Optional[int],
    completion_receipt: Optional[Dict],
) -> Dict:
    if max_workers is not None and (
        not isinstance(max_workers, int)
        or isinstance(max_workers, bool)
        or not 1 <= max_workers <= MAX_AGENT_WORKERS
    ):
        raise ValidationError("max_workers must be an integer from 1 to %d" % MAX_AGENT_WORKERS)
    approvals = sorted(validate_approval_tokens(policy.approvals))
    if len(approvals) > 1000 or any(len(item) > 1000 for item in approvals):
        raise ValidationError("background approval set exceeds supported limits")
    return {
        "schema": BACKGROUND_LAUNCH_PAYLOAD_SCHEMA,
        "workspace": str(Path(workspace).resolve()),
        "runs_dir": str(Path(runs_dir).resolve()),
        "max_workers": max_workers,
        "completion_receipt": _validate_completion_receipt(completion_receipt),
        "policy": {
            "allow_writes": bool(policy.allow_writes),
            "allow_destructive": bool(policy.allow_destructive),
            "allow_network": bool(policy.allow_network),
            "allow_agent": bool(policy.allow_agent),
            "allow_parallel": bool(policy.allow_parallel),
            "approvals": approvals,
        },
    }


def _encode_launch_payload(payload: Dict) -> bytes:
    payload_bytes = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(payload_bytes) > MAX_BACKGROUND_PAYLOAD_BYTES:
        raise ValidationError("background launch payload exceeds %d bytes" % MAX_BACKGROUND_PAYLOAD_BYTES)
    return payload_bytes


def _read_launch_payload(stream: BinaryIO) -> Dict:
    raw = stream.read(MAX_BACKGROUND_PAYLOAD_BYTES + 1)
    if len(raw) > MAX_BACKGROUND_PAYLOAD_BYTES:
        raise ValidationError("background launch payload exceeds %d bytes" % MAX_BACKGROUND_PAYLOAD_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("background launch payload must be UTF-8")
    try:
        payload = json.loads(text, object_pairs_hook=_object_without_duplicates, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("background launch payload is not strict JSON: %s" % exc)
    root = _exact_object(
        payload,
        {"schema", "workspace", "runs_dir", "max_workers", "policy", "completion_receipt"},
        "background payload",
    )
    if root.get("schema") != BACKGROUND_LAUNCH_PAYLOAD_SCHEMA:
        raise ValidationError("background launch payload schema is invalid")
    for key in ("workspace", "runs_dir"):
        if not isinstance(root.get(key), str) or not root[key] or not Path(root[key]).is_absolute():
            raise ValidationError("background launch payload %s is invalid" % key)
    workers = root.get("max_workers")
    if workers is not None and (
        not isinstance(workers, int)
        or isinstance(workers, bool)
        or not 1 <= workers <= MAX_AGENT_WORKERS
    ):
        raise ValidationError("background launch payload max_workers is invalid")
    policy = _exact_object(
        root.get("policy"),
        {"allow_writes", "allow_destructive", "allow_network", "allow_agent", "allow_parallel", "approvals"},
        "background payload policy",
    )
    for key in ("allow_writes", "allow_destructive", "allow_network", "allow_agent", "allow_parallel"):
        if not isinstance(policy.get(key), bool):
            raise ValidationError("background launch payload policy.%s must be boolean" % key)
    if not isinstance(policy.get("approvals"), list) or len(policy["approvals"]) > 1000 or not all(
        isinstance(item, str) and len(item) <= 1000 for item in policy["approvals"]
    ):
        raise ValidationError("background launch payload approvals are invalid")
    validate_approval_tokens(policy["approvals"])
    _validate_completion_receipt(root.get("completion_receipt"))
    return root


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


def _load_prepared_launch(run_dir: Path) -> Dict:
    path = run_dir / "launch.json"
    raw = read_regular_text_file_no_follow(path, "prepared launch manifest", MAX_BACKGROUND_METADATA_BYTES)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("prepared launch manifest is not valid JSON: %s" % exc)
    if not isinstance(data, dict) or data.get("schema") != "conductor.launch_manifest.v1":
        raise ValidationError("prepared launch manifest is invalid")
    workers = data.get("max_workers")
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= MAX_AGENT_WORKERS:
        raise ValidationError("prepared launch manifest max_workers is invalid")
    return data


def _validate_prepared_launch(
    *,
    launch: Dict,
    run: RunArtifacts,
    workspace: Path,
    runs_dir: Path,
    policy: RuntimePolicy,
    max_workers: Optional[int],
) -> None:
    state = run.read_state()
    if state.get("status") != "needs_resume":
        raise PolicyError("background launch requires a prepared run in needs_resume status")
    preflight = launch.get("preflight")
    if not isinstance(preflight, dict) or preflight.get("status") != "planned" or preflight.get("dry_run") is not True:
        raise PolicyError("background launch requires a completed prepared-run preflight")
    if launch.get("no_process_started") is not True:
        raise PolicyError("prepared launch process contract is invalid")
    if Path(str(launch.get("workspace") or "")).resolve() != Path(workspace).resolve():
        raise PolicyError("background workspace does not match prepared launch")
    if Path(str(launch.get("runs_dir") or "")).resolve() != Path(runs_dir).resolve():
        raise PolicyError("background runs_dir does not match prepared launch")
    if Path(str(launch.get("run_dir") or "")).resolve() != run.run_dir.resolve():
        raise PolicyError("background run directory does not match prepared launch")
    launch_policy = launch.get("policy")
    if not isinstance(launch_policy, dict):
        raise ValidationError("prepared launch policy is invalid")
    for key in ("allow_writes", "allow_destructive", "allow_network", "allow_agent", "allow_parallel"):
        if launch_policy.get(key) is not bool(getattr(policy, key)):
            raise PolicyError("background policy does not match prepared launch")
    if launch_policy.get("approval_count") != len(policy.approvals):
        raise PolicyError("background approval count does not match prepared launch")
    if max_workers is not None and launch["max_workers"] > max_workers:
        raise PolicyError("background max_workers does not match prepared launch")


def _validate_completion_receipt(value: Optional[Dict]) -> Optional[Dict]:
    if value is None:
        return None
    receipt = _exact_object(value, {"kind", "path", "sha256"}, "background completion receipt")
    if receipt.get("kind") != COMPLETION_RECEIPT_KIND:
        raise ValidationError("background completion receipt kind is invalid")
    path = receipt.get("path")
    digest = receipt.get("sha256")
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        raise ValidationError("background completion receipt path is invalid")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValidationError("background completion receipt hash is invalid")
    return dict(receipt)


def _finalize_completion_receipt(receipt: Dict, run: RunArtifacts, run_status: str, error_class: str) -> None:
    path = Path(receipt["path"])
    raw = read_regular_text_file_no_follow(
        path,
        "background completion receipt",
        MAX_BACKGROUND_METADATA_BYTES,
    )
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != receipt["sha256"]:
        raise PolicyError("background completion receipt changed after launch")
    from .model_orchestrator import finalize_background_model_workflow_run_receipt

    finalize_background_model_workflow_run_receipt(
        path=path,
        expected_sha256=receipt["sha256"],
        run_dir=run.run_dir,
        run_status=run_status,
        error_class=error_class,
    )


def _validate_worker_identity(metadata: Dict) -> None:
    if metadata.get("status") != "active":
        raise PolicyError("background worker requires active launch metadata")
    worker = metadata["worker"]
    if worker.get("pid") != os.getpid():
        raise PolicyError("background worker pid does not match launch metadata")
    if worker.get("process_group_id") != os.getpid() or worker.get("session_id") != os.getpid():
        raise PolicyError("background worker session metadata does not match its pid")
    try:
        if os.getpgrp() != os.getpid() or os.getsid(0) != os.getpid():
            raise PolicyError("background worker is not isolated in its own process session")
    except AttributeError:
        raise PolicyError("background worker session isolation is unavailable")


def _validate_payload_matches_metadata(payload: Dict, metadata: Dict) -> None:
    payload_policy = payload["policy"]
    metadata_policy = metadata["policy"]
    for key in ("allow_writes", "allow_destructive", "allow_network", "allow_agent", "allow_parallel"):
        if payload_policy[key] is not metadata_policy[key]:
            raise PolicyError("background launch policy does not match recorded preflight policy")
    if len(payload_policy["approvals"]) != metadata_policy["approval_count"]:
        raise PolicyError("background approval count does not match recorded preflight policy")
    if payload["max_workers"] != metadata["handoff"]["max_workers"]:
        raise PolicyError("background max_workers does not match recorded preflight policy")


def _write_background_metadata(run: RunArtifacts, metadata: Dict) -> None:
    validate_background_run(metadata)
    run.write_json(BACKGROUND_RUN_FILENAME, metadata)


def _record_background_state(run: RunArtifacts, metadata: Dict) -> None:
    state = run.read_state()
    state["background_run"] = {
        "schema": BACKGROUND_RUN_SCHEMA,
        "status": metadata["status"],
        "metadata": BACKGROUND_RUN_FILENAME,
        "worker_pid": metadata["worker"]["pid"],
        "updated_at_utc": metadata["updated_at_utc"],
        "approval_values_persisted": False,
    }
    run.save_state(state)


def _finish_launch_failure(run: RunArtifacts, metadata: Dict, error_class: str) -> Dict:
    metadata["status"] = "launch-failed"
    metadata["updated_at_utc"] = utc_now()
    metadata["finished_at_utc"] = metadata["updated_at_utc"]
    metadata["run_status"] = str(run.read_state().get("status") or "needs_resume")
    metadata["exit_code"] = 1
    metadata["error_class"] = redact_text(error_class)
    _record_background_state(run, metadata)
    _write_background_metadata(run, metadata)
    return metadata


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
    except OSError as exc:
        raise ValidationError("failed to create %s: %s" % (label, exc.__class__.__name__))
    finally:
        os.close(parent_fd)


def _write_pipe(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise BrokenPipeError("background worker policy pipe closed")
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
        return


def _validate_timestamp(value, label: str, *, nullable: bool) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError("%s must use UTC Z notation" % label)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("%s must be an ISO timestamp" % label)


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
