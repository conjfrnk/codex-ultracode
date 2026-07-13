import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Dict, Optional

from tools.evaluate_readonly_diagnostic import (
    TASK_SPECS,
    evaluate_readonly_diagnostic,
    load_readonly_diagnostic_evaluation,
    write_readonly_diagnostic_evaluation,
)

from . import __version__
from .artifacts import RunArtifacts
from .benchmark import (
    load_benchmark_report,
    load_parity_tasks,
    render_parity_task_contract,
)
from .build_identity import current_runtime_build_sha256
from .claude_live import CLAUDE_PAID_RUN_APPROVAL, run_claude_readonly_task
from .codex_checkpoint import load_codex_step_checkpoint_from_run_dir
from .codex_stream import parse_codex_stream
from .codex_native_usage import NativeUsageUnavailable, reconcile_codex_session_usage
from .codex_progress import codex_step_progress_path_from_run_dir, load_codex_progress
from .errors import PolicyError, ValidationError
from .model_orchestrator import (
    MODEL_WORKFLOW_EXECUTE_APPROVAL,
    load_model_workflow_run_receipt,
    run_model_workflow,
)
from .readonly_parity_campaign import (
    MAX_RESULT_BYTES,
    READONLY_PARITY_CLAUDE_MAX_TURNS,
    READONLY_PARITY_LAUNCH_SCHEMA,
    READONLY_PARITY_NATIVE_THREADS,
    READONLY_PARITY_RESULT_SCHEMA,
    READONLY_PARITY_SYSTEMS,
    load_readonly_parity_campaign,
    load_readonly_parity_launch,
    load_readonly_parity_result,
    readonly_parity_campaign_sha256,
    readonly_parity_campaign_status,
    resolve_readonly_arm_paths,
    validate_readonly_parity_launch,
    validate_readonly_parity_result,
)
from .redaction import redact_text
from .risk import risk_for_text
from .security import (
    NATIVE_AGENT_DELEGATION_APPROVAL,
    RuntimePolicy,
    ensure_dir_no_follow,
    read_regular_file_bytes_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    write_new_text_file_no_follow,
)
from .staged_workspace import copy_workspace_to_stage, require_path_outside_workspace, snapshot_workspace


READONLY_PARITY_ARM_APPROVAL = "readonly-parity-campaign-arm"
READONLY_PARITY_ARM_PLAN_SCHEMA = "conductor.readonly_parity_arm_plan.v1"
READONLY_PARITY_ARM_RUN_SCHEMA = "conductor.readonly_parity_arm_run.v1"
MAX_ANSWER_BYTES = 1024 * 1024
MAX_PROVIDER_REPORT_BYTES = 4 * 1024 * 1024


def run_readonly_parity_arm(
    *,
    campaign_path: Path,
    parity_tasks_path: Path,
    cohort_id: Optional[str] = None,
    system: Optional[str] = None,
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
        system=system,
        next_arm=next_arm,
        fixture_root=fixture_root,
        resume=resume,
    )
    plan = _public_plan(context, dry_run=dry_run)
    if dry_run or context["arm_status"] == "completed":
        return plan
    if context["campaign"]["runtime_version"] != __version__:
        raise ValidationError(
            "read-only parity campaign requires Conductor runtime %s; current runtime is %s"
            % (context["campaign"]["runtime_version"], __version__)
        )
    if context["campaign"]["runtime_build_sha256"] != context["runtime_build_sha256"]:
        raise ValidationError(
            "read-only parity campaign requires exact runtime build %s; current build is %s"
            % (context["campaign"]["runtime_build_sha256"], context["runtime_build_sha256"])
        )
    _require_policy(context, policy)
    _require_provider_tool(context["system"])

    launch, fresh = _open_or_resume_launch(context, resume=resume)
    _prepare_workspace(context, launch)
    launched = _capture_provider(context, launch, policy)
    result = _finalize(context, launch)
    status = readonly_parity_campaign_status(context["campaign_path"])
    cohort_status = next(item for item in status["cohorts"] if item["id"] == context["cohort"]["id"])
    arm_status = cohort_status["arms"][context["system"]]
    if arm_status["status"] != "completed":
        raise ValidationError("read-only parity arm evidence did not finalize cleanly")
    return {
        "schema": READONLY_PARITY_ARM_RUN_SCHEMA,
        "campaign": context["campaign"]["name"],
        "cohort_id": context["cohort"]["id"],
        "task_id": context["cohort"]["task_id"],
        "system": context["system"],
        "status": "completed",
        "provider_launch_started": launched,
        "provider_launch_reused": not launched and not fresh,
        "score": result["outcome"]["score"],
        "provider_success": result["outcome"]["provider_success"],
        "final_success": result["outcome"]["final_success"],
        "topology_executed": result["topology"]["topology_executed"],
        "duration_ms": result["resources"]["duration_ms"],
        "cap_compliant": result["resources"]["cap_compliant"],
        "cap_overshoot": result["resources"]["cap_overshoot"],
        "weighted_tokens": result["resources"]["weighted_tokens"],
        "cost_usd": result["resources"]["cost_usd"],
    }


