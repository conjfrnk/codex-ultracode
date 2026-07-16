import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .artifacts import utc_now, utc_stamp
from .background_goal import (
    background_model_goal_liveness,
    load_background_model_goal,
    start_background_model_goal,
)
from .background_run import load_background_run
from .errors import ConductorError, PolicyError, ValidationError
from .effort_policy import (
    AUTO_REASONING_EFFORT,
    AUTO_REASONING_EFFORTS,  # noqa: F401 - compatibility re-export
    resolve_auto_reasoning_effort,
)
from .goal_loop import MAX_GOAL_CHECK_TIMEOUT_SECONDS, MAX_GOAL_ITERATIONS, load_goal_state
from .model_goal_loop import ModelGoalLoopResult, run_model_goal_loop
from .model_orchestrator import (
    MODEL_WORKFLOW_EXECUTE_APPROVAL,
    ModelWorkflowRunResult,
    default_model_workflow_run_receipt,
    load_model_workflow_run_receipt,
    run_model_workflow,
    validate_model_workflow_execution_request,
)
from .model_planner import (
    MAX_MODEL_WORKFLOW_ITEMS,
    MAX_MODEL_WORKFLOW_STEPS,
    MODEL_WORKFLOW_WRITE_APPROVAL,
    default_model_workflow_output,
    model_workflow_task_metadata,
    validate_model_workflow_plan_limits,
    validate_model_workflow_reasoning_effort,
)
from .paths import default_generated_dir
from .redaction import redact_text
from .runner import DEFAULT_OUTPUT_LIMIT_BYTES
from .security import (
    NATIVE_AGENT_DELEGATION_APPROVAL,
    RuntimePolicy,
    open_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    write_new_text_file_no_follow,
)
from .workflow import MAX_AGENT_WORKERS
from .codex_config import (
    DEFAULT_CODEX_NATIVE_AGENT_THREADS,
    codex_native_agent_funded_token_cap,
    codex_native_agent_token_cap,
    validate_codex_native_agents,
)


AUTO_ORCHESTRATION_RECEIPT_SCHEMA_V1 = "conductor.auto_orchestration_receipt.v1"
AUTO_ORCHESTRATION_RECEIPT_SCHEMA = "conductor.auto_orchestration_receipt.v2"
LEGACY_AUTO_ROUTE_VERSION = "deterministic-v1"
DIRECT_AUTO_ROUTE_VERSION = "deterministic-v2"
PROGRESSIVE_AUTO_ROUTE_VERSION = "deterministic-v3"
NATIVE_AUTO_ROUTE_VERSION = "deterministic-v4"
AUTO_ROUTE_VERSION = "deterministic-v5"
AUTO_ROUTE_VERSIONS = {
    LEGACY_AUTO_ROUTE_VERSION,
    DIRECT_AUTO_ROUTE_VERSION,
    PROGRESSIVE_AUTO_ROUTE_VERSION,
    NATIVE_AUTO_ROUTE_VERSION,
    AUTO_ROUTE_VERSION,
}
AUTO_STRATEGIES_V1 = {"auto", "direct", "progressive", "workflow", "goal"}
AUTO_STRATEGIES = AUTO_STRATEGIES_V1 | {"native"}
AUTO_ROUTES = {
    "direct-workflow",
    "model-workflow",
    "adaptive-direct-goal",
    "adaptive-progressive-goal",
    "adaptive-model-goal",
}
AUTO_EXECUTION_MODES = {"plan-only", "foreground", "background"}
AUTO_VERIFIER_KINDS = {"none", "command", "model", "command+model"}
AUTO_RECEIPT_STATUSES = {
    "routing",
    "planned",
    "running",
    "completed",
    "blocked",
    "failed",
    "paused",
    "stopped",
    "terminated",
    "workflow_blocked",
    "workflow_failed",
    "workflow_paused",
    "workflow_stopped",
    "workflow_terminated",
    "verifier_failed",
    "max_iterations_exhausted",
}
AUTO_REASON_CODES_V1 = {
    "explicit-direct",
    "explicit-progressive",
    "explicit-workflow",
    "explicit-goal",
    "serial-direct-no-verifier",
    "serial-direct-verifier",
    "verifier-progressive",
    "verifier-present",
    "no-verifier",
}
AUTO_REASON_CODES = AUTO_REASON_CODES_V1 | {"explicit-native"}
AUTO_EXECUTION_REASON_CODES = {
    "plan-only-requested",
    "execute-approval-present",
    "execute-approval-absent",
}
AUTO_WORKER_SOURCES_V1 = {
    "direct-strategy",
    "efficient-default",
    "explicit",
    "parallel-envelope",
    "serial-envelope",
}
AUTO_WORKER_SOURCES = AUTO_WORKER_SOURCES_V1 | {"native-envelope"}
AUTO_ARTIFACT_KINDS = {
    "model-workflow-draft",
    "model-workflow-planning-receipt",
    "model-workflow-run-receipt",
    "model-goal",
    "background-model-goal",
}
AUTO_ARTIFACT_CONTENT_STATES = {"terminal", "handoff-mutable"}
AUTO_RECEIPT_FIELDS = {
    "schema",
    "generated_at_utc",
    "updated_at_utc",
    "status",
    "task",
    "decision",
    "policy",
    "limits",
    "artifacts",
    "result",
}
AUTO_TASK_FIELDS = {"sha256", "chars", "raw_persisted"}
AUTO_DECISION_FIELDS = {
    "route_version",
    "strategy_requested",
    "route",
    "reason_code",
    "execution_mode",
    "execution_reason_code",
    "verifier_kind",
    "worker_source",
    "write_capable",
    "task_text_controls_route",
    "task_text_controls_capabilities",
}
AUTO_POLICY_FIELDS_V1 = {
    "allow_writes",
    "allow_destructive",
    "allow_network",
    "allow_agent",
    "allow_parallel",
    "execution_approval_present",
    "write_approval_present",
    "approval_count",
    "approval_values_persisted",
}
AUTO_POLICY_FIELDS = AUTO_POLICY_FIELDS_V1 | {"native_agent_approval_present"}
AUTO_LIMIT_FIELDS_V1 = {
    "max_workers",
    "max_items",
    "max_steps",
    "max_iterations",
    "planning_timeout_seconds",
    "planning_output_limit_bytes",
    "check_timeout_seconds",
    "check_output_limit_bytes",
    "planner_effort",
}
AUTO_LIMIT_FIELDS = AUTO_LIMIT_FIELDS_V1 | {
    "native_agent_max_threads",
    "native_agent_max_total_tokens",
}
AUTO_ARTIFACT_FIELDS = {"kind", "filename", "path_sha256", "content_sha256", "content_state"}
AUTO_RESULT_FIELDS = {
    "status",
    "execution_started",
    "background_handoff",
    "iterations",
    "run_count",
    "error_class",
}
MAX_AUTO_RECEIPT_BYTES = 512 * 1024
MAX_AUTO_ARTIFACT_BYTES = 5 * 1024 * 1024
MAX_AUTO_ARTIFACTS = 8
MAX_AUTO_ITERATIONS = MAX_GOAL_ITERATIONS
MAX_AUTO_CHECK_TIMEOUT_SECONDS = MAX_GOAL_CHECK_TIMEOUT_SECONDS
MAX_AUTO_RECORDS = 500
MAX_AUTO_DIRECTORY_ENTRIES = 5000
AUTO_HANDLE_PREFIX = "auto-"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class AutoOrchestrationResult:
    route: str
    execution_mode: str
    status: str
    receipt: Dict
    receipt_path: Path
    artifact_paths: Tuple[Path, ...]
    run_dir: Optional[Path]
    goal_path: Optional[Path]
    background_pid: Optional[int]
    error_class: str
    error_message: str


