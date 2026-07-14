import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from tools.evaluate_implementation_canary import (
    TASK_SPECS,
    evaluate_implementation_canary_workspace,
)

from . import __version__
from .auto_orchestrator import load_auto_orchestration_receipt, run_auto_orchestration
from .auto_topology_campaign import (
    AUTO_TOPOLOGIES,
    AUTO_TOPOLOGY_CAMPAIGN_SCHEMA,
    AUTO_TOPOLOGY_LAUNCH_SCHEMA,
    AUTO_TOPOLOGY_RESULT_SCHEMA,
    auto_topology_campaign_sha256,
    auto_topology_campaign_status,
    capture_auto_topology_model_workflow_receipts,
    derive_auto_topology_resources,
    load_auto_topology_campaign,
    load_auto_topology_launch,
    load_auto_topology_result,
    render_auto_topology_task_contract,
    resolve_auto_topology_arm_paths,
    validate_auto_topology_evaluation,
    validate_auto_topology_result,
)
from .benchmark import load_parity_tasks
from .build_identity import current_runtime_build_sha256
from .errors import PolicyError, ValidationError
from .goal_loop import load_goal_state
from .model_orchestrator import MODEL_WORKFLOW_EXECUTE_APPROVAL
from .model_planner import MODEL_WORKFLOW_WRITE_APPROVAL
from .risk import risk_for_text
from .security import (
    RuntimePolicy,
    ensure_dir_no_follow,
    read_regular_file_bytes_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    write_new_text_file_no_follow,
)
from .staged_workspace import copy_workspace_to_stage, require_path_outside_workspace, snapshot_workspace


AUTO_TOPOLOGY_ARM_APPROVAL = "auto-topology-campaign-arm"
AUTO_TOPOLOGY_ARM_PLAN_SCHEMA = "conductor.auto_topology_arm_plan.v1"
AUTO_TOPOLOGY_ARM_RUN_SCHEMA = "conductor.auto_topology_arm_run.v1"


