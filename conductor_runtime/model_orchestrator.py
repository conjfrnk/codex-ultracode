import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from .artifacts import utc_now
from .background_run import COMPLETION_RECEIPT_KIND, start_prepared_background_run
from .errors import ConductorError, PolicyError, ValidationError
from .model_planner import (
    DEFAULT_MODEL_WORKFLOW_REASONING_EFFORT,
    MAX_MODEL_WORKFLOW_ITEMS,
    MAX_MODEL_WORKFLOW_STEPS,
    MAX_MODEL_WORKFLOW_TASK_CHARS,
    MODEL_WORKFLOW_RECEIPT_SCHEMA,
    MODEL_WORKFLOW_RECEIPT_SCHEMA_V3,
    MODEL_WORKFLOW_RECEIPT_STATUS,
    MODEL_WORKFLOW_WRITE_APPROVAL,
    ModelWorkflowDraftResult,
    load_model_workflow_receipt_snapshot,
    model_workflow_task_metadata,
    plan_direct_model_workflow,
    plan_model_workflow,
    validate_model_workflow_reasoning_effort,
    verify_model_workflow_workspace_binding,
)
from .redaction import contains_secret_like, redact_text
from .prepared_run import record_prepared_run
from .runner import DEFAULT_OUTPUT_LIMIT_BYTES, WorkflowRunner
from .security import (
    HIGH_SCALE_AGENT_WORKER_THRESHOLD,
    RuntimePolicy,
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    write_new_text_file_no_follow,
)
from .workflow import MAX_AGENT_WORKERS, load_workflow, workflow_fingerprint


MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA_V1 = "conductor.model_workflow_run_receipt.v1"
MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA_V2 = "conductor.model_workflow_run_receipt.v2"
MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA = "conductor.model_workflow_run_receipt.v3"
MODEL_WORKFLOW_EXECUTE_APPROVAL = "model-workflow-execute"
REVIEWED_MODEL_WORKFLOW_UNBOUND_APPROVAL = "unbound-reviewed-workspace"
MAX_MODEL_WORKFLOW_RUN_RECEIPT_BYTES = 512 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RUN_RECEIPT_FIELDS = {
    "schema",
    "generated_at_utc",
    "status",
    "task",
    "workflow",
    "planning",
    "preflight",
    "execution",
    "policy",
    "limits",
    "run",
    "error",
}
RUN_STATUSES = {"planned", "running", "completed", "blocked", "failed", "paused", "stopped", "terminated"}


@dataclass(frozen=True)
class ModelWorkflowRunResult:
    draft: Optional[ModelWorkflowDraftResult]
    receipt: Dict
    receipt_path: Path
    run_dir: Optional[Path]
    status: str
    error_class: str
    background_pid: Optional[int] = None


def default_model_workflow_run_receipt(output_path: Path) -> Path:
    return Path(str(Path(output_path).expanduser()) + ".run-receipt.json")


