"""Canonical CLI for the small default Conductor runtime."""

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__


CANONICAL_COMMANDS = ("auto", "run", "validate", "status", "list", "apply", "doctor")
EFFORTS = ("low", "medium", "high", "xhigh", "ultra")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conductor-runtime",
        description="Run secure, resumable Codex workflows.",
    )
    parser.add_argument("--version", action="version", version="conductor-runtime %s" % __version__)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    _add_auto_parser(commands)
    _add_run_parser(commands)

    validate = commands.add_parser("validate", help="Validate core workflow JSON without execution.")
    validate.add_argument("paths", nargs="+", type=Path)

    status = commands.add_parser("status", help="Verify and inspect one external run record.")
    status.add_argument("run_dir", type=Path)

    listing = commands.add_parser("list", help="List valid core workflow files.")
    listing.add_argument("paths", nargs="+", type=Path)

    apply_command = commands.add_parser("apply", help="Apply a verified staged result.")
    apply_command.add_argument("evidence", type=Path)
    apply_command.add_argument("--workspace", type=Path, default=Path("."))
    apply_command.add_argument("--receipt", type=Path)
    apply_command.add_argument("--allow-writes", action="store_true")
    apply_command.add_argument("--allow-destructive", action="store_true")
    apply_command.add_argument("--approve", action="append", default=[])

    doctor = commands.add_parser("doctor", help="Check whether the Codex CLI can be launched.")
    doctor.add_argument("--json", action="store_true")

    return parser


def _add_auto_parser(commands) -> None:
    auto = commands.add_parser("auto", help="Execute a task directly with the model.")
    task = auto.add_mutually_exclusive_group(required=True)
    task.add_argument("--task", help="Untrusted task text.")
    task.add_argument("--task-file", type=Path, help="File containing untrusted task text.")
    auto.add_argument("--strategy", choices=("auto", "direct", "goal", "workflow"), default="auto")
    auto.add_argument("--check-command-json", help="Verification command as a JSON argv array.")
    verifier = auto.add_mutually_exclusive_group()
    verifier.add_argument("--check-prompt")
    verifier.add_argument("--check-prompt-file", type=Path)
    auto.add_argument("--workspace", type=Path, default=Path("."))
    auto.add_argument("--runs-dir", type=Path)
    auto.add_argument("--output", type=Path)
    auto.add_argument("--receipt", type=Path)
    auto.add_argument("--run-id")
    auto.add_argument("--resume", type=Path)
    auto.add_argument("--goals-dir", type=Path)
    auto.add_argument("--goal-id")
    auto.add_argument("--resume-goal", type=Path)
    auto.add_argument("--max-iterations", type=int, default=3)
    auto.add_argument("--name")
    auto.add_argument("--model")
    auto.add_argument("--effort", choices=EFFORTS)
    auto.add_argument("--max-tokens", type=int, default=20000)
    auto.add_argument("--verifier-max-tokens", type=int, default=8000)
    auto.add_argument("--planner-max-tokens", type=int, default=12000)
    auto.add_argument("--execution-max-tokens", type=int, default=120000)
    auto.add_argument("--max-steps", type=int, default=32)
    auto.add_argument("--max-items", type=int, default=1000)
    auto.add_argument("--max-workers", type=int, default=8)
    auto.add_argument("--timeout-seconds", type=int, default=900)
    auto.add_argument("--check-timeout-seconds", type=int, default=300)
    auto.add_argument("--output-limit-bytes", type=int, default=1024 * 1024)
    auto.add_argument("--plan-only", action="store_true")
    _add_policy_arguments(auto)


def _add_run_parser(commands) -> None:
    run = commands.add_parser("run", help="Run or resume a validated core workflow.")
    run.add_argument("workflow", type=Path)
    run.add_argument("--workspace", type=Path, default=Path("."))
    run.add_argument("--runs-dir", type=Path)
    run.add_argument("--run-id")
    run.add_argument("--resume", type=Path)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--print-result", action="store_true")
    run.add_argument("--max-workers", type=int)
    _add_policy_arguments(run)


def _add_policy_arguments(parser) -> None:
    parser.add_argument("--allow-writes", action="store_true")
    parser.add_argument("--allow-destructive", action="store_true")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--allow-agent", action="store_true")
    parser.add_argument("--allow-parallel", action="store_true")
    parser.add_argument("--approve", action="append", default=[])


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw == ["--version"]:
        print("conductor-runtime %s" % __version__)
        return 0
    parser = build_parser()
    if not raw:
        parser.print_help()
        return 0
    if raw[0] not in CANONICAL_COMMANDS and raw[0] not in {"-h", "--help"}:
        parser.error("unknown command: %s" % raw[0])
    args = parser.parse_args(raw)
    try:
        return {
            "auto": _auto,
            "run": _run,
            "validate": _validate,
            "status": _status,
            "list": _list,
            "apply": _apply,
            "doctor": _doctor,
        }[args.command](args)
    except Exception as exc:
        from .errors import ConductorError
        from .redaction import redact_text

        if isinstance(exc, (ConductorError, FileNotFoundError)):
            print("ERROR: %s" % redact_text(str(exc)), file=sys.stderr)
            return 2
        raise