def default_auto_orchestration_receipt(workspace: Path) -> Path:
    filename = "%s-auto-%s.json" % (utc_stamp(), secrets.token_hex(4))
    return default_generated_dir(workspace) / "auto" / filename


def run_auto_orchestration(
    *,
    task: str,
    workspace: Path,
    runs_dir: Path,
    goals_dir: Path,
    policy: RuntimePolicy,
    strategy: str = "auto",
    plan_only: bool = False,
    background: bool = False,
    check_command: Optional[List[str]] = None,
    check_prompt: Optional[str] = None,
    output_path: Optional[Path] = None,
    receipt_path: Optional[Path] = None,
    name: Optional[str] = None,
    planner_model: Optional[str] = None,
    planner_effort: str = AUTO_REASONING_EFFORT,
    check_model: Optional[str] = None,
    max_iterations: int = 3,
    max_workers: Optional[int] = None,
    max_items: int = MAX_MODEL_WORKFLOW_ITEMS,
    max_steps: int = 32,
    planning_timeout_seconds: int = 900,
    planning_output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    check_timeout_seconds: int = 120,
    check_output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    run_id: Optional[str] = None,
    goal_id: Optional[str] = None,
    resume_goal: Optional[Path] = None,
    reuse_planner_session: bool = True,
    native_agent_threads: Optional[int] = None,
    desktop_notify: bool = False,
) -> AutoOrchestrationResult:
    if not isinstance(desktop_notify, bool):
        raise ValidationError("desktop_notify must be boolean")
    task_metadata = model_workflow_task_metadata(task)
    decision = decide_auto_orchestration(
        policy=policy,
        strategy=strategy,
        plan_only=plan_only,
        background=background,
        check_command=check_command,
        check_prompt=check_prompt,
        max_workers=max_workers,
        output_path=output_path,
        run_id=run_id,
        goal_id=goal_id,
        resume_goal=resume_goal,
        native_agent_threads=native_agent_threads,
    )
    if desktop_notify and decision["execution_mode"] == "plan-only":
        raise ValidationError("desktop notifications require real automatic execution")
    resolved_workers = decision["max_workers"]
    clean_effort = resolve_auto_reasoning_effort(
        task,
        planner_effort,
        route=decision["route"],
        verifier_kind=decision["verifier_kind"],
    )
    validate_model_workflow_plan_limits(
        resolved_workers,
        max_items,
        max_steps,
        planning_timeout_seconds,
        planning_output_limit_bytes,
    )
    _bounded_int(max_iterations, "max_iterations", 1, MAX_AUTO_ITERATIONS)
    _bounded_int(
        check_timeout_seconds,
        "check_timeout_seconds",
        1,
        MAX_GOAL_CHECK_TIMEOUT_SECONDS,
    )
    _bounded_int(check_output_limit_bytes, "check_output_limit_bytes", 1, 10 * 1024 * 1024)
    validate_model_workflow_execution_request(
        policy=policy,
        dry_run=decision["execution_mode"] == "plan-only",
        write_capable=decision["write_capable"],
        max_workers=resolved_workers,
    )

    receipt = Path(receipt_path).expanduser() if receipt_path is not None else default_auto_orchestration_receipt(workspace)
    output = Path(output_path).expanduser() if output_path is not None else None
    _preflight_receipt_path(receipt)
    if output is not None and receipt == output:
        raise ValidationError("auto receipt path must differ from the workflow output path")
    if output is not None and receipt == default_model_workflow_run_receipt(output):
        raise ValidationError("auto receipt path must differ from the model workflow run receipt")

    limits = {
        "max_workers": resolved_workers,
        "max_items": max_items,
        "max_steps": max_steps,
        "max_iterations": max_iterations,
        "planning_timeout_seconds": planning_timeout_seconds,
        "planning_output_limit_bytes": planning_output_limit_bytes,
        "check_timeout_seconds": check_timeout_seconds,
        "check_output_limit_bytes": check_output_limit_bytes,
        "planner_effort": clean_effort,
        "native_agent_max_threads": decision.get("native_agent_max_threads"),
        "native_agent_max_total_tokens": decision.get(
            "native_agent_max_total_tokens"
        ),
    }
    pending = _build_auto_receipt(
        task_metadata=task_metadata,
        decision=decision,
        policy=policy,
        limits=limits,
        status="routing",
        artifacts=[],
        execution_started=False,
        background_handoff=False,
        iterations=0,
        run_count=0,
        error_class="",
    )
    write_new_text_file_no_follow(
        receipt,
        "auto orchestration receipt",
        _receipt_text(pending),
        sync=True,
    )

    try:
        if decision["route"] in {"direct-workflow", "model-workflow"}:
            result = _run_auto_workflow(
                task=task,
                workspace=workspace,
                runs_dir=runs_dir,
                policy=policy,
                decision=decision,
                output=output,
                name=name,
                planner_model=planner_model,
                planner_effort=clean_effort,
                max_items=max_items,
                max_steps=max_steps,
                planning_timeout_seconds=planning_timeout_seconds,
                planning_output_limit_bytes=planning_output_limit_bytes,
                run_id=run_id,
                direct_plan=decision["route"] == "direct-workflow",
                native_agent_threads=decision.get("native_agent_max_threads"),
                desktop_notify=desktop_notify,
            )
        else:
            result = _run_auto_goal(
                task=task,
                workspace=workspace,
                runs_dir=runs_dir,
                goals_dir=goals_dir,
                policy=policy,
                decision=decision,
                check_command=check_command,
                check_prompt=check_prompt,
                check_model=check_model,
                planner_model=planner_model,
                planner_effort=clean_effort,
                name=name,
                max_iterations=max_iterations,
                max_items=max_items,
                max_steps=max_steps,
                planning_timeout_seconds=planning_timeout_seconds,
                planning_output_limit_bytes=planning_output_limit_bytes,
                check_timeout_seconds=check_timeout_seconds,
                check_output_limit_bytes=check_output_limit_bytes,
                goal_id=goal_id,
                resume_goal=resume_goal,
                reuse_planner_session=reuse_planner_session,
                direct_plan=decision["route"] == "adaptive-direct-goal",
                progressive_plan=decision["route"] == "adaptive-progressive-goal",
                desktop_notify=desktop_notify,
            )
    except ConductorError as exc:
        failed = _build_auto_receipt(
            task_metadata=task_metadata,
            decision=decision,
            policy=policy,
            limits=limits,
            status="failed",
            artifacts=[],
            execution_started=False,
            background_handoff=False,
            iterations=0,
            run_count=0,
            error_class=exc.__class__.__name__,
        )
        _replace_auto_receipt(receipt, failed)
        return AutoOrchestrationResult(
            route=decision["route"],
            execution_mode=decision["execution_mode"],
            status="failed",
            receipt=failed,
            receipt_path=receipt,
            artifact_paths=(),
            run_dir=None,
            goal_path=None,
            background_pid=None,
            error_class=exc.__class__.__name__,
            error_message=redact_text(str(exc)),
        )

    final = _build_auto_receipt(
        task_metadata=task_metadata,
        decision=decision,
        policy=policy,
        limits=limits,
        status=result["status"],
        artifacts=result["artifacts"],
        execution_started=result["execution_started"],
        background_handoff=(
            result["background_pid"] is not None and result["status"] == "running"
        ),
        iterations=result["iterations"],
        run_count=result["run_count"],
        error_class=result["error_class"],
    )
    _replace_auto_receipt(receipt, final)
    return AutoOrchestrationResult(
        route=decision["route"],
        execution_mode=decision["execution_mode"],
        status=result["status"],
        receipt=final,
        receipt_path=receipt,
        artifact_paths=tuple(result["artifact_paths"]),
        run_dir=result["run_dir"],
        goal_path=result["goal_path"],
        background_pid=result["background_pid"],
        error_class=result["error_class"],
        error_message="",
    )