def run_auto_topology_arm(
    *,
    campaign_path: Path,
    parity_tasks_path: Path,
    cohort_id: Optional[str] = None,
    topology: Optional[str] = None,
    next_arm: bool = False,
    fixture_root: Optional[Path] = None,
    policy: Optional[RuntimePolicy] = None,
    resume: bool = False,
    dry_run: bool = False,
) -> Dict:
    context = _preflight(
        campaign_path=Path(campaign_path),
        parity_tasks_path=Path(parity_tasks_path),
        cohort_id=cohort_id,
        topology=topology,
        next_arm=next_arm,
        fixture_root=fixture_root,
        resume=resume,
    )
    if dry_run or context["arm_status"] == "completed":
        return _public_plan(context, dry_run=dry_run)
    if context["campaign"]["schema"] != AUTO_TOPOLOGY_CAMPAIGN_SCHEMA:
        raise ValidationError(
            "frozen auto topology campaign %s cannot launch under the current runtime; use its archived exact runtime or create a v3 campaign"
            % context["campaign"]["schema"]
        )
    if context["campaign"]["runtime_version"] != __version__:
        raise ValidationError(
            "auto topology campaign requires Conductor runtime %s; current runtime is %s"
            % (context["campaign"]["runtime_version"], __version__)
        )
    if context["campaign"]["runtime_build_sha256"] != context["runtime_build_sha256"]:
        raise ValidationError(
            "auto topology campaign requires exact runtime build %s; current build is %s"
            % (
                context["campaign"]["runtime_build_sha256"],
                context["runtime_build_sha256"],
            )
        )
    _require_policy(policy, context["topology"], context["execution_task"])

    launch, fresh = _open_or_resume_launch(context, resume=resume)
    launched = False
    if fresh or _launch_can_safely_continue(context, launch):
        result = run_auto_orchestration(
            task=context["execution_task"],
            workspace=context["paths"]["workspace"],
            runs_dir=context["paths"]["runs"],
            goals_dir=context["paths"]["goals"],
            policy=policy,
            strategy={"direct": "direct", "progressive": "progressive", "plan-first": "goal"}[
                context["topology"]
            ],
            check_command=context["check_command"],
            planner_model=context["campaign"]["model"]["name"],
            planner_effort=context["campaign"]["model"]["effort"],
            max_iterations=context["campaign"]["design"]["max_iterations"],
            max_workers=(
                1
                if context["topology"] == "direct"
                else context["campaign"]["design"]["parallel_workers"]
            ),
            planning_timeout_seconds=context["timeout_seconds"],
            check_timeout_seconds=context["check_timeout_seconds"],
            goal_id=context["goal_id"],
            name=context["goal_id"],
            receipt_path=context["paths"]["auto_receipt"],
            reuse_planner_session=True,
        )
        launched = True
        if result.status in {"routing", "running"}:
            raise ValidationError("auto topology foreground arm did not reach a terminal status")

    auto_receipt = load_auto_orchestration_receipt(context["paths"]["auto_receipt"])
    if auto_receipt["status"] in {"routing", "running"}:
        raise ValidationError(
            "auto topology arm outcome is unknown after an interrupted launch; refusing an automatic rerun"
        )
    goal = load_goal_state(context["paths"]["goal"]) if context["paths"]["goal"].is_file() else None
    capture_auto_topology_model_workflow_receipts(
        goal,
        workspace=context["paths"]["workspace"],
        planning_dir=context["paths"]["planning"],
    )
    launch["state"] = "captured"
    launch["auto_receipt_sha256"] = _sha256_file(context["paths"]["auto_receipt"])
    launch["goal_sha256"] = _sha256_file(context["paths"]["goal"]) if goal is not None else None
    launch["updated_at_utc"] = _now_utc()
    _save_launch(context["paths"]["launch"], launch, replace=True)

    if context["paths"]["result"].exists() or context["paths"]["result"].is_symlink():
        existing = load_auto_topology_result(context["paths"]["result"])
        validated = validate_auto_topology_result(
            existing,
            campaign_path=context["campaign_path"],
            campaign=context["campaign"],
            campaign_sha256=context["campaign_sha256"],
            cohort=context["cohort"],
            topology=context["topology"],
        )
        launch["state"] = "finalized"
        launch["updated_at_utc"] = _now_utc()
        _save_launch(context["paths"]["launch"], launch, replace=True)
        return _public_run_result(context, validated, launched=launched)

    evaluation = _load_or_create_evaluation(context, auto_receipt, goal)
    resources, run_records, model_workflow_records = derive_auto_topology_resources(
        goal,
        workspace=context["paths"]["workspace"],
        runs_dir=context["paths"]["runs"],
        planning_dir=context["paths"]["planning"],
        duration_ms=_receipt_duration_ms(auto_receipt, goal=goal),
    )
    result = _build_result(
        context,
        auto_receipt=auto_receipt,
        goal=goal,
        evaluation=evaluation,
        resources=resources,
        run_records=run_records,
        model_workflow_records=model_workflow_records,
    )
    write_new_text_file_no_follow(
        context["paths"]["result"],
        "auto topology arm result",
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        sync=True,
    )
    validated = validate_auto_topology_result(
        result,
        campaign_path=context["campaign_path"],
        campaign=context["campaign"],
        campaign_sha256=context["campaign_sha256"],
        cohort=context["cohort"],
        topology=context["topology"],
    )
    launch["state"] = "finalized"
    launch["updated_at_utc"] = _now_utc()
    _save_launch(context["paths"]["launch"], launch, replace=True)
    return _public_run_result(context, validated, launched=launched)