def _preflight(
    *,
    campaign_path,
    parity_tasks_path,
    cohort_id,
    system,
    next_arm,
    fixture_root,
    resume,
):
    explicit = cohort_id is not None or system is not None
    if next_arm == explicit or (explicit and (cohort_id is None or system is None)):
        raise ValidationError("select exactly one read-only parity arm with --next or both cohort_id and system")
    campaign = load_readonly_parity_campaign(campaign_path)
    campaign_sha256 = readonly_parity_campaign_sha256(campaign_path)
    tasks = load_parity_tasks(parity_tasks_path)
    public_tasks = _public_tasks(tasks)
    if _sha256_json(public_tasks) != campaign["source"]["parity_tasks_sha256"]:
        raise ValidationError("read-only parity task fixture does not match the campaign hash")
    if public_tasks["name"] != campaign["source"]["suite"]:
        raise ValidationError("read-only parity task suite does not match the campaign")
    status = readonly_parity_campaign_status(campaign_path)
    if status["lifecycle"]["invalid"]:
        raise ValidationError("read-only parity campaign contains invalid evidence")
    status_by_id = {item["id"]: item for item in status["cohorts"]}
    if next_arm:
        candidate = None
        for cohort in campaign["cohorts"]:
            for candidate_system in cohort["arm_order"]:
                arm_status = status_by_id[cohort["id"]]["arms"][candidate_system]["status"]
                if arm_status == "pending" or (resume and arm_status == "partial"):
                    candidate = (cohort, candidate_system)
                    break
            if candidate is not None:
                break
        if candidate is None:
            raise ValidationError("read-only parity campaign has no selectable remaining arm")
        cohort, selected_system = candidate
    else:
        if system not in READONLY_PARITY_SYSTEMS:
            raise ValidationError("read-only parity system is invalid")
        matches = [item for item in campaign["cohorts"] if item["id"] == cohort_id]
        if len(matches) != 1:
            raise ValidationError("read-only parity cohort does not exist: %s" % cohort_id)
        cohort = matches[0]
        selected_system = system
    arm_status = status_by_id[cohort["id"]]["arms"][selected_system]["status"]
    if arm_status == "partial" and not resume:
        raise ValidationError("read-only parity arm is partial; use --resume to avoid duplicate provider calls")
    if arm_status not in {"pending", "partial", "completed"}:
        raise ValidationError("read-only parity arm cannot run from status %s" % arm_status)
    task_matches = [item for item in public_tasks["tasks"] if item["id"] == cohort["task_id"]]
    if len(task_matches) != 1:
        raise ValidationError("read-only parity task is missing from the bound fixture")
    task = task_matches[0]
    if _sha256_json(task) != cohort["task_contract_sha256"] or task["budget"] != cohort["budget"]:
        raise ValidationError("read-only parity task contract or budget drifted")
    task_contract = render_parity_task_contract(task)
    if hashlib.sha256(task_contract.encode("utf-8")).hexdigest() != cohort["rendered_task_sha256"]:
        raise ValidationError("read-only parity rendered task contract drifted")
    resolved_fixture_root = (
        Path(fixture_root).resolve()
        if fixture_root is not None
        else (parity_tasks_path.resolve().parent / "implementation-canary").resolve()
    )
    source = resolved_fixture_root / TASK_SPECS[task["id"]]["source"]
    if not source.is_dir() or source.is_symlink():
        raise ValidationError("read-only parity source fixture is missing")
    source_sha256 = snapshot_workspace(source).tracked_fingerprint_sha256
    if source_sha256 != cohort["source_fixture_sha256"]:
        raise ValidationError("read-only parity source fixture drifted")
    paths = resolve_readonly_arm_paths(campaign_path, cohort, selected_system)
    for key, path in paths.items():
        require_path_outside_workspace(source, path, "read-only parity %s" % key)
        reject_symlink_path(path, "read-only parity %s" % key)
    if arm_status == "pending" and not resume:
        unexpected = [
            key for key, path in paths.items()
            if key != "root" and (path.exists() or path.is_symlink())
        ]
        if unexpected:
            raise ValidationError("read-only parity arm has unaccounted artifacts")
    return {
        "campaign_path": campaign_path,
        "campaign_sha256": campaign_sha256,
        "campaign": campaign,
        "tasks": tasks,
        "task": task,
        "cohort": cohort,
        "system": selected_system,
        "arm_status": arm_status,
        "source": source,
        "source_sha256": source_sha256,
        "paths": paths,
        "run_id": "%s-%s" % (cohort["id"], selected_system),
        "task_contract": task_contract,
        "runtime_build_sha256": current_runtime_build_sha256(),
    }