def _auto(args) -> int:
    from .core.auto import parse_command_json, run_direct
    from .core.safe import read_regular_text
    from .redaction import redact_text

    task = read_regular_text(args.task_file, "task file", 65536) if args.task_file else args.task
    check_prompt = args.check_prompt
    if args.check_prompt_file is not None:
        check_prompt = read_regular_text(args.check_prompt_file, "check prompt file", 65536)
    check_command = (
        parse_command_json(args.check_command_json)
        if args.check_command_json is not None
        else None
    )
    options = {
        "runs_dir": args.runs_dir,
        "check_command": check_command,
        "check_prompt": check_prompt,
        "name": args.name,
        "model": args.model,
        "effort": args.effort,
        "max_tokens": args.max_tokens,
        "verifier_tokens": args.verifier_max_tokens,
        "timeout_seconds": args.timeout_seconds,
        "check_timeout_seconds": args.check_timeout_seconds,
        "output_limit_bytes": args.output_limit_bytes,
    }
    if args.strategy == "workflow":
        from .core.planner import run_planned_workflow
        from .errors import ValidationError

        if args.resume is not None or args.resume_goal is not None or args.run_id is not None:
            raise ValidationError("workflow strategy does not combine with run or goal resume/id")
        if check_command is not None or check_prompt is not None:
            raise ValidationError("workflow strategy expresses verification inside the planned workflow")
        planned = run_planned_workflow(
            task,
            workspace=args.workspace,
            policy=_runtime_policy(args),
            runs_dir=args.runs_dir,
            output_path=args.output,
            receipt_path=args.receipt,
            plan_only=args.plan_only,
            model=args.model,
            effort=args.effort,
            planner_max_tokens=args.planner_max_tokens,
            execution_max_tokens=args.execution_max_tokens,
            max_steps=args.max_steps,
            max_items=args.max_items,
            max_workers=args.max_workers,
            timeout_seconds=args.timeout_seconds,
            output_limit_bytes=args.output_limit_bytes,
        )
        print("Route: workflow")
        print("Planner run: %s" % redact_text(str(planned.planner_run_dir)))
        if planned.run_dir:
            print("Run directory: %s" % redact_text(str(planned.run_dir)))
        print("Receipt: %s" % redact_text(str(planned.receipt_path)))
        if planned.result_path:
            print("Result: %s" % redact_text(str(planned.result_path)))
        if planned.stage_evidence_path:
            print("Verified stage: %s" % redact_text(str(planned.stage_evidence_path)))
        print("Status: %s" % planned.status)
        if planned.status not in {"planned", "completed"}:
            _print_run_detail(planned.run_dir or planned.planner_run_dir)
        return 0 if planned.status in {"planned", "completed"} else 1
    if args.strategy == "goal":
        from .core.goal import run_goal
        from .errors import ValidationError

        if args.plan_only or args.resume is not None or args.run_id is not None or args.receipt is not None:
            raise ValidationError("goal strategy does not combine with plan-only, run resume/id, or receipt")
        goal = run_goal(
            task,
            workspace=args.workspace,
            policy=_runtime_policy(args),
            max_iterations=args.max_iterations,
            goals_dir=args.goals_dir,
            goal_id=args.goal_id,
            resume_goal=args.resume_goal,
            output_path=args.output,
            **options,
        )
        result = goal.latest
        status = goal.status
        print("Route: goal")
        print("Goal state: %s" % redact_text(str(goal.goal_path)))
        print("Iterations: %d" % goal.iterations)
    else:
        result = run_direct(
            task,
            workspace=args.workspace,
            policy=_runtime_policy(args),
            run_id=args.run_id,
            resume_dir=args.resume,
            dry_run=args.plan_only,
            receipt_path=args.receipt,
            output_path=args.output,
            **options,
        )
        status = result.status
        print("Route: direct")
    print("Run directory: %s" % redact_text(str(result.run_dir)))
    print("Receipt: %s" % redact_text(str(result.receipt_path)))
    if result.result_path:
        print("Result: %s" % redact_text(str(result.result_path)))
    if result.stage_evidence_path:
        print("Verified stage: %s" % redact_text(str(result.stage_evidence_path)))
    print("Status: %s" % status)
    if status not in {"planned", "completed"}:
        _print_run_detail(result.run_dir)
    return 0 if status in {"planned", "completed"} else 1