def _public_run_result(context: Dict, validated: Dict, *, launched: bool) -> Dict:
    return {
        "schema": AUTO_TOPOLOGY_ARM_RUN_SCHEMA,
        "campaign": context["campaign"]["name"],
        "cohort_id": context["cohort"]["id"],
        "task_id": context["cohort"]["task_id"],
        "topology": context["topology"],
        "status": "completed",
        "provider_launch_started": launched,
        "outcome": validated["outcome"],
        "resources": validated["resources"],
        "progressive": validated["progressive"],
    }


def _preflight(
    *,
    campaign_path: Path,
    parity_tasks_path: Path,
    cohort_id: Optional[str],
    topology: Optional[str],
    next_arm: bool,
    fixture_root: Optional[Path],
    resume: bool,
) -> Dict:
    explicit = cohort_id is not None or topology is not None
    if next_arm == explicit or (explicit and (cohort_id is None or topology is None)):
        raise ValidationError("select exactly one topology arm with --next or both cohort_id and topology")
    campaign = load_auto_topology_campaign(campaign_path)
    campaign_sha256 = auto_topology_campaign_sha256(campaign_path)
    tasks = load_parity_tasks(parity_tasks_path)
    public_tasks = {key: value for key, value in tasks.items() if not key.startswith("_")}
    if _sha256_json(public_tasks) != campaign["source"]["parity_tasks_sha256"]:
        raise ValidationError("auto topology task fixture does not match the preregistered campaign hash")
    if public_tasks["name"] != campaign["source"]["suite"]:
        raise ValidationError("auto topology task fixture suite does not match the campaign")
    status = auto_topology_campaign_status(campaign_path)
    if status["lifecycle"]["invalid"] or status["arm_lifecycle"]["invalid"]:
        raise ValidationError("auto topology campaign contains invalid evidence")
    status_by_cohort = {item["id"]: item for item in status["cohorts"]}
    if next_arm:
        allowed = {"partial"} if resume else {"pending"}
        selected = None
        for cohort in campaign["cohorts"]:
            cohort_status = status_by_cohort[cohort["id"]]
            for candidate in cohort["arm_order"]:
                if cohort_status["arms"][candidate]["status"] in allowed:
                    selected = (cohort, candidate, cohort_status["arms"][candidate]["status"])
                    break
            if selected is not None:
                break
        if selected is None:
            raise ValidationError("auto topology campaign has no selectable remaining arm")
        cohort, topology, arm_status = selected
    else:
        if topology not in AUTO_TOPOLOGIES:
            raise ValidationError("auto topology arm name is invalid")
        matches = [item for item in campaign["cohorts"] if item["id"] == cohort_id]
        if len(matches) != 1:
            raise ValidationError("auto topology cohort does not exist")
        cohort = matches[0]
        arm_status = status_by_cohort[cohort["id"]]["arms"][topology]["status"]
    if arm_status == "partial" and not resume:
        raise ValidationError("auto topology arm is partial; use --resume to avoid duplicate provider calls")
    if arm_status == "pending" and resume:
        raise ValidationError("auto topology arm has no launch to resume")
    if arm_status not in {"pending", "partial", "completed"}:
        raise ValidationError("auto topology arm cannot run from status %s" % arm_status)

    task_matches = [task for task in public_tasks["tasks"] if task["id"] == cohort["task_id"]]
    if len(task_matches) != 1:
        raise ValidationError("auto topology task is missing from the bound fixture")
    task = task_matches[0]
    if (
        _sha256_json(task) != cohort["task_contract_sha256"]
        or _sha256_text(task["prompt"].strip()) != cohort["task_prompt_sha256"]
        or task["budget"] != cohort["budget"]
    ):
        raise ValidationError("auto topology task contract does not match the fixture")
    execution_task = (
        render_auto_topology_task_contract(task)
        if campaign["schema"] == AUTO_TOPOLOGY_CAMPAIGN_SCHEMA
        else task["prompt"].strip()
    )
    if (
        campaign["schema"] == AUTO_TOPOLOGY_CAMPAIGN_SCHEMA
        and _sha256_text(execution_task) != cohort["task_execution_sha256"]
    ):
        raise ValidationError(
            "auto topology task execution contract does not match the campaign"
        )
    spec = TASK_SPECS.get(task["id"])
    if spec is None:
        raise ValidationError("auto topology task has no deterministic execution profile")
    root = Path(fixture_root) if fixture_root is not None else parity_tasks_path.parent / "implementation-canary"
    root = root.resolve()
    source = (root / spec["source"]).resolve()
    held_out = (root / spec["held_out"]).resolve()
    if not source.is_dir() or source.is_symlink():
        raise ValidationError("auto topology source fixture must be a real directory")
    if not held_out.is_dir() or held_out.is_symlink():
        raise ValidationError("auto topology held-out fixture must be a real directory")
    if (
        snapshot_workspace(source).fingerprint_sha256 != cohort["source_fixture_sha256"]
        or snapshot_workspace(held_out).fingerprint_sha256 != cohort["held_out_fixture_sha256"]
    ):
        raise ValidationError("auto topology source or held-out fixture drifted from the campaign")
    paths = resolve_auto_topology_arm_paths(campaign_path, cohort, topology)
    for path in paths.values():
        require_path_outside_workspace(source, path, "auto topology arm artifact")
        reject_symlink_path(path, "auto topology arm artifact")
    timeout_seconds = int(task["budget"]["max_minutes"]) * 60
    return {
        "campaign_path": campaign_path,
        "campaign": campaign,
        "campaign_sha256": campaign_sha256,
        "tasks": tasks,
        "task": task,
        "execution_task": execution_task,
        "cohort": cohort,
        "topology": topology,
        "arm_status": arm_status,
        "paths": paths,
        "fixture_root": root,
        "source": source,
        "goal_id": "%s-%s" % (cohort["id"], topology),
        "check_command": [
            "python3",
            "-B",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            spec["visible_pattern"],
            "-v",
        ],
        "timeout_seconds": timeout_seconds,
        "check_timeout_seconds": min(120, max(30, timeout_seconds // 4)),
        "runtime_build_sha256": current_runtime_build_sha256(),
    }


def _public_plan(context: Dict, *, dry_run: bool) -> Dict:
    campaign_runtime_version = context["campaign"].get("runtime_version")
    campaign_runtime_build_sha256 = context["campaign"].get("runtime_build_sha256")
    return {
        "schema": AUTO_TOPOLOGY_ARM_PLAN_SCHEMA,
        "campaign": context["campaign"]["name"],
        "cohort_id": context["cohort"]["id"],
        "sequence": context["cohort"]["sequence"],
        "task_id": context["cohort"]["task_id"],
        "repetition": context["cohort"]["repetition"],
        "topology": context["topology"],
        "arm_order": list(context["cohort"]["arm_order"]),
        "current_status": context["arm_status"],
        "runtime_version": __version__,
        "runtime_build_sha256": context["runtime_build_sha256"],
        "campaign_runtime_version": campaign_runtime_version,
        "campaign_runtime_build_sha256": campaign_runtime_build_sha256,
        "runtime_release_compatible": campaign_runtime_version == __version__,
        "runtime_build_compatible": (
            campaign_runtime_build_sha256 == context["runtime_build_sha256"]
        ),
        "model": dict(context["campaign"]["model"]),
        "limits": {
            "max_iterations": context["campaign"]["design"]["max_iterations"],
            "max_workers": (
                1
                if context["topology"] == "direct"
                else context["campaign"]["design"]["parallel_workers"]
            ),
        },
        "dry_run": dry_run,
        "provider_launches": 0 if dry_run or context["arm_status"] == "completed" else None,
    }


def _require_policy(policy: Optional[RuntimePolicy], topology: str, task_prompt: str) -> None:
    if policy is None:
        raise PolicyError("auto topology arm execution requires an explicit runtime policy")
    expected_parallel = topology != "direct"
    if (
        not policy.allow_agent
        or not policy.allow_writes
        or policy.allow_network
        or policy.allow_destructive
        or policy.allow_parallel is not expected_parallel
    ):
        raise PolicyError(
            "auto topology arm requires agent/write access, no network/destructive access, and topology-matched parallel access"
        )
    required = {
        AUTO_TOPOLOGY_ARM_APPROVAL,
        MODEL_WORKFLOW_EXECUTE_APPROVAL,
        MODEL_WORKFLOW_WRITE_APPROVAL,
    }
    missing = sorted(token for token in required if not policy.has_approval(token))
    if missing:
        raise PolicyError("auto topology arm execution requires approval(s): %s" % ", ".join(missing))
    if risk_for_text(task_prompt) == "high" and not policy.has_approval("high-risk"):
        raise PolicyError(
            "high-risk auto topology task requires --approve high-risk before a launch receipt is written"
        )


def _open_or_resume_launch(context: Dict, *, resume: bool):
    path = context["paths"]["launch"]
    source_snapshot = snapshot_workspace(context["source"])
    if path.exists() or path.is_symlink():
        if not resume:
            raise ValidationError("auto topology launch already exists; use --resume")
        launch = load_auto_topology_launch(path)
        if (
            launch["campaign_sha256"] != context["campaign_sha256"]
            or launch["cohort_id"] != context["cohort"]["id"]
            or launch["task_id"] != context["cohort"]["task_id"]
            or launch["topology"] != context["topology"]
            or launch["source_fingerprint_sha256"] != source_snapshot.fingerprint_sha256
        ):
            raise ValidationError("auto topology launch does not match the immutable campaign")
        return launch, False
    if resume:
        raise ValidationError("auto topology arm cannot resume without its launch receipt")
    root_fd = ensure_dir_no_follow(context["paths"]["root"], "auto topology arm root")
    os.close(root_fd)
    launch = {
        "schema": AUTO_TOPOLOGY_LAUNCH_SCHEMA,
        "campaign_sha256": context["campaign_sha256"],
        "cohort_id": context["cohort"]["id"],
        "task_id": context["cohort"]["task_id"],
        "topology": context["topology"],
        "state": "launching",
        "source_fingerprint_sha256": source_snapshot.fingerprint_sha256,
        "auto_receipt_sha256": None,
        "goal_sha256": None,
        "updated_at_utc": _now_utc(),
    }
    _save_launch(path, launch, replace=False)
    copy_workspace_to_stage(context["source"], context["paths"]["workspace"])
    return launch, True


def _launch_can_safely_continue(context: Dict, launch: Dict) -> bool:
    auto_path = context["paths"]["auto_receipt"]
    workspace_path = context["paths"]["workspace"]
    if launch["state"] != "launching":
        return False
    if auto_path.exists() or auto_path.is_symlink():
        receipt = load_auto_orchestration_receipt(auto_path)
        if receipt["status"] in {"routing", "running"}:
            raise ValidationError(
                "auto topology arm outcome is unknown after an interrupted launch; refusing an automatic rerun"
            )
        return False
    if not workspace_path.is_dir():
        copy_workspace_to_stage(context["source"], workspace_path)
        return True
    current = snapshot_workspace(workspace_path)
    if current.fingerprint_sha256 != launch["source_fingerprint_sha256"]:
        raise ValidationError("auto topology workspace changed without a captured auto receipt")
    return True


def _load_or_create_evaluation(context: Dict, auto_receipt: Dict, goal: Optional[Dict]) -> Dict:
    path = context["paths"]["evaluation"]
    if path.exists() or path.is_symlink():
        raw = read_regular_text_file_no_follow(path, "auto topology evaluation", 2 * 1024 * 1024)
        try:
            evaluation = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError("auto topology evaluation is invalid JSON: %s" % exc)
        validate_auto_topology_evaluation(evaluation, source=str(path))
        return evaluation
    orchestration_completed = bool(goal is not None and goal.get("status") == "completed" and auto_receipt["status"] == "completed")
    evaluation = evaluate_implementation_canary_workspace(
        context["task"]["id"],
        context["paths"]["workspace"],
        fixture_root=context["fixture_root"],
        orchestration_completed=orchestration_completed,
    )
    validate_auto_topology_evaluation(evaluation)
    write_new_text_file_no_follow(
        path,
        "auto topology evaluation",
        json.dumps(evaluation, indent=2, sort_keys=True) + "\n",
        sync=True,
    )
    return evaluation


def _build_result(
    context: Dict,
    *,
    auto_receipt: Dict,
    goal: Optional[Dict],
    evaluation: Dict,
    resources: Dict,
    run_records,
    model_workflow_records,
) -> Dict:
    evaluation_bytes = read_regular_file_bytes_no_follow(
        context["paths"]["evaluation"], "auto topology evaluation", 2 * 1024 * 1024
    )
    workspace_sha256 = snapshot_workspace(context["paths"]["workspace"]).fingerprint_sha256
    iterations = goal.get("iterations", []) if isinstance(goal, dict) else []
    return {
        "schema": AUTO_TOPOLOGY_RESULT_SCHEMA,
        "runtime_version": __version__,
        "runtime_build_sha256": context["runtime_build_sha256"],
        "campaign_sha256": context["campaign_sha256"],
        "cohort_id": context["cohort"]["id"],
        "task_id": context["cohort"]["task_id"],
        "repetition": context["cohort"]["repetition"],
        "topology": context["topology"],
        "generated_at_utc": _now_utc(),
        "status": "finalized",
        "outcome": {
            "auto_status": auto_receipt["status"],
            "execution_started": bool(auto_receipt["result"]["execution_started"]),
            "goal_satisfied": bool(
                goal is not None and goal.get("status") == "completed" and auto_receipt["status"] == "completed"
            ),
            "score": evaluation["score"],
            "max_score": evaluation["max_score"],
            "passed": evaluation["passed"],
        },
        "resources": resources,
        "progressive": {
            "escalated": context["topology"] == "progressive" and resources["planning_provider_calls"] > 0,
            "first_pass_success": bool(goal is not None and goal.get("status") == "completed" and len(iterations) == 1),
        },
        "artifacts": {
            "auto_receipt_sha256": _sha256_file(context["paths"]["auto_receipt"]),
            "goal_sha256": _sha256_file(context["paths"]["goal"]) if goal is not None else None,
            "evaluation_sha256": hashlib.sha256(evaluation_bytes).hexdigest(),
            "workspace_sha256": workspace_sha256,
            "runs": run_records,
            "model_workflow_receipts": model_workflow_records,
        },
    }


def _save_launch(path: Path, launch: Dict, *, replace: bool) -> None:
    text = json.dumps(launch, indent=2, sort_keys=True) + "\n"
    if replace:
        replace_text_file_no_follow(path, "auto topology arm launch", text, ".launch-", sync=True)
    else:
        write_new_text_file_no_follow(path, "auto topology arm launch", text, sync=True)


def _receipt_duration_ms(receipt: Dict, *, goal: Optional[Dict] = None) -> int:
    timestamp_pairs = []
    if isinstance(goal, dict):
        timestamp_pairs.append((goal.get("started_at_utc"), goal.get("finished_at_utc")))
    timestamp_pairs.append((receipt.get("generated_at_utc"), receipt.get("updated_at_utc")))
    for started_text, finished_text in timestamp_pairs:
        try:
            started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
            finished = datetime.fromisoformat(finished_text.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            continue
        return max(0, int((finished - started).total_seconds() * 1000))
    return 0


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(read_regular_file_bytes_no_follow(path, "auto topology artifact", 2 * 1024 * 1024)).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
