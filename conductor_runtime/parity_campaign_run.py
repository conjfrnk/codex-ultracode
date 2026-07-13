import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from tools.evaluate_implementation_canary import (
    SUITE as IMPLEMENTATION_CANARY_SUITE,
    TASK_SPECS,
    evaluate_implementation_canary,
)

from . import __version__
from .benchmark import (
    build_run_manifest_from_report,
    load_benchmark_report,
    load_parity_tasks,
    load_run_manifest,
    validate_run_manifest_matches_report,
    write_run_manifest,
)
from .benchmark_quality import load_benchmark_score_input, score_benchmark_report
from .claude_live import CLAUDE_PAID_RUN_APPROVAL
from .claude_staged import CLAUDE_STAGED_WRITE_APPROVAL, run_claude_staged_task
from .codex_live import CODEX_LIVE_RUN_APPROVAL, DEFAULT_CODEX_TOOL_OUTPUT_TOKEN_LIMIT
from .codex_staged import CODEX_STAGED_WRITE_APPROVAL, run_codex_staged_task
from .errors import PolicyError, ValidationError
from .effort_policy import AUTO_EFFORT_POLICY_V1, resolve_auto_reasoning_effort
from .parity_campaign import (
    DESIGN_EFFORT_POLICY_FIELD,
    PARITY_CAMPAIGN_SCHEMA,
    PARITY_CAMPAIGN_SCHEMA_V3,
    PARITY_CAMPAIGN_SYSTEMS,
    load_parity_campaign,
)
from .parity_campaign_status import campaign_status
from .security import (
    RuntimePolicy,
    ensure_dir_no_follow,
    read_regular_file_bytes_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    write_new_text_file_no_follow,
)
from .staged_workspace import require_path_outside_workspace


PARITY_CAMPAIGN_TRIAL_APPROVAL = "parity-campaign-trial"
PARITY_CAMPAIGN_TRIAL_PLAN_SCHEMA = "conductor.parity_campaign_trial_plan.v1"
PARITY_CAMPAIGN_TRIAL_RUN_SCHEMA = "conductor.parity_campaign_trial_run.v1"
PARITY_CAMPAIGN_TRIAL_RECEIPT_SCHEMA = "conductor.parity_campaign_trial_receipt.v1"
TRIAL_RECEIPT_NAME = "trial-run.json"
MAX_TRIAL_RECEIPT_BYTES = 256 * 1024
CLAUDE_MAX_TURNS = 12
PROVIDER_OUTPUT_LIMIT_BYTES = 1024 * 1024
VERIFIER_OUTPUT_LIMIT_BYTES = 1024 * 1024
FINALIZABLE_STAGED_STATUSES = {
    "success",
    "no-changes",
    "verification-failed",
    "verification-timed-out",
}
LAUNCH_STATES = {"pending", "launching", "captured", "finalized"}


def run_parity_campaign_trial(
    *,
    campaign_path: Path,
    parity_tasks_path: Path,
    trial_id: Optional[str] = None,
    next_trial: bool = False,
    fixture_root: Optional[Path] = None,
    policy: Optional[RuntimePolicy] = None,
    resume: bool = False,
    dry_run: bool = False,
) -> Dict:
    context = _preflight(
        campaign_path=Path(campaign_path),
        parity_tasks_path=Path(parity_tasks_path),
        trial_id=trial_id,
        next_trial=next_trial,
        fixture_root=fixture_root,
        resume=resume,
    )
    plan = _public_plan(context, dry_run=dry_run)
    if dry_run or context["trial_status"] == "completed":
        return plan
    if not context["runtime_release_compatible"]:
        raise ValidationError(
            "campaign continuation requires Conductor runtime %s; current runtime is %s"
            % (", ".join(context["campaign_release_versions"]), __version__)
        )

    _require_trial_policy(policy)
    _require_provider_tools()
    receipt = _open_or_create_receipt(context, resume=resume)
    launched = []
    reused = []
    for system in context["trial"]["system_order"]:
        if _capture_system(context, receipt, system, policy):
            launched.append(system)
        else:
            reused.append(system)
    for system in context["trial"]["system_order"]:
        _finalize_system(context, receipt, system)

    status = campaign_status(context["campaign_path"])
    trial_status = next(item for item in status["trials"] if item["id"] == context["trial"]["id"])
    if trial_status["status"] != "completed":
        raise ValidationError(
            "campaign trial evidence did not finalize cleanly: %s" % trial_status["status"]
        )
    receipt["status"] = "completed"
    _save_receipt(context["receipt_path"], receipt, replace=True)
    return {
        "schema": PARITY_CAMPAIGN_TRIAL_RUN_SCHEMA,
        "campaign": context["campaign"]["name"],
        "trial_id": context["trial"]["id"],
        "task_id": context["trial"]["task_id"],
        "system_order": list(context["trial"]["system_order"]),
        "status": "completed",
        "launched_systems": launched,
        "reused_systems": reused,
        "winner": trial_status["pair"]["winner"],
        "scores": {
            system: trial_status["systems"][system]["score"]
            for system in PARITY_CAMPAIGN_SYSTEMS
        },
        "claim_eligible": trial_status["pair"]["claim_eligible"],
    }


