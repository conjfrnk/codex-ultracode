"""Canonical CLI for the small default Conductor runtime."""

import argparse
import json
import shlex
import shutil
import sys
from pathlib import Path

from . import __version__


CANONICAL_COMMANDS = (
    "auto",
    "run",
    "validate",
    "schema",
    "init",
    "migrate",
    "status",
    "results",
    "list",
    "apply",
    "doctor",
)
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

    schema = commands.add_parser("schema", help="Print the core workflow JSON Schema.")
    schema.add_argument("--output", type=Path, help="Write a new schema file instead of printing to stdout.")

    init = commands.add_parser("init", help="Write a new validated core workflow example.")
    init.add_argument("path", type=Path)
    init.add_argument("--template", choices=("read-only", "staged-write"), default="read-only")

    migrate = commands.add_parser("migrate", help="Convert a legacy core-shaped workflow to the core schema.")
    migrate.add_argument("workflow", type=Path)
    migrate.add_argument("--output", type=Path, help="Write a new migrated file instead of printing to stdout.")

    status = commands.add_parser("status", help="Verify and inspect one external run record.")
    status.add_argument("run_dir", type=Path)

    results = commands.add_parser(
        "results",
        help="Inspect hash-bound overflow captured outside model context.",
    )
    result_commands = results.add_subparsers(dest="results_command", metavar="ACTION", required=True)
    result_list = result_commands.add_parser("list", help="Verify and list captured results.")
    result_list.add_argument("run_dir", type=Path)
    result_list.add_argument("--step")
    result_list.add_argument("--json", action="store_true")
    result_get = result_commands.add_parser("get", help="Read a bounded cited line range.")
    result_get.add_argument("run_dir", type=Path)
    result_get.add_argument("result_id")
    result_get.add_argument("--start-line", type=int, default=1)
    result_get.add_argument("--max-lines", type=int, default=200)
    result_get.add_argument("--max-bytes", type=int, default=64 * 1024)
    result_get.add_argument("--json", action="store_true")
    result_search = result_commands.add_parser("search", help="Search captured text literally.")
    result_search.add_argument("run_dir", type=Path)
    result_search.add_argument("--query", required=True)
    result_search.add_argument("--result-id", action="append")
    result_search.add_argument("--step")
    result_search.add_argument("--max-matches", type=int, default=20)
    result_search.add_argument("--json", action="store_true")
    result_outline = result_commands.add_parser("outline", help="List bounded line chunks for one result.")
    result_outline.add_argument("run_dir", type=Path)
    result_outline.add_argument("result_id")
    result_outline.add_argument("--chunk-lines", type=int, default=200)
    result_outline.add_argument("--max-chunks", type=int, default=50)
    result_outline.add_argument("--json", action="store_true")

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
    auto = commands.add_parser(
        "auto",
        help="Execute a task directly or through an explicit bounded strategy.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "Examples: direct: auto --task TASK --allow-agent; goal: auto --strategy goal "
            "--task TASK --allow-agent --allow-writes; workflow: auto --strategy workflow "
            "--task TASK --allow-agent --allow-parallel"
        ),
    )
    task = auto.add_mutually_exclusive_group(required=True)
    task.add_argument("--task", help="Untrusted task text.")
    task.add_argument("--task-file", type=Path, help="File containing untrusted task text.")
    auto.add_argument(
        "--strategy",
        choices=("auto", "direct", "goal", "workflow"),
        default="direct",
        help="Execution route; 'auto' is a deprecated alias for 'direct'.",
    )
    auto.add_argument("--check-command-json", help="Verification command as a JSON argv array.")
    verifier = auto.add_mutually_exclusive_group()
    verifier.add_argument("--check-prompt")
    verifier.add_argument("--check-prompt-file", type=Path)
    auto.add_argument("--workspace", type=Path, default=Path("."), help="Source workspace to inspect.")
    auto.add_argument("--runs-dir", type=Path, help="External run-state directory override.")
    auto.add_argument(
        "--output",
        type=Path,
        help="Explicit result export path; may be inside the workspace and is no-clobber by default.",
    )
    auto.add_argument(
        "--replace-output",
        action="store_true",
        help="Allow --output to replace an existing regular file.",
    )
    auto.add_argument("--receipt", type=Path, help="External automatic-run receipt path.")
    auto.add_argument("--run-id", help="Safe identifier for a new direct run.")
    auto.add_argument("--resume", type=Path, help="Resume an exact direct run directory.")
    auto.add_argument("--goals-dir", type=Path, help="External goal-state directory override.")
    auto.add_argument("--goal-id", help="Safe identifier for a new goal.")
    auto.add_argument("--resume-goal", type=Path, help="Resume an exact goal-state file.")
    auto.add_argument("--max-iterations", type=int, default=3, help="Goal verifier-repair attempt cap.")
    auto.add_argument("--name", help="Generated workflow name.")
    auto.add_argument("--model", help="Codex model override.")
    auto.add_argument("--effort", choices=EFFORTS, help="Codex reasoning effort override.")
    auto.add_argument("--max-tokens", type=int, default=20000, help="Direct worker token cap.")
    auto.add_argument("--verifier-max-tokens", type=int, default=8000, help="Verifier token cap.")
    auto.add_argument("--planner-max-tokens", type=int, default=12000, help="Workflow planner token cap.")
    auto.add_argument(
        "--execution-max-tokens", type=int, default=120000, help="Aggregate planned execution token cap."
    )
    auto.add_argument("--max-steps", type=int, default=32, help="Generated workflow step cap.")
    auto.add_argument("--max-items", type=int, default=1000, help="Generated map item cap.")
    auto.add_argument("--max-workers", type=int, default=8, help="Generated workflow worker cap.")
    auto.add_argument("--timeout-seconds", type=int, default=900, help="Provider call timeout.")
    auto.add_argument("--check-timeout-seconds", type=int, default=300, help="Shell verifier timeout.")
    auto.add_argument(
        "--output-limit-bytes", type=int, default=1024 * 1024, help="Per-stream and result byte cap."
    )
    auto.add_argument(
        "--plan-only",
        action="store_true",
        help="Persist without direct execution; workflow strategy still launches its planner.",
    )
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
    parser.add_argument("--allow-writes", action="store_true", help="Permit staged workspace-write workers.")
    parser.add_argument("--allow-destructive", action="store_true", help="Permit declared destructive steps.")
    parser.add_argument("--allow-network", action="store_true", help="Permit declared network steps.")
    parser.add_argument("--allow-agent", action="store_true", help="Permit Codex provider calls.")
    parser.add_argument("--allow-parallel", action="store_true", help="Permit more than one agent worker.")
    parser.add_argument("--approve", action="append", default=[], help="Add one exact approval token.")


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
            "schema": _schema,
            "init": _init,
            "migrate": _migrate,
            "status": _status,
            "results": _results,
            "list": _list,
            "apply": _apply,
            "doctor": _doctor,
        }[args.command](args)
    except Exception as exc:
        from .errors import ConductorError
        from .redaction import redact_terminal_text

        if isinstance(exc, (ConductorError, FileNotFoundError)):
            print("ERROR: %s" % redact_terminal_text(str(exc)), file=sys.stderr)
            return 2
        raise