def run_model_workflow(
    *,
    task: str,
    workspace: Path,
    output_path: Path,
    planning_receipt_path: Optional[Path],
    run_receipt_path: Optional[Path],
    runs_dir: Path,
    policy: RuntimePolicy,
    name: Optional[str] = None,
    model: Optional[str] = None,
    planner_effort: str = DEFAULT_MODEL_WORKFLOW_REASONING_EFFORT,
    write_capable: bool = False,
    max_workers: int = 1,
    max_items: int = MAX_MODEL_WORKFLOW_ITEMS,
    max_steps: int = 32,
    planning_timeout_seconds: int = 900,
    planning_output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    run_id: Optional[str] = None,
    dry_run: bool = False,
    background: bool = False,
    planner_session_id: Optional[str] = None,
    direct_plan: bool = False,
    direct_external_verifier: bool = False,
    external_verifier: bool = False,
    direct_native_agent_threads: Optional[int] = None,
    direct_max_tokens: Optional[int] = None,
    desktop_notify: bool = False,
) -> ModelWorkflowRunResult:
    if not isinstance(background, bool):
        raise ValidationError("background must be boolean")
    if not isinstance(direct_plan, bool):
        raise ValidationError("direct_plan must be boolean")
    if not isinstance(direct_external_verifier, bool):
        raise ValidationError("direct_external_verifier must be boolean")
    if not isinstance(external_verifier, bool):
        raise ValidationError("external_verifier must be boolean")
    if not isinstance(desktop_notify, bool):
        raise ValidationError("desktop_notify must be boolean")
    if direct_external_verifier and not direct_plan:
        raise ValidationError("direct_external_verifier requires direct_plan")
    effective_external_verifier = external_verifier or direct_external_verifier
    if direct_native_agent_threads is not None and not direct_plan:
        raise ValidationError("direct_native_agent_threads requires direct_plan")
    if direct_native_agent_threads is not None and effective_external_verifier:
        raise ValidationError("direct native model workflows do not support an adaptive verifier")
    if direct_max_tokens is not None and not direct_plan:
        raise ValidationError("direct_max_tokens requires direct_plan")
    if dry_run and background:
        raise ValidationError("background execution cannot be combined with dry_run")
    if dry_run and desktop_notify:
        raise ValidationError("desktop notifications require real model workflow execution")
    if direct_plan and planner_session_id is not None:
        raise ValidationError("direct model workflows cannot resume a planner session")
    if direct_plan and max_workers != 1:
        raise ValidationError("direct model workflows require max_workers 1")
    output = Path(output_path).expanduser()
    planning_receipt = (
        Path(planning_receipt_path).expanduser()
        if planning_receipt_path is not None
        else Path(str(output) + ".receipt.json")
    )
    run_receipt = (
        Path(run_receipt_path).expanduser()
        if run_receipt_path is not None
        else default_model_workflow_run_receipt(output)
    )
    validate_model_workflow_execution_request(
        policy=policy,
        dry_run=dry_run,
        write_capable=write_capable,
        max_workers=max_workers,
    )
    _preflight_receipt_path(run_receipt)
    if run_receipt in {output, planning_receipt}:
        raise ValidationError("model workflow run receipt path must differ from planning outputs")
    run_receipt_parent_fd = ensure_dir_no_follow(run_receipt.parent, "model workflow run receipt parent")
    os.close(run_receipt_parent_fd)
    task_metadata = model_workflow_task_metadata(task)
    clean_planner_effort = validate_model_workflow_reasoning_effort(planner_effort)

    try:
        planning_function = plan_direct_model_workflow if direct_plan else plan_model_workflow
        draft = planning_function(
            task=task,
            workspace=workspace,
            output_path=output,
            receipt_path=planning_receipt,
            policy=policy,
            name=name,
            model=model,
            reasoning_effort=clean_planner_effort,
            write_capable=write_capable,
            max_workers=max_workers,
            max_items=max_items,
            max_steps=max_steps,
            timeout_seconds=planning_timeout_seconds,
            output_limit_bytes=planning_output_limit_bytes,
            **(
                {
                    "external_verifier": effective_external_verifier,
                    "native_agent_threads": direct_native_agent_threads,
                    "max_tokens": direct_max_tokens,
                }
                if direct_plan
                else {
                    "planner_session_id": planner_session_id,
                    "external_verifier": effective_external_verifier,
                }
            ),
        )
    except ConductorError as exc:
        receipt = _build_planning_failure_receipt(
            task_metadata=task_metadata,
            policy=policy,
            max_workers=max_workers,
            max_items=max_items,
            max_steps=max_steps,
            planning_timeout_seconds=planning_timeout_seconds,
            planning_output_limit_bytes=planning_output_limit_bytes,
            planner_effort=clean_planner_effort,
            dry_run=dry_run,
            background=background,
            error_class=exc.__class__.__name__,
        )
        write_new_text_file_no_follow(
            run_receipt,
            "model workflow run receipt",
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        )
        return ModelWorkflowRunResult(
            draft=None,
            receipt=receipt,
            receipt_path=run_receipt,
            run_dir=None,
            status="failed",
            error_class=exc.__class__.__name__,
        )

    return _execute_model_workflow_draft(
        draft=draft,
        workspace=workspace,
        runs_dir=runs_dir,
        policy=policy,
        run_receipt=run_receipt,
        max_workers=max_workers,
        max_items=max_items,
        max_steps=max_steps,
        planning_timeout_seconds=planning_timeout_seconds,
        planning_output_limit_bytes=planning_output_limit_bytes,
        planner_effort=clean_planner_effort,
        planning_receipt_sha256=_sha256_file(
            draft.receipt_path, "model workflow planning receipt"
        ),
        run_id=run_id,
        dry_run=dry_run,
        background=background,
        desktop_notify=desktop_notify,
    )


