import shlex
from pathlib import Path
from typing import Dict, Optional

from .artifacts import utc_now
from .redaction import redact_text
from .runner import WorkflowRunner
from .security import RuntimePolicy


LAUNCH_MANIFEST_SCHEMA = "conductor.launch_manifest.v1"
PREPARED_RUN_SCHEMA = "conductor.prepared_run.v1"


def prepare_run(
    workflow: Dict,
    workflow_path: Path,
    workspace: Path,
    runs_dir: Path,
    policy: RuntimePolicy,
    run_id: Optional[str] = None,
    max_workers: Optional[int] = None,
) -> Dict:
    runner = WorkflowRunner(
        workflow=workflow,
        workspace=workspace,
        base_run_dir=runs_dir,
        policy=policy,
        dry_run=True,
        run_id=run_id,
        max_workers=max_workers,
    )
    run = runner.execute()
    return record_prepared_run(
        run=run,
        workflow=workflow,
        workflow_path=workflow_path,
        workspace=workspace,
        runs_dir=runs_dir,
        policy=policy,
        max_workers=max_workers,
        effective_max_workers=runner.max_workers,
    )


def record_prepared_run(
    *,
    run,
    workflow: Dict,
    workflow_path: Path,
    workspace: Path,
    runs_dir: Path,
    policy: RuntimePolicy,
    max_workers: Optional[int],
    effective_max_workers: int,
) -> Dict:
    state = run.read_state()
    timestamp = utc_now()
    preflight_status = str(state.get("status") or "")
    command_argv = _resume_command_argv(
        workflow_path=workflow_path,
        workspace=workspace,
        runs_dir=runs_dir,
        run_dir=run.run_dir,
        policy=policy,
        max_workers=max_workers,
    )
    manifest = _launch_manifest(
        workflow=workflow,
        workflow_path=workflow_path,
        workspace=workspace,
        runs_dir=runs_dir,
        run_dir=run.run_dir,
        policy=policy,
        command_argv=command_argv,
        max_workers=effective_max_workers,
        preflight_status=preflight_status,
        timestamp=timestamp,
    )
    run.write_json("launch.json", manifest)

    state["status"] = "needs_resume"
    state["updated_at_utc"] = timestamp
    state.pop("finished_at_utc", None)
    state.pop("duration_ms", None)
    state["prepared_run"] = {
        "schema": PREPARED_RUN_SCHEMA,
        "prepared_at_utc": timestamp,
        "launch_manifest": "launch.json",
        "preflight_status": redact_text(preflight_status),
        "workflow_path": redact_text(str(workflow_path)),
        "workspace": redact_text(str(Path(workspace).resolve())),
        "runs_dir": redact_text(str(runs_dir)),
        "no_process_started": True,
        "approval_values_persisted": False,
    }
    run.save_state_with_standard_append(
        state,
        "05-decision-log.md",
        "| %s | Runtime prepared run for external launch | Wrote `launch.json` and left run `needs_resume` | Start hidden daemon |\n"
        % timestamp,
    )
    return {
        "action": "prepare-run",
        "run_dir": str(run.run_dir),
        "status": state["status"],
        "launch_manifest": str(run.run_dir / "launch.json"),
        "resume_command_argv": command_argv,
        "resume_command": shlex.join(command_argv),
    }


def _resume_command_argv(
    workflow_path: Path,
    workspace: Path,
    runs_dir: Path,
    run_dir: Path,
    policy: RuntimePolicy,
    max_workers: Optional[int],
) -> list:
    argv = [
        "python3",
        "-B",
        "-m",
        "conductor_runtime",
        "run",
        str(workflow_path),
        "--workspace",
        str(workspace),
        "--runs-dir",
        str(runs_dir),
        "--resume",
        str(run_dir),
    ]
    if policy.allow_writes:
        argv.append("--allow-writes")
    if policy.allow_destructive:
        argv.append("--allow-destructive")
    if policy.allow_network:
        argv.append("--allow-network")
    if policy.allow_agent:
        argv.append("--allow-agent")
    if policy.allow_parallel:
        argv.append("--allow-parallel")
    if max_workers is not None:
        argv.extend(["--max-workers", str(max_workers)])
    return argv


def _launch_manifest(
    workflow: Dict,
    workflow_path: Path,
    workspace: Path,
    runs_dir: Path,
    run_dir: Path,
    policy: RuntimePolicy,
    command_argv: list,
    max_workers: int,
    preflight_status: str,
    timestamp: str,
) -> Dict:
    approval_count = len(policy.approvals)
    return {
        "schema": LAUNCH_MANIFEST_SCHEMA,
        "kind": "prepared-run",
        "created_at_utc": timestamp,
        "workflow": redact_text(str(workflow.get("name") or "")),
        "workflow_path": redact_text(str(workflow_path)),
        "workspace": redact_text(str(Path(workspace).resolve())),
        "runs_dir": redact_text(str(runs_dir)),
        "run_dir": redact_text(str(run_dir)),
        "resume_command_argv": [redact_text(str(part)) for part in command_argv],
        "resume_command": redact_text(shlex.join(command_argv)),
        "no_process_started": True,
        "process_model": "operator-owned external launch",
        "preflight": {
            "status": redact_text(preflight_status),
            "dry_run": True,
            "steps_planned": len(workflow.get("steps", [])) if isinstance(workflow.get("steps"), list) else 0,
        },
        "policy": {
            "allow_writes": bool(policy.allow_writes),
            "allow_destructive": bool(policy.allow_destructive),
            "allow_network": bool(policy.allow_network),
            "allow_agent": bool(policy.allow_agent),
            "allow_parallel": bool(policy.allow_parallel),
            "approval_count": approval_count,
            "approval_values_persisted": False,
        },
        "max_workers": max_workers,
        "external_supervisor_contract": {
            "may_run_command_argv": [redact_text(str(part)) for part in command_argv],
            "must_not_assume_daemon": True,
            "must_not_run_without_operator_or_supervisor_approval": True,
            "approval_tokens_must_be_supplied_at_launch_if_required": approval_count > 0,
            "start_in_separate_session_if_terminate_run_is_required": True,
            "expected_runner_metadata": [
                "runner.json",
                "pid",
                "process_group_id",
                "session_id",
                "heartbeat_at_utc",
                "run_status",
            ],
        },
    }