def _auto(args) -> int:
    from .core.auto import parse_command_json, run_direct
    from .core.safe import read_regular_text
    from .redaction import redact_terminal_text as redact_text

    from .errors import ValidationError

    if args.replace_output and args.output is None:
        raise ValidationError("--replace-output requires --output")
    if args.strategy == "auto":
        print("WARNING: --strategy auto is deprecated; use --strategy direct", file=sys.stderr)
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
            replace_output=args.replace_output,
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
            replace_output=args.replace_output,
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
            replace_output=args.replace_output,
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
    from .redaction import redact_terminal_text as redact_text

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
            text = redact_text(run.read_artifact(relative).decode("utf-8", errors="replace"))
            sys.stdout.write("Result:\n" + text + ("" if text.endswith("\n") else "\n"))
    return 0 if status in {"planned", "completed"} else 1


def _validate(args) -> int:
    from .core.workflow import iter_workflow_files, load_workflow
    from .errors import ValidationError
    from .redaction import redact_terminal_text as redact_text

    files = iter_workflow_files(args.paths)
    if not files:
        raise ValidationError("no workflow files found")
    for path in files:
        load_workflow(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _schema(args) -> int:
    from .core.safe import canonical_json_bytes, write_new_bytes
    from .core.workflow import workflow_json_schema

    payload = canonical_json_bytes(workflow_json_schema())
    if args.output is None:
        sys.stdout.write(payload.decode("utf-8"))
    else:
        write_new_bytes(args.output, payload, "workflow schema", mode=0o644)
        print("Wrote: %s" % args.output)
    return 0


def _init(args) -> int:
    from .core.safe import canonical_json_bytes, write_new_bytes
    from .core.workflow import validate_workflow, workflow_template

    workflow = workflow_template(args.template)
    validate_workflow(workflow, source="generated workflow")
    write_new_bytes(args.path, canonical_json_bytes(workflow), "workflow", mode=0o644)
    print("Wrote: %s" % args.path)
    return 0


def _migrate(args) -> int:
    from .core.safe import canonical_json_bytes, read_regular_bytes, strict_json_bytes, write_new_bytes
    from .core.workflow import MAX_WORKFLOW_BYTES, migrate_legacy_workflow
    from .errors import ValidationError

    value = strict_json_bytes(read_regular_bytes(args.workflow, "workflow", MAX_WORKFLOW_BYTES), "workflow")
    if not isinstance(value, dict):
        raise ValidationError("workflow must contain a JSON object")
    payload = canonical_json_bytes(migrate_legacy_workflow(value, source=str(args.workflow)))
    if args.output is None:
        sys.stdout.write(payload.decode("utf-8"))
    else:
        write_new_bytes(args.output, payload, "migrated workflow", mode=0o644)
        print("Wrote: %s" % args.output)
    return 0


def _status(args) -> int:
    from .core.state import RunState
    from .redaction import redact_json_value

    run = RunState.inspect(args.run_dir)
    print(json.dumps(redact_json_value(run.state), indent=2, sort_keys=True))
    return 0


def _results(args) -> int:
    from .core.results import RunResultStore
    from .redaction import redact_json_value, redact_terminal_text

    store = RunResultStore.inspect(args.run_dir)
    if args.results_command == "list":
        payload = store.list_records(step_id=args.step)
    elif args.results_command == "get":
        payload = store.get(
            args.result_id,
            start_line=args.start_line,
            max_lines=args.max_lines,
            max_bytes=args.max_bytes,
        )
    elif args.results_command == "search":
        payload = store.search(
            args.query,
            result_ids=args.result_id,
            step_id=args.step,
            max_matches=args.max_matches,
        )
    elif args.results_command == "outline":
        payload = store.outline(
            args.result_id,
            chunk_lines=args.chunk_lines,
            max_chunks=args.max_chunks,
        )
    else:
        raise AssertionError("unknown result action")
    if args.json:
        print(json.dumps(redact_json_value(payload), indent=2, sort_keys=True))
        return 0
    if args.results_command == "list":
        for record in payload:
            source = record["source"]
            print(
                "%s\t%s\t%s\t%d bytes\tcomplete=%s"
                % (
                    record["result_id"],
                    source["step_id"],
                    source["stream"],
                    record["size_bytes"],
                    "yes" if record["pipe_complete"] else "no",
                )
            )
    elif args.results_command == "get":
        print("Citation: %s" % payload["citation"])
        print("Pipe complete: %s" % ("yes" if payload["pipe_complete"] else "no"))
        print("Producer status: %s" % payload["producer_status"])
        if payload["text"]:
            print(redact_terminal_text(payload["text"]))
    else:
        for item in payload:
            label = item.get("text", item.get("label", ""))
            print("%s\t%s" % (item["citation"], redact_terminal_text(label)))
    return 0


def _list(args) -> int:
    from .core.workflow import iter_workflow_files, load_workflow, workflow_summary
    from .errors import ConductorError
    from .redaction import redact_terminal_text as redact_text

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
    from .redaction import redact_terminal_text as redact_text

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
    from .redaction import redact_terminal_text

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
        from .redaction import redact_json_value

        print(json.dumps(redact_json_value(payload), indent=2, sort_keys=True))
    else:
        print("python: %s" % redact_terminal_text(str(payload["python"])))
        print("codex: %s" % redact_terminal_text(str(payload["codex_executable"] or "not found")))
        print("status: %s" % redact_terminal_text(str(payload["status"])))
    return 0 if payload["status"] == "ready" else 1


def _print_run_detail(run_or_path) -> None:
    from .core.results import RunResultStore
    from .core.state import RunState
    from .redaction import redact_terminal_text as redact_text

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
    for record in RunResultStore(run).list_records():
        source = record["source"]
        print(
            "Recoverable result: %s (%s/%s)"
            % (record["result_id"], source["step_id"], source["stream"])
        )
        command = shlex.join(
            ["conductor-runtime", "results", "get", str(run.run_dir), record["result_id"]]
        )
        print("Inspect: %s" % redact_text(command))


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