def decide_auto_orchestration(
    *,
    policy: RuntimePolicy,
    strategy: str,
    plan_only: bool,
    background: bool,
    check_command: Optional[List[str]],
    check_prompt: Optional[str],
    max_workers: Optional[int],
    output_path: Optional[Path],
    run_id: Optional[str],
    goal_id: Optional[str],
    resume_goal: Optional[Path],
    native_agent_threads: Optional[int] = None,
) -> Dict:
    if strategy not in AUTO_STRATEGIES:
        raise ValidationError("auto strategy must be one of: %s" % ", ".join(sorted(AUTO_STRATEGIES)))
    if not isinstance(plan_only, bool) or not isinstance(background, bool):
        raise ValidationError("plan_only and background must be boolean")
    verifier_kind = _verifier_kind(check_command, check_prompt)
    if strategy in {"direct", "native"} and max_workers is not None and max_workers != 1:
        raise ValidationError("%s auto strategy requires max_workers 1" % strategy)
    native_config = None
    if strategy == "native":
        native_config = validate_codex_native_agents(
            {
                "max_threads": (
                    DEFAULT_CODEX_NATIVE_AGENT_THREADS
                    if native_agent_threads is None
                    else native_agent_threads
                )
            },
            "native auto strategy",
        )
        if verifier_kind != "none":
            raise ValidationError("native auto strategy does not support an adaptive verifier")
        if policy.allow_writes:
            raise ValidationError("native auto strategy must remain read-only")
        if not policy.allow_parallel:
            raise PolicyError("native auto strategy requires --allow-parallel")
        if not policy.has_approval(NATIVE_AGENT_DELEGATION_APPROVAL):
            raise PolicyError(
                "native auto strategy requires --approve %s"
                % NATIVE_AGENT_DELEGATION_APPROVAL
            )
    elif native_agent_threads is not None:
        raise ValidationError("--native-agent-threads requires --strategy native")

    if max_workers is None:
        if strategy == "native":
            resolved_workers = 1
            worker_source = "native-envelope"
        elif strategy == "direct":
            resolved_workers = 1
            worker_source = "direct-strategy"
        elif strategy in {"workflow", "goal", "progressive"}:
            resolved_workers = 4 if policy.allow_parallel else 1
            worker_source = "parallel-envelope" if policy.allow_parallel else "serial-envelope"
        else:
            resolved_workers = 1
            worker_source = "efficient-default"
    else:
        _bounded_int(max_workers, "max_workers", 1, MAX_AGENT_WORKERS)
        resolved_workers = max_workers
        worker_source = "native-envelope" if strategy == "native" else "explicit"
    if strategy == "progressive" and resolved_workers <= 1:
        raise ValidationError("progressive auto strategy requires max_workers above 1")

    if strategy == "native":
        route = "direct-workflow"
        reason_code = "explicit-native"
    elif strategy == "workflow":
        if verifier_kind != "none":
            raise ValidationError("workflow auto strategy cannot accept an adaptive verifier")
        route = "model-workflow"
        reason_code = "explicit-workflow"
    elif strategy == "goal":
        if verifier_kind == "none":
            raise ValidationError("goal auto strategy requires a verifier")
        route = "adaptive-model-goal"
        reason_code = "explicit-goal"
    elif strategy == "progressive":
        if verifier_kind == "none":
            raise ValidationError("progressive auto strategy requires a verifier")
        route = "adaptive-progressive-goal"
        reason_code = "explicit-progressive"
    elif strategy == "direct":
        route = "direct-workflow" if verifier_kind == "none" else "adaptive-direct-goal"
        reason_code = "explicit-direct"
    elif resolved_workers == 1:
        if verifier_kind == "none":
            route = "direct-workflow"
            reason_code = "serial-direct-no-verifier"
        else:
            route = "adaptive-direct-goal"
            reason_code = "serial-direct-verifier"
    elif verifier_kind == "none":
        route = "model-workflow"
        reason_code = "no-verifier"
    else:
        route = "adaptive-progressive-goal"
        reason_code = "verifier-progressive"

    execute_approved = policy.has_approval(MODEL_WORKFLOW_EXECUTE_APPROVAL)
    if plan_only:
        execution_mode = "plan-only"
        execution_reason = "plan-only-requested"
    elif execute_approved:
        execution_mode = "background" if background else "foreground"
        execution_reason = "execute-approval-present"
    else:
        execution_mode = "plan-only"
        execution_reason = "execute-approval-absent"

    if background and execution_mode != "background":
        raise PolicyError("auto background execution requires --approve %s" % MODEL_WORKFLOW_EXECUTE_APPROVAL)
    adaptive_routes = {
        "adaptive-direct-goal",
        "adaptive-progressive-goal",
        "adaptive-model-goal",
    }
    workflow_routes = {"direct-workflow", "model-workflow"}
    if route in adaptive_routes and execution_mode == "plan-only":
        raise PolicyError("adaptive auto routing requires --approve %s" % MODEL_WORKFLOW_EXECUTE_APPROVAL)
    if route in adaptive_routes and output_path is not None:
        raise ValidationError("--output is only valid for a one-shot auto workflow route")
    if route in adaptive_routes and run_id is not None:
        raise ValidationError("--run-id is only valid for a one-shot auto workflow route")
    if route in workflow_routes and (goal_id is not None or resume_goal is not None):
        raise ValidationError("--goal-id and --resume-goal require the adaptive model-goal auto route")

    return {
        "route_version": AUTO_ROUTE_VERSION,
        "strategy_requested": strategy,
        "route": route,
        "reason_code": reason_code,
        "execution_mode": execution_mode,
        "execution_reason_code": execution_reason,
        "verifier_kind": verifier_kind,
        "worker_source": worker_source,
        "write_capable": bool(policy.allow_writes),
        "task_text_controls_route": False,
        "task_text_controls_capabilities": False,
        "max_workers": resolved_workers,
        "native_agent_max_threads": (
            native_config["max_threads"] if native_config is not None else None
        ),
        "native_agent_max_total_tokens": (
            codex_native_agent_funded_token_cap(native_config)
            if native_config is not None
            else None
        ),
    }


def load_auto_orchestration_receipt(path: Path) -> Dict:
    raw = read_regular_text_file_no_follow(
        path,
        "auto orchestration receipt",
        MAX_AUTO_RECEIPT_BYTES,
    )
    try:
        receipt = json.loads(raw, parse_constant=_reject_json_constant, object_pairs_hook=_object_without_duplicates)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("%s is not strict JSON: %s" % (path, exc))
    validate_auto_orchestration_receipt(receipt, source=str(path))
    return receipt


