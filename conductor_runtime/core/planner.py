"""One-call opt-in workflow planning with strict post-generation validation."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..errors import PolicyError, ValidationError
from .auto import write_requested
from .policy import RuntimePolicy
from .runner import WorkflowRunner
from .safe import canonical_json_bytes, sha256_bytes, strict_json_bytes
from .workflow import MAX_ITEMS, MAX_STEPS, MAX_WORKERS, validate_workflow, workflow_fingerprint


DEFAULT_PLANNER_TOKENS = 12000
DEFAULT_EXECUTION_TOKENS = 120000
PLANNER_SCHEMA = {
    "type": "object",
    "required": ["schema", "name", "steps"],
    "properties": {
        "schema": {"const": "conductor.workflow.v1"},
        "name": {"type": "string", "minLength": 1, "maxLength": 128},
        "steps": {"type": "array", "minItems": 1, "maxItems": MAX_STEPS},
    },
}


@dataclass(frozen=True)
class WorkflowAutoResult:
    status: str
    planner_run_dir: Path
    run_dir: Optional[Path]
    receipt_path: Path
    result_path: Optional[Path]
    stage_evidence_path: Optional[Path]


def run_planned_workflow(
    task: str,
    *,
    workspace: Path,
    policy: RuntimePolicy,
    runs_dir: Optional[Path] = None,
    output_path: Optional[Path] = None,
    receipt_path: Optional[Path] = None,
    plan_only: bool = False,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    planner_max_tokens: int = DEFAULT_PLANNER_TOKENS,
    execution_max_tokens: int = DEFAULT_EXECUTION_TOKENS,
    max_steps: int = 32,
    max_items: int = 1000,
    max_workers: int = 8,
    timeout_seconds: int = 900,
    output_limit_bytes: int = 1024 * 1024,
) -> WorkflowAutoResult:
    if not isinstance(task, str) or not task.strip() or len(task) > 65536:
        raise ValidationError("planning task must be non-empty bounded text")
    _bounded_limit(max_steps, 1, MAX_STEPS, "max_steps")
    _bounded_limit(max_items, 1, MAX_ITEMS, "max_items")
    _bounded_limit(max_workers, 1, MAX_WORKERS, "max_workers")
    _bounded_limit(planner_max_tokens, 100, 10**9, "planner_max_tokens")
    _bounded_limit(execution_max_tokens, 100, 10**9, "execution_max_tokens")
    planner_step = {
        "id": "plan",
        "kind": "codex_exec",
        "prompt": _planner_prompt(task, policy, max_steps, max_items, max_workers, execution_max_tokens),
        "capture": "planned-workflow.json",
        "sandbox": "read-only",
        "max_tokens": planner_max_tokens,
        "timeout_seconds": timeout_seconds,
        "output_limit_bytes": output_limit_bytes,
        "output_schema": PLANNER_SCHEMA,
    }
    if model:
        planner_step["model"] = model
    if effort:
        planner_step["effort"] = effort
    planner_workflow = {
        "schema": "conductor.workflow.v1",
        "name": "workflow-planner",
        "mode": "read_only",
        "result_artifact": "planned-workflow.json",
        "steps": [planner_step],
    }
    planner = WorkflowRunner(planner_workflow, workspace, runs_dir, policy)
    planning_run = planner.execute()
    if planning_run.state["status"] != "completed":
        return _result(
            "failed",
            planning_run,
            None,
            None,
            receipt_path,
            planner_workflow,
            None,
        )
    planned = strict_json_bytes(planning_run.read_artifact("planned-workflow.json"), "planned workflow")
    if not isinstance(planned, dict):
        raise ValidationError("planned workflow must be an object")
    validate_workflow(planned, source="model-planned workflow")
    planned.setdefault("max_items", max_items)
    validate_workflow(planned, source="bounded model-planned workflow")
    _enforce_plan_limits(planned, policy, max_steps, max_items, max_workers, execution_max_tokens)
    if plan_only:
        planned_path = planning_run.run_dir / "planned-workflow.json"
        write_requested(planned_path, canonical_json_bytes(planned))
        return _result(
            "planned",
            planning_run,
            None,
            planned_path,
            receipt_path,
            planner_workflow,
            planned,
        )
    execution = WorkflowRunner(
        planned,
        workspace,
        runs_dir,
        policy,
        max_workers=max_workers,
    ).execute()
    result_path = None
    relative = planned.get("result_artifact")
    if execution.state["status"] == "completed" and relative:
        payload = execution.read_artifact(relative)
        result_path = execution.artifact_path(relative)
        if output_path is not None:
            write_requested(Path(output_path), payload)
            result_path = Path(output_path)
    return _result(
        execution.state["status"],
        planning_run,
        execution,
        result_path,
        receipt_path,
        planner_workflow,
        planned,
    )


def _result(status, planning_run, execution, result_path, requested_receipt, planner_workflow, planned):
    stage = None
    if execution is not None:
        for record in execution.state["steps"].values():
            relative = record.get("stage_evidence")
            if relative:
                stage = execution.artifact_path(relative)
                break
    receipt = {
        "schema": "conductor.core_workflow_auto_receipt.v1",
        "route": "workflow",
        "status": status,
        "planner_run_id": planning_run.descriptor["run_id"],
        "planner_workflow_sha256": workflow_fingerprint(planner_workflow),
        "planned_workflow_sha256": workflow_fingerprint(planned) if planned is not None else None,
        "execution_run_id": execution.descriptor["run_id"] if execution is not None else None,
        "provider_planning_calls": 1,
        "approval_values_persisted": False,
    }
    receipt["receipt_sha256"] = sha256_bytes(canonical_json_bytes(receipt))
    path = Path(requested_receipt) if requested_receipt is not None else planning_run.run_dir / "workflow-auto.json"
    write_requested(path, canonical_json_bytes(receipt))
    return WorkflowAutoResult(
        status=status,
        planner_run_dir=planning_run.run_dir,
        run_dir=execution.run_dir if execution is not None else None,
        receipt_path=path,
        result_path=result_path,
        stage_evidence_path=stage,
    )


def _enforce_plan_limits(workflow, policy, max_steps, max_items, max_workers, token_cap):
    if len(workflow["steps"]) > max_steps:
        raise ValidationError("planned workflow exceeds the requested step limit")
    if workflow.get("max_workers", 1) > max_workers:
        raise ValidationError("planned workflow exceeds the requested worker limit")
    if workflow.get("max_items", MAX_ITEMS) > max_items:
        raise ValidationError("planned workflow exceeds the requested item limit")
    if not workflow.get("result_artifact"):
        raise ValidationError("planned workflow must declare a result_artifact")
    total = 0
    for step in workflow["steps"]:
        if step["kind"] == "codex_exec":
            total += step.get("max_tokens", workflow.get("agent_max_tokens", 0))
            if step.get("sandbox", "read-only") == "workspace-write" and not policy.allow_writes:
                raise PolicyError("planned workflow requests writes without --allow-writes")
        elif step["kind"] == "agent_map":
            total += step.get("max_total_tokens", workflow.get("agent_map_max_total_tokens", 0))
            if len(step.get("items", [])) > max_items:
                raise ValidationError("planned map exceeds the requested item limit")
            if step.get("max_workers", workflow.get("max_workers", 1)) > max_workers:
                raise ValidationError("planned map exceeds the requested worker limit")
    if total > token_cap:
        raise ValidationError("planned workflow exceeds the aggregate execution token budget")


def _planner_prompt(task, policy, max_steps, max_items, max_workers, token_cap):
    return """Design one strict conductor.workflow.v1 JSON workflow for the task below.

Use direct sequential steps unless independent investigation over multiple items is materially useful. For parallel work, use exactly this bounded shape: one read-only agent_map, one collect_results step, then one codex_exec synthesis step that directly depends on and uses context_from the collector. Map workers must be read-only. Every Codex call needs max_tokens; every map needs max_total_tokens. A workspace-write Codex step is allowed only when writes_allowed is true and must have a downstream read-only shell check or strict-v1 Codex verifier. Use at most {steps} steps, {items} items, {workers} workers, and {tokens} aggregate declared execution tokens. Use argv arrays for shell commands. Set result_artifact. Return only the workflow JSON.

writes_allowed={writes}
network_allowed={network}
destructive_allowed={destructive}

BEGIN_UNTRUSTED_TASK
{task}
END_UNTRUSTED_TASK
""".format(
        steps=max_steps,
        items=max_items,
        workers=max_workers,
        tokens=token_cap,
        writes=str(policy.allow_writes).lower(),
        network=str(policy.allow_network).lower(),
        destructive=str(policy.allow_destructive).lower(),
        task=task,
    )


def _bounded_limit(value, minimum, maximum, label):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError("%s must be from %d to %d" % (label, minimum, maximum))