def _preflight(
    *,
    campaign_path: Path,
    parity_tasks_path: Path,
    trial_id: Optional[str],
    next_trial: bool,
    fixture_root: Optional[Path],
    resume: bool,
) -> Dict:
    if bool(trial_id) == bool(next_trial):
        raise ValidationError("select exactly one campaign trial with trial_id or next_trial")
    campaign = load_parity_campaign(campaign_path)
    tasks = load_parity_tasks(parity_tasks_path)
    public_tasks = _public_json(tasks)
    if _sha256_json(public_tasks) != campaign["source"]["parity_tasks_sha256"]:
        raise ValidationError("parity task fixture does not match the preregistered campaign hash")
    if public_tasks["name"] != campaign["source"]["suite"]:
        raise ValidationError("parity task fixture suite does not match the campaign")
    if public_tasks["name"] != IMPLEMENTATION_CANARY_SUITE:
        raise ValidationError("campaign trial execution currently supports the implementation canary suite")

    status = campaign_status(campaign_path)
    if status["lifecycle"]["invalid"]:
        raise ValidationError("campaign contains invalid evidence; repair it before launching another trial")
    status_by_id = {item["id"]: item for item in status["trials"]}
    if next_trial:
        allowed = {"pending", "partial"} if resume else {"pending"}
        candidates = [
            trial for trial in campaign["trials"]
            if status_by_id[trial["id"]]["status"] in allowed
        ]
        if not candidates:
            raise ValidationError("campaign has no selectable remaining trial")
        trial = min(candidates, key=lambda item: item["sequence"])
    else:
        matches = [trial for trial in campaign["trials"] if trial["id"] == trial_id]
        if len(matches) != 1:
            raise ValidationError("campaign trial does not exist: %s" % trial_id)
        trial = matches[0]
    trial_status = status_by_id[trial["id"]]["status"]
    if trial_status == "partial" and not resume:
        raise ValidationError("campaign trial is partial; use --resume to avoid duplicate provider calls")
    if trial_status not in {"pending", "partial", "completed"}:
        raise ValidationError("campaign trial cannot run from status %s" % trial_status)

    task_matches = [task for task in public_tasks["tasks"] if task["id"] == trial["task_id"]]
    if len(task_matches) != 1:
        raise ValidationError("campaign task is missing from the bound parity fixture")
    task = task_matches[0]
    if _sha256_json(task) != trial["task_contract_sha256"] or task["budget"] != trial["budget"]:
        raise ValidationError("campaign trial task contract or budget does not match the fixture")
    if campaign["schema"] in {PARITY_CAMPAIGN_SCHEMA, PARITY_CAMPAIGN_SCHEMA_V3}:
        codex_profile = _system_config(campaign, "codex")
        effort_policy = (
            campaign["design"][DESIGN_EFFORT_POLICY_FIELD]
            if campaign["schema"] == PARITY_CAMPAIGN_SCHEMA
            else AUTO_EFFORT_POLICY_V1
        )
        expected_effort = resolve_auto_reasoning_effort(
            task["prompt"],
            codex_profile["effort"],
            route="adaptive-direct-goal",
            verifier_kind="command",
            policy=effort_policy,
        )
        if trial.get("codex_effort") != expected_effort:
            raise ValidationError("campaign trial Codex effort does not match the preregistered policy")
    spec = TASK_SPECS.get(trial["task_id"])
    if spec is None:
        raise ValidationError("campaign task has no deterministic execution profile")
    resolved_fixture_root = (
        Path(fixture_root) if fixture_root is not None else parity_tasks_path.parent / "implementation-canary"
    ).resolve()
    workspace = (resolved_fixture_root / spec["source"]).resolve()
    if not workspace.is_dir() or workspace.is_symlink():
        raise ValidationError("campaign source fixture is not a real directory")
    check_command = [
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
    ]
    paths = {
        system: {
            key: _resolve_artifact_path(campaign_path, value)
            for key, value in trial["artifacts"][system].items()
        }
        for system in PARITY_CAMPAIGN_SYSTEMS
    }
    roots = {
        path.parent.resolve()
        for system_paths in paths.values()
        for key, path in system_paths.items()
        if key in {"report", "scored_report", "manifest", "patch", "stage", "evaluation"}
    }
    if len(roots) != 1:
        raise ValidationError("campaign trial artifacts must share one preregistered directory")
    trial_root = next(iter(roots))
    for system_paths in paths.values():
        for key, path in system_paths.items():
            require_path_outside_workspace(workspace, path, "campaign %s artifact" % key)
            reject_symlink_path(path, "campaign %s artifact" % key)
    receipt_path = trial_root / TRIAL_RECEIPT_NAME
    reject_symlink_path(receipt_path, "campaign trial receipt")
    if trial_status == "pending" and not resume and (
        receipt_path.exists()
        or receipt_path.is_symlink()
        or any(path.exists() or path.is_symlink() for values in paths.values() for path in values.values())
    ):
        raise ValidationError("campaign trial has unaccounted artifacts; use --resume only with a valid receipt")

    campaign_sha256 = hashlib.sha256(
        read_regular_file_bytes_no_follow(campaign_path, "parity campaign", max_bytes=4 * 1024 * 1024)
    ).hexdigest()
    return {
        "campaign_path": campaign_path,
        "campaign": campaign,
        "campaign_sha256": campaign_sha256,
        "tasks": tasks,
        "task": task,
        "trial": trial,
        "trial_status": trial_status,
        "paths": paths,
        "trial_root": trial_root,
        "receipt_path": receipt_path,
        "fixture_root": resolved_fixture_root,
        "workspace": workspace,
        "check_command": check_command,
        "campaign_release_versions": list(status["release_cohort"]["versions"]),
        "runtime_release_compatible": _runtime_release_compatible(status),
    }