def auto_orchestration_handle(filename: str) -> str:
    return "%s%s" % (AUTO_HANDLE_PREFIX, _sha256_text(str(filename))[:20])


def collect_auto_orchestrations(
    auto_dir: Path,
    *,
    generated_dir: Path,
    goals_dir: Path,
    runs_dir: Path,
    max_records: int = 50,
) -> List[Dict]:
    _bounded_int(max_records, "max_records", 1, MAX_AUTO_RECORDS)
    root = Path(auto_dir)
    reject_symlink_path(root, "auto orchestration directory")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValidationError("auto orchestration directory must be a directory: %s" % root)
    directory_fd = open_dir_no_follow(root, "auto orchestration directory")
    candidates = []
    try:
        names = os.listdir(directory_fd)
        if len(names) > MAX_AUTO_DIRECTORY_ENTRIES:
            raise ValidationError(
                "auto orchestration directory exceeds %d entries" % MAX_AUTO_DIRECTORY_ENTRIES
            )
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                candidates.append((info.st_mtime, name, root / name))
    finally:
        os.close(directory_fd)
    summaries = []
    for mtime, name, path in sorted(candidates, reverse=True)[:max_records]:
        try:
            summary = summarize_auto_orchestration(
                path,
                generated_dir=generated_dir,
                goals_dir=goals_dir,
                runs_dir=runs_dir,
            )
        except (OSError, ValidationError) as exc:
            summary = _invalid_auto_summary(name, mtime, exc)
        summaries.append(summary)
    return summaries


def summarize_auto_orchestration(
    receipt_path: Path,
    *,
    generated_dir: Path,
    goals_dir: Path,
    runs_dir: Path,
    child_artifact: Optional[Path] = None,
) -> Dict:
    path = Path(receipt_path)
    receipt = load_auto_orchestration_receipt(path)
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ValidationError("failed to stat auto orchestration receipt: %s" % exc.__class__.__name__)
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError("auto orchestration receipt must be a regular file")
    decision = receipt["decision"]
    policy = receipt["policy"]
    limits = receipt["limits"]
    result = receipt["result"]
    child = _resolve_auto_child(
        receipt,
        generated_dir=Path(generated_dir),
        goals_dir=Path(goals_dir),
        runs_dir=Path(runs_dir),
        explicit_child=Path(child_artifact) if child_artifact is not None else None,
    )
    if child["resolution"] == "resolved":
        effective_status = child["status"]
    elif child["resolution"] in {"invalid", "unresolved"}:
        effective_status = child["resolution"]
    else:
        effective_status = receipt["status"]
    liveness = child["liveness"]
    attention = _auto_needs_attention(effective_status, liveness, child["resolution"])
    return {
        "schema": "conductor.auto_orchestration_summary.v1",
        "name": redact_text(path.name),
        "auto_handle": auto_orchestration_handle(path.name),
        "mtime_utc": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": receipt["status"],
        "effective_status": effective_status,
        "route": decision["route"],
        "reason_code": decision["reason_code"],
        "execution_mode": decision["execution_mode"],
        "verifier_kind": decision["verifier_kind"],
        "worker_source": decision["worker_source"],
        "write_capable": decision["write_capable"],
        "max_workers": limits["max_workers"],
        "native_agent_max_threads": limits.get("native_agent_max_threads"),
        "native_agent_max_total_tokens": limits.get(
            "native_agent_max_total_tokens"
        ),
        "max_iterations": limits["max_iterations"],
        "execution_approval_present": policy["execution_approval_present"],
        "write_approval_present": policy["write_approval_present"],
        "native_agent_approval_present": policy.get(
            "native_agent_approval_present",
            False,
        ),
        "task_sha256": receipt["task"]["sha256"],
        "task_chars": receipt["task"]["chars"],
        "artifact_count": len(receipt["artifacts"]),
        "execution_started": result["execution_started"],
        "iterations": result["iterations"],
        "run_count": result["run_count"],
        "error_class": result["error_class"],
        "child": child,
        "liveness": liveness,
        "needs_attention": attention,
        "updated_at_utc": receipt["updated_at_utc"],
    }


def _resolve_auto_child(
    receipt: Dict,
    *,
    generated_dir: Path,
    goals_dir: Path,
    runs_dir: Path,
    explicit_child: Optional[Path],
) -> Dict:
    if receipt["decision"]["execution_mode"] != "background":
        return _child_summary("not-required", "", receipt["status"], "not-applicable", "", "")
    mutable = [
        item for item in receipt["artifacts"] if item.get("content_state") == "handoff-mutable"
    ]
    if not mutable:
        return _child_summary(
            "unresolved",
            "",
            receipt["status"],
            "unavailable",
            "",
            "background handoff has no mutable child artifact",
        )
    if explicit_child is not None:
        try:
            record = _matching_explicit_child(mutable, explicit_child)
            return _load_auto_child(record, explicit_child, runs_dir)
        except (OSError, ValidationError) as exc:
            return _child_summary(
                "invalid",
                "",
                receipt["status"],
                "unavailable",
                "",
                _public_auto_error(exc, "explicit child validation failed"),
            )

    priority = {
        "background-model-goal": 0,
        "model-workflow-run-receipt": 1,
        "model-goal": 2,
    }
    failures = []
    for record in sorted(mutable, key=lambda item: priority.get(str(item.get("kind")), 99)):
        candidate = _default_child_candidate(record, generated_dir, goals_dir)
        if candidate is None or not candidate.exists():
            failures.append("%s not found" % record.get("kind"))
            continue
        try:
            return _load_auto_child(record, candidate, runs_dir)
        except (OSError, ValidationError) as exc:
            return _child_summary(
                "invalid",
                str(record.get("kind") or ""),
                "invalid",
                "unavailable",
                "",
                _public_auto_error(exc, "child validation failed"),
            )
    return _child_summary(
        "unresolved",
        "",
        receipt["status"],
        "unavailable",
        "",
        "; ".join(failures[:3]) or "child artifact was not resolved",
    )


def _matching_explicit_child(records: List[Dict], path: Path) -> Dict:
    reject_symlink_path(path, "auto child artifact")
    if not path.is_file():
        raise ValidationError("auto child artifact must be a regular file")
    path_sha256 = _sha256_text(str(path.resolve()))
    for record in records:
        if record.get("filename") == path.name and record.get("path_sha256") == path_sha256:
            return record
    raise ValidationError("explicit child artifact does not match the auto receipt")


def _default_child_candidate(record: Dict, generated_dir: Path, goals_dir: Path) -> Optional[Path]:
    filename = str(record.get("filename") or "")
    if not filename or Path(filename).name != filename:
        return None
    kind = record.get("kind")
    if kind == "model-workflow-run-receipt":
        return generated_dir / "model-workflows" / filename
    if kind == "background-model-goal":
        return goals_dir / "background-workers" / filename
    if kind == "model-goal":
        return goals_dir / filename
    return None