def run_reviewed_model_workflow(
    *,
    workflow_path: Path,
    planning_receipt_path: Path,
    workspace: Path,
    runs_dir: Path,
    policy: RuntimePolicy,
    run_receipt_path: Optional[Path] = None,
    run_id: Optional[str] = None,
    dry_run: bool = False,
    background: bool = False,
    allow_unbound_workspace: bool = False,
    desktop_notify: bool = False,
) -> ModelWorkflowRunResult:
    if not isinstance(background, bool):
        raise ValidationError("background must be boolean")
    if dry_run and background:
        raise ValidationError("background execution cannot be combined with dry_run")
    if not isinstance(desktop_notify, bool):
        raise ValidationError("desktop_notify must be boolean")
    if dry_run and desktop_notify:
        raise ValidationError("desktop notifications require real reviewed workflow execution")
    if not isinstance(allow_unbound_workspace, bool):
        raise ValidationError("allow_unbound_workspace must be boolean")
    draft, planning_receipt_sha256 = _load_reviewed_model_workflow_draft(
        workflow_path=workflow_path,
        planning_receipt_path=planning_receipt_path,
    )
    workspace_binding = draft.receipt.get("workspace")
    workspace_is_bound = (
        draft.receipt["schema"] == MODEL_WORKFLOW_RECEIPT_SCHEMA
        and isinstance(workspace_binding, dict)
        and workspace_binding.get("mode") == "bound"
    )
    if workspace_is_bound:
        verify_model_workflow_workspace_binding(workspace_binding, workspace)
    else:
        if not allow_unbound_workspace:
            raise PolicyError(
                "reviewed model workflow has no workspace binding; use --allow-unbound-workspace"
            )
        if not dry_run and not policy.has_approval(REVIEWED_MODEL_WORKFLOW_UNBOUND_APPROVAL):
            raise PolicyError(
                "unbound reviewed model workflow execution requires --approve %s"
                % REVIEWED_MODEL_WORKFLOW_UNBOUND_APPROVAL
            )
    limits = draft.receipt["limits"]
    max_workers = limits["max_workers"]
    max_items = limits["max_items"]
    max_steps = limits["max_steps"]
    planning_timeout_seconds = limits["timeout_seconds"]
    planning_output_limit_bytes = limits["output_limit_bytes"]
    planner_effort = draft.receipt["model"]["reasoning_effort"]
    write_capable = draft.receipt["policy"]["write_capable_draft_allowed"]
    requires_agent = any(
        isinstance(step, dict)
        and step.get("kind") in {"codex_exec", "agent_map", "agent_team"}
        for step in draft.workflow["steps"]
    )
    validate_model_workflow_execution_request(
        policy=policy,
        dry_run=dry_run,
        write_capable=write_capable,
        max_workers=max_workers,
        requires_agent=requires_agent,
    )
    run_receipt = (
        Path(run_receipt_path).expanduser()
        if run_receipt_path is not None
        else default_model_workflow_run_receipt(draft.output_path)
    )
    _preflight_receipt_path(run_receipt)
    if run_receipt in {draft.output_path, draft.receipt_path}:
        raise ValidationError("model workflow run receipt path must differ from reviewed planning outputs")
    run_receipt_parent_fd = ensure_dir_no_follow(run_receipt.parent, "model workflow run receipt parent")
    os.close(run_receipt_parent_fd)
    return _execute_model_workflow_draft(
        draft=draft,
        workspace=workspace,
        runs_dir=runs_dir,
        policy=policy,
        run_receipt=run_receipt,
        max_workers=max_workers,
        max_items=max_items,
        max_steps=max_steps,
        planning_timeout_seconds=planning_timeout_seconds,
        planning_output_limit_bytes=planning_output_limit_bytes,
        planner_effort=planner_effort,
        planning_receipt_sha256=planning_receipt_sha256,
        run_id=run_id,
        dry_run=dry_run,
        background=background,
        desktop_notify=desktop_notify,
    )


def load_reviewed_model_workflow_draft(
    *, workflow_path: Path, planning_receipt_path: Path
) -> ModelWorkflowDraftResult:
    draft, _ = _load_reviewed_model_workflow_draft(
        workflow_path=workflow_path,
        planning_receipt_path=planning_receipt_path,
    )
    return draft


def _load_reviewed_model_workflow_draft(
    *, workflow_path: Path, planning_receipt_path: Path
) -> Tuple[ModelWorkflowDraftResult, str]:
    output = Path(workflow_path).expanduser()
    receipt_path = Path(planning_receipt_path).expanduser()
    receipt, receipt_sha256 = load_model_workflow_receipt_snapshot(receipt_path)
    if receipt["schema"] not in {
        MODEL_WORKFLOW_RECEIPT_SCHEMA_V3,
        MODEL_WORKFLOW_RECEIPT_SCHEMA,
    }:
        raise ValidationError("reviewed model workflow execution requires a v3 or v4 planning receipt")
    workflow = load_workflow(output)
    expected = receipt["workflow"]
    observed = {
        "name": workflow["name"],
        "filename": output.name,
        "sha256": workflow_fingerprint(workflow),
        "steps": len(workflow["steps"]),
        "mode": workflow.get("mode", "read_only"),
    }
    if observed != expected:
        raise ValidationError("reviewed model workflow does not match its planning receipt")
    return (
        ModelWorkflowDraftResult(
            workflow=workflow,
            receipt=receipt,
            output_path=output,
            receipt_path=receipt_path,
        ),
        receipt_sha256,
    )