def _public_plan(context, *, dry_run):
    system = _system_config(context["campaign"], context["system"])
    required_approvals = [READONLY_PARITY_ARM_APPROVAL]
    if risk_for_text(context["task_contract"]) == "high":
        required_approvals.append("high-risk")
    if context["system"] == "claude-sonnet":
        required_approvals.append(CLAUDE_PAID_RUN_APPROVAL)
    else:
        required_approvals.append(MODEL_WORKFLOW_EXECUTE_APPROVAL)
        if context["system"] == "native-codex":
            required_approvals.append(NATIVE_AGENT_DELEGATION_APPROVAL)
    return {
        "schema": READONLY_PARITY_ARM_PLAN_SCHEMA,
        "campaign": context["campaign"]["name"],
        "cohort_id": context["cohort"]["id"],
        "sequence": context["cohort"]["sequence"],
        "task_id": context["cohort"]["task_id"],
        "repetition": context["cohort"]["repetition"],
        "system": context["system"],
        "current_status": context["arm_status"],
        "model": system["model"],
        "effort": system["effort"],
        "token_cap": system["max_tokens"],
        "cost_cap_usd": context["cohort"]["budget"]["max_cost_usd"] if context["system"] == "claude-sonnet" else None,
        "required_approvals": required_approvals,
        "runtime_version": __version__,
        "runtime_build_sha256": context["runtime_build_sha256"],
        "campaign_runtime_version": context["campaign"]["runtime_version"],
        "campaign_runtime_build_sha256": context["campaign"]["runtime_build_sha256"],
        "runtime_release_compatible": context["campaign"]["runtime_version"] == __version__,
        "runtime_build_compatible": (
            context["campaign"]["runtime_build_sha256"] == context["runtime_build_sha256"]
        ),
        "dry_run": dry_run,
        "provider_launches": 0 if dry_run or context["arm_status"] == "completed" else 1,
    }


def _require_policy(context, policy):
    if policy is None:
        raise PolicyError("read-only parity execution requires an explicit runtime policy")
    if not policy.allow_agent:
        raise PolicyError("read-only parity execution requires --allow-agent")
    if policy.allow_writes or policy.allow_destructive:
        raise PolicyError("read-only parity execution forbids write and destructive capabilities")
    required = {READONLY_PARITY_ARM_APPROVAL}
    if risk_for_text(context["task_contract"]) == "high":
        required.add("high-risk")
    if context["system"] == "claude-sonnet":
        if not policy.allow_network:
            raise PolicyError("Claude read-only parity requires --allow-network")
        required.add(CLAUDE_PAID_RUN_APPROVAL)
    else:
        required.add(MODEL_WORKFLOW_EXECUTE_APPROVAL)
        if context["system"] == "native-codex":
            if not policy.allow_parallel:
                raise PolicyError("native Codex parity requires --allow-parallel")
            required.add(NATIVE_AGENT_DELEGATION_APPROVAL)
    missing = sorted(token for token in required if not policy.has_approval(token))
    if missing:
        raise PolicyError("read-only parity execution requires approval(s): %s" % ", ".join(missing))