def _load_auto_child(record: Dict, path: Path, runs_dir: Path) -> Dict:
    reject_symlink_path(path, "auto child artifact")
    if not path.is_file():
        raise ValidationError("auto child artifact must be a regular file")
    if _sha256_text(str(path.resolve())) != record.get("path_sha256"):
        raise ValidationError("auto child artifact path hash does not match the handoff receipt")
    kind = str(record.get("kind") or "")
    if kind == "model-workflow-run-receipt":
        child = load_model_workflow_run_receipt(path)
        status = str(child.get("status") or "unknown")
        updated = str(child.get("generated_at_utc") or "")
        liveness = "finished" if status not in {"running", "planned"} else "unavailable"
        run = child.get("run") if isinstance(child.get("run"), dict) else {}
        run_id = str(run.get("id") or "")
        if run_id:
            background_dir = Path(runs_dir) / run_id
            background_path = background_dir / "background.json"
            if background_path.is_file() and not background_path.is_symlink():
                background = load_background_run(background_dir)
                lifecycle = str(background.get("status") or "")
                if lifecycle in {"launching", "active"}:
                    liveness = "alive"
                    status = "running"
                elif lifecycle == "launch-failed":
                    liveness = "finished"
                    status = "failed"
                elif lifecycle == "finished":
                    liveness = "finished"
                    status = str(background.get("run_status") or status)
                updated = str(background.get("updated_at_utc") or updated)
        return _child_summary("resolved", kind, status, liveness, updated, "")
    if kind == "background-model-goal":
        child = load_background_model_goal(path)
        lifecycle = str(child.get("status") or "")
        liveness = background_model_goal_liveness(child)
        if lifecycle in {"launching", "active"}:
            status = "running"
        elif lifecycle == "launch-failed":
            status = "failed"
        else:
            status = str(child.get("goal_status") or "unknown")
        return _child_summary(
            "resolved",
            kind,
            status,
            liveness,
            str(child.get("updated_at_utc") or ""),
            "",
        )
    if kind == "model-goal":
        child = load_goal_state(path)
        return _child_summary(
            "resolved",
            kind,
            str(child.get("status") or "unknown"),
            "unavailable",
            str(child.get("updated_at_utc") or ""),
            "",
        )
    raise ValidationError("unsupported auto child artifact kind")


def _child_summary(
    resolution: str,
    kind: str,
    status: str,
    liveness: str,
    updated_at_utc: str,
    error: str,
) -> Dict:
    return {
        "resolution": resolution,
        "kind": kind,
        "status": status,
        "liveness": liveness,
        "updated_at_utc": updated_at_utc,
        "error": error,
    }


def _auto_needs_attention(status: str, liveness: str, resolution: str) -> bool:
    if resolution in {"invalid", "unresolved"}:
        return True
    if liveness in {"dead", "stale", "remote", "unavailable"} and status == "running":
        return True
    return status not in {"completed", "planned", "running"}


def _invalid_auto_summary(name: str, mtime: float, exc: Exception) -> Dict:
    return {
        "schema": "conductor.auto_orchestration_summary.v1",
        "name": redact_text(name),
        "auto_handle": auto_orchestration_handle(name),
        "mtime_utc": datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": "invalid",
        "effective_status": "invalid",
        "route": "unknown",
        "reason_code": "unknown",
        "execution_mode": "unknown",
        "verifier_kind": "unknown",
        "worker_source": "unknown",
        "write_capable": False,
        "max_workers": 0,
        "native_agent_max_threads": None,
        "native_agent_max_total_tokens": None,
        "max_iterations": 0,
        "execution_approval_present": False,
        "write_approval_present": False,
        "native_agent_approval_present": False,
        "task_sha256": "",
        "task_chars": 0,
        "artifact_count": 0,
        "execution_started": False,
        "iterations": 0,
        "run_count": 0,
        "error_class": exc.__class__.__name__,
        "child": _child_summary(
            "invalid",
            "",
            "invalid",
            "unavailable",
            "",
            _public_auto_error(exc, "auto receipt validation failed"),
        ),
        "liveness": "unavailable",
        "needs_attention": True,
        "updated_at_utc": "",
    }


def _public_auto_error(exc: Exception, prefix: str) -> str:
    text = redact_text(str(exc)).lower()
    if "path hash" in text:
        detail = "path hash mismatch"
    elif "strict json" in text or "not valid json" in text:
        detail = "invalid strict JSON"
    elif "symlink" in text:
        detail = "symlink rejected"
    elif "does not match the auto receipt" in text:
        detail = "receipt binding mismatch"
    else:
        detail = exc.__class__.__name__
    return "%s: %s" % (prefix, detail)