def _run(args) -> int:
    from .core.runner import WorkflowRunner
    from .core.workflow import load_workflow
    from .errors import ValidationError
    from .redaction import redact_text

    workflow = load_workflow(args.workflow)
    if args.print_result and (args.dry_run or not workflow.get("result_artifact")):
        raise ValidationError("--print-result requires real execution and a result_artifact")
    runner = WorkflowRunner(
        workflow,
        args.workspace,
        args.runs_dir,
        _runtime_policy(args),
        dry_run=args.dry_run,
        run_id=args.run_id,
        resume_dir=args.resume,
        max_workers=args.max_workers,
    )
    run = runner.execute()
    status = run.state["status"]
    print("Run directory: %s" % redact_text(str(run.run_dir)))
    print("Status: %s" % status)
    if status not in {"planned", "completed"}:
        _print_run_detail(run)
    relative = workflow.get("result_artifact")
    if relative and relative in run.state["artifacts"]:
        path = run.artifact_path(relative)
        print("Result artifact: %s" % redact_text(str(path)))
        if args.print_result:
            if status != "completed":
                raise ValidationError("--print-result requires a completed workflow")
            text = run.read_artifact(relative).decode("utf-8", errors="replace")
            sys.stdout.write("Result:\n" + text + ("" if text.endswith("\n") else "\n"))
    return 0 if status in {"planned", "completed"} else 1


def _validate(args) -> int:
    from .core.workflow import iter_workflow_files, load_workflow
    from .errors import ValidationError
    from .redaction import redact_text

    files = iter_workflow_files(args.paths)
    if not files:
        raise ValidationError("no workflow files found")
    for path in files:
        load_workflow(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _status(args) -> int:
    from .core.state import RunState
    from .redaction import redact_json_value

    run = RunState.inspect(args.run_dir)
    print(json.dumps(redact_json_value(run.state), indent=2, sort_keys=True))
    return 0


def _list(args) -> int:
    from .core.workflow import iter_workflow_files, load_workflow, workflow_summary
    from .errors import ConductorError
    from .redaction import redact_text

    for path in iter_workflow_files(args.paths):
        try:
            summary = workflow_summary(load_workflow(path))
        except ConductorError as exc:
            print("INVALID\t%s\t%s" % (redact_text(str(path)), redact_text(str(exc))))
            continue
        print("OK\t%s\t%s" % (redact_text(str(path)), redact_text(summary)))
    return 0


def _apply(args) -> int:
    from .core.staged import apply_verified_stage
    from .redaction import redact_text

    result = apply_verified_stage(
        args.evidence,
        args.workspace,
        receipt_path=args.receipt,
        policy=_runtime_policy(args),
    )
    print("Status: %s" % result["status"])
    print("Files applied: %d" % result["change_count"])
    print("Already applied: %s" % ("yes" if result["already_applied"] else "no"))
    if args.receipt:
        print("Receipt: %s" % redact_text(str(args.receipt)))
    return 0


def _doctor(args) -> int:
    from .core.process import run_process

    executable = shutil.which("codex")
    payload = {
        "schema": "conductor.core_doctor.v1",
        "python": "%s.%s.%s" % sys.version_info[:3],
        "codex_executable": executable,
        "status": "missing" if executable is None else "ready",
    }
    if executable is not None:
        result = run_process([executable, "--version"], cwd=Path.cwd(), timeout_seconds=10, output_limit_bytes=4096)
        payload["status"] = "ready" if result.returncode == 0 and not result.timed_out else "unhealthy"
        payload["codex_version"] = (result.stdout or result.stderr).strip()[:200]
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("python: %s" % payload["python"])
        print("codex: %s" % (payload["codex_executable"] or "not found"))
        print("status: %s" % payload["status"])
    return 0 if payload["status"] == "ready" else 1


def _print_run_detail(run_or_path) -> None:
    from .core.state import RunState
    from .redaction import redact_text

    run = run_or_path if isinstance(run_or_path, RunState) else RunState.inspect(run_or_path)
    details = []
    if run.state.get("detail"):
        details.append(str(run.state["detail"]))
    for step_id, record in run.state.get("steps", {}).items():
        detail = record.get("detail")
        if record.get("status") in {"blocked", "failed"} and detail and detail not in details:
            details.append("%s: %s" % (step_id, detail))
    for detail in details:
        print("Detail: %s" % redact_text(detail))


def _runtime_policy(args):
    from .core.policy import RuntimePolicy, validate_approval_tokens

    return RuntimePolicy(
        allow_writes=bool(getattr(args, "allow_writes", False)),
        allow_destructive=bool(getattr(args, "allow_destructive", False)),
        allow_network=bool(getattr(args, "allow_network", False)),
        allow_agent=bool(getattr(args, "allow_agent", False)),
        allow_parallel=bool(getattr(args, "allow_parallel", False)),
        approvals=validate_approval_tokens(getattr(args, "approve", [])),
    )