def _require_provider_tool(system):
    executable = "claude" if system == "claude-sonnet" else "codex"
    if shutil.which(executable) is None:
        raise ValidationError("read-only parity arm requires %s on PATH" % executable)


def _open_or_resume_launch(context, *, resume):
    path = context["paths"]["launch"]
    if path.exists() or path.is_symlink():
        if not resume:
            raise ValidationError("read-only parity launch already exists; use --resume")
        launch = load_readonly_parity_launch(path)
        _validate_launch_context(context, launch)
        return launch, False
    if resume:
        raise ValidationError("read-only parity arm cannot resume without its launch receipt")
    root_fd = ensure_dir_no_follow(context["paths"]["root"], "read-only parity arm root")
    os.close(root_fd)
    launch = {
        "schema": READONLY_PARITY_LAUNCH_SCHEMA,
        "campaign_sha256": context["campaign_sha256"],
        "cohort_id": context["cohort"]["id"],
        "task_id": context["cohort"]["task_id"],
        "system": context["system"],
        "state": "preparing",
        "source_fixture_sha256": context["source_sha256"],
        "workspace_sha256": None,
        "provider_artifact_sha256": None,
        "answer_sha256": None,
        "result_sha256": None,
        "updated_at_utc": _now_utc(),
    }
    _save_launch(path, launch, replace=False)
    return launch, True


def _validate_launch_context(context, launch):
    expected = (
        context["campaign_sha256"],
        context["cohort"]["id"],
        context["cohort"]["task_id"],
        context["system"],
        context["source_sha256"],
    )
    observed = (
        launch["campaign_sha256"],
        launch["cohort_id"],
        launch["task_id"],
        launch["system"],
        launch["source_fixture_sha256"],
    )
    if observed != expected:
        raise ValidationError("read-only parity launch does not match its immutable campaign arm")


def _prepare_workspace(context, launch):
    workspace = context["paths"]["workspace"]
    if launch["state"] == "preparing":
        if workspace.exists() or workspace.is_symlink():
            if not workspace.is_dir() or workspace.is_symlink():
                raise ValidationError("read-only parity workspace is invalid")
            workspace_sha256 = snapshot_workspace(workspace).tracked_fingerprint_sha256
        else:
            copy_workspace_to_stage(context["source"], workspace)
            workspace_sha256 = snapshot_workspace(workspace).tracked_fingerprint_sha256
        if workspace_sha256 != context["source_sha256"]:
            raise ValidationError("read-only parity workspace copy is incomplete or drifted")
        launch["state"] = "ready"
        launch["workspace_sha256"] = workspace_sha256
        launch["updated_at_utc"] = _now_utc()
        _save_launch(context["paths"]["launch"], launch, replace=True)
    elif launch["workspace_sha256"] != snapshot_workspace(workspace).tracked_fingerprint_sha256:
        raise ValidationError("read-only parity workspace drifted after preparation")