def validate_auto_orchestration_receipt(receipt: Dict, source: str = "<memory>") -> None:
    root = _exact_object(receipt, AUTO_RECEIPT_FIELDS, source)
    receipt_schema = root.get("schema")
    if receipt_schema not in {
        AUTO_ORCHESTRATION_RECEIPT_SCHEMA_V1,
        AUTO_ORCHESTRATION_RECEIPT_SCHEMA,
    }:
        raise ValidationError("%s has an unsupported auto orchestration receipt schema" % source)
    current_receipt = receipt_schema == AUTO_ORCHESTRATION_RECEIPT_SCHEMA
    _timestamp(root.get("generated_at_utc"), "%s generated_at_utc" % source)
    _timestamp(root.get("updated_at_utc"), "%s updated_at_utc" % source)
    status = _enum(root.get("status"), AUTO_RECEIPT_STATUSES, "%s status" % source)

    task = _exact_object(root.get("task"), AUTO_TASK_FIELDS, "%s task" % source)
    _sha256(task.get("sha256"), "%s task sha256" % source)
    _bounded_int(task.get("chars"), "%s task chars" % source, 1, 65536)
    if task.get("raw_persisted") is not False:
        raise ValidationError("%s task raw_persisted must be false" % source)

    decision = _exact_object(root.get("decision"), AUTO_DECISION_FIELDS, "%s decision" % source)
    route_version = decision.get("route_version")
    if route_version not in AUTO_ROUTE_VERSIONS:
        raise ValidationError("%s decision route_version is invalid" % source)
    strategy = _enum(
        decision.get("strategy_requested"),
        AUTO_STRATEGIES if current_receipt else AUTO_STRATEGIES_V1,
        "%s strategy" % source,
    )
    route = _enum(decision.get("route"), AUTO_ROUTES, "%s route" % source)
    reason_code = _enum(
        decision.get("reason_code"),
        AUTO_REASON_CODES if current_receipt else AUTO_REASON_CODES_V1,
        "%s reason_code" % source,
    )
    execution_mode = _enum(
        decision.get("execution_mode"), AUTO_EXECUTION_MODES, "%s execution_mode" % source
    )
    _enum(
        decision.get("execution_reason_code"),
        AUTO_EXECUTION_REASON_CODES,
        "%s execution_reason_code" % source,
    )
    verifier_kind = _enum(
        decision.get("verifier_kind"), AUTO_VERIFIER_KINDS, "%s verifier_kind" % source
    )
    worker_source = _enum(
        decision.get("worker_source"),
        AUTO_WORKER_SOURCES if current_receipt else AUTO_WORKER_SOURCES_V1,
        "%s worker_source" % source,
    )
    for key in ("write_capable", "task_text_controls_route", "task_text_controls_capabilities"):
        _boolean(decision.get(key), "%s decision %s" % (source, key))
    if decision["task_text_controls_route"] or decision["task_text_controls_capabilities"]:
        raise ValidationError("%s task text must not control routing or capabilities" % source)
    if not current_receipt and route_version == AUTO_ROUTE_VERSION:
        raise ValidationError("%s v1 receipt cannot use the current routing version" % source)
    if route_version == LEGACY_AUTO_ROUTE_VERSION and (
        strategy in {"direct", "progressive"}
        or route in {"direct-workflow", "adaptive-direct-goal", "adaptive-progressive-goal"}
    ):
        raise ValidationError("%s legacy routing version cannot record direct orchestration" % source)
    if route_version == DIRECT_AUTO_ROUTE_VERSION and (
        strategy == "progressive" or route == "adaptive-progressive-goal"
    ):
        raise ValidationError("%s deterministic-v2 routing cannot record progressive orchestration" % source)
    if verifier_kind == "command+model" and route_version not in {
        PROGRESSIVE_AUTO_ROUTE_VERSION,
        NATIVE_AUTO_ROUTE_VERSION,
        AUTO_ROUTE_VERSION,
    }:
        raise ValidationError("%s composite verification requires the current routing version" % source)
    expected_reasons = {
        "direct-workflow": {
            "explicit-direct",
            "explicit-native",
            "serial-direct-no-verifier",
        },
        "model-workflow": {"explicit-workflow", "no-verifier"},
        "adaptive-direct-goal": {"explicit-direct", "serial-direct-verifier"},
        "adaptive-progressive-goal": {"explicit-progressive", "verifier-progressive"},
        "adaptive-model-goal": {"explicit-goal", "verifier-present"},
    }
    if reason_code not in expected_reasons[route]:
        raise ValidationError("%s route and reason_code are inconsistent" % source)
    strategy_routes = {
        "direct": {"direct-workflow", "adaptive-direct-goal"},
        "progressive": {"adaptive-progressive-goal"},
        "workflow": {"model-workflow"},
        "goal": {"adaptive-model-goal"},
        "native": {"direct-workflow"},
    }
    if strategy in strategy_routes and route not in strategy_routes[strategy]:
        raise ValidationError("%s strategy and route are inconsistent" % source)
    if route in {"direct-workflow", "model-workflow"} and verifier_kind != "none":
        raise ValidationError("%s one-shot workflow route must not record a verifier" % source)
    adaptive_routes = {
        "adaptive-direct-goal",
        "adaptive-progressive-goal",
        "adaptive-model-goal",
    }
    if route in adaptive_routes and verifier_kind == "none":
        raise ValidationError("%s adaptive route must record a verifier" % source)
    if route in adaptive_routes and execution_mode == "plan-only":
        raise ValidationError("%s adaptive route cannot be plan-only" % source)

    policy_fields = AUTO_POLICY_FIELDS if current_receipt else AUTO_POLICY_FIELDS_V1
    policy = _exact_object(root.get("policy"), policy_fields, "%s policy" % source)
    boolean_policy_fields = [
        "allow_writes",
        "allow_destructive",
        "allow_network",
        "allow_agent",
        "allow_parallel",
        "execution_approval_present",
        "write_approval_present",
        "approval_values_persisted",
    ]
    if current_receipt:
        boolean_policy_fields.append("native_agent_approval_present")
    for key in boolean_policy_fields:
        _boolean(policy.get(key), "%s policy %s" % (source, key))
    if policy["approval_values_persisted"]:
        raise ValidationError("%s approval values must not be persisted" % source)
    _bounded_int(policy.get("approval_count"), "%s approval_count" % source, 0, 10000)
    if decision["write_capable"] != policy["allow_writes"]:
        raise ValidationError("%s write-capable decision must match the policy envelope" % source)
    if execution_mode != "plan-only" and not policy["execution_approval_present"]:
        raise ValidationError("%s execution route requires recorded execution approval" % source)

    limit_fields = AUTO_LIMIT_FIELDS if current_receipt else AUTO_LIMIT_FIELDS_V1
    limits = _exact_object(root.get("limits"), limit_fields, "%s limits" % source)
    _bounded_int(limits.get("max_workers"), "%s max_workers" % source, 1, MAX_AGENT_WORKERS)
    if route in {"direct-workflow", "adaptive-direct-goal"} and limits.get("max_workers") != 1:
        raise ValidationError("%s direct orchestration route requires one worker" % source)
    if route == "adaptive-progressive-goal" and limits.get("max_workers") <= 1:
        raise ValidationError("%s progressive orchestration route requires multiple workers" % source)
    if route_version in {
        DIRECT_AUTO_ROUTE_VERSION,
        PROGRESSIVE_AUTO_ROUTE_VERSION,
        NATIVE_AUTO_ROUTE_VERSION,
        AUTO_ROUTE_VERSION,
    } and strategy == "auto":
        if limits["max_workers"] == 1:
            expected_route = "direct-workflow" if verifier_kind == "none" else "adaptive-direct-goal"
        else:
            if verifier_kind == "none":
                expected_route = "model-workflow"
            elif route_version == DIRECT_AUTO_ROUTE_VERSION:
                expected_route = "adaptive-model-goal"
            else:
                expected_route = "adaptive-progressive-goal"
        if route != expected_route:
            raise ValidationError("%s automatic route does not match its worker envelope" % source)
    native_agent_max_threads = limits.get("native_agent_max_threads")
    native_agent_max_total_tokens = limits.get("native_agent_max_total_tokens")
    if strategy == "native":
        _bounded_int(
            native_agent_max_threads,
            "%s native_agent_max_threads" % source,
            1,
            6,
        )
        native_config = {"max_threads": native_agent_max_threads}
        expected_native_token_cap = (
            codex_native_agent_token_cap(native_config)
            if route_version == NATIVE_AUTO_ROUTE_VERSION
            else codex_native_agent_funded_token_cap(native_config)
        )
        if native_agent_max_total_tokens != expected_native_token_cap:
            raise ValidationError(
                "%s native_agent_max_total_tokens must match the bounded native budget"
                % source
            )
        if (
            route_version not in {NATIVE_AUTO_ROUTE_VERSION, AUTO_ROUTE_VERSION}
            or route != "direct-workflow"
            or reason_code != "explicit-native"
            or verifier_kind != "none"
            or worker_source != "native-envelope"
            or limits["max_workers"] != 1
            or decision["write_capable"]
            or not policy["allow_agent"]
            or not policy["allow_parallel"]
            or not policy["native_agent_approval_present"]
        ):
            raise ValidationError("%s native orchestration envelope is inconsistent" % source)
    elif current_receipt and (
        native_agent_max_threads is not None
        or native_agent_max_total_tokens is not None
        or reason_code == "explicit-native"
        or worker_source == "native-envelope"
    ):
        raise ValidationError("%s non-native orchestration must not set native-agent authority" % source)
    _bounded_int(limits.get("max_items"), "%s max_items" % source, 1, MAX_MODEL_WORKFLOW_ITEMS)
    _bounded_int(limits.get("max_steps"), "%s max_steps" % source, 1, MAX_MODEL_WORKFLOW_STEPS)
    _bounded_int(limits.get("max_iterations"), "%s max_iterations" % source, 1, MAX_AUTO_ITERATIONS)
    _bounded_int(
        limits.get("planning_timeout_seconds"), "%s planning_timeout_seconds" % source, 1, 3600
    )
    _bounded_int(
        limits.get("planning_output_limit_bytes"),
        "%s planning_output_limit_bytes" % source,
        1,
        10 * 1024 * 1024,
    )
    _bounded_int(
        limits.get("check_timeout_seconds"),
        "%s check_timeout_seconds" % source,
        1,
        MAX_GOAL_CHECK_TIMEOUT_SECONDS,
    )
    _bounded_int(
        limits.get("check_output_limit_bytes"),
        "%s check_output_limit_bytes" % source,
        1,
        10 * 1024 * 1024,
    )
    validate_model_workflow_reasoning_effort(limits.get("planner_effort"))

    artifacts = root.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) > MAX_AUTO_ARTIFACTS:
        raise ValidationError("%s artifacts must be a list with at most %d items" % (source, MAX_AUTO_ARTIFACTS))
    for index, item in enumerate(artifacts):
        artifact = _exact_object(item, AUTO_ARTIFACT_FIELDS, "%s artifact %d" % (source, index))
        _enum(artifact.get("kind"), AUTO_ARTIFACT_KINDS, "%s artifact kind" % source)
        _non_empty_string(artifact.get("filename"), "%s artifact filename" % source, 255)
        _sha256(artifact.get("path_sha256"), "%s artifact path_sha256" % source)
        _sha256(artifact.get("content_sha256"), "%s artifact content_sha256" % source)
        content_state = _enum(
            artifact.get("content_state"), AUTO_ARTIFACT_CONTENT_STATES, "%s artifact content_state" % source
        )
        if content_state == "handoff-mutable" and execution_mode != "background":
            raise ValidationError("%s mutable artifacts require background execution" % source)

    result = _exact_object(root.get("result"), AUTO_RESULT_FIELDS, "%s result" % source)
    if result.get("status") != status:
        raise ValidationError("%s result status must match receipt status" % source)
    _boolean(result.get("execution_started"), "%s execution_started" % source)
    _boolean(result.get("background_handoff"), "%s background_handoff" % source)
    _bounded_int(result.get("iterations"), "%s iterations" % source, 0, MAX_AUTO_ITERATIONS)
    _bounded_int(result.get("run_count"), "%s run_count" % source, 0, MAX_AUTO_ITERATIONS)
    _non_empty_or_empty_string(result.get("error_class"), "%s error_class" % source, 200)
    if result["background_handoff"] != (execution_mode == "background" and status == "running"):
        raise ValidationError("%s background handoff metadata is inconsistent" % source)
    if execution_mode == "plan-only" and result["execution_started"]:
        raise ValidationError("%s plan-only receipt must not record execution" % source)
    if status == "routing" and (artifacts or result["execution_started"] or result["run_count"]):
        raise ValidationError("%s routing receipt must not record completed work" % source)