def _execute_model_workflow_draft(
    *,
    draft: ModelWorkflowDraftResult,
    workspace: Path,
    runs_dir: Path,
    policy: RuntimePolicy,
    run_receipt: Path,
    max_workers: int,
    max_items: int,
    max_steps: int,
    planning_timeout_seconds: int,
    planning_output_limit_bytes: int,
    planner_effort: str,
    planning_receipt_sha256: str,
    run_id: Optional[str],
    dry_run: bool,
    background: bool,
    desktop_notify: bool,
) -> ModelWorkflowRunResult:
    run = None
    preflight_completed = False
    preflight_status = "not-started"
    execution_started = False
    status = "failed"
    error_class = ""
    try:
        preflight_runner = WorkflowRunner(
            workflow=draft.workflow,
            workspace=workspace,
            base_run_dir=runs_dir,
            policy=policy,
            dry_run=True,
            run_id=run_id,
            max_workers=max_workers,
        )
        run = preflight_runner.run
        run = preflight_runner.execute()
        preflight_status = _run_status(run)
        preflight_completed = preflight_status == "planned"
        if not preflight_completed:
            status = _normalized_run_status(preflight_status)
        elif dry_run:
            status = "planned"
        elif background:
            record_prepared_run(
                run=run,
                workflow=draft.workflow,
                workflow_path=draft.output_path,
                workspace=workspace,
                runs_dir=runs_dir,
                policy=policy,
                max_workers=max_workers,
                effective_max_workers=preflight_runner.max_workers,
            )
            status = "running"
            execution_started = True
            receipt = _build_run_receipt(
                draft=draft,
                planning_receipt_sha256=planning_receipt_sha256,
                policy=policy,
                max_workers=max_workers,
                max_items=max_items,
                max_steps=max_steps,
                planning_timeout_seconds=planning_timeout_seconds,
                planning_output_limit_bytes=planning_output_limit_bytes,
                planner_effort=planner_effort,
                dry_run=False,
                background=True,
                preflight_completed=True,
                preflight_status=preflight_status,
                execution_started=True,
                run_dir=run.run_dir,
                status=status,
                error_class="",
            )
            write_new_text_file_no_follow(
                run_receipt,
                "model workflow run receipt",
                json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            )
            receipt_sha256 = _sha256_file(run_receipt, "model workflow run receipt")
            try:
                launched = start_prepared_background_run(
                    run_dir=run.run_dir,
                    workspace=workspace,
                    runs_dir=runs_dir,
                    policy=policy,
                    max_workers=max_workers,
                    completion_receipt={
                        "kind": COMPLETION_RECEIPT_KIND,
                        "path": str(run_receipt.resolve()),
                        "sha256": receipt_sha256,
                    },
                    desktop_notify=desktop_notify,
                )
            except ConductorError as exc:
                receipt = finalize_background_model_workflow_run_receipt(
                    path=run_receipt,
                    expected_sha256=receipt_sha256,
                    run_dir=run.run_dir,
                    run_status="failed",
                    error_class=exc.__class__.__name__,
                    execution_started=False,
                )
                return ModelWorkflowRunResult(
                    draft=draft,
                    receipt=receipt,
                    receipt_path=run_receipt,
                    run_dir=run.run_dir,
                    status="failed",
                    error_class=exc.__class__.__name__,
                )
            return ModelWorkflowRunResult(
                draft=draft,
                receipt=receipt,
                receipt_path=run_receipt,
                run_dir=run.run_dir,
                status="running",
                error_class="",
                background_pid=launched.pid,
            )
        else:
            execution_runner = WorkflowRunner(
                workflow=draft.workflow,
                workspace=workspace,
                base_run_dir=runs_dir,
                policy=policy,
                dry_run=False,
                resume_dir=run.run_dir,
                max_workers=max_workers,
            )
            execution_started = True
            run = execution_runner.execute()
            status = _run_status(run)
    except ConductorError as exc:
        error_class = exc.__class__.__name__
        policy_blocked = isinstance(exc, PolicyError)
        if run is not None:
            try:
                status = "blocked" if policy_blocked else _run_status(run)
                if preflight_status == "not-started":
                    preflight_status = status
            except ConductorError:
                status = "blocked" if policy_blocked else "failed"
        else:
            status = "blocked" if policy_blocked else "failed"

    receipt = _build_run_receipt(
        draft=draft,
        planning_receipt_sha256=planning_receipt_sha256,
        policy=policy,
        max_workers=max_workers,
        max_items=max_items,
        max_steps=max_steps,
        planning_timeout_seconds=planning_timeout_seconds,
        planning_output_limit_bytes=planning_output_limit_bytes,
        planner_effort=planner_effort,
        dry_run=dry_run,
        background=background,
        preflight_completed=preflight_completed,
        preflight_status=preflight_status,
        execution_started=execution_started,
        run_dir=run.run_dir if run is not None else None,
        status=status,
        error_class=error_class,
    )
    write_new_text_file_no_follow(
        run_receipt,
        "model workflow run receipt",
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
    )
    return ModelWorkflowRunResult(
        draft=draft,
        receipt=receipt,
        receipt_path=run_receipt,
        run_dir=run.run_dir if run is not None else None,
        status=status,
        error_class=error_class,
    )