def _capture_provider(context, launch, policy):
    if launch["state"] in {"captured", "finalized"}:
        return False
    if launch["state"] == "launching":
        if not _provider_artifact_path(context).is_file():
            raise ValidationError(
                "read-only parity provider outcome is unknown after interruption; refusing an automatic rerun"
            )
        answer = _recover_provider_answer(context)
        _write_or_validate_answer(context["paths"]["answer"], answer)
        _mark_captured(context, launch)
        return False
    if launch["state"] != "ready":
        raise ValidationError("read-only parity launch is not ready for provider execution")
    launch["state"] = "launching"
    launch["updated_at_utc"] = _now_utc()
    _save_launch(context["paths"]["launch"], launch, replace=True)
    if context["system"] == "claude-sonnet":
        report = run_claude_readonly_task(
            parity_tasks=context["tasks"],
            task_id=context["task"]["id"],
            workspace=context["paths"]["workspace"],
            policy=_narrow_policy(policy, context["system"]),
            max_budget_usd=context["cohort"]["budget"]["max_cost_usd"],
            max_turns=READONLY_PARITY_CLAUDE_MAX_TURNS,
            timeout_seconds=context["cohort"]["budget"]["max_minutes"] * 60,
            output_limit_bytes=MAX_ANSWER_BYTES,
        )
        write_new_text_file_no_follow(
            context["paths"]["provider_report"],
            "read-only parity Claude report",
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            sync=True,
        )
        answer = report["results"][0]["stdout"]
    else:
        system = _system_config(context["campaign"], context["system"])
        result = run_model_workflow(
            task=context["task_contract"],
            workspace=context["paths"]["workspace"],
            output_path=context["paths"]["workflow"],
            planning_receipt_path=context["paths"]["planning_receipt"],
            run_receipt_path=context["paths"]["run_receipt"],
            runs_dir=context["paths"]["runs"],
            policy=_narrow_policy(policy, context["system"]),
            name=context["run_id"],
            model=system["model"],
            planner_effort=system["effort"],
            write_capable=False,
            max_workers=1,
            planning_timeout_seconds=context["cohort"]["budget"]["max_minutes"] * 60,
            run_id=context["run_id"],
            dry_run=False,
            background=False,
            direct_plan=True,
            direct_native_agent_threads=(
                READONLY_PARITY_NATIVE_THREADS if context["system"] == "native-codex" else None
            ),
            direct_max_tokens=system["max_tokens"],
        )
        if result.status == "running":
            raise ValidationError("read-only parity foreground Codex arm did not terminate")
        answer = _codex_answer(context)
    _write_or_validate_answer(context["paths"]["answer"], answer)
    _mark_captured(context, launch)
    return True


def _recover_provider_answer(context):
    if context["system"] == "claude-sonnet":
        report = load_benchmark_report(context["paths"]["provider_report"])
        _validate_claude_report(context, report)
        return report["results"][0]["stdout"]
    load_model_workflow_run_receipt(context["paths"]["run_receipt"])
    return _codex_answer(context)


def _mark_captured(context, launch):
    provider_path = _provider_artifact_path(context)
    launch["state"] = "captured"
    launch["provider_artifact_sha256"] = _sha256_file(provider_path, MAX_PROVIDER_REPORT_BYTES)
    launch["answer_sha256"] = _sha256_file(context["paths"]["answer"], MAX_ANSWER_BYTES)
    launch["updated_at_utc"] = _now_utc()
    _save_launch(context["paths"]["launch"], launch, replace=True)


def _finalize(context, launch):
    if launch["state"] == "finalized":
        result = load_readonly_parity_result(context["paths"]["result"])
        _validate_result_context(context, result)
        return result
    if launch["state"] != "captured":
        raise ValidationError("read-only parity arm cannot finalize before provider capture")
    if context["paths"]["result"].exists() or context["paths"]["result"].is_symlink():
        result = load_readonly_parity_result(context["paths"]["result"])
        _validate_result_context(context, result)
    else:
        answer = read_regular_text_file_no_follow(
            context["paths"]["answer"], "read-only parity answer", MAX_ANSWER_BYTES
        )
        provider = _provider_outcome(context)
        workspace_after = snapshot_workspace(context["paths"]["workspace"]).tracked_fingerprint_sha256
        if context["paths"]["evaluation"].exists() or context["paths"]["evaluation"].is_symlink():
            evaluation = load_readonly_diagnostic_evaluation(context["paths"]["evaluation"])
        else:
            evaluation = evaluate_readonly_diagnostic(
                context["task"]["id"],
                answer,
                context["source"],
                expected_source_sha256=context["source_sha256"],
                provider_success=provider["success"],
            )
            write_readonly_diagnostic_evaluation(evaluation, context["paths"]["evaluation"])
        answer_sha256 = _sha256_file(context["paths"]["answer"], MAX_ANSWER_BYTES)
        if (
            evaluation["answer_sha256"] != answer_sha256
            or evaluation["provider_success"] != provider["success"]
            or evaluation["task_id"] != context["task"]["id"]
        ):
            raise ValidationError("read-only parity evaluation does not match captured output")
        source_preserved = workspace_after == launch["workspace_sha256"]
        artifacts = _artifact_hashes(context, workspace_after)
        result = {
            "schema": READONLY_PARITY_RESULT_SCHEMA,
            "campaign_sha256": context["campaign_sha256"],
            "cohort_id": context["cohort"]["id"],
            "task_id": context["task"]["id"],
            "repetition": context["cohort"]["repetition"],
            "system": context["system"],
            "runtime_version": __version__,
            "runtime_build_sha256": context["runtime_build_sha256"],
            "generated_at_utc": _now_utc(),
            "status": "completed",
            "outcome": {
                "provider_status": "success" if provider["success"] else "failed",
                "provider_success": provider["success"],
                "source_preserved": source_preserved,
                "answer_valid": evaluation["parse_status"] != "invalid",
                "score": evaluation["score"],
                "max_score": 10,
                "passed": evaluation["passed"],
                "final_success": bool(evaluation["passed"] and source_preserved),
            },
            "resources": provider["resources"],
            "topology": provider["topology"],
            "artifacts": artifacts,
        }
        validate_readonly_parity_result(result)
        write_new_text_file_no_follow(
            context["paths"]["result"],
            "read-only parity arm result",
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            sync=True,
        )
    launch["state"] = "finalized"
    launch["result_sha256"] = _sha256_file(context["paths"]["result"], MAX_RESULT_BYTES)
    launch["updated_at_utc"] = _now_utc()
    _save_launch(context["paths"]["launch"], launch, replace=True)
    return result