def _public_plan(context: Dict, *, dry_run: bool) -> Dict:
    return {
        "schema": PARITY_CAMPAIGN_TRIAL_PLAN_SCHEMA,
        "campaign": context["campaign"]["name"],
        "trial_id": context["trial"]["id"],
        "sequence": context["trial"]["sequence"],
        "task_id": context["trial"]["task_id"],
        "repetition": context["trial"]["repetition"],
        "current_status": context["trial_status"],
        "system_order": list(context["trial"]["system_order"]),
        "systems": {
            system: {
                "model": _trial_system_config(context["campaign"], context["trial"], system)["model"],
                "effort": _trial_system_config(context["campaign"], context["trial"], system)["effort"],
            }
            for system in PARITY_CAMPAIGN_SYSTEMS
        },
        "budget": dict(context["trial"]["budget"]),
        "execution_profile": "implementation-canary-v1",
        "runtime_version": __version__,
        "campaign_release_versions": list(context["campaign_release_versions"]),
        "runtime_release_compatible": context["runtime_release_compatible"],
        "dry_run": dry_run,
        "provider_launches": 0 if dry_run or context["trial_status"] == "completed" else 2,
    }


def _runtime_release_compatible(status: Dict) -> bool:
    versions = status["release_cohort"]["versions"]
    return not versions or versions == [__version__]