def load_model_workflow_run_receipt(path: Path) -> Dict:
    raw = read_regular_text_file_no_follow(path, "model workflow run receipt", MAX_MODEL_WORKFLOW_RUN_RECEIPT_BYTES)
    try:
        data = json.loads(raw, parse_constant=_reject_json_constant, object_pairs_hook=_object_without_duplicates)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("%s is not strict JSON: %s" % (path, exc))
    validate_model_workflow_run_receipt(data, source=str(path))
    return data


def validate_model_workflow_run_receipt(receipt: Dict, source: str = "<memory>") -> None:
    root = _exact_object(receipt, RUN_RECEIPT_FIELDS, source)
    if root.get("schema") == MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA_V1:
        workflow = _exact_object(
            root.get("workflow"),
            {"name", "filename", "sha256", "steps", "mode"},
            "%s legacy workflow" % source,
        )
        limits = _exact_object(
            root.get("limits"),
            {"max_workers", "max_items", "max_steps"},
            "%s legacy limits" % source,
        )
        normalized = dict(root)
        normalized["schema"] = MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA_V2
        normalized["workflow"] = {"available": True, **dict(workflow)}
        normalized["limits"] = {
            **dict(limits),
            "planning_timeout_seconds": 900,
            "planning_output_limit_bytes": DEFAULT_OUTPUT_LIMIT_BYTES,
            "planner_effort": DEFAULT_MODEL_WORKFLOW_REASONING_EFFORT,
        }
        validate_model_workflow_run_receipt(normalized, source=source)
        return
    if root.get("schema") == MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA_V2:
        legacy_execution = root.get("execution")
        if not isinstance(legacy_execution, dict) or legacy_execution.get("foreground") is not True:
            raise ValidationError("%s v2 receipt must record foreground execution" % source)
        if root.get("status") == "running":
            raise ValidationError("%s v2 receipt cannot remain running" % source)
        normalized = dict(root)
        normalized["schema"] = MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA
        validate_model_workflow_run_receipt(normalized, source=source)
        return
    if root.get("schema") != MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA:
        raise ValidationError("%s has unsupported model workflow run receipt schema" % source)
    timestamp = root.get("generated_at_utc")
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ValidationError("%s generated_at_utc must use UTC Z notation" % source)
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("%s generated_at_utc must be an ISO timestamp" % source)
    if root.get("status") not in RUN_STATUSES:
        raise ValidationError("%s status is invalid" % source)

    task = _exact_object(root.get("task"), {"sha256", "chars", "raw_persisted"}, "%s task" % source)
    _validate_sha256(task.get("sha256"), "%s task.sha256" % source)
    if not _bounded_int(task.get("chars"), 1, MAX_MODEL_WORKFLOW_TASK_CHARS) or task.get("raw_persisted") is not False:
        raise ValidationError("%s task metadata is invalid" % source)

    workflow = _exact_object(
        root.get("workflow"),
        {"available", "name", "filename", "sha256", "steps", "mode"},
        "%s workflow" % source,
    )
    if not isinstance(workflow.get("available"), bool):
        raise ValidationError("%s workflow.available must be boolean" % source)
    _validate_filename(workflow["filename"], "%s workflow.filename" % source)
    if workflow["available"]:
        for key in ("name", "mode"):
            if not isinstance(workflow.get(key), str) or not workflow[key]:
                raise ValidationError("%s workflow.%s is invalid" % (source, key))
        _validate_sha256(workflow.get("sha256"), "%s workflow.sha256" % source)
        if not _bounded_int(workflow.get("steps"), 1, MAX_MODEL_WORKFLOW_STEPS):
            raise ValidationError("%s workflow.steps is invalid" % source)
    elif not (
        workflow.get("name") == ""
        and workflow.get("mode") == ""
        and workflow.get("sha256") is None
        and workflow.get("steps") == 0
    ):
        raise ValidationError("%s unavailable workflow metadata is invalid" % source)

    planning = _exact_object(
        root.get("planning"),
        {"status", "receipt_filename", "receipt_sha256"},
        "%s planning" % source,
    )
    if planning.get("status") not in {MODEL_WORKFLOW_RECEIPT_STATUS, "failed"}:
        raise ValidationError("%s planning status is invalid" % source)
    planning_failed = planning["status"] == "failed"
    if planning_failed:
        if planning.get("receipt_filename") != "" or planning.get("receipt_sha256") is not None:
            raise ValidationError("%s failed planning receipt metadata is invalid" % source)
        if workflow["available"] or root["status"] != "failed":
            raise ValidationError("%s failed planning must not record an available workflow" % source)
    else:
        _validate_filename(planning.get("receipt_filename"), "%s planning.receipt_filename" % source)
        _validate_sha256(planning.get("receipt_sha256"), "%s planning.receipt_sha256" % source)
        if not workflow["available"]:
            raise ValidationError("%s validated planning requires an available workflow" % source)

    preflight = _exact_object(
        root.get("preflight"), {"completed", "status"}, "%s preflight" % source
    )
    if not isinstance(preflight.get("completed"), bool) or not isinstance(preflight.get("status"), str):
        raise ValidationError("%s preflight metadata is invalid" % source)
    if preflight["completed"] is True and preflight["status"] != "planned":
        raise ValidationError("%s completed preflight must have planned status" % source)
    if planning_failed and (preflight["completed"] or preflight["status"] != "not-started"):
        raise ValidationError("%s failed planning cannot record a workflow preflight" % source)

    execution = _exact_object(
        root.get("execution"),
        {
            "mode",
            "foreground",
            "automatic_handoff",
            "started",
            "execute_approval_id",
            "execute_approval_present",
            "background_service",
            "approval_values_persisted",
        },
        "%s execution" % source,
    )
    if execution.get("mode") not in {"run", "dry-run"}:
        raise ValidationError("%s execution mode is invalid" % source)
    if not isinstance(execution.get("foreground"), bool) or execution.get("background_service") is not False:
        raise ValidationError("%s execution process contract is invalid" % source)
    if execution.get("approval_values_persisted") is not False:
        raise ValidationError("%s execution approval persistence is invalid" % source)
    if execution.get("execute_approval_id") != MODEL_WORKFLOW_EXECUTE_APPROVAL:
        raise ValidationError("%s execution approval id is invalid" % source)
    for key in ("automatic_handoff", "started", "execute_approval_present"):
        if not isinstance(execution.get(key), bool):
            raise ValidationError("%s execution.%s must be boolean" % (source, key))
    if execution["mode"] == "dry-run":
        if execution["automatic_handoff"] or execution["started"] or execution["foreground"] is not True:
            raise ValidationError("%s dry-run must not record workflow execution" % source)
    elif not execution["execute_approval_present"]:
        raise ValidationError("%s run mode requires explicit automatic handoff approval" % source)
    elif planning_failed:
        if execution["automatic_handoff"] or execution["started"]:
            raise ValidationError("%s failed planning cannot hand off to execution" % source)
    elif not execution["automatic_handoff"]:
        raise ValidationError("%s run mode requires explicit automatic handoff approval" % source)
    if execution["started"] and preflight["completed"] is not True:
        raise ValidationError("%s workflow execution cannot start before completed preflight" % source)
    if root["status"] == "running" and (
        execution["mode"] != "run" or execution["started"] is not True or execution["foreground"] is not False
    ):
        raise ValidationError("%s running status requires a started detached run" % source)
    if execution["foreground"] is True and root["status"] == "running":
        raise ValidationError("%s foreground receipt cannot remain running" % source)
    if root["status"] == "completed" and (execution["mode"] != "run" or execution["started"] is not True):
        raise ValidationError("%s completed status requires a started foreground run" % source)
    if root["status"] == "planned" and execution["mode"] != "dry-run":
        raise ValidationError("%s planned status is only valid for dry-run mode" % source)

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
    if not _bounded_int(policy.get("approval_count"), 0, 1000) or policy.get("approval_values_persisted") is not False:
        raise ValidationError("%s policy approval metadata is invalid" % source)

    limits = _exact_object(
        root.get("limits"),
        {
            "max_workers",
            "max_items",
            "max_steps",
            "planning_timeout_seconds",
            "planning_output_limit_bytes",
            "planner_effort",
        },
        "%s limits" % source,
    )
    if not _bounded_int(limits.get("max_workers"), 1, MAX_AGENT_WORKERS):
        raise ValidationError("%s limits.max_workers is invalid" % source)
    if not _bounded_int(limits.get("max_items"), 1, MAX_MODEL_WORKFLOW_ITEMS):
        raise ValidationError("%s limits.max_items is invalid" % source)
    if not _bounded_int(limits.get("max_steps"), 1, MAX_MODEL_WORKFLOW_STEPS):
        raise ValidationError("%s limits.max_steps is invalid" % source)
    if not _bounded_int(limits.get("planning_timeout_seconds"), 1, 60 * 60):
        raise ValidationError("%s limits.planning_timeout_seconds is invalid" % source)
    if not _bounded_int(limits.get("planning_output_limit_bytes"), 1, DEFAULT_OUTPUT_LIMIT_BYTES):
        raise ValidationError("%s limits.planning_output_limit_bytes is invalid" % source)
    validate_model_workflow_reasoning_effort(limits.get("planner_effort"))

    run = _exact_object(root.get("run"), {"id", "status"}, "%s run" % source)
    if not isinstance(run.get("id"), str) or not isinstance(run.get("status"), str):
        raise ValidationError("%s run metadata is invalid" % source)
    if run["status"] not in RUN_STATUSES:
        raise ValidationError("%s run status is invalid" % source)
    if run["status"] != root["status"]:
        raise ValidationError("%s run status does not match receipt status" % source)
    if execution["started"] and not run["id"]:
        raise ValidationError("%s started execution requires a run id" % source)
    if planning_failed and run["id"]:
        raise ValidationError("%s failed planning cannot record a run id" % source)
    error = _exact_object(root.get("error"), {"class"}, "%s error" % source)
    if not isinstance(error.get("class"), str) or len(error["class"]) > 200:
        raise ValidationError("%s error class is invalid" % source)
    if planning_failed and not error["class"]:
        raise ValidationError("%s failed planning requires an error class" % source)
    _reject_secret_values(root, source)