def _provider_outcome(context):
    if context["system"] == "claude-sonnet":
        report = load_benchmark_report(context["paths"]["provider_report"])
        _validate_claude_report(context, report)
        result = report["results"][0]
        evidence = result["provider_evidence"]
        observed = evidence["observed"]
        token_values = [
            observed.get("input_tokens"),
            observed.get("cache_creation_input_tokens"),
            observed.get("cache_read_input_tokens"),
            observed.get("output_tokens"),
            observed.get("total_tokens"),
        ]
        token_observed = all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in token_values)
        if token_observed:
            input_tokens = token_values[0] + token_values[1] + token_values[2]
            cached_input_tokens = token_values[2]
            output_tokens = token_values[3]
            gross_tokens = token_values[4]
            if gross_tokens != input_tokens + output_tokens:
                token_observed = False
        if not token_observed:
            input_tokens = cached_input_tokens = output_tokens = gross_tokens = None
        cost = observed.get("cost_usd")
        cost_observed = isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0
        cost_cap = context["cohort"]["budget"]["max_cost_usd"]
        cap_overshoot = max(0.0, float(cost) - cost_cap) if cost_observed else None
        cap_compliant = cap_overshoot == 0 if cap_overshoot is not None else None
        helpers = observed.get("helper_models") if isinstance(observed.get("helper_models"), list) else []
        return {
            "success": evidence["status"] == "success" and cap_compliant is not False,
            "resources": {
                "duration_ms": report["duration_ms"],
                "provider_invocations": 1,
                "provider_turns": observed.get("turns") if isinstance(observed.get("turns"), int) else None,
                "token_accounting": "provider-native-gross-v1",
                "token_observed": token_observed,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
                "gross_tokens": gross_tokens,
                "weighted_tokens": None,
                "cost_observed": cost_observed,
                "cost_usd": float(cost) if cost_observed else None,
                "cap_enforcement": "provider-dollar-threshold-fail-closed",
                "token_cap": None,
                "cost_cap_usd": cost_cap,
                "cap_compliant": cap_compliant,
                "cap_overshoot": cap_overshoot,
            },
            "topology": {
                "kind": "provider-managed",
                "requested_child_threads": None,
                "observed_child_threads": None,
                "topology_executed": True,
                "helper_models": sorted(set(helpers)),
            },
        }
    receipt = load_model_workflow_run_receipt(context["paths"]["run_receipt"])
    run_dir = context["paths"]["runs"] / context["run_id"]
    step = {}
    if run_dir.is_dir() and not run_dir.is_symlink():
        state = RunArtifacts.resume(run_dir).read_state()
        steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
        step = steps.get("complete-task") if isinstance(steps.get("complete-task"), dict) else {}
    invocations = step.get("agent_invocations", 0)
    if isinstance(invocations, bool) or not isinstance(invocations, int) or invocations < 0:
        raise ValidationError("read-only parity Codex invocation count is invalid")
    duration_ms = step.get("duration_ms", 0)
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
        raise ValidationError("read-only parity Codex duration is invalid")
    usage = None
    if run_dir.is_dir():
        try:
            checkpoint = load_codex_step_checkpoint_from_run_dir(run_dir, "complete-task")
            usage = reconcile_codex_session_usage(
                checkpoint["session_id"],
                context["paths"]["workspace"],
            )
        except (FileNotFoundError, OSError, NativeUsageUnavailable, ValidationError):
            usage = None
    token_observed = usage is not None and usage["status"] == "complete"
    weighted_tokens = usage["rollout_tokens"] if token_observed else None
    token_cap = _system_config(context["campaign"], context["system"])["max_tokens"]
    cap_overshoot = (
        max(0, weighted_tokens - token_cap)
        if weighted_tokens is not None
        else None
    )
    cap_compliant = cap_overshoot == 0 if cap_overshoot is not None else None
    child_count = usage["child_count"] if token_observed else 0
    if context["system"] == "serial-codex" and child_count != 0:
        raise ValidationError("serial Codex arm unexpectedly spawned a child session")
    return {
        "success": receipt["status"] == "completed" and cap_compliant is not False,
        "resources": {
            "duration_ms": duration_ms,
            "provider_invocations": invocations,
            "provider_turns": None,
            "token_accounting": "local-rollout-aggregate-v1",
            "token_observed": token_observed,
            "input_tokens": usage["input_tokens"] if token_observed else None,
            "cached_input_tokens": usage["cached_input_tokens"] if token_observed else None,
            "output_tokens": usage["output_tokens"] if token_observed else None,
            "gross_tokens": usage["total_tokens"] if token_observed else None,
            "weighted_tokens": weighted_tokens,
            "cost_observed": False,
            "cost_usd": None,
            "cap_enforcement": "runtime-weighted-threshold-fail-closed",
            "token_cap": token_cap,
            "cost_cap_usd": None,
            "cap_compliant": cap_compliant,
            "cap_overshoot": cap_overshoot,
        },
        "topology": {
            "kind": "native-depth-1" if context["system"] == "native-codex" else "serial",
            "requested_child_threads": READONLY_PARITY_NATIVE_THREADS if context["system"] == "native-codex" else 0,
            "observed_child_threads": child_count,
            "topology_executed": child_count >= 1 if context["system"] == "native-codex" else True,
            "helper_models": [],
        },
    }