def _run_auto_workflow(
    *,
    task: str,
    workspace: Path,
    runs_dir: Path,
    policy: RuntimePolicy,
    decision: Dict,
    output: Optional[Path],
    name: Optional[str],
    planner_model: Optional[str],
    planner_effort: str,
    max_items: int,
    max_steps: int,
    planning_timeout_seconds: int,
    planning_output_limit_bytes: int,
    run_id: Optional[str],
    direct_plan: bool,
    native_agent_threads: Optional[int],
    desktop_notify: bool,
) -> Dict:
    workflow_output = output or default_model_workflow_output(workspace, name)
    result: ModelWorkflowRunResult = run_model_workflow(
        task=task,
        workspace=workspace,
        output_path=workflow_output,
        planning_receipt_path=None,
        run_receipt_path=None,
        runs_dir=runs_dir,
        policy=policy,
        name=name,
        model=planner_model,
        planner_effort=planner_effort,
        write_capable=decision["write_capable"],
        max_workers=decision["max_workers"],
        max_items=max_items,
        max_steps=max_steps,
        planning_timeout_seconds=planning_timeout_seconds,
        planning_output_limit_bytes=planning_output_limit_bytes,
        run_id=run_id,
        dry_run=decision["execution_mode"] == "plan-only",
        background=decision["execution_mode"] == "background",
        direct_plan=direct_plan,
        direct_native_agent_threads=native_agent_threads,
        desktop_notify=desktop_notify,
    )
    artifact_paths: List[Path] = []
    artifacts: List[Dict] = []
    if result.draft is not None:
        _append_artifact(
            artifacts,
            artifact_paths,
            result.draft.output_path,
            "model-workflow-draft",
            "terminal",
        )
        _append_artifact(
            artifacts,
            artifact_paths,
            result.draft.receipt_path,
            "model-workflow-planning-receipt",
            "terminal",
        )
    _append_artifact(
        artifacts,
        artifact_paths,
        result.receipt_path,
        "model-workflow-run-receipt",
        "handoff-mutable" if decision["execution_mode"] == "background" and result.status == "running" else "terminal",
    )
    execution = result.receipt.get("execution") if isinstance(result.receipt.get("execution"), dict) else {}
    return {
        "status": result.status,
        "artifacts": artifacts,
        "artifact_paths": artifact_paths,
        "execution_started": bool(execution.get("started")),
        "iterations": 0,
        "run_count": 1 if result.run_dir is not None else 0,
        "run_dir": result.run_dir,
        "goal_path": None,
        "background_pid": result.background_pid,
        "error_class": result.error_class,
    }


def _run_auto_goal(
    *,
    task: str,
    workspace: Path,
    runs_dir: Path,
    goals_dir: Path,
    policy: RuntimePolicy,
    decision: Dict,
    check_command: Optional[List[str]],
    check_prompt: Optional[str],
    check_model: Optional[str],
    planner_model: Optional[str],
    planner_effort: str,
    name: Optional[str],
    max_iterations: int,
    max_items: int,
    max_steps: int,
    planning_timeout_seconds: int,
    planning_output_limit_bytes: int,
    check_timeout_seconds: int,
    check_output_limit_bytes: int,
    goal_id: Optional[str],
    resume_goal: Optional[Path],
    reuse_planner_session: bool,
    direct_plan: bool,
    progressive_plan: bool,
    desktop_notify: bool,
) -> Dict:
    artifact_paths: List[Path] = []
    artifacts: List[Dict] = []
    if decision["execution_mode"] == "background":
        background = start_background_model_goal(
            task=task,
            workspace=workspace,
            runs_dir=runs_dir,
            goals_dir=goals_dir,
            policy=policy,
            check_command=check_command,
            check_prompt=check_prompt,
            check_model=check_model,
            planner_model=planner_model,
            planner_effort=planner_effort,
            name=name,
            write_capable=decision["write_capable"],
            max_iterations=max_iterations,
            max_workers=decision["max_workers"],
            max_items=max_items,
            max_steps=max_steps,
            planning_timeout_seconds=planning_timeout_seconds,
            planning_output_limit_bytes=planning_output_limit_bytes,
            check_timeout_seconds=check_timeout_seconds,
            check_output_limit_bytes=check_output_limit_bytes,
            goal_id=goal_id,
            resume_goal=resume_goal,
            reuse_planner_session=reuse_planner_session,
            direct_plan=direct_plan,
            progressive_plan=progressive_plan,
            desktop_notify=desktop_notify,
        )
        if background.goal_path.is_file():
            _append_artifact(
                artifacts,
                artifact_paths,
                background.goal_path,
                "model-goal",
                "handoff-mutable",
            )
        _append_artifact(
            artifacts,
            artifact_paths,
            background.metadata_path,
            "background-model-goal",
            "handoff-mutable",
        )
        lifecycle_status = str(background.metadata.get("status") or "")
        if lifecycle_status in {"launching", "active"}:
            auto_status = "running"
        elif lifecycle_status == "finished":
            auto_status = str(background.metadata.get("goal_status") or "failed")
        else:
            auto_status = "failed"
        return {
            "status": auto_status,
            "artifacts": artifacts,
            "artifact_paths": artifact_paths,
            "execution_started": True,
            "iterations": int(background.metadata.get("iterations") or 0),
            "run_count": 0,
            "run_dir": None,
            "goal_path": background.goal_path,
            "background_pid": background.pid,
            "error_class": "",
        }

    result: ModelGoalLoopResult = run_model_goal_loop(
        task=task,
        workspace=workspace,
        runs_dir=runs_dir,
        goals_dir=goals_dir,
        policy=policy,
        check_command=check_command,
        check_prompt=check_prompt,
        check_model=check_model,
        planner_model=planner_model,
        planner_effort=planner_effort,
        name=name,
        write_capable=decision["write_capable"],
        max_iterations=max_iterations,
        max_workers=decision["max_workers"],
        max_items=max_items,
        max_steps=max_steps,
        planning_timeout_seconds=planning_timeout_seconds,
        planning_output_limit_bytes=planning_output_limit_bytes,
        check_timeout_seconds=check_timeout_seconds,
        check_output_limit_bytes=check_output_limit_bytes,
        goal_id=goal_id,
        resume_goal=resume_goal,
        reuse_planner_session=reuse_planner_session,
        direct_plan=direct_plan,
        progressive_plan=progressive_plan,
    )
    _append_artifact(artifacts, artifact_paths, result.goal_path, "model-goal", "terminal")
    return {
        "status": result.status,
        "artifacts": artifacts,
        "artifact_paths": artifact_paths,
        "execution_started": True,
        "iterations": result.iterations,
        "run_count": len(result.run_dirs),
        "run_dir": result.run_dirs[-1] if result.run_dirs else None,
        "goal_path": result.goal_path,
        "background_pid": None,
        "error_class": "" if result.status == "completed" else result.status,
    }