def _require_trial_policy(policy: Optional[RuntimePolicy]) -> None:
    if policy is None:
        raise PolicyError("campaign trial execution requires an explicit runtime policy")
    if not policy.allow_agent or not policy.allow_network or not policy.allow_writes:
        raise PolicyError("campaign trial execution requires --allow-agent --allow-network --allow-writes")
    if policy.allow_destructive or policy.allow_parallel:
        raise PolicyError("campaign trial execution forbids destructive and parallel capabilities")
    required = {
        PARITY_CAMPAIGN_TRIAL_APPROVAL,
        CLAUDE_PAID_RUN_APPROVAL,
        CLAUDE_STAGED_WRITE_APPROVAL,
        CODEX_LIVE_RUN_APPROVAL,
        CODEX_STAGED_WRITE_APPROVAL,
    }
    missing = sorted(token for token in required if not policy.has_approval(token))
    if missing:
        raise PolicyError("campaign trial execution requires approval(s): %s" % ", ".join(missing))


def _require_provider_tools() -> None:
    missing = [name for name in ["claude", "codex", "patch"] if shutil.which(name) is None]
    if missing:
        raise ValidationError("campaign trial requires tool(s) on PATH: %s" % ", ".join(missing))


def _open_or_create_receipt(context: Dict, *, resume: bool) -> Dict:
    path = context["receipt_path"]
    if path.exists() or path.is_symlink():
        if not resume:
            raise ValidationError("campaign trial receipt already exists; use --resume")
        receipt = _load_receipt(path)
        if (
            receipt["campaign_sha256"] != context["campaign_sha256"]
            or receipt["trial_id"] != context["trial"]["id"]
            or receipt["task_id"] != context["trial"]["task_id"]
            or receipt["system_order"] != context["trial"]["system_order"]
        ):
            raise ValidationError("campaign trial receipt does not match the immutable plan")
        return receipt
    if resume:
        raise ValidationError("campaign trial cannot resume without its launch receipt")
    parent_fd = ensure_dir_no_follow(context["trial_root"], "campaign trial artifact directory")
    os.close(parent_fd)
    receipt = {
        "schema": PARITY_CAMPAIGN_TRIAL_RECEIPT_SCHEMA,
        "campaign_sha256": context["campaign_sha256"],
        "campaign_name": context["campaign"]["name"],
        "trial_id": context["trial"]["id"],
        "task_id": context["trial"]["task_id"],
        "system_order": list(context["trial"]["system_order"]),
        "status": "active",
        "systems": {
            system: {"launch_state": "pending", "report_sha256": None}
            for system in PARITY_CAMPAIGN_SYSTEMS
        },
        "updated_at_utc": _now_utc(),
    }
    _save_receipt(path, receipt, replace=False)
    return receipt


def _capture_system(context: Dict, receipt: Dict, system: str, policy: RuntimePolicy) -> bool:
    record = receipt["systems"][system]
    report_path = context["paths"][system]["report"]
    if report_path.exists() or report_path.is_symlink():
        report = load_benchmark_report(report_path)
        _validate_raw_report(context, system, report)
        observed_hash = _sha256_file(report_path, "campaign raw report")
        if record["launch_state"] == "pending":
            raise ValidationError("campaign %s report exists without a launch marker" % system)
        if record["report_sha256"] is not None and record["report_sha256"] != observed_hash:
            raise ValidationError("campaign %s raw report changed after capture" % system)
        record["launch_state"] = "captured"
        record["report_sha256"] = observed_hash
        _save_receipt(context["receipt_path"], receipt, replace=True)
        return False
    if record["launch_state"] == "launching":
        raise ValidationError(
            "campaign %s outcome is unknown after an interrupted launch; refusing an automatic rerun" % system
        )
    if record["launch_state"] != "pending":
        raise ValidationError("campaign %s receipt state has no raw report" % system)
    system_paths = context["paths"][system]
    if any(
        path.exists() or path.is_symlink()
        for key, path in system_paths.items()
        if key != "report"
    ):
        raise ValidationError("campaign %s has artifacts without a captured report" % system)

    record["launch_state"] = "launching"
    _save_receipt(context["receipt_path"], receipt, replace=True)
    report = _run_system(context, system, policy)
    write_new_text_file_no_follow(
        report_path,
        "campaign raw benchmark report",
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        sync=True,
    )
    record["launch_state"] = "captured"
    record["report_sha256"] = _sha256_file(report_path, "campaign raw report")
    _save_receipt(context["receipt_path"], receipt, replace=True)
    _validate_raw_report(context, system, report)
    return True