def _artifact_hashes(context, workspace_after):
    paths = context["paths"]
    codex = context["system"] in {"serial-codex", "native-codex"}
    run_dir = paths["runs"] / context["run_id"]
    progress = codex_step_progress_path_from_run_dir(run_dir, "complete-task")
    if progress.exists() or progress.is_symlink():
        load_codex_progress(progress)
    return {
        "source_fixture_sha256": context["source_sha256"],
        "workspace_before_sha256": context["source_sha256"],
        "workspace_after_sha256": workspace_after,
        "answer_sha256": _sha256_file(paths["answer"], MAX_ANSWER_BYTES),
        "evaluation_sha256": _sha256_file(paths["evaluation"], MAX_RESULT_BYTES),
        "workflow_sha256": _optional_sha256(paths["workflow"], MAX_RESULT_BYTES) if codex else None,
        "planning_receipt_sha256": _optional_sha256(paths["planning_receipt"], MAX_RESULT_BYTES) if codex else None,
        "run_receipt_sha256": _sha256_file(paths["run_receipt"], MAX_RESULT_BYTES) if codex else None,
        "provider_report_sha256": _sha256_file(paths["provider_report"], MAX_PROVIDER_REPORT_BYTES) if not codex else None,
        "run_state_sha256": _optional_sha256(run_dir / "state.json", MAX_RESULT_BYTES) if codex else None,
        "codex_progress_sha256": _optional_sha256(progress, MAX_RESULT_BYTES) if codex else None,
        "codex_stream_sha256": _optional_sha256(
            run_dir / "logs" / "complete-task.stdout.log",
            MAX_PROVIDER_REPORT_BYTES,
        ) if codex else None,
    }