def _build_auto_receipt(
    *,
    task_metadata: Dict,
    decision: Dict,
    policy: RuntimePolicy,
    limits: Dict,
    status: str,
    artifacts: List[Dict],
    execution_started: bool,
    background_handoff: bool,
    iterations: int,
    run_count: int,
    error_class: str,
) -> Dict:
    now = utc_now()
    public_decision = {key: value for key, value in decision.items() if key in AUTO_DECISION_FIELDS}
    receipt = {
        "schema": AUTO_ORCHESTRATION_RECEIPT_SCHEMA,
        "generated_at_utc": now,
        "updated_at_utc": now,
        "status": status,
        "task": dict(task_metadata),
        "decision": public_decision,
        "policy": {
            "allow_writes": bool(policy.allow_writes),
            "allow_destructive": bool(policy.allow_destructive),
            "allow_network": bool(policy.allow_network),
            "allow_agent": bool(policy.allow_agent),
            "allow_parallel": bool(policy.allow_parallel),
            "execution_approval_present": policy.has_approval(MODEL_WORKFLOW_EXECUTE_APPROVAL),
            "write_approval_present": policy.has_approval(MODEL_WORKFLOW_WRITE_APPROVAL),
            "native_agent_approval_present": policy.has_approval(
                NATIVE_AGENT_DELEGATION_APPROVAL
            ),
            "approval_count": len(policy.approvals),
            "approval_values_persisted": False,
        },
        "limits": dict(limits),
        "artifacts": list(artifacts),
        "result": {
            "status": status,
            "execution_started": bool(execution_started),
            "background_handoff": bool(background_handoff),
            "iterations": iterations,
            "run_count": run_count,
            "error_class": redact_text(error_class),
        },
    }
    validate_auto_orchestration_receipt(receipt)
    return receipt


def _verifier_kind(check_command: Optional[List[str]], check_prompt: Optional[str]) -> str:
    if check_command is not None:
        if not isinstance(check_command, list) or not check_command:
            raise ValidationError("auto command verifier must be a non-empty argv list")
    if check_prompt is not None:
        if not isinstance(check_prompt, str) or not check_prompt.strip():
            raise ValidationError("auto model verifier prompt must be non-empty")
    if check_command is not None and check_prompt is not None:
        return "command+model"
    if check_command is not None:
        return "command"
    if check_prompt is not None:
        return "model"
    return "none"


def _append_artifact(
    records: List[Dict],
    paths: List[Path],
    path: Path,
    kind: str,
    content_state: str,
) -> None:
    if len(records) >= MAX_AUTO_ARTIFACTS:
        raise ValidationError("auto orchestration produced too many receipt artifacts")
    artifact_path = Path(path)
    content = read_regular_text_file_no_follow(
        artifact_path,
        "auto orchestration artifact",
        MAX_AUTO_ARTIFACT_BYTES,
    )
    records.append(
        {
            "kind": kind,
            "filename": artifact_path.name,
            "path_sha256": _sha256_text(str(artifact_path.resolve())),
            "content_sha256": _sha256_text(content),
            "content_state": content_state,
        }
    )
    paths.append(artifact_path)


def _preflight_receipt_path(path: Path) -> None:
    reject_symlink_path(path, "auto orchestration receipt")
    if path.exists():
        raise ValidationError("auto orchestration receipt already exists: %s" % path)


def _replace_auto_receipt(path: Path, receipt: Dict) -> None:
    replace_text_file_no_follow(
        path,
        "auto orchestration receipt",
        _receipt_text(receipt),
        ".auto-receipt-",
    )


def _receipt_text(receipt: Dict) -> str:
    return json.dumps(receipt, indent=2, sort_keys=True) + "\n"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _exact_object(value, fields, label: str) -> Dict:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % label)
    unknown = set(value) - set(fields)
    missing = set(fields) - set(value)
    if unknown or missing:
        raise ValidationError(
            "%s fields are invalid (missing=%s, unknown=%s)"
            % (label, sorted(missing), sorted(unknown))
        )
    return value


def _enum(value, allowed, label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError("%s is invalid" % label)
    return value


def _boolean(value, label: str) -> None:
    if not isinstance(value, bool):
        raise ValidationError("%s must be boolean" % label)


def _bounded_int(value, label: str, minimum: int, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or value > maximum:
        raise ValidationError("%s must be an integer from %d to %d" % (label, minimum, maximum))


def _sha256(value, label: str) -> None:
    if not isinstance(value, str) or not _SHA256_PATTERN.match(value):
        raise ValidationError("%s must be a lowercase SHA-256 digest" % label)


def _timestamp(value, label: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError("%s must be a UTC timestamp" % label)
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValidationError("%s must be a UTC timestamp" % label) from exc


def _non_empty_string(value, label: str, max_chars: int) -> None:
    if not isinstance(value, str) or not value or len(value) > max_chars:
        raise ValidationError("%s must be a non-empty string of at most %d characters" % (label, max_chars))


def _non_empty_or_empty_string(value, label: str, max_chars: int) -> None:
    if not isinstance(value, str) or len(value) > max_chars:
        raise ValidationError("%s must be a string of at most %d characters" % (label, max_chars))


def _reject_json_constant(value):
    raise ValueError("invalid JSON constant %s" % value)


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key %s" % key)
        result[key] = value
    return result