def _run_system(context: Dict, system: str, policy: RuntimePolicy) -> Dict:
    trial = context["trial"]
    budget = trial["budget"]
    paths = context["paths"][system]
    timeout_seconds = int(budget["max_minutes"]) * 60
    check_timeout_seconds = min(120, max(30, timeout_seconds // 4))
    if system == "codex":
        expected = _trial_system_config(context["campaign"], trial, "codex")
        return run_codex_staged_task(
            parity_tasks=context["tasks"],
            task_id=trial["task_id"],
            workspace=context["workspace"],
            stage_dir=paths["stage"],
            patch_output=paths["patch"],
            check_command=context["check_command"],
            policy=policy,
            model=expected["model"],
            effort=expected["effort"],
            max_tokens=budget["max_tokens"],
            timeout_seconds=timeout_seconds,
            check_timeout_seconds=check_timeout_seconds,
            output_limit_bytes=PROVIDER_OUTPUT_LIMIT_BYTES,
            check_output_limit_bytes=VERIFIER_OUTPUT_LIMIT_BYTES,
            tool_output_token_limit=DEFAULT_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
        )
    return run_claude_staged_task(
        parity_tasks=context["tasks"],
        task_id=trial["task_id"],
        workspace=context["workspace"],
        stage_dir=paths["stage"],
        patch_output=paths["patch"],
        check_command=context["check_command"],
        policy=policy,
        max_budget_usd=budget["max_cost_usd"],
        max_turns=CLAUDE_MAX_TURNS,
        timeout_seconds=timeout_seconds,
        check_timeout_seconds=check_timeout_seconds,
        output_limit_bytes=PROVIDER_OUTPUT_LIMIT_BYTES,
        check_output_limit_bytes=VERIFIER_OUTPUT_LIMIT_BYTES,
    )


def _validate_raw_report(context: Dict, system: str, report: Dict) -> None:
    if report.get("suite") != context["campaign"]["source"]["suite"] or len(report.get("results", [])) != 1:
        raise ValidationError("campaign %s report suite or result count is invalid" % system)
    result = report["results"][0]
    if result.get("id") != context["trial"]["task_id"]:
        raise ValidationError("campaign %s report task id is invalid" % system)
    staged = result.get("staged_evidence") or {}
    source = staged.get("source") or {}
    if source.get("unchanged") is not True:
        raise ValidationError("campaign %s run changed the source fixture; paired execution stopped" % system)
    provider = result.get("provider_evidence") or {}
    requested = provider.get("requested") or {}
    expected = _trial_system_config(context["campaign"], context["trial"], system)
    if requested.get("model") != expected["model"] or requested.get("effort") != expected["effort"]:
        raise ValidationError("campaign %s report does not match the preregistered model" % system)
    if system == "codex":
        if report.get("system") != "codex-isolated-staged":
            raise ValidationError("campaign Codex report system is invalid")
    else:
        observed = provider.get("observed") or {}
        if (
            report.get("system") != "claude-sonnet-ultracode-staged"
            or requested.get("model") != "sonnet"
            or requested.get("effort") != "ultracode"
            or observed.get("opus_observed") is not False
            or observed.get("sonnet_main_only") is not True
        ):
            raise ValidationError("campaign Claude execution was not exclusively Sonnet Ultracode")


def _finalize_system(context: Dict, receipt: Dict, system: str) -> None:
    record = receipt["systems"][system]
    if record["launch_state"] == "finalized":
        _validate_existing_finalization(context, system)
        return
    if record["launch_state"] != "captured":
        raise ValidationError("campaign %s report has not been captured" % system)
    paths = context["paths"][system]
    report = load_benchmark_report(paths["report"])
    _validate_raw_report(context, system, report)
    result = report["results"][0]
    staged = result["staged_evidence"]
    staged_status = staged["status"]
    if staged_status not in FINALIZABLE_STAGED_STATUSES:
        raise ValidationError(
            "campaign %s produced non-finalizable staged status %s" % (system, staged_status)
        )
    terminal_no_patch = staged_status == "no-changes"
    if terminal_no_patch:
        if not paths["patch"].exists() and not paths["patch"].is_symlink():
            write_new_text_file_no_follow(paths["patch"], "campaign empty patch marker", "", sync=True)
    if not paths["evaluation"].exists() and not paths["evaluation"].is_symlink():
        evaluate_implementation_canary(
            paths["report"],
            paths["patch"],
            paths["evaluation"],
            fixture_root=context["fixture_root"],
            terminal_no_patch=terminal_no_patch,
        )
    score_input = load_benchmark_score_input(paths["evaluation"] / "score-input.json")
    if not paths["scored_report"].exists() and not paths["scored_report"].is_symlink():
        scored = score_benchmark_report(report, context["tasks"], score_input)
        write_new_text_file_no_follow(
            paths["scored_report"],
            "campaign scored benchmark report",
            json.dumps(scored, indent=2, sort_keys=True) + "\n",
            sync=True,
        )
    scored = load_benchmark_report(paths["scored_report"])
    if not paths["manifest"].exists() and not paths["manifest"].is_symlink():
        manifest = _build_manifest(context, system, scored)
        write_run_manifest(manifest, paths["manifest"])
    manifest = load_run_manifest(paths["manifest"])
    validate_run_manifest_matches_report(manifest, scored, source="campaign finalized manifest")
    record["launch_state"] = "finalized"
    _save_receipt(context["receipt_path"], receipt, replace=True)


def _validate_existing_finalization(context: Dict, system: str) -> None:
    paths = context["paths"][system]
    scored = load_benchmark_report(paths["scored_report"])
    manifest = load_run_manifest(paths["manifest"])
    validate_run_manifest_matches_report(manifest, scored, source="campaign resumed manifest")


def _build_manifest(context: Dict, system: str, scored_report: Dict) -> Dict:
    result = scored_report["results"][0]
    provider = result["provider_evidence"]
    observed = provider["observed"]
    trial = context["trial"]
    budget = dict(trial["budget"])
    permissions = {
        "allow_writes": True,
        "allow_destructive": False,
        "allow_network": True,
        "allow_agent": True,
        "allow_parallel": False,
        "approved_tokens": [],
    }
    if system == "codex":
        budget.update({"cost_cap_enforcement": "unavailable", "token_cap_enforcement": "runtime-hard"})
        model = {
            "provider": "openai",
            "name": provider["requested"]["model"],
            "effort": provider["requested"]["effort"],
            "version": observed["cli_version"],
        }
    else:
        main_models = observed.get("main_models") or []
        if not main_models or observed.get("sonnet_main_only") is not True or observed.get("opus_observed") is not False:
            raise ValidationError("campaign Claude manifest requires observed Sonnet main-model evidence")
        budget.update({"cost_cap_enforcement": "hard-provider", "token_cap_enforcement": "unavailable"})
        model = {
            "provider": "anthropic",
            "name": main_models[0],
            "effort": "ultracode",
            "version": observed["cli_version"],
        }
    return build_run_manifest_from_report(
        report=scored_report,
        run_id=_run_id(context["campaign"]["name"], trial["id"], system),
        live_run=True,
        repository={
            "state": "preregistered-fixture-%s" % trial["task_contract_sha256"][:16],
            "commit": trial["task_contract_sha256"],
            "dirty": "false",
        },
        model=model,
        budget=budget,
        permissions=permissions,
        system_name=scored_report["system"],
    )


def _load_receipt(path: Path) -> Dict:
    text = read_regular_text_file_no_follow(path, "campaign trial receipt", max_bytes=MAX_TRIAL_RECEIPT_BYTES)
    try:
        receipt = json.loads(text, object_pairs_hook=_reject_duplicate_pairs)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("campaign trial receipt is not valid JSON: %s" % exc)
    _validate_receipt(receipt)
    return receipt


def _save_receipt(path: Path, receipt: Dict, *, replace: bool) -> None:
    receipt["updated_at_utc"] = _now_utc()
    _validate_receipt(receipt)
    text = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if replace:
        replace_text_file_no_follow(
            path,
            "campaign trial receipt",
            text,
            temp_prefix="trial-run",
            sync=True,
        )
    else:
        write_new_text_file_no_follow(path, "campaign trial receipt", text, sync=True)


def _validate_receipt(receipt: Dict) -> None:
    fields = {
        "schema",
        "campaign_sha256",
        "campaign_name",
        "trial_id",
        "task_id",
        "system_order",
        "status",
        "systems",
        "updated_at_utc",
    }
    if not isinstance(receipt, dict) or set(receipt) != fields:
        raise ValidationError("campaign trial receipt fields are invalid")
    if receipt.get("schema") != PARITY_CAMPAIGN_TRIAL_RECEIPT_SCHEMA:
        raise ValidationError("campaign trial receipt schema is invalid")
    _require_sha256(receipt.get("campaign_sha256"), "campaign trial receipt hash")
    if receipt.get("status") not in {"active", "completed"}:
        raise ValidationError("campaign trial receipt status is invalid")
    if receipt.get("system_order") not in [["codex", "claude"], ["claude", "codex"]]:
        raise ValidationError("campaign trial receipt system order is invalid")
    systems = receipt.get("systems")
    if not isinstance(systems, dict) or set(systems) != set(PARITY_CAMPAIGN_SYSTEMS):
        raise ValidationError("campaign trial receipt systems are invalid")
    for system, record in systems.items():
        if not isinstance(record, dict) or set(record) != {"launch_state", "report_sha256"}:
            raise ValidationError("campaign trial receipt %s record is invalid" % system)
        if record.get("launch_state") not in LAUNCH_STATES:
            raise ValidationError("campaign trial receipt launch state is invalid")
        report_sha256 = record.get("report_sha256")
        if report_sha256 is not None:
            _require_sha256(report_sha256, "campaign trial receipt report hash")
        if record["launch_state"] in {"captured", "finalized"} and report_sha256 is None:
            raise ValidationError("campaign trial receipt captured report hash is missing")
        if record["launch_state"] in {"pending", "launching"} and report_sha256 is not None:
            raise ValidationError("campaign trial receipt pending report hash is invalid")
    if receipt["status"] == "completed" and any(
        record["launch_state"] != "finalized" for record in systems.values()
    ):
        raise ValidationError("completed campaign trial receipt requires finalized systems")
    for field in ["campaign_name", "trial_id", "task_id", "updated_at_utc"]:
        if not isinstance(receipt.get(field), str) or not receipt[field]:
            raise ValidationError("campaign trial receipt %s is invalid" % field)


def _system_config(campaign: Dict, system: str) -> Dict:
    matches = [item for item in campaign["systems"] if item["name"] == system]
    if len(matches) != 1:
        raise ValidationError("campaign system configuration is invalid")
    return matches[0]


def _trial_system_config(campaign: Dict, trial: Dict, system: str) -> Dict:
    configured = _system_config(campaign, system)
    if system != "codex" or "codex_effort" not in trial:
        return configured
    resolved = dict(configured)
    resolved["effort"] = trial["codex_effort"]
    return resolved


def _resolve_artifact_path(campaign_path: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = campaign_path.parent / path
    return Path(os.path.abspath(str(path)))


def _public_json(value: Dict) -> Dict:
    return {key: item for key, item in value.items() if not str(key).startswith("_")}


def _sha256_json(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path, label: str) -> str:
    return hashlib.sha256(
        read_regular_file_bytes_no_follow(path, label, max_bytes=32 * 1024 * 1024)
    ).hexdigest()


def _require_sha256(value, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValidationError("%s must be a SHA-256 value" % label)


def _reject_duplicate_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _run_id(campaign_name: str, trial_id: str, system: str) -> str:
    value = "%s-%s-%s" % (campaign_name, trial_id, system)
    if len(value) <= 128:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return "%s-%s" % (value[:110].rstrip("-"), digest)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