def validate_model_workflow_execution_request(
    *,
    policy: RuntimePolicy,
    dry_run: bool,
    write_capable: bool,
    max_workers: int,
    requires_agent: bool = True,
) -> None:
    if not isinstance(dry_run, bool):
        raise ValidationError("dry_run must be boolean")
    if not _bounded_int(max_workers, 1, MAX_AGENT_WORKERS):
        raise ValidationError("max_workers must be an integer from 1 to %d" % MAX_AGENT_WORKERS)
    if not isinstance(requires_agent, bool):
        raise ValidationError("requires_agent must be boolean")
    if requires_agent and not policy.allow_agent:
        raise PolicyError("model workflow requires --allow-agent")
    if max_workers > 1 and not policy.allow_parallel:
        raise PolicyError("model workflow max_workers above 1 requires --allow-parallel")
    if max_workers > HIGH_SCALE_AGENT_WORKER_THRESHOLD and not policy.has_approval("high-scale-agent-map"):
        raise PolicyError(
            "model workflow max_workers above %d requires --approve high-scale-agent-map"
            % HIGH_SCALE_AGENT_WORKER_THRESHOLD
        )
    if write_capable and not policy.allow_writes:
        raise PolicyError("write-capable model workflow requires --allow-writes")
    if dry_run:
        return
    if not policy.has_approval(MODEL_WORKFLOW_EXECUTE_APPROVAL):
        raise PolicyError("model workflow execution requires --approve %s" % MODEL_WORKFLOW_EXECUTE_APPROVAL)
    if write_capable and not policy.has_approval(MODEL_WORKFLOW_WRITE_APPROVAL):
        raise PolicyError("write-capable model workflow execution requires --approve %s" % MODEL_WORKFLOW_WRITE_APPROVAL)