def _validate_result_context(context, result):
    expected = (
        context["campaign_sha256"],
        context["cohort"]["id"],
        context["task"]["id"],
        context["cohort"]["repetition"],
        context["system"],
        context["campaign"]["runtime_version"],
        context["campaign"]["runtime_build_sha256"],
    )
    observed = (
        result["campaign_sha256"],
        result["cohort_id"],
        result["task_id"],
        result["repetition"],
        result["system"],
        result["runtime_version"],
        result["runtime_build_sha256"],
    )
    if observed != expected:
        raise ValidationError("read-only parity result does not match its campaign arm")
    if context["system"] in {"serial-codex", "native-codex"}:
        expected_cap = _system_config(context["campaign"], context["system"])["max_tokens"]
        if result["resources"]["token_cap"] != expected_cap:
            raise ValidationError("read-only parity result token cap does not match its campaign arm")
    elif result["resources"]["cost_cap_usd"] != context["cohort"]["budget"]["max_cost_usd"]:
        raise ValidationError("read-only parity result cost cap does not match its campaign arm")


def _validate_claude_report(context, report):
    if (
        report.get("suite") != context["campaign"]["source"]["suite"]
        or report.get("system") != "claude-sonnet-ultracode"
        or report.get("total_tasks") != 1
        or len(report.get("results", [])) != 1
        or report["results"][0].get("id") != context["task"]["id"]
    ):
        raise ValidationError("Claude report does not match the read-only parity arm")


def _narrow_policy(policy, system):
    return RuntimePolicy(
        allow_writes=False,
        allow_destructive=False,
        allow_network=system == "claude-sonnet",
        allow_agent=True,
        allow_parallel=system == "native-codex",
        approvals=set(policy.approvals),
        approval_sha256=set(policy.approval_sha256),
    )


def _codex_answer(context):
    run_dir = context["paths"]["runs"] / context["run_id"]
    result_path = run_dir / "artifacts" / "direct" / "result.md"
    result_text = None
    if result_path.exists() or result_path.is_symlink():
        result_text = read_regular_text_file_no_follow(
            result_path,
            "read-only parity Codex answer",
            MAX_ANSWER_BYTES,
        )
    stream_path = run_dir / "logs" / "complete-task.stdout.log"
    if stream_path.exists() or stream_path.is_symlink():
        stream = read_regular_text_file_no_follow(
            stream_path,
            "read-only parity Codex stream",
            MAX_PROVIDER_REPORT_BYTES,
        )
        parsed = parse_codex_stream(stream)
        if result_text:
            return result_text
        return parsed["output_text"]
    return result_text or ""


def _provider_artifact_path(context):
    return context["paths"]["provider_report"] if context["system"] == "claude-sonnet" else context["paths"]["run_receipt"]


def _write_or_validate_answer(path, answer):
    clean = redact_text(str(answer))
    if len(clean.encode("utf-8")) > MAX_ANSWER_BYTES:
        raise ValidationError("read-only parity answer exceeds the supported size")
    if path.exists() or path.is_symlink():
        observed = read_regular_text_file_no_follow(path, "read-only parity answer", MAX_ANSWER_BYTES)
        if observed != clean:
            raise ValidationError("read-only parity answer changed after capture")
        return
    write_new_text_file_no_follow(path, "read-only parity answer", clean, sync=True)


def _save_launch(path, launch, *, replace):
    validate_readonly_parity_launch(launch, source=str(path))
    text = json.dumps(launch, indent=2, sort_keys=True) + "\n"
    if replace:
        replace_text_file_no_follow(path, "read-only parity arm launch", text, ".launch-", sync=True)
    else:
        write_new_text_file_no_follow(path, "read-only parity arm launch", text, sync=True)


def _system_config(campaign, name):
    matches = [item for item in campaign["systems"] if item["name"] == name]
    if len(matches) != 1:
        raise ValidationError("read-only parity campaign system configuration is invalid")
    return matches[0]


def _optional_sha256(path, maximum):
    if not path.exists() and not path.is_symlink():
        return None
    return _sha256_file(path, maximum)


def _sha256_file(path, maximum):
    return hashlib.sha256(
        read_regular_file_bytes_no_follow(path, "read-only parity artifact", maximum)
    ).hexdigest()


def _sha256_json(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _public_tasks(tasks):
    return {key: value for key, value in tasks.items() if not str(key).startswith("_")}


def _now_utc():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