def _build_run_receipt(
    *,
    draft: ModelWorkflowDraftResult,
    planning_receipt_sha256: str,
    policy: RuntimePolicy,
    max_workers: int,
    max_items: int,
    max_steps: int,
    planning_timeout_seconds: int,
    planning_output_limit_bytes: int,
    planner_effort: str,
    dry_run: bool,
    background: bool,
    preflight_completed: bool,
    preflight_status: str,
    execution_started: bool,
    run_dir: Optional[Path],
    status: str,
    error_class: str,
) -> Dict:
    receipt = {
        "schema": MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA,
        "generated_at_utc": utc_now(),
        "status": status,
        "task": dict(draft.receipt["task"]),
        "workflow": {"available": True, **dict(draft.receipt["workflow"])},
        "planning": {
            "status": draft.receipt["status"],
            "receipt_filename": draft.receipt_path.name,
            "receipt_sha256": planning_receipt_sha256,
        },
        "preflight": {
            "completed": bool(preflight_completed),
            "status": redact_text(preflight_status),
        },
        "execution": {
            "mode": "dry-run" if dry_run else "run",
            "foreground": not background,
            "automatic_handoff": not dry_run,
            "started": bool(execution_started),
            "execute_approval_id": MODEL_WORKFLOW_EXECUTE_APPROVAL,
            "execute_approval_present": policy.has_approval(MODEL_WORKFLOW_EXECUTE_APPROVAL),
            "background_service": False,
            "approval_values_persisted": False,
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
        "limits": {
            "max_workers": max_workers,
            "max_items": max_items,
            "max_steps": max_steps,
            "planning_timeout_seconds": planning_timeout_seconds,
            "planning_output_limit_bytes": planning_output_limit_bytes,
            "planner_effort": planner_effort,
        },
        "run": {
            "id": redact_text(run_dir.name) if run_dir is not None else "",
            "status": status,
        },
        "error": {"class": redact_text(error_class)},
    }
    validate_model_workflow_run_receipt(receipt)
    return receipt


def _build_planning_failure_receipt(
    *,
    task_metadata: Dict,
    policy: RuntimePolicy,
    max_workers: int,
    max_items: int,
    max_steps: int,
    planning_timeout_seconds: int,
    planning_output_limit_bytes: int,
    planner_effort: str,
    dry_run: bool,
    background: bool,
    error_class: str,
) -> Dict:
    receipt = {
        "schema": MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA,
        "generated_at_utc": utc_now(),
        "status": "failed",
        "task": dict(task_metadata),
        "workflow": {
            "available": False,
            "name": "",
            "filename": "unavailable.json",
            "sha256": None,
            "steps": 0,
            "mode": "",
        },
        "planning": {
            "status": "failed",
            "receipt_filename": "",
            "receipt_sha256": None,
        },
        "preflight": {"completed": False, "status": "not-started"},
        "execution": {
            "mode": "dry-run" if dry_run else "run",
            "foreground": not background,
            "automatic_handoff": False,
            "started": False,
            "execute_approval_id": MODEL_WORKFLOW_EXECUTE_APPROVAL,
            "execute_approval_present": policy.has_approval(MODEL_WORKFLOW_EXECUTE_APPROVAL),
            "background_service": False,
            "approval_values_persisted": False,
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
        "limits": {
            "max_workers": max_workers,
            "max_items": max_items,
            "max_steps": max_steps,
            "planning_timeout_seconds": planning_timeout_seconds,
            "planning_output_limit_bytes": planning_output_limit_bytes,
            "planner_effort": planner_effort,
        },
        "run": {"id": "", "status": "failed"},
        "error": {"class": redact_text(error_class)},
    }
    validate_model_workflow_run_receipt(receipt)
    return receipt


def _preflight_receipt_path(path: Path) -> None:
    if contains_secret_like(path.name):
        raise ValidationError("model workflow run receipt filename must not contain secret-like values")
    reject_symlink_path(path, "model workflow run receipt")
    if path.exists():
        raise ValidationError("model workflow run receipt already exists: %s" % path)
    reject_symlink_path(path.parent, "model workflow run receipt parent")


def finalize_background_model_workflow_run_receipt(
    *,
    path: Path,
    expected_sha256: str,
    run_dir: Path,
    run_status: str,
    error_class: str,
    execution_started: bool = True,
) -> Dict:
    raw = read_regular_text_file_no_follow(
        path,
        "model workflow run receipt",
        MAX_MODEL_WORKFLOW_RUN_RECEIPT_BYTES,
    )
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != expected_sha256:
        raise PolicyError("model workflow run receipt changed after background launch")
    receipt = load_model_workflow_run_receipt(path)
    if receipt.get("schema") != MODEL_WORKFLOW_RUN_RECEIPT_SCHEMA:
        raise ValidationError("background completion requires the current model workflow run receipt schema")
    if receipt.get("status") != "running":
        raise PolicyError("model workflow run receipt is not awaiting background completion")
    if receipt.get("run", {}).get("id") != Path(run_dir).name:
        raise PolicyError("model workflow run receipt does not match the background run")
    execution = receipt.get("execution")
    if not isinstance(execution, dict) or execution.get("foreground") is not False:
        raise PolicyError("model workflow run receipt is not detached execution evidence")

    final_status = _background_receipt_status(run_status)
    receipt["generated_at_utc"] = utc_now()
    receipt["status"] = final_status
    receipt["run"]["status"] = final_status
    receipt["execution"]["started"] = bool(execution_started)
    receipt["error"]["class"] = redact_text(error_class)
    validate_model_workflow_run_receipt(receipt)
    replace_text_file_no_follow(
        path,
        "model workflow run receipt",
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        ".model-workflow-run-receipt-",
    )
    return receipt


def _sha256_file(path: Path, label: str) -> str:
    text = read_regular_text_file_no_follow(path, label, MAX_MODEL_WORKFLOW_RUN_RECEIPT_BYTES)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalized_run_status(value: str) -> str:
    return value if value in RUN_STATUSES else "failed"


def _background_receipt_status(value: str) -> str:
    if value == "stop_requested":
        return "terminated"
    return _normalized_run_status(value)


def _run_status(run) -> str:
    state = run.read_state()
    steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    step_statuses = {
        value.get("status")
        for value in steps.values()
        if isinstance(value, dict) and isinstance(value.get("status"), str)
    }
    if "failed" in step_statuses:
        return "failed"
    if "blocked" in step_statuses:
        return "blocked"
    return _normalized_run_status(str(state.get("status") or "failed"))


def _exact_object(value, fields, label: str) -> Dict:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError("%s must contain exactly: %s" % (label, ", ".join(sorted(fields))))
    return value


def _bounded_int(value, minimum: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ValidationError("%s is invalid" % label)


def _validate_filename(value, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise ValidationError("%s is invalid" % label)


def _reject_secret_values(value, label: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_secret_values(item, "%s.%s" % (label, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_values(item, "%s[%d]" % (label, index))
    elif isinstance(value, str) and contains_secret_like(value):
        raise ValidationError("%s must not contain secret-like values" % label)


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant %s" % value)


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key %s" % key)
        result[key] = value
    return result
