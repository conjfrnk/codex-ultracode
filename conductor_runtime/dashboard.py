import json
import heapq
import hashlib
import os
import re
import shlex
import stat
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .agent_packets import AgentPacket, MAX_AGENT_PACKETS, packetize_agent_items
from .agent_native_tool_hooks import validate_agent_native_tool_summary
from .agent_map_packet_terminal import (
    agent_map_packet_terminal_summary,
    load_agent_map_packet_terminal,
)
from .agent_team import (
    agent_team_state_summary,
    load_agent_team_state,
    validate_agent_team_state,
    verify_agent_team_state_outputs,
)
from .agent_team_operator import (
    agent_team_operator_inbox_summary,
    load_agent_team_operator_inbox,
    verify_agent_team_operator_inbox,
)
from .agent_team_chat import (
    agent_team_operator_chat_summary,
    load_agent_team_operator_chat,
    verify_agent_team_operator_chat,
)
from .agent_team_merge import (
    agent_team_merge_intent_summary,
    agent_team_merge_recovery_summary,
    agent_team_merge_summary,
    agent_team_merge_transaction_summary,
    load_agent_team_merge_intent,
    load_agent_team_merge_ledger,
    load_agent_team_merge_recovery,
    load_agent_team_merge_transaction,
)
from .agent_team_quality_retry import (
    agent_team_quality_retry_summary,
    load_agent_team_quality_retry,
)
from .agent_team_turn_completion import (
    agent_team_turn_completion_summary,
    load_agent_team_turn_completion,
)
from .agent_team_turn_terminal import (
    agent_team_turn_terminal_summary,
    load_agent_team_turn_terminal,
)
from .agent_team_plan_approval import (
    agent_team_plan_approval_summary,
    load_agent_team_plan_approval,
    verify_agent_team_plan_approval_outputs,
)
from .agent_team_transcript import list_agent_team_transcript_summaries
from .clock import utc_from_timestamp, utc_now
from .auto_orchestrator import collect_auto_orchestrations
from .artifacts import STANDARD_FILES
from .background_run import load_background_run
from .background_goal import latest_background_model_goal
from .background_supervisor import latest_background_routine_supervisor
from .codex_checkpoint import load_codex_step_checkpoint_from_run_dir
from .codex_step_terminal import (
    codex_step_terminal_path_from_run_dir,
    codex_step_terminal_summary,
    load_codex_step_terminal,
)
from .codex_progress import (
    codex_progress_summary,
    codex_step_progress_path_from_run_dir,
    list_codex_packet_progress,
    load_codex_progress,
)
from .diagnostics import diagnose_run_detail
from .errors import ValidationError
from .model_verdict import (
    COMPLETION_VERDICT_CONTRACT,
    COMPLETION_VERDICT_FEEDBACK_SCHEMA,
    MAX_MODEL_FEEDBACK_CHARS,
    MAX_MODEL_VERDICT_BYTES,
    load_model_verdict_record,
)
from .packet_items import clean_packet_items, read_packet_items_file
from .redaction import APPROVAL_ID_PLACEHOLDER, redact_text
from .routines import iter_routine_manifest_paths, load_routine_manifest, routine_list_record
from .routine_supervisor import load_routine_controls, load_supervisor_state, routine_control_record
from .routine_service import status_routine_service
from .security import (
    ensure_dir_no_follow,
    open_dir_no_follow,
    read_regular_file_bytes_no_follow,
    read_regular_file_prefix_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    require_no_path_escape,
)
from .workflow import workflow_fingerprint


MAX_JSON_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_RUNS = 50
DEFAULT_MAX_REPORTS = 50
DEFAULT_MAX_GOALS = 50
DEFAULT_MAX_AUTO = 50
DEFAULT_MAX_TEXT_BYTES = 4096
FILE_COUNT_LIMIT = 1000
FILE_SCAN_LIMIT = 5000
DETAIL_FILE_LIMIT = 40
DETAIL_TEXT_LIMIT = 2048
DETAIL_AGENT_PACKET_LIMIT = 80
DETAIL_AGENT_TRACE_BYTES = 256 * 1024
DETAIL_AGENT_TRACE_LIMIT = 400
DETAIL_SESSION_TRACE_LIMIT = 80
DETAIL_AGENT_TEAM_QUALITY_RETRY_LIMIT = 256
GOAL_DETAIL_ITERATION_LIMIT = 100
GOAL_HANDLE_PREFIX = "goal-"
STEP_HANDLE_PREFIX = "step-"
AGENT_MAP_TRACE_SCHEMA = "conductor.agent_map_trace.v1"
LAUNCH_MANIFEST_SCHEMA = "conductor.launch_manifest.v1"
LAUNCH_COMMAND_MAX_CHARS = 1200
LAUNCH_RESUME_PREFIX = ["python3", "-B", "-m", "conductor_runtime", "run"]
LAUNCH_RESUME_VALUE_OPTIONS = {"--workspace", "--runs-dir", "--resume", "--max-workers"}
LAUNCH_RESUME_FLAGS = {
    "--allow-writes",
    "--allow-destructive",
    "--allow-network",
    "--allow-agent",
    "--allow-parallel",
}
RUNNER_STALE_AFTER_SECONDS = 5


def write_dashboard(
    runs_dir: Path,
    benchmarks_dir: Path,
    output: Path,
    title: str = "Codex Conductor Dashboard",
    max_runs: int = DEFAULT_MAX_RUNS,
    max_reports: int = DEFAULT_MAX_REPORTS,
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
    goals_dir: Optional[Path] = None,
    max_goals: int = DEFAULT_MAX_GOALS,
    routines_dir: Optional[Path] = None,
    generated_dir: Optional[Path] = None,
    auto_dir: Optional[Path] = None,
    max_auto: int = DEFAULT_MAX_AUTO,
) -> Path:
    html = build_dashboard(
        runs_dir=runs_dir,
        benchmarks_dir=benchmarks_dir,
        goals_dir=goals_dir,
        title=title,
        max_runs=max_runs,
        max_reports=max_reports,
        max_goals=max_goals,
        max_text_bytes=max_text_bytes,
        routines_dir=routines_dir,
        generated_dir=generated_dir,
        auto_dir=auto_dir,
        max_auto=max_auto,
    )
    if output.exists() and output.is_symlink():
        raise ValidationError("output must not be a symlink: %s" % output)
    if output.parent.exists() and output.parent.is_symlink():
        raise ValidationError("dashboard output parent must not be a symlink: %s" % output.parent)
    parent_fd = ensure_dir_no_follow(output.parent, "dashboard output parent")
    os.close(parent_fd)
    replace_text_file_no_follow(output, "output", html, ".dashboard-")
    return output


def build_dashboard(
    runs_dir: Path,
    benchmarks_dir: Path,
    title: str = "Codex Conductor Dashboard",
    max_runs: int = DEFAULT_MAX_RUNS,
    max_reports: int = DEFAULT_MAX_REPORTS,
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
    goals_dir: Optional[Path] = None,
    max_goals: int = DEFAULT_MAX_GOALS,
    routines_dir: Optional[Path] = None,
    generated_dir: Optional[Path] = None,
    auto_dir: Optional[Path] = None,
    max_auto: int = DEFAULT_MAX_AUTO,
) -> str:
    _validate_positive_int(max_runs, "max_runs")
    _validate_positive_int(max_reports, "max_reports")
    _validate_positive_int(max_goals, "max_goals")
    _validate_positive_int(max_auto, "max_auto")
    _validate_positive_int(max_text_bytes, "max_text_bytes")
    resolved_goals_dir = _default_goals_dir(runs_dir) if goals_dir is None else Path(goals_dir)
    resolved_routines_dir = Path(runs_dir).parent / "routines" if routines_dir is None else Path(routines_dir)
    resolved_generated_dir = Path(runs_dir).parent / "generated" if generated_dir is None else Path(generated_dir)
    resolved_auto_dir = resolved_generated_dir / "auto" if auto_dir is None else Path(auto_dir)
    runs = collect_runs(runs_dir, max_runs=max_runs, max_text_bytes=max_text_bytes)
    reports = collect_reports(benchmarks_dir, max_reports=max_reports)
    goals = collect_goals(resolved_goals_dir, max_goals=max_goals)
    auto_runs = collect_auto_orchestrations(
        resolved_auto_dir,
        generated_dir=resolved_generated_dir,
        goals_dir=resolved_goals_dir,
        runs_dir=runs_dir,
        max_records=max_auto,
    )
    run_details = [_safe_run_detail(runs_dir, run["id"], max_text_bytes=max_text_bytes) for run in runs]
    goal_details = [_safe_goal_detail(goal) for goal in goals]
    routine_supervisor = latest_background_routine_supervisor(resolved_routines_dir)
    routine_service = status_routine_service(resolved_routines_dir)
    routines = collect_routines(resolved_routines_dir)
    generated_at = utc_now().isoformat(timespec="seconds") + "Z"
    return _render_dashboard(
        title,
        generated_at,
        runs_dir,
        benchmarks_dir,
        resolved_goals_dir,
        resolved_routines_dir,
        resolved_auto_dir,
        runs,
        auto_runs,
        reports,
        goals,
        run_details,
        goal_details,
        routines,
        routine_supervisor,
        routine_service,
    )


def collect_routines(routines_dir: Path, max_routines: int = 50) -> List[Dict]:
    _validate_positive_int(max_routines, "max_routines")
    try:
        controls = load_routine_controls(routines_dir)
        control_error = ""
    except (OSError, ValidationError) as exc:
        controls = {"controls": {}}
        control_error = redact_text(str(exc))
    try:
        state = load_supervisor_state(routines_dir)
        state_error = ""
    except (OSError, ValidationError) as exc:
        state = {"routines": {}}
        state_error = redact_text(str(exc))
    records = []
    for path in iter_routine_manifest_paths(routines_dir, max_routines=max_routines):
        try:
            manifest = load_routine_manifest(path)
            record = routine_list_record(path, manifest)
            control = routine_control_record(manifest["name"], controls)
            execution = state.get("routines", {}).get(manifest["name"], {})
            execution = execution if isinstance(execution, dict) else {}
            history = execution.get("history") if isinstance(execution.get("history"), list) else []
            record.update(
                {
                    "routine_handle": routine_handle(path.name),
                    "paused": control["paused"],
                    "control_updated_at_utc": control["updated_at_utc"],
                    "control_error": control_error,
                    "last_status": execution.get("last_status") or "never",
                    "last_trigger": execution.get("last_trigger") or "",
                    "last_finished_at_utc": execution.get("last_finished_at_utc") or "",
                    "executions": execution.get("executions") if isinstance(execution.get("executions"), int) else 0,
                    "history_count": len(history),
                    "recent_history": history[-5:],
                    "state_error": state_error,
                }
            )
        except (OSError, ValidationError) as exc:
            record = {
                "path": str(path),
                "name": path.stem,
                "routine_handle": routine_handle(path.name),
                "schedule": "invalid",
                "workflow": "unknown",
                "paused": False,
                "last_status": "invalid",
                "last_trigger": "",
                "last_finished_at_utc": "",
                "executions": 0,
                "history_count": 0,
                "recent_history": [],
                "control_error": control_error,
                "state_error": state_error,
                "error": redact_text(str(exc)),
            }
        records.append(record)
    return records


def collect_runs(
    runs_dir: Path,
    max_runs: int = DEFAULT_MAX_RUNS,
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
    status_filter: Optional[List[str]] = None,
) -> List[Dict]:
    _validate_positive_int(max_runs, "max_runs")
    _validate_positive_int(max_text_bytes, "max_text_bytes")
    if runs_dir.is_symlink():
        raise ValidationError("runs_dir must not be a symlink: %s" % runs_dir)
    if not runs_dir.exists():
        return []
    if not runs_dir.is_dir():
        raise ValidationError("runs_dir is not a directory: %s" % runs_dir)

    statuses = {str(status) for status in (status_filter or []) if str(status)}
    if statuses:
        entries = _scan_directory_entries_no_follow(runs_dir, "dashboard children")
        if entries is None:
            return []
        candidates = sorted(
            ((mtime, name, runs_dir / name) for name, kind, mtime in entries if kind == "dir"),
            reverse=True,
        )
    else:
        candidates = _newest_children(runs_dir, limit=max_runs, kind="dir")
    runs = []
    for mtime, _name, child in candidates:
        run = _summarize_run(child, max_text_bytes=max_text_bytes, mtime=mtime)
        if statuses and run.get("status") not in statuses:
            continue
        runs.append(run)
        if len(runs) >= max_runs:
            break
    return runs


def collect_reports(benchmarks_dir: Path, max_reports: int = DEFAULT_MAX_REPORTS) -> List[Dict]:
    _validate_positive_int(max_reports, "max_reports")
    if benchmarks_dir.is_symlink():
        raise ValidationError("benchmarks_dir must not be a symlink: %s" % benchmarks_dir)
    if not benchmarks_dir.exists():
        return []
    if not benchmarks_dir.is_dir():
        raise ValidationError("benchmarks_dir is not a directory: %s" % benchmarks_dir)

    candidates = _newest_children(benchmarks_dir, limit=max_reports, kind="file", pattern="*.json")
    entries = _scan_directory_entries_no_follow(benchmarks_dir, "dashboard benchmark children") or []
    for directory_name, kind, _mtime in sorted(entries)[:FILE_SCAN_LIMIT]:
        if kind != "dir":
            continue
        child = benchmarks_dir / directory_name
        child_entries = _scan_directory_entries_no_follow(child, "dashboard benchmark bundle") or []
        for name, child_kind, mtime in child_entries:
            if name == "benchmark-report.json" and child_kind == "file":
                candidates.append((mtime, "%s/%s" % (directory_name, name), child / name))
                break
    candidates = sorted(candidates, reverse=True)[:max_reports]
    summaries = []
    for mtime, name, path in candidates:
        summary = _summarize_report(path, mtime=mtime)
        if "/" in name:
            summary["name"] = redact_text(name)
        summaries.append(summary)
    return summaries


def collect_goals(goals_dir: Path, max_goals: int = DEFAULT_MAX_GOALS) -> List[Dict]:
    _validate_positive_int(max_goals, "max_goals")
    if goals_dir.is_symlink():
        raise ValidationError("goals_dir must not be a symlink: %s" % goals_dir)
    if not goals_dir.exists():
        return []
    if not goals_dir.is_dir():
        raise ValidationError("goals_dir is not a directory: %s" % goals_dir)
    candidates = _newest_children(goals_dir, limit=max_goals, kind="file", pattern="*.json")
    return [_summarize_goal(path, mtime=mtime) for mtime, _name, path in candidates]


def goal_handle(goal_name: str) -> str:
    digest = hashlib.sha256(str(goal_name).encode("utf-8")).hexdigest()
    return "%s%s" % (GOAL_HANDLE_PREFIX, digest[:20])


def routine_handle(filename: str) -> str:
    digest = hashlib.sha256(str(filename).encode("utf-8")).hexdigest()
    return "routine-%s" % digest[:20]


def resolve_goal_artifact(goals_dir: Path, handle: str) -> Path:
    if not _valid_goal_handle(handle):
        raise ValidationError("goal_handle is invalid")
    reject_symlink_path(goals_dir, "goals_dir")
    if not goals_dir.exists():
        raise ValidationError("goals_dir does not exist: %s" % goals_dir)
    if not goals_dir.is_dir():
        raise ValidationError("goals_dir is not a directory: %s" % goals_dir)
    goals_fd = open_dir_no_follow(goals_dir, "goals_dir")
    try:
        for name in os.listdir(goals_fd):
            if not name.endswith(".json"):
                continue
            try:
                info = os.stat(name, dir_fd=goals_fd, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if goal_handle(name) == handle:
                require_no_path_escape(name)
                return goals_dir / name
    finally:
        os.close(goals_fd)
    raise ValidationError("goal_handle does not exist")


def collect_goal_detail(
    goals_dir: Path,
    handle: str,
    max_iterations: int = GOAL_DETAIL_ITERATION_LIMIT,
) -> Dict:
    _validate_positive_int(max_iterations, "max_iterations")
    path = resolve_goal_artifact(goals_dir, handle)
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ValidationError("failed to stat goal artifact: %s" % exc.__class__.__name__)
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError("goal artifact must be a regular file")
    return _summarize_goal_detail(path, mtime=info.st_mtime, max_iterations=max_iterations)


def run_handle(run_id: str) -> str:
    digest = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()
    return "run-%s" % digest[:20]


def step_handle(step_id: str) -> str:
    digest = hashlib.sha256(str(step_id).encode("utf-8")).hexdigest()
    return "%s%s" % (STEP_HANDLE_PREFIX, digest[:20])


def collect_run_detail(
    runs_dir: Path,
    run_id: str,
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
    max_files: int = DETAIL_FILE_LIMIT,
) -> Dict:
    _validate_positive_int(max_text_bytes, "max_text_bytes")
    _validate_positive_int(max_files, "max_files")
    detail_text_bytes = min(max_text_bytes, DETAIL_TEXT_LIMIT)
    run_dir = _resolve_run_dir(runs_dir, run_id)
    state, state_error = _read_json(run_dir / "state.json")
    workflow, workflow_error = _read_json(run_dir / "workflow.json")
    state = state if isinstance(state, dict) else {}
    workflow = workflow if isinstance(workflow, dict) else {}
    try:
        workflow_fingerprint_value = workflow_fingerprint(workflow)
    except (TypeError, ValidationError):
        workflow_fingerprint_value = None
    steps = _summarize_steps(state.get("steps"), workflow.get("steps"))
    _attach_codex_partial_outputs(run_dir, steps, workflow.get("steps"))
    _attach_codex_completion_verdicts(run_dir, steps, workflow.get("steps"))
    _attach_codex_step_checkpoints(
        run_dir,
        steps,
        workflow_fingerprint_value=workflow_fingerprint_value,
    )
    phases = _summarize_phases(workflow.get("steps"), steps)
    approvals = _approval_summary(steps)
    runtime = _runtime_summary(workflow.get("steps"), steps, _string_or_unknown(state.get("status")))
    private_team_artifacts, private_team_steps = _agent_team_private_excerpt_scope(workflow)
    private_verdict_artifacts, private_verdict_steps = _completion_verdict_private_scope(
        workflow
    )
    session_trace_events = _summarize_session_trace_events(
        state.get("session_trace_events"),
        private_step_ids=private_team_steps,
    )
    session_trace_summary = _summarize_session_trace_summary(
        state.get("session_trace_summary"),
        state.get("session_trace_events"),
    )
    agent_maps = _agent_map_details(
        run_dir,
        workflow.get("steps"),
        steps,
        state.get("agent_map_packet_generations"),
        max_text_bytes=detail_text_bytes,
        max_packets=min(max_files, DETAIL_AGENT_PACKET_LIMIT),
        workflow_fingerprint_value=workflow_fingerprint_value,
    )
    agent_teams = _agent_team_details(run_dir, workflow)
    agent_native_tool_hooks = _summarize_agent_native_tool_hooks(
        state.get("agent_native_tool_hooks")
    )
    detail = {
        "id": run_dir.name,
        "status": _string_or_unknown(state.get("status")),
        "workflow": _string_or_unknown(state.get("workflow") or workflow.get("name")),
        "state_error": state_error,
        "workflow_error": workflow_error,
        "started_at_utc": _string_or_empty(state.get("started_at_utc")),
        "finished_at_utc": _string_or_empty(state.get("finished_at_utc")),
        "duration_ms": _optional_int(state.get("duration_ms")),
        "runner": summarize_runner(run_dir),
        "background": summarize_background_run(run_dir),
        "launch": summarize_launch_manifest(run_dir, state),
        "usage": _usage_summary(state.get("usage")),
        "approvals": approvals,
        "runtime": runtime,
        "phases": phases,
        "steps": steps,
        "agent_maps": agent_maps,
        "agent_teams": agent_teams,
        "agent_native_tool_hooks": agent_native_tool_hooks,
        "hook_events": _summarize_hook_events(state.get("hook_events")),
        "session_trace_events": session_trace_events,
        "session_trace_summary": session_trace_summary,
        "session_trace_count": session_trace_summary["count"],
        "session_trace_counts": session_trace_summary["by_provider"],
        "session_trace_status_counts": session_trace_summary["by_status"],
        "session_trace_display_count": len(session_trace_events),
        "standard_files": _detail_standard_files(run_dir, max_text_bytes=detail_text_bytes),
        "artifacts": _detail_tree(
            run_dir / "artifacts",
            max_text_bytes=detail_text_bytes,
            max_files=max_files,
            omit_excerpt=lambda name: (
                _is_private_agent_team_artifact(name, private_team_artifacts)
                or _is_private_agent_map_artifact(name)
                or name in private_verdict_artifacts
            ),
        ),
        "logs": _detail_tree(
            run_dir / "logs",
            max_text_bytes=detail_text_bytes,
            max_files=max_files,
            omit_excerpt=lambda name: (
                _is_private_agent_team_log(name, private_team_steps)
                or _is_private_completion_verdict_log(name, private_verdict_steps)
            ),
        ),
    }
    detail["diagnostics"] = diagnose_run_detail(detail)
    return detail


def _newest_children(base: Path, limit: int, kind: str, pattern: Optional[str] = None) -> List[Tuple[float, str, Path]]:
    heap: List[Tuple[float, str, Path]] = []
    if pattern not in {None, "*.json"}:
        raise ValidationError("unsupported dashboard child pattern: %s" % pattern)
    entries = _scan_directory_entries_no_follow(base, "dashboard children")
    if entries is None:
        return []
    for name, child_kind, mtime in entries:
        if pattern == "*.json" and not name.endswith(".json"):
            continue
        if kind not in {"dir", "file"}:
            raise ValidationError("unknown dashboard child kind: %s" % kind)
        if child_kind != kind:
            continue
        item = (mtime, name, base / name)
        if len(heap) < limit:
            heapq.heappush(heap, item)
        elif item > heap[0]:
            heapq.heapreplace(heap, item)
    return sorted(heap, reverse=True)


def _safe_run_detail(runs_dir: Path, run_id: str, max_text_bytes: int) -> Dict:
    try:
        return collect_run_detail(runs_dir, run_id, max_text_bytes=max_text_bytes)
    except ValidationError as exc:
        detail = {
            "id": _string_or_unknown(run_id),
            "status": "unknown",
            "workflow": "unknown",
            "error": redact_text(str(exc)),
            "steps": [],
            "standard_files": [],
            "artifacts": _empty_detail_group(error="unavailable"),
            "logs": _empty_detail_group(error="unavailable"),
        }
        detail["diagnostics"] = diagnose_run_detail(detail)
        return detail


def _safe_goal_detail(goal: Dict) -> Dict:
    path = goal.get("path")
    if not isinstance(path, Path):
        detail = dict(goal)
        detail.update(
            {
                "status": goal.get("status") or "invalid",
                "error": goal.get("error") or "missing goal artifact path",
                "iterations_detail": [],
                "iteration_display_count": 0,
                "iteration_display_limit": GOAL_DETAIL_ITERATION_LIMIT,
                "iterations_truncated": False,
                "output_policy": "raw verifier output omitted; bounded redacted model verdicts and output metrics retained",
            }
        )
        return detail
    try:
        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError("goal artifact must be a regular file")
        return _summarize_goal_detail(path, mtime=info.st_mtime, max_iterations=GOAL_DETAIL_ITERATION_LIMIT)
    except (OSError, ValidationError) as exc:
        detail = dict(goal)
        detail.update(
            {
                "status": goal.get("status") or "invalid",
                "error": redact_text(str(exc)),
                "iterations_detail": [],
                "iteration_display_count": 0,
                "iteration_display_limit": GOAL_DETAIL_ITERATION_LIMIT,
                "iterations_truncated": False,
                "output_policy": "raw verifier output omitted; bounded redacted model verdicts and output metrics retained",
            }
        )
        return detail


def _valid_goal_handle(handle: str) -> bool:
    return (
        isinstance(handle, str)
        and len(handle) == len(GOAL_HANDLE_PREFIX) + 20
        and handle.startswith(GOAL_HANDLE_PREFIX)
        and all(char in "0123456789abcdef" for char in handle[len(GOAL_HANDLE_PREFIX) :])
    )


def _resolve_run_dir(runs_dir: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not run_id or run_id in {".", ".."} or "/" in run_id or "\\" in run_id:
        raise ValidationError("run_id must be a single run directory name")
    require_no_path_escape(run_id)
    reject_symlink_path(runs_dir, "runs_dir")
    if not runs_dir.exists():
        raise ValidationError("runs_dir does not exist: %s" % runs_dir)
    if not runs_dir.is_dir():
        raise ValidationError("runs_dir is not a directory: %s" % runs_dir)
    root_fd = open_dir_no_follow(runs_dir, "runs_dir")
    run_fd = None
    try:
        run_fd = _open_child_dir_no_follow(root_fd, run_id, "run_dir")
        return runs_dir / run_id
    finally:
        if run_fd is not None:
            os.close(run_fd)
        os.close(root_fd)


def _detail_standard_files(run_dir: Path, max_text_bytes: int) -> List[Dict]:
    return [_detail_file(run_dir / name, name, max_text_bytes=max_text_bytes) for name in STANDARD_FILES]


def _detail_tree(
    root: Path,
    max_text_bytes: int,
    max_files: int,
    omit_excerpt: Optional[Callable[[str], bool]] = None,
) -> Dict:
    if root.is_symlink():
        return _empty_detail_group(error="symlink skipped")
    if not root.exists():
        return _empty_detail_group(error="missing")
    if not root.is_dir():
        return _empty_detail_group(error="not a directory")
    files = []
    visited = 0
    stack = [root]
    while stack:
        current = stack.pop()
        entries = _scan_directory_entries_no_follow(current, "detail directory")
        if entries is None:
            continue
        for name, kind, _mtime in sorted(entries, key=lambda item: item[0], reverse=True):
            child = current / name
            visited += 1
            if visited > FILE_SCAN_LIMIT or len(files) >= max_files:
                return {"files": files, "truncated": True, "error": None}
            relative = child.relative_to(root).as_posix()
            if kind == "symlink":
                files.append(_skipped_file(relative, "symlink skipped"))
                continue
            if kind == "file":
                files.append(
                    _detail_file(
                        child,
                        relative,
                        max_text_bytes=max_text_bytes,
                        omit_excerpt=bool(omit_excerpt and omit_excerpt(relative)),
                    )
                )
            elif kind == "dir":
                stack.append(child)
    files.sort(key=lambda item: item["name"])
    return {"files": files, "truncated": False, "error": None}


def _detail_file(
    path: Path,
    name: str,
    max_text_bytes: int,
    omit_excerpt: bool = False,
) -> Dict:
    base = {
        "name": redact_text(name),
        "present": False,
        "bytes": 0,
        "mtime_utc": "",
        "excerpt": {"present": False, "text": "", "truncated": False, "error": None},
        "error": None,
    }
    if path.is_symlink():
        return _skipped_file(name, "symlink skipped")
    if not path.exists():
        base["error"] = "missing"
        return base
    try:
        reject_symlink_path(path, "detail file")
    except ValidationError as exc:
        return _skipped_file(name, str(exc))
    if not path.is_file():
        base["error"] = "not a file"
        return base
    try:
        info = path.stat()
    except OSError as exc:
        base["error"] = exc.__class__.__name__
        return base
    base.update(
        {
            "present": True,
            "bytes": info.st_size,
            "mtime_utc": _utc_from_timestamp(info.st_mtime),
            "excerpt": (
                {
                    "present": False,
                    "text": "",
                    "truncated": False,
                    "error": "private runtime content omitted",
                }
                if omit_excerpt
                else _read_excerpt(path, max_text_bytes=max_text_bytes)
            ),
        }
    )
    return base


def _agent_team_private_excerpt_scope(workflow: Dict) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    capture_dirs = set()
    step_ids = set()
    steps = workflow.get("steps") if isinstance(workflow, dict) else None
    if not isinstance(steps, list):
        return (), ()
    for step in steps:
        if not isinstance(step, dict) or step.get("kind") != "agent_team":
            continue
        capture_dir = step.get("capture_dir")
        report = step.get("report")
        step_id = step.get("id")
        if isinstance(capture_dir, str) and capture_dir:
            capture_dirs.add(capture_dir.rstrip("/"))
        if isinstance(report, str) and report:
            capture_dirs.add(report.rstrip("/"))
        if isinstance(step_id, str) and step_id:
            step_ids.add(step_id)
    return tuple(sorted(capture_dirs)), tuple(sorted(step_ids))


def _is_private_agent_team_artifact(name: str, capture_dirs: Tuple[str, ...]) -> bool:
    return any(name == capture_dir or name.startswith(capture_dir + "/") for capture_dir in capture_dirs)


def _completion_verdict_private_scope(
    workflow: Dict,
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    captures = set()
    step_ids = set()
    steps = workflow.get("steps") if isinstance(workflow, dict) else None
    if not isinstance(steps, list):
        return (), ()
    for step in steps:
        if (
            not isinstance(step, dict)
            or step.get("kind") != "codex_exec"
            or step.get("completion_verdict") != COMPLETION_VERDICT_CONTRACT
        ):
            continue
        step_id = step.get("id")
        capture = step.get("capture")
        if isinstance(step_id, str) and step_id:
            step_ids.add(step_id)
            captures.add(
                capture if isinstance(capture, str) and capture else "%s.md" % step_id
            )
    return tuple(sorted(captures)), tuple(sorted(step_ids))


def _is_private_agent_map_artifact(name: str) -> bool:
    parts = name.split("/")
    return (
        ".agent-map-turn-terminals" in parts
        or ".codex-step-checkpoints" in parts
        or ".codex-step-terminals" in parts
    )


def _is_private_agent_team_log(name: str, step_ids: Tuple[str, ...]) -> bool:
    for step_id in step_ids:
        if name.startswith(step_id + ".team.") or name.startswith(step_id + ".team-plan."):
            return True
        hook_step_id = _dashboard_capture_name(step_id)
        parts = name.split("/")
        if len(parts) >= 4 and parts[0] == "hooks" and parts[3] == hook_step_id:
            return True
    return False


def _is_private_completion_verdict_log(
    name: str,
    step_ids: Tuple[str, ...],
) -> bool:
    return any(
        name in {"%s.stdout.log" % step_id, "%s.stderr.log" % step_id}
        for step_id in step_ids
    )


def _dashboard_capture_name(value: str) -> str:
    safe = "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe[:60] or "item"


def _skipped_file(name: str, error: str) -> Dict:
    return {
        "name": redact_text(name),
        "present": False,
        "bytes": 0,
        "mtime_utc": "",
        "excerpt": {"present": False, "text": "", "truncated": False, "error": error},
        "error": error,
    }


def _empty_detail_group(error: Optional[str] = None) -> Dict:
    return {"files": [], "truncated": False, "error": error}


def _summarize_run(path: Path, max_text_bytes: int, mtime: float) -> Dict:
    state, state_error = _read_json(path / "state.json")
    workflow, workflow_error = _read_json(path / "workflow.json")
    state = state if isinstance(state, dict) else {}
    workflow = workflow if isinstance(workflow, dict) else {}
    steps = _summarize_steps(state.get("steps"), workflow.get("steps"))
    phases = _summarize_phases(workflow.get("steps"), steps)
    approvals = _approval_summary(steps)
    runtime = _runtime_summary(workflow.get("steps"), steps, _string_or_unknown(state.get("status")))
    hook_events = _summarize_hook_events(state.get("hook_events"))
    agent_native_tool_hooks = _summarize_agent_native_tool_hooks(
        state.get("agent_native_tool_hooks")
    )
    session_trace_summary = _summarize_session_trace_summary(
        state.get("session_trace_summary"),
        state.get("session_trace_events"),
    )
    standard_files = _summarize_standard_files(path)
    artifact_count, artifact_truncated = _count_files(path / "artifacts")
    log_count, log_truncated = _count_files(path / "logs")
    excerpt = _read_excerpt(path / "09-final-report.md", max_text_bytes=max_text_bytes)
    evidence_complete = _run_has_completion_evidence(_string_or_unknown(state.get("status")), steps, standard_files, excerpt)

    return {
        "id": path.name,
        "path": path,
        "mtime_utc": _utc_from_timestamp(mtime),
        "status": _string_or_unknown(state.get("status")),
        "workflow": _string_or_unknown(state.get("workflow") or workflow.get("name")),
        "workflow_source": _string_or_empty(state.get("workflow_source") or workflow.get("_source_path")),
        "started_at_utc": _string_or_empty(state.get("started_at_utc")),
        "finished_at_utc": _string_or_empty(state.get("finished_at_utc")),
        "duration_ms": _optional_int(state.get("duration_ms")),
        "runner": summarize_runner(path),
        "background": summarize_background_run(path),
        "launch": summarize_launch_manifest(path, state),
        "state_error": state_error,
        "workflow_error": workflow_error,
        "steps": steps,
        "step_counts": _step_counts(steps),
        "phases": phases,
        "phase_counts": _phase_counts(phases),
        "hook_events": hook_events,
        "hook_counts": _hook_counts(hook_events),
        "agent_native_tool_hooks": agent_native_tool_hooks,
        "session_trace_summary": session_trace_summary,
        "session_trace_count": session_trace_summary["count"],
        "session_trace_counts": session_trace_summary["by_provider"],
        "session_trace_status_counts": session_trace_summary["by_status"],
        "agent_metrics": _agent_metrics(steps),
        "usage": _usage_summary(state.get("usage")),
        "approvals": approvals,
        "runtime": runtime,
        "standard_files": standard_files,
        "artifact_count": artifact_count,
        "artifact_count_truncated": artifact_truncated,
        "log_count": log_count,
        "log_count_truncated": log_truncated,
        "final_report_excerpt": excerpt,
        "evidence_complete": evidence_complete,
    }


def _summarize_goal(path: Path, mtime: float) -> Dict:
    data, error = _read_json(path)
    if not isinstance(data, dict):
        return _invalid_goal(path, mtime, error or "invalid goal artifact")
    if data.get("schema") != "conductor.goal_loop.v1":
        return _invalid_goal(path, mtime, "unsupported goal schema")
    iterations = data.get("iterations")
    if not isinstance(iterations, list):
        return _invalid_goal(path, mtime, "invalid goal iterations")
    sanitized_iterations = [item for item in iterations if isinstance(item, dict)]
    max_iterations = _optional_int(data.get("max_iterations"))
    status = _goal_text(data.get("status"), default="unknown")
    last_iteration = sanitized_iterations[-1] if sanitized_iterations else {}
    check = last_iteration.get("check") if isinstance(last_iteration.get("check"), dict) else {}
    verifier = data.get("verifier") if isinstance(data.get("verifier"), dict) else {}
    planner = data.get("planner") if isinstance(data.get("planner"), dict) else {}
    dynamic_interval = _summarize_dynamic_interval(data.get("dynamic_interval"))
    monitor = _summarize_goal_monitor_config(data.get("monitor"))
    latest_dynamic_wait = {}
    for iteration in reversed(sanitized_iterations):
        latest_dynamic_wait = _summarize_dynamic_wait(iteration.get("dynamic_wait"))
        if latest_dynamic_wait:
            break
    goal_id = _goal_string(data.get("goal_id"), path.stem)
    return {
        "name": redact_text(path.name),
        "path": path,
        "goal_handle": goal_handle(path.name),
        "mtime_utc": _utc_from_timestamp(mtime),
        "goal_id": goal_id,
        "status": status,
        "workflow": _goal_text(data.get("workflow"), default="unknown"),
        "started_at_utc": _goal_text(data.get("started_at_utc"), default=""),
        "updated_at_utc": _goal_updated_at(data),
        "finished_at_utc": _goal_text(data.get("finished_at_utc"), default=""),
        "iterations": len(sanitized_iterations),
        "max_iterations": max_iterations,
        "remaining_iterations": _goal_remaining_iterations(max_iterations, len(sanitized_iterations)),
        "planning_mode": _goal_planning_mode(planner),
        "planning_provider_calls": _goal_planning_provider_calls(sanitized_iterations, planner),
        "planner_session": _summarize_planner_session(planner.get("session")),
        "dynamic_interval": dynamic_interval,
        "monitor": monitor,
        "next_wake": latest_dynamic_wait,
        "last_iteration": _summarize_goal_iteration(last_iteration),
        "verifier_kind": _goal_text(verifier.get("kind") or check.get("kind"), default="command"),
        "verifier_policy": _summarize_goal_verifier_policy(verifier),
        "last_check_kind": _goal_text(check.get("kind"), default="command"),
        "last_check_status": _goal_text(check.get("status"), default=""),
        "last_check_returncode": _optional_signed_int(check.get("returncode")),
        "resume_available": status != "completed",
        "evidence_attention": _goal_needs_attention(status),
        "background": latest_background_model_goal(path.parent, goal_id),
        "error": error,
    }


def _summarize_goal_verifier_policy(verifier: Dict) -> Dict:
    if not isinstance(verifier, dict):
        return {}
    candidate = verifier.get("model") if verifier.get("kind") == "command+model" else verifier
    if not isinstance(candidate, dict) or not candidate.get("policy_version"):
        return {}
    max_tokens = _optional_int(candidate.get("max_tokens"))
    if max_tokens is None:
        return {}
    return {
        "effort": _goal_text(candidate.get("effort"), default="unknown"),
        "max_tokens": max_tokens,
        "token_cap_enforcement": _goal_text(
            candidate.get("token_cap_enforcement"), default="unknown"
        ),
        "completion_checkpoint": candidate.get("completion_checkpoint") is True,
        "isolated_profile": candidate.get("isolated_profile") is True,
        "ephemeral_session": candidate.get("ephemeral_session") is True,
        "policy_version": _goal_text(candidate.get("policy_version"), default="unknown"),
    }


def _invalid_goal(path: Path, mtime: float, error: str) -> Dict:
    return {
        "name": redact_text(path.name),
        "path": path,
        "goal_handle": goal_handle(path.name),
        "mtime_utc": _utc_from_timestamp(mtime),
        "goal_id": redact_text(path.stem),
        "status": "invalid",
        "workflow": "unknown",
        "started_at_utc": "",
        "updated_at_utc": _utc_from_timestamp(mtime),
        "finished_at_utc": "",
        "iterations": 0,
        "max_iterations": None,
        "remaining_iterations": None,
        "planning_mode": "unknown",
        "planning_provider_calls": 0,
        "planner_session": {},
        "dynamic_interval": {},
        "monitor": {},
        "next_wake": {},
        "last_iteration": {},
        "verifier_kind": "unknown",
        "last_check_kind": "unknown",
        "last_check_status": "",
        "last_check_returncode": None,
        "resume_available": False,
        "evidence_attention": True,
        "background": {},
        "error": redact_text(str(error or "invalid goal artifact")),
    }


def _summarize_goal_detail(path: Path, mtime: float, max_iterations: int) -> Dict:
    data, error = _read_json(path)
    if not isinstance(data, dict):
        return _invalid_goal_detail(path, mtime, error or "invalid goal artifact", max_iterations)
    if data.get("schema") != "conductor.goal_loop.v1":
        return _invalid_goal_detail(path, mtime, "unsupported goal schema", max_iterations)
    iterations = data.get("iterations")
    if not isinstance(iterations, list):
        return _invalid_goal_detail(path, mtime, "invalid goal iterations", max_iterations)
    detail = _summarize_goal(path, mtime)
    sanitized_iterations = [item for item in iterations if isinstance(item, dict)]
    display_iterations = sanitized_iterations[-max_iterations:]
    detail.update(
        {
            "iterations_detail": [_summarize_goal_iteration_detail(item) for item in display_iterations],
            "iteration_display_count": len(display_iterations),
            "iteration_display_limit": max_iterations,
            "iterations_truncated": len(sanitized_iterations) > len(display_iterations),
            "output_policy": "raw verifier output, private completion feedback, monitor argv, and monitor event text omitted; bounded redacted verdict and output metrics retained",
        }
    )
    return detail


def _invalid_goal_detail(path: Path, mtime: float, error: str, max_iterations: int) -> Dict:
    detail = _invalid_goal(path, mtime, error)
    detail.update(
        {
            "iterations_detail": [],
            "iteration_display_count": 0,
            "iteration_display_limit": max_iterations,
            "iterations_truncated": False,
            "output_policy": "raw verifier output, private completion feedback, monitor argv, and monitor event text omitted; bounded redacted verdict and output metrics retained",
        }
    )
    return detail


def _summarize_goal_iteration(iteration: Dict) -> Dict:
    if not isinstance(iteration, dict):
        return {}
    result = {
        "iteration": _optional_int(iteration.get("iteration")),
        "status": _goal_text(iteration.get("status"), default=""),
        "run_id": _goal_text(iteration.get("run_id"), default=""),
        "run_status": _goal_text(iteration.get("run_status"), default=""),
        "started_at_utc": _goal_text(iteration.get("started_at_utc"), default=""),
        "finished_at_utc": _goal_text(iteration.get("finished_at_utc"), default=""),
    }
    if isinstance(iteration.get("error_class"), str) and iteration["error_class"]:
        result["error_class"] = _goal_text(iteration["error_class"], default="")
    planner_session = _summarize_planner_session_event(iteration.get("planner_session"))
    if planner_session:
        result["planner_session"] = planner_session
    planning = iteration.get("planning") if isinstance(iteration.get("planning"), dict) else {}
    if planning.get("mode") in {"deterministic-direct", "model-authored"}:
        result["planning_mode"] = planning["mode"]
        result["planning_provider_calls"] = _optional_int(planning.get("provider_calls"))
    completion_verdict = _summarize_completion_verdict_feedback(
        iteration.get("completion_verdict")
    )
    if completion_verdict:
        result["completion_verdict"] = completion_verdict
    dynamic_wait = _summarize_dynamic_wait(iteration.get("dynamic_wait"))
    if dynamic_wait:
        result["dynamic_wait"] = dynamic_wait
    return result


def _summarize_dynamic_interval(raw) -> Dict:
    value = raw if isinstance(raw, dict) else {}
    minimum = _optional_int(value.get("min_delay_minutes"))
    maximum = _optional_int(value.get("max_delay_minutes"))
    added_calls = _optional_int(value.get("additional_provider_calls_per_selection"))
    if (
        value.get("mode") != "model-selected-v1"
        or minimum is None
        or maximum is None
        or not 1 <= minimum <= maximum <= 60
        or added_calls != 0
    ):
        return {}
    return {
        "mode": "model-selected-v1",
        "min_delay_minutes": minimum,
        "max_delay_minutes": maximum,
        "additional_provider_calls_per_selection": 0,
    }


def _summarize_goal_monitor_config(raw) -> Dict:
    value = raw if isinstance(raw, dict) else {}
    argv_sha256 = _goal_sha256(value.get("argv_sha256"))
    argv_count = _optional_int(value.get("argv_count"))
    output_limit = _optional_int(value.get("output_limit_bytes"))
    event_limit = _optional_int(value.get("event_line_max_chars"))
    if (
        value.get("mode") != "stdout-event-v1"
        or not argv_sha256
        or argv_count is None
        or not 1 <= argv_count <= 64
        or value.get("raw_argv_persisted") is not False
        or output_limit != 64 * 1024
        or event_limit != 2000
    ):
        return {}
    return {
        "mode": "stdout-event-v1",
        "argv_sha256": argv_sha256,
        "argv_count": argv_count,
        "raw_argv_persisted": False,
        "output_limit_bytes": output_limit,
        "event_line_max_chars": event_limit,
    }


def _summarize_goal_monitor_record(raw) -> Dict:
    value = raw if isinstance(raw, dict) else {}
    status = value.get("status")
    argv_sha256 = _goal_sha256(value.get("argv_sha256"))
    stdout_bytes = _optional_int(value.get("stdout_bytes"))
    stderr_bytes = _optional_int(value.get("stderr_bytes"))
    event_chars = _optional_int(value.get("event_chars"))
    event_sha256 = _goal_sha256(value.get("event_sha256"))
    returncode = _optional_signed_int(value.get("returncode"))
    if (
        status not in {"pending", "active", "event", "deadline", "failed", "uncertain"}
        or not argv_sha256
        or stdout_bytes is None
        or stderr_bytes is None
        or stdout_bytes < 0
        or stderr_bytes < 0
        or event_chars is None
        or event_chars < 0
        or not isinstance(value.get("timed_out"), bool)
        or not isinstance(value.get("interrupted"), bool)
        or not isinstance(value.get("stdout_truncated"), bool)
        or not isinstance(value.get("stderr_truncated"), bool)
        or not isinstance(value.get("event_present"), bool)
    ):
        return {}
    if status == "event":
        if value.get("event_present") is not True or event_chars < 1 or not event_sha256:
            return {}
    elif value.get("event_present") is not False or event_chars != 0:
        return {}
    return {
        "status": status,
        "argv_sha256": argv_sha256,
        "started_at_utc": _goal_text(value.get("started_at_utc"), default=""),
        "finished_at_utc": _goal_text(value.get("finished_at_utc"), default=""),
        "returncode": returncode,
        "timed_out": value.get("timed_out") is True,
        "interrupted": value.get("interrupted") is True,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "stdout_truncated": value.get("stdout_truncated") is True,
        "stderr_truncated": value.get("stderr_truncated") is True,
        "event_present": value.get("event_present") is True,
        "event_chars": event_chars,
        "event_sha256": event_sha256,
        "error_class": _goal_text(value.get("error_class"), default=""),
    }


def _summarize_dynamic_wait(raw) -> Dict:
    value = raw if isinstance(raw, dict) else {}
    status = value.get("status")
    delay = _optional_int(value.get("delay_minutes"))
    reason_chars = _optional_int(value.get("reason_chars"))
    reason_sha256 = _goal_sha256(value.get("reason_sha256"))
    if (
        status not in {"scheduled", "waiting", "elapsed"}
        or delay is None
        or not 1 <= delay <= 60
        or reason_chars is None
        or not 1 <= reason_chars <= 500
        or not reason_sha256
    ):
        return {}
    result = {
        "status": status,
        "delay_minutes": delay,
        "reason_chars": reason_chars,
        "reason_sha256": reason_sha256,
        "selected_at_utc": _goal_text(value.get("selected_at_utc"), default=""),
        "scheduled_for_utc": _goal_text(value.get("scheduled_for_utc"), default=""),
        "wait_started_at_utc": _goal_text(value.get("wait_started_at_utc"), default=""),
        "elapsed_at_utc": _goal_text(value.get("elapsed_at_utc"), default=""),
    }
    if "monitor" in value:
        monitor = _summarize_goal_monitor_record(value.get("monitor"))
        if not monitor:
            return {}
        result["monitor"] = monitor
    return result


def _goal_planning_mode(planner: Dict) -> str:
    kind = planner.get("kind") if isinstance(planner, dict) else None
    if kind == "deterministic-direct-per-iteration":
        return "deterministic-direct"
    if kind == "progressive-direct-then-model":
        return "progressive"
    if kind == "model-authored-per-iteration":
        return "model-authored"
    return "unknown"


def _goal_planning_provider_calls(iterations: List[Dict], planner: Dict) -> int:
    recorded = []
    for iteration in iterations:
        planning = iteration.get("planning") if isinstance(iteration.get("planning"), dict) else {}
        calls = planning.get("provider_calls")
        if isinstance(calls, int) and not isinstance(calls, bool) and calls >= 0:
            recorded.append(calls)
    if recorded:
        return sum(recorded)
    session = planner.get("session") if isinstance(planner, dict) else {}
    turns = session.get("turns") if isinstance(session, dict) else 0
    return turns if isinstance(turns, int) and not isinstance(turns, bool) and turns >= 0 else 0


def _summarize_planner_session(raw) -> Dict:
    session = raw if isinstance(raw, dict) else {}
    mode = session.get("mode")
    if mode not in {"same-session", "fresh-per-iteration"}:
        return {}
    turns = session.get("turns")
    if not isinstance(turns, int) or isinstance(turns, bool) or turns < 0:
        turns = 0
    return {
        "mode": mode,
        "available": isinstance(session.get("id"), str) and bool(session["id"]),
        "id_sha256": _goal_sha256(session.get("id_sha256")),
        "turns": turns,
    }


def _summarize_planner_session_event(raw) -> Dict:
    session = raw if isinstance(raw, dict) else {}
    mode = session.get("mode")
    if mode not in {"started", "resumed", "unavailable"}:
        return {}
    turn = session.get("turn")
    if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
        turn = None
    return {
        "mode": mode,
        "id_sha256": _goal_sha256(session.get("id_sha256")),
        "id_persisted_in_goal": session.get("id_persisted_in_goal") is True,
        "turn": turn,
    }


def _goal_sha256(value) -> str:
    if not isinstance(value, str) or len(value) != 64:
        return ""
    return value if all(character in "0123456789abcdef" for character in value) else ""


def _summarize_completion_verdict_feedback(raw) -> Dict:
    value = raw if isinstance(raw, dict) else {}
    if value.get("schema") != COMPLETION_VERDICT_FEEDBACK_SCHEMA:
        return {}
    step_id = value.get("step_id")
    confidence = value.get("confidence")
    if (
        not isinstance(step_id, str)
        or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", step_id)
        or value.get("contract") != COMPLETION_VERDICT_CONTRACT
        or confidence not in {"low", "medium", "high"}
        or value.get("feedback_present") is not True
    ):
        return {}
    evidence_count = _optional_int(value.get("evidence_count"))
    feedback_chars = _optional_int(value.get("feedback_chars"))
    output_bytes = _optional_int(value.get("output_bytes"))
    feedback_sha256 = _goal_sha256(value.get("feedback_sha256"))
    output_sha256 = _goal_sha256(value.get("output_sha256"))
    if (
        evidence_count is None
        or evidence_count > 20
        or feedback_chars is None
        or feedback_chars < 1
        or feedback_chars > MAX_MODEL_FEEDBACK_CHARS
        or output_bytes is None
        or output_bytes < 1
        or output_bytes > MAX_MODEL_VERDICT_BYTES
        or not feedback_sha256
        or not output_sha256
    ):
        return {}
    return {
        "schema": COMPLETION_VERDICT_FEEDBACK_SCHEMA,
        "step_id": step_id,
        "contract": COMPLETION_VERDICT_CONTRACT,
        "confidence": confidence,
        "evidence_count": evidence_count,
        "feedback_present": True,
        "feedback_chars": feedback_chars,
        "feedback_sha256": feedback_sha256,
        "output_bytes": output_bytes,
        "output_sha256": output_sha256,
    }


def _summarize_goal_iteration_detail(iteration: Dict) -> Dict:
    detail = _summarize_goal_iteration(iteration)
    check = iteration.get("check") if isinstance(iteration.get("check"), dict) else {}
    summarized_check = {
        "kind": _goal_text(check.get("kind"), default="command"),
        "status": _goal_text(check.get("status"), default=""),
        "returncode": _optional_signed_int(check.get("returncode")),
        "started_at_utc": _goal_text(check.get("started_at_utc"), default=""),
        "finished_at_utc": _goal_text(check.get("finished_at_utc"), default=""),
        "stdout_bytes": _optional_int(check.get("stdout_bytes")),
        "stderr_bytes": _optional_int(check.get("stderr_bytes")),
        "stdout_truncated": _bool_or_none(check.get("stdout_truncated")),
        "stderr_truncated": _bool_or_none(check.get("stderr_truncated")),
        "timed_out": _bool_or_none(check.get("timed_out")),
        "artifact": _goal_text(check.get("artifact"), default=""),
        "error": _goal_text(check.get("error"), default=""),
        "provider_events": _optional_int(check.get("provider_events")),
        "usage": _goal_check_usage(check.get("usage")),
        "verdict": _summarize_goal_verdict(check.get("verdict")),
        "feedback": _goal_text(check.get("feedback"), default="")[:1000],
        "output_excerpts_omitted": bool(check.get("stdout_excerpt") or check.get("stderr_excerpt")),
    }
    if summarized_check["kind"] == "command+model":
        summarized_check.update(
            {
                "short_circuited": _bool_or_none(check.get("short_circuited")),
                "model_provider_calls": _optional_int(check.get("model_provider_calls")),
                "command": _summarize_goal_check_stage(check.get("command")),
                "model": _summarize_goal_check_stage(check.get("model")),
            }
        )
    elif summarized_check["kind"] == "workflow-completion":
        summarized_check.update(
            {
                "provider_calls": _optional_int(check.get("provider_calls")),
                "completion_verdict": _summarize_workflow_completion_verdict(
                    check.get("completion_verdict")
                ),
            }
        )
    detail["check"] = summarized_check
    return detail


def _summarize_goal_check_stage(raw) -> Dict:
    stage = raw if isinstance(raw, dict) else {}
    if not stage:
        return {}
    return {
        "kind": _goal_text(stage.get("kind"), default=""),
        "status": _goal_text(stage.get("status"), default=""),
        "returncode": _optional_signed_int(stage.get("returncode")),
        "timed_out": _bool_or_none(stage.get("timed_out")),
        "error": _goal_text(stage.get("error"), default=""),
    }


def _summarize_goal_verdict(raw) -> Dict:
    verdict = raw if isinstance(raw, dict) else {}
    evidence = verdict.get("evidence") if isinstance(verdict.get("evidence"), list) else []
    result = {
        "satisfied": _bool_or_none(verdict.get("satisfied")),
        "confidence": _goal_text(verdict.get("confidence"), default=""),
        "feedback": _goal_text(verdict.get("feedback"), default="")[:1000],
        "evidence": [
            _goal_text(item, default="")[:500]
            for item in evidence[:20]
            if isinstance(item, str)
        ],
    }
    delay = _optional_int(verdict.get("next_delay_minutes"))
    reason = verdict.get("next_delay_reason")
    if delay is not None and 1 <= delay <= 60 and isinstance(reason, str):
        result["next_delay_minutes"] = delay
        result["next_delay_reason"] = _goal_text(reason, default="")[:500]
    return result


def _summarize_workflow_completion_verdict(raw) -> Dict:
    value = raw if isinstance(raw, dict) else {}
    step_id = value.get("step_id")
    confidence = value.get("confidence")
    evidence_count = _optional_int(value.get("evidence_count"))
    output_bytes = _optional_int(value.get("output_bytes"))
    output_sha256 = _goal_sha256(value.get("output_sha256"))
    if (
        not isinstance(step_id, str)
        or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", step_id)
        or value.get("contract") != COMPLETION_VERDICT_CONTRACT
        or value.get("satisfied") is not True
        or confidence not in {"low", "medium", "high"}
        or evidence_count is None
        or evidence_count < 1
        or evidence_count > 20
        or not isinstance(value.get("feedback_present"), bool)
        or output_bytes is None
        or not 1 <= output_bytes <= MAX_MODEL_VERDICT_BYTES
        or not output_sha256
    ):
        return {}
    return {
        "step_id": step_id,
        "contract": COMPLETION_VERDICT_CONTRACT,
        "satisfied": True,
        "confidence": confidence,
        "evidence_count": evidence_count,
        "feedback_present": value["feedback_present"],
        "output_bytes": output_bytes,
        "output_sha256": output_sha256,
    }


def _goal_check_usage(raw) -> Dict:
    usage = raw if isinstance(raw, dict) else {}
    return {
        "input_tokens": _optional_int(usage.get("input_tokens")),
        "output_tokens": _optional_int(usage.get("output_tokens")),
        "total_tokens": _optional_int(usage.get("total_tokens")),
        "cost_usd": usage.get("cost_usd") if _is_number(usage.get("cost_usd")) else None,
    }


def _goal_text(value, default: str) -> str:
    if isinstance(value, str) and (value.strip() or default == ""):
        return redact_text(value)
    return default


def _goal_string(value, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return redact_text(value)
    return redact_text(fallback)


def _goal_updated_at(data: Dict) -> str:
    for key in ("updated_at_utc", "finished_at_utc", "started_at_utc"):
        value = data.get(key)
        if isinstance(value, str):
            return redact_text(value)
    return ""


def _goal_remaining_iterations(max_iterations: Optional[int], iterations: int) -> Optional[int]:
    if max_iterations is None:
        return None
    return max(0, max_iterations - iterations)


def _goal_needs_attention(status: str) -> bool:
    return status not in {"completed"}


def summarize_runner(run_dir: Path) -> Dict:
    data, error = _read_json(run_dir / "runner.json")
    if not isinstance(data, dict):
        return {
            "status": "unavailable",
            "liveness": "unavailable",
            "stale_after_seconds": RUNNER_STALE_AFTER_SECONDS,
            "error": error or "missing",
        }
    summary = {
        "status": _string_or_unknown(data.get("status")),
        "event": _string_or_empty(data.get("event")),
        "pid": _optional_int(data.get("pid")),
        "hostname": _string_or_empty(data.get("hostname")),
        "mode": _string_or_empty(data.get("mode")),
        "run_status": _string_or_empty(data.get("run_status")),
        "started_at_utc": _string_or_empty(data.get("started_at_utc")),
        "heartbeat_at_utc": _string_or_empty(data.get("heartbeat_at_utc")),
        "finished_at_utc": _string_or_empty(data.get("finished_at_utc")),
        "max_workers": _optional_int(data.get("max_workers")),
        "active_process_count": _runner_active_process_count(data.get("active_processes")),
        "error": error,
    }
    summary.update(_runner_liveness(summary))
    return summary


def summarize_background_run(run_dir: Path) -> Dict:
    try:
        data = load_background_run(run_dir)
    except FileNotFoundError:
        return {"status": "unavailable", "error": "missing"}
    except ValidationError as exc:
        return {"status": "invalid", "error": redact_text(str(exc))}
    worker = data["worker"]
    policy = data["policy"]
    handoff = data["handoff"]
    return {
        "status": _string_or_unknown(data.get("status")),
        "run_status": _string_or_unknown(data.get("run_status")),
        "created_at_utc": _string_or_empty(data.get("created_at_utc")),
        "updated_at_utc": _string_or_empty(data.get("updated_at_utc")),
        "finished_at_utc": _string_or_empty(data.get("finished_at_utc")),
        "pid": _optional_int(worker.get("pid")),
        "process_group_id": _optional_int(worker.get("process_group_id")),
        "session_id": _optional_int(worker.get("session_id")),
        "detached_session": _bool_or_none(worker.get("detached_session")),
        "approval_count": _optional_int(policy.get("approval_count")),
        "approval_values_persisted": _bool_or_none(policy.get("approval_values_persisted")),
        "payload_persisted": _bool_or_none(handoff.get("payload_persisted")),
        "daemon_installed": _bool_or_none(handoff.get("daemon_installed")),
        "max_workers": _optional_int(handoff.get("max_workers")),
        "exit_code": _optional_int(data.get("exit_code")),
        "error_class": _string_or_empty(data.get("error_class")),
        "logs": dict(data["logs"]),
        "error": "",
    }


def summarize_launch_manifest(run_dir: Path, state: Optional[Dict] = None) -> Dict:
    state = state if isinstance(state, dict) else {}
    prepared = state.get("prepared_run") if isinstance(state.get("prepared_run"), dict) else {}
    manifest_name = _string_or_empty(prepared.get("launch_manifest")) or "launch.json"
    if manifest_name in {".", ".."} or "/" in manifest_name or "\\" in manifest_name:
        return {
            "status": "invalid",
            "schema": "unknown",
            "kind": "unknown",
            "manifest": redact_text(manifest_name),
            "error": "invalid launch_manifest path",
        }
    try:
        require_no_path_escape(manifest_name)
    except ValidationError:
        return {
            "status": "invalid",
            "schema": "unknown",
            "kind": "unknown",
            "manifest": redact_text(manifest_name),
            "error": "invalid launch_manifest path",
        }

    data, error = _read_json(run_dir / manifest_name)
    if not isinstance(data, dict):
        manifest_error = error or "invalid launch manifest object"
        return {
            "status": "missing" if error == "missing" and prepared else "unavailable",
            "schema": "unknown",
            "kind": _string_or_unknown(prepared.get("schema")),
            "manifest": redact_text(manifest_name),
            "prepared_at_utc": _launch_string(prepared.get("prepared_at_utc")),
            "preflight_status": _launch_string(prepared.get("preflight_status")),
            "no_process_started": _bool_or_none(prepared.get("no_process_started")),
            "approval_values_persisted": _bool_or_none(prepared.get("approval_values_persisted")),
            "error": manifest_error,
        }

    schema = _string_or_unknown(data.get("schema"))
    kind = _string_or_unknown(data.get("kind"))
    if schema != LAUNCH_MANIFEST_SCHEMA:
        return {
            "status": "invalid",
            "schema": schema,
            "kind": kind,
            "manifest": redact_text(manifest_name),
            "error": "unsupported schema",
        }
    if kind != "prepared-run":
        return {
            "status": "invalid",
            "schema": schema,
            "kind": kind,
            "manifest": redact_text(manifest_name),
            "error": "unsupported kind",
        }

    preflight = data.get("preflight") if isinstance(data.get("preflight"), dict) else {}
    policy = data.get("policy") if isinstance(data.get("policy"), dict) else {}
    contract = (
        data.get("external_supervisor_contract")
        if isinstance(data.get("external_supervisor_contract"), dict)
        else {}
    )
    command, command_truncated, command_error = _launch_resume_command(data)
    return {
        "status": "prepared",
        "schema": schema,
        "kind": kind,
        "manifest": redact_text(manifest_name),
        "created_at_utc": _launch_string(data.get("created_at_utc")),
        "workflow": _launch_string(data.get("workflow")),
        "preflight_status": _launch_string(preflight.get("status")),
        "dry_run": _bool_or_none(preflight.get("dry_run")),
        "steps_planned": _optional_int(preflight.get("steps_planned")),
        "no_process_started": _bool_or_none(data.get("no_process_started")),
        "process_model": _launch_string(data.get("process_model")),
        "resume_command": command,
        "resume_command_truncated": command_truncated,
        "resume_command_error": command_error,
        "policy": {
            "allow_writes": _bool_or_none(policy.get("allow_writes")),
            "allow_destructive": _bool_or_none(policy.get("allow_destructive")),
            "allow_network": _bool_or_none(policy.get("allow_network")),
            "allow_agent": _bool_or_none(policy.get("allow_agent")),
            "allow_parallel": _bool_or_none(policy.get("allow_parallel")),
            "approval_count": _optional_int(policy.get("approval_count")),
            "approval_values_persisted": _bool_or_none(policy.get("approval_values_persisted")),
        },
        "max_workers": _optional_int(data.get("max_workers")),
        "external_supervisor": {
            "must_not_assume_daemon": _bool_or_none(contract.get("must_not_assume_daemon")),
            "requires_operator_or_supervisor_approval": _bool_or_none(
                contract.get("must_not_run_without_operator_or_supervisor_approval")
            ),
            "approval_tokens_required_at_launch": _bool_or_none(
                contract.get("approval_tokens_must_be_supplied_at_launch_if_required")
            ),
            "start_in_separate_session_recommended": _bool_or_none(
                contract.get("start_in_separate_session_if_terminate_run_is_required")
            ),
        },
        "error": error,
    }


def _launch_resume_command(data: Dict) -> Tuple[str, bool, str]:
    argv = data.get("resume_command_argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        return "", False, "invalid resume_command_argv"
    error = _validate_launch_resume_argv(argv)
    if error:
        return "", False, error
    text = redact_text(shlex.join([redact_text(item) for item in argv]))
    if len(text) <= LAUNCH_COMMAND_MAX_CHARS:
        return text, False, ""
    return text[:LAUNCH_COMMAND_MAX_CHARS] + "...", True, ""


def _validate_launch_resume_argv(argv: List[str]) -> str:
    if len(argv) < len(LAUNCH_RESUME_PREFIX) + 2:
        return "invalid resume_command_argv"
    if argv[: len(LAUNCH_RESUME_PREFIX)] != LAUNCH_RESUME_PREFIX:
        return "invalid resume_command_argv"
    workflow_arg = argv[len(LAUNCH_RESUME_PREFIX)]
    if not workflow_arg or workflow_arg.startswith("-"):
        return "invalid workflow argument"
    i = len(LAUNCH_RESUME_PREFIX) + 1
    seen_resume = False
    while i < len(argv):
        option = argv[i]
        if option == "--approve" or option.startswith("--approve="):
            return "approval values must not be persisted"
        if option in LAUNCH_RESUME_FLAGS:
            i += 1
            continue
        if option not in LAUNCH_RESUME_VALUE_OPTIONS:
            return "unsupported resume command argument"
        if i + 1 >= len(argv) or argv[i + 1] == "":
            return "missing resume command option value"
        if option == "--resume":
            seen_resume = True
        if option == "--max-workers":
            try:
                workers = int(argv[i + 1])
            except ValueError:
                return "invalid max-workers value"
            if workers <= 0:
                return "invalid max-workers value"
        i += 2
    if not seen_resume:
        return "missing --resume"
    return ""


def _launch_string(value, limit: int = 300) -> str:
    if not isinstance(value, str):
        return ""
    text = redact_text(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _bool_or_none(value):
    return value if isinstance(value, bool) else None


def _runner_active_process_count(value) -> Optional[int]:
    if not isinstance(value, list) or len(value) > 1000:
        return None
    for record in value:
        if not isinstance(record, dict) or _optional_int(record.get("pid")) is None:
            return None
    return len(value)


def _runner_liveness(runner: Dict) -> Dict:
    status = _string_or_unknown(runner.get("status"))
    result = {
        "liveness": "unknown",
        "heartbeat_age_seconds": None,
        "stale_after_seconds": RUNNER_STALE_AFTER_SECONDS,
        "liveness_reason": "",
    }
    if status == "unavailable":
        result["liveness"] = "unavailable"
        result["liveness_reason"] = _string_or_empty(runner.get("error")) or "missing runner metadata"
        return result
    if status == "finished":
        result["liveness"] = "finished"
        result["liveness_reason"] = "runner finished"
        return result
    heartbeat = _parse_utc_timestamp(runner.get("heartbeat_at_utc"))
    if heartbeat is None:
        result["liveness_reason"] = "missing or invalid heartbeat"
        return result
    age_seconds = max(0, int((utc_now() - heartbeat).total_seconds()))
    result["heartbeat_age_seconds"] = age_seconds
    if status == "active":
        if age_seconds > RUNNER_STALE_AFTER_SECONDS:
            result["liveness"] = "stale"
            result["liveness_reason"] = "heartbeat older than %s seconds" % RUNNER_STALE_AFTER_SECONDS
        else:
            result["liveness"] = "alive"
            result["liveness_reason"] = "recent heartbeat"
    else:
        result["liveness_reason"] = "runner status is %s" % status
    return result


def _parse_utc_timestamp(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _summarize_report(path: Path, mtime: float) -> Dict:
    data, error = _read_json(path)
    if not isinstance(data, dict):
        data = {}
    schema = data.get("schema")
    base = {
        "path": path,
        "name": redact_text(path.name),
        "mtime_utc": _utc_from_timestamp(mtime),
        "schema": _report_string(schema),
        "error": error,
    }
    if schema == "conductor.benchmark_report.v1":
        report_error = error or _benchmark_report_shape_error(data)
        provider = _benchmark_provider_summary(data)
        base.update(
            {
                "kind": "benchmark",
                "suite": _report_string(data.get("suite")),
                "system": _report_string(data.get("system")),
                "result": "%s/%s passed" % (_safe_int(data.get("passed_tasks")), _safe_int(data.get("total_tasks"))),
                "failed_tasks": _safe_int(data.get("failed_tasks")),
                "duration_ms": _safe_int(data.get("duration_ms")),
                "claim_eligible": None,
                "warnings": provider.get("warnings", []),
                "provider": provider,
                "error": report_error,
            }
        )
    elif schema == "conductor.comparison_report.v1":
        report_error = error or _comparison_report_shape_error(data)
        provenance = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
        systems = data.get("systems") if isinstance(data.get("systems"), list) else []
        system_names = []
        system_failures = 0
        system_totals = 0
        for system in systems:
            if isinstance(system, dict):
                system_names.append(_report_string(system.get("system")))
                system_failures += _safe_int(system.get("failed_tasks"))
                system_totals += _safe_int(system.get("total_tasks"))
        warnings = provenance.get("warnings") if isinstance(provenance.get("warnings"), list) else []
        base.update(
            {
                "kind": "comparison",
                "suite": _report_string(data.get("suite")),
                "system": ", ".join(system_names) if system_names else "unknown",
                "result": "%s systems, %s/%s failed tasks" % (len(system_names), system_failures, system_totals),
                "failed_tasks": system_failures,
                "duration_ms": None,
                "claim_eligible": provenance.get("claim_eligible"),
                "provenance_complete": provenance.get("complete"),
                "all_live": provenance.get("all_live"),
                "warnings": [_report_string(item) for item in warnings],
                "error": report_error,
            }
        )
    elif schema == "conductor.live_parity_runbook.v1":
        report_error = error or _live_parity_runbook_shape_error(data)
        systems = data.get("systems") if isinstance(data.get("systems"), list) else []
        system_names = [
            _report_string(system.get("name"))
            for system in systems
            if isinstance(system, dict)
        ]
        tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
        base.update(
            {
                "kind": "runbook",
                "suite": _report_string(data.get("suite")),
                "system": ", ".join(system_names) if system_names else "unknown",
                "result": "%s tasks planned" % len(tasks),
                "failed_tasks": None,
                "duration_ms": None,
                "claim_eligible": False,
                "provenance_complete": False,
                "all_live": False,
                "warnings": [],
                "error": report_error,
            }
        )
    elif schema == "conductor.run_manifest.v1":
        report_error = error or _run_manifest_shape_error(data)
        live_run = data.get("live_run") is True
        base.update(
            {
                "kind": "run-manifest",
                "suite": _report_string(data.get("suite")),
                "system": _report_string(data.get("system")),
                "result": "live run provenance" if live_run else "non-live provenance template",
                "failed_tasks": None,
                "duration_ms": None,
                "claim_eligible": False,
                "provenance_complete": live_run,
                "all_live": live_run,
                "warnings": [],
                "error": report_error,
            }
        )
    elif schema == "conductor.evidence_bundle.v1":
        report_error = error or _evidence_bundle_shape_error(data)
        claim = data.get("claim") if isinstance(data.get("claim"), dict) else {}
        counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
        warnings = claim.get("warnings") if isinstance(claim.get("warnings"), list) else []
        base.update(
            {
                "kind": "evidence-bundle",
                "suite": "aggregated evidence",
                "system": "multiple",
                "result": "%s; %s indexed artifacts"
                % (_report_string(claim.get("status")), _safe_int(counts.get("reports"))),
                "failed_tasks": None,
                "duration_ms": None,
                "claim_eligible": claim.get("parity_claim_eligible"),
                "provenance_complete": claim.get("parity_claim_eligible"),
                "all_live": claim.get("parity_claim_eligible"),
                "warnings": [_report_string(item) for item in warnings],
                "error": report_error,
            }
        )
    else:
        base.update(
            {
                "kind": "unknown",
                "suite": "unknown",
                "system": "unknown",
                "result": "unknown",
                "failed_tasks": None,
                "duration_ms": None,
                "claim_eligible": None,
                "warnings": [],
            }
        )
    base.update(_report_evidence_status(base))
    return base


def _report_string(value, default: str = "unknown") -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    return redact_text(value.strip())


def _benchmark_report_shape_error(data: Dict) -> Optional[str]:
    if not isinstance(data.get("suite"), str) or not data["suite"].strip():
        return "invalid benchmark report"
    for field in ["total_tasks", "passed_tasks", "failed_tasks", "duration_ms"]:
        if not _valid_non_negative_int(data.get(field)):
            return "invalid benchmark report"
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return "invalid benchmark report"
    passed = 0
    for result in results:
        if not isinstance(result, dict):
            return "invalid benchmark report"
        if not isinstance(result.get("id"), str) or not result["id"].strip():
            return "invalid benchmark report"
        if not isinstance(result.get("passed"), bool):
            return "invalid benchmark report"
        for field in ["duration_ms"]:
            if not _valid_non_negative_int(result.get(field)):
                return "invalid benchmark report"
        for field in ["timed_out", "stdout_truncated", "stderr_truncated"]:
            if not isinstance(result.get(field), bool):
                return "invalid benchmark report"
        if result["passed"]:
            passed += 1
    if data.get("total_tasks") != len(results):
        return "invalid benchmark report"
    if data.get("passed_tasks") != passed:
        return "invalid benchmark report"
    if data.get("failed_tasks") != len(results) - passed:
        return "invalid benchmark report"
    for result in results:
        provider = result.get("provider_evidence")
        staged = result.get("staged_evidence")
        repair = result.get("repair_evidence")
        quality = result.get("quality_evidence")
        execution_passed = result.get("execution_passed", result.get("passed"))
        if quality is not None:
            try:
                from .benchmark_quality import validate_benchmark_quality_evidence

                if not isinstance(result.get("execution_passed"), bool):
                    return "invalid benchmark report"
                validate_benchmark_quality_evidence(
                    quality,
                    source="dashboard benchmark quality evidence",
                    result=result,
                )
            except ValidationError:
                return "invalid benchmark report"
            if result["passed"] != (execution_passed and quality["passed"]):
                return "invalid benchmark report"
        elif "execution_passed" in result:
            return "invalid benchmark report"
        if repair is not None or "repair_attempt_reports" in result:
            try:
                from .codex_staged_repair import validate_codex_staged_repair_benchmark_result

                if provider is not None or staged is not None:
                    return "invalid benchmark report"
                validate_codex_staged_repair_benchmark_result(
                    result,
                    source="dashboard Codex staged repair result",
                )
            except ValidationError:
                return "invalid benchmark report"
        if provider is not None:
            try:
                from .benchmark import _validate_provider_evidence

                _validate_provider_evidence(
                    provider,
                    source="dashboard benchmark provider evidence",
                )
            except ValidationError:
                return "invalid benchmark report"
        if staged is not None:
            try:
                from .benchmark import _validate_staged_evidence

                _validate_staged_evidence(
                    staged,
                    source="dashboard benchmark staged evidence",
                )
            except ValidationError:
                return "invalid benchmark report"
            if not isinstance(provider, dict) or provider["policy"]["read_only_tools"]:
                return "invalid benchmark report"
            provider_status = provider["status"]
            staged_status = staged["status"]
            if provider_status == "workspace-drift":
                if staged_status not in {"source-drift", "invalid-stage"}:
                    return "invalid benchmark report"
            elif provider_status in {"budget-exceeded", "token-budget-exceeded"}:
                pass
            elif (staged_status == "provider-failed") != (provider_status != "success"):
                return "invalid benchmark report"
            if execution_passed != (provider["status"] == "success" and staged["status"] == "success"):
                return "invalid benchmark report"
        elif isinstance(provider, dict) and not provider["policy"]["read_only_tools"]:
            return "invalid benchmark report"
        elif isinstance(provider, dict) and execution_passed != (provider["status"] == "success"):
            return "invalid benchmark report"
    return None


def _benchmark_provider_summary(data: Dict) -> Dict:
    records = []
    warnings = []
    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        repair = result.get("repair_evidence")
        if isinstance(repair, dict):
            attempts = repair.get("attempts") if isinstance(repair.get("attempts"), list) else []
            usage = repair.get("usage") if isinstance(repair.get("usage"), dict) else {}
            final = repair.get("final") if isinstance(repair.get("final"), dict) else {}
            quality = result.get("quality_evidence") if isinstance(result.get("quality_evidence"), dict) else {}
            embedded_reports = (
                result.get("repair_attempt_reports")
                if isinstance(result.get("repair_attempt_reports"), list)
                else []
            )
            partial = any(
                report.get("results", [{}])[0].get("provider_evidence", {}).get("observed", {}).get(
                    "partial_output_preserved"
                )
                is True
                for report in embedded_reports
                if isinstance(report, dict) and report.get("results")
            )
            records.append(
                {
                    "status": _report_string(repair.get("status")),
                    "cost_usd": None,
                    "budget_overshoot_usd": None,
                    "main_models": sorted(
                        {
                            _report_string(attempt.get("model"))
                            for attempt in attempts
                            if isinstance(attempt, dict) and isinstance(attempt.get("model"), str)
                        }
                    ),
                    "helper_models": [],
                    "partial_output_preserved": partial,
                    "total_tokens": usage.get("gross_total_tokens")
                    if isinstance(usage.get("gross_total_tokens"), int)
                    else None,
                    "input_tokens": None,
                    "cache_creation_input_tokens": None,
                    "cache_read_input_tokens": None,
                    "output_tokens": None,
                    "token_accounting": None,
                    "rollout_budget_tokens": usage.get("rollout_budget_tokens")
                    if isinstance(usage.get("rollout_budget_tokens"), int)
                    else None,
                    "rollout_budget_overshoot": 0,
                    "token_overshoot": None,
                    "model_binding": "command-enforced",
                    "service_tier": None,
                    "service_tier_binding": None,
                    "source_unchanged": repair.get("source", {}).get("unchanged")
                    if isinstance(repair.get("source"), dict)
                    else None,
                    "stage_unchanged": False,
                    "stage_mutation_allowed": True,
                    "execution_mode": "staged-repair",
                    "quality_score": quality.get("score")
                    if isinstance(quality.get("score"), int) and not isinstance(quality.get("score"), bool)
                    else None,
                    "quality_max_score": quality.get("max_score")
                    if isinstance(quality.get("max_score"), int)
                    and not isinstance(quality.get("max_score"), bool)
                    else None,
                    "quality_independent": quality.get("reviewer", {}).get("independent")
                    if isinstance(quality.get("reviewer"), dict)
                    else None,
                    "staged_status": _report_string(repair.get("status")),
                    "change_count": final.get("change_count")
                    if isinstance(final.get("change_count"), int)
                    else None,
                    "verification_status": _report_string(final.get("verification_status"))
                    if isinstance(final.get("verification_status"), str)
                    else None,
                    "patch_written": final.get("patch_written") is True,
                }
            )
            warnings.extend(
                _report_string(incident.get("description"))
                for incident in repair.get("incidents", [])
                if isinstance(incident, dict) and isinstance(incident.get("description"), str)
            )
            continue
        evidence = result.get("provider_evidence")
        if not isinstance(evidence, dict) or evidence.get("schema") not in {
            "conductor.claude_provider_evidence.v1",
            "conductor.claude_provider_evidence.v2",
            "conductor.claude_provider_evidence.v3",
            "conductor.codex_provider_evidence.v1",
            "conductor.codex_provider_evidence.v2",
            "conductor.codex_provider_evidence.v3",
            "conductor.codex_provider_evidence.v4",
            "conductor.codex_provider_evidence.v5",
        }:
            continue
        is_codex = evidence.get("schema") in {
            "conductor.codex_provider_evidence.v1",
            "conductor.codex_provider_evidence.v2",
            "conductor.codex_provider_evidence.v3",
            "conductor.codex_provider_evidence.v4",
            "conductor.codex_provider_evidence.v5",
        }
        requested = evidence.get("requested") if isinstance(evidence.get("requested"), dict) else {}
        observed = evidence.get("observed") if isinstance(evidence.get("observed"), dict) else {}
        workspace = evidence.get("workspace") if isinstance(evidence.get("workspace"), dict) else {}
        incidents = evidence.get("incidents") if isinstance(evidence.get("incidents"), list) else []
        staged = result.get("staged_evidence") if isinstance(result.get("staged_evidence"), dict) else {}
        quality = result.get("quality_evidence") if isinstance(result.get("quality_evidence"), dict) else {}
        staged_changes = staged.get("changes") if isinstance(staged.get("changes"), dict) else {}
        staged_verification = staged.get("verification") if isinstance(staged.get("verification"), dict) else {}
        records.append(
            {
                "status": _report_string(evidence.get("status")),
                "cost_usd": observed.get("cost_usd")
                if isinstance(observed.get("cost_usd"), (int, float)) and not isinstance(observed.get("cost_usd"), bool)
                else None,
                "budget_overshoot_usd": observed.get("budget_overshoot_usd")
                if isinstance(observed.get("budget_overshoot_usd"), (int, float))
                and not isinstance(observed.get("budget_overshoot_usd"), bool)
                else None,
                "main_models": [
                    _report_string(model)
                    for model in ([requested.get("model")] if is_codex else observed.get("main_models", []))
                    if isinstance(model, str)
                ],
                "helper_models": [
                    _report_string(model)
                    for model in observed.get("helper_models", [])
                    if isinstance(model, str)
                ],
                "partial_output_preserved": observed.get("partial_output_preserved") is True,
                "total_tokens": observed.get("total_tokens")
                if isinstance(observed.get("total_tokens"), int)
                and not isinstance(observed.get("total_tokens"), bool)
                else None,
                "input_tokens": observed.get("input_tokens")
                if isinstance(observed.get("input_tokens"), int)
                and not isinstance(observed.get("input_tokens"), bool)
                else None,
                "cache_creation_input_tokens": observed.get("cache_creation_input_tokens")
                if isinstance(observed.get("cache_creation_input_tokens"), int)
                and not isinstance(observed.get("cache_creation_input_tokens"), bool)
                else None,
                "cache_read_input_tokens": observed.get("cache_read_input_tokens")
                if isinstance(observed.get("cache_read_input_tokens"), int)
                and not isinstance(observed.get("cache_read_input_tokens"), bool)
                else None,
                "output_tokens": observed.get("output_tokens")
                if isinstance(observed.get("output_tokens"), int)
                and not isinstance(observed.get("output_tokens"), bool)
                else None,
                "token_accounting": _report_string(observed.get("token_accounting"))
                if isinstance(observed.get("token_accounting"), str)
                else None,
                "rollout_budget_tokens": observed.get("rollout_budget_tokens")
                if isinstance(observed.get("rollout_budget_tokens"), int)
                and not isinstance(observed.get("rollout_budget_tokens"), bool)
                else None,
                "rollout_budget_overshoot": observed.get("rollout_budget_overshoot")
                if isinstance(observed.get("rollout_budget_overshoot"), int)
                and not isinstance(observed.get("rollout_budget_overshoot"), bool)
                else None,
                "token_overshoot": observed.get("token_overshoot")
                if isinstance(observed.get("token_overshoot"), int)
                and not isinstance(observed.get("token_overshoot"), bool)
                else None,
                "model_binding": _report_string(observed.get("model_binding"))
                if is_codex and isinstance(observed.get("model_binding"), str)
                else None,
                "service_tier": _report_string(requested.get("service_tier"))
                if is_codex and isinstance(requested.get("service_tier"), str)
                else None,
                "service_tier_binding": _report_string(observed.get("service_tier_binding"))
                if is_codex and isinstance(observed.get("service_tier_binding"), str)
                else None,
                "source_unchanged": workspace.get("source_unchanged") if is_codex else None,
                "stage_unchanged": workspace.get("stage_unchanged") if is_codex else None,
                "stage_mutation_allowed": workspace.get("stage_mutation_allowed") if is_codex else None,
                "execution_mode": _report_string(requested.get("execution_mode"))
                if is_codex and isinstance(requested.get("execution_mode"), str)
                else "read-only" if is_codex else None,
                "quality_score": quality.get("score")
                if isinstance(quality.get("score"), int) and not isinstance(quality.get("score"), bool)
                else None,
                "quality_max_score": quality.get("max_score")
                if isinstance(quality.get("max_score"), int) and not isinstance(quality.get("max_score"), bool)
                else None,
                "quality_independent": quality.get("reviewer", {}).get("independent")
                if isinstance(quality.get("reviewer"), dict)
                else None,
                "staged_status": _report_string(staged.get("status")) if staged else None,
                "change_count": staged_changes.get("change_count")
                if isinstance(staged_changes.get("change_count"), int)
                and not isinstance(staged_changes.get("change_count"), bool)
                else None,
                "verification_status": _report_string(staged_verification.get("status"))
                if staged_verification
                else None,
                "patch_written": staged_changes.get("patch_written") is True,
            }
        )
        warnings.extend(
            _report_string(incident.get("description"))
            for incident in incidents
            if isinstance(incident, dict) and isinstance(incident.get("description"), str)
        )
        warnings.extend(
            _report_string(incident.get("description"))
            for incident in staged.get("incidents", [])
            if isinstance(incident, dict) and isinstance(incident.get("description"), str)
        )
    if not records:
        return {}
    statuses = sorted({record["status"] for record in records})
    main_models = sorted({model for record in records for model in record["main_models"]})
    helper_models = sorted({model for record in records for model in record["helper_models"]})
    costs = [record["cost_usd"] for record in records if record["cost_usd"] is not None]
    overshoots = [
        record["budget_overshoot_usd"]
        for record in records
        if record["budget_overshoot_usd"] is not None
    ]
    staged_statuses = sorted({record["staged_status"] for record in records if record["staged_status"]})
    verification_statuses = sorted(
        {record["verification_status"] for record in records if record["verification_status"]}
    )
    change_counts = [record["change_count"] for record in records if record["change_count"] is not None]
    total_tokens = [record["total_tokens"] for record in records if record["total_tokens"] is not None]
    input_tokens = [record["input_tokens"] for record in records if record["input_tokens"] is not None]
    cache_creation_input_tokens = [
        record["cache_creation_input_tokens"]
        for record in records
        if record["cache_creation_input_tokens"] is not None
    ]
    cache_read_input_tokens = [
        record["cache_read_input_tokens"]
        for record in records
        if record["cache_read_input_tokens"] is not None
    ]
    output_tokens = [record["output_tokens"] for record in records if record["output_tokens"] is not None]
    token_accountings = sorted({record["token_accounting"] for record in records if record["token_accounting"]})
    rollout_budget_tokens = [
        record["rollout_budget_tokens"]
        for record in records
        if record["rollout_budget_tokens"] is not None
    ]
    rollout_budget_overshoots = [
        record["rollout_budget_overshoot"]
        for record in records
        if record["rollout_budget_overshoot"] is not None
    ]
    token_overshoots = [record["token_overshoot"] for record in records if record["token_overshoot"] is not None]
    model_bindings = sorted({record["model_binding"] for record in records if record["model_binding"]})
    service_tiers = sorted({record["service_tier"] for record in records if record["service_tier"]})
    service_tier_bindings = sorted(
        {record["service_tier_binding"] for record in records if record["service_tier_binding"]}
    )
    execution_modes = sorted({record["execution_mode"] for record in records if record["execution_mode"]})
    isolated_records = [record for record in records if record["source_unchanged"] is not None]
    quality_records = [record for record in records if record["quality_score"] is not None]
    return {
        "statuses": statuses,
        "main_models": main_models,
        "helper_models": helper_models,
        "cost_usd": round(sum(costs), 6) if costs else None,
        "budget_overshoot_usd": round(sum(overshoots), 6) if overshoots else None,
        "partial_output_preserved": any(record["partial_output_preserved"] for record in records),
        "total_tokens": sum(total_tokens) if total_tokens else None,
        "input_tokens": sum(input_tokens) if input_tokens else None,
        "cache_creation_input_tokens": sum(cache_creation_input_tokens) if cache_creation_input_tokens else None,
        "cache_read_input_tokens": sum(cache_read_input_tokens) if cache_read_input_tokens else None,
        "output_tokens": sum(output_tokens) if output_tokens else None,
        "token_accountings": token_accountings,
        "rollout_budget_tokens": sum(rollout_budget_tokens) if rollout_budget_tokens else None,
        "rollout_budget_overshoot": sum(rollout_budget_overshoots) if rollout_budget_overshoots else None,
        "token_overshoot": sum(token_overshoots) if token_overshoots else None,
        "model_bindings": model_bindings,
        "service_tiers": service_tiers,
        "service_tier_bindings": service_tier_bindings,
        "execution_modes": execution_modes,
        "source_unchanged": all(record["source_unchanged"] is True for record in isolated_records)
        if isolated_records
        else None,
        "stage_unchanged": all(record["stage_unchanged"] is True for record in isolated_records)
        if isolated_records and all(record["stage_mutation_allowed"] is not True for record in isolated_records)
        else None,
        "quality_score": sum(record["quality_score"] for record in quality_records) if quality_records else None,
        "quality_max_score": sum(record["quality_max_score"] for record in quality_records) if quality_records else None,
        "quality_independent": all(record["quality_independent"] is True for record in quality_records)
        if quality_records
        else None,
        "staged_statuses": staged_statuses,
        "verification_statuses": verification_statuses,
        "change_count": sum(change_counts) if change_counts else None,
        "patches_written": sum(1 for record in records if record["patch_written"]),
        "warnings": warnings[:20],
    }


def _comparison_report_shape_error(data: Dict) -> Optional[str]:
    if not isinstance(data.get("suite"), str) or not data["suite"].strip():
        return "invalid comparison report"
    systems = data.get("systems")
    if not isinstance(systems, list) or not systems:
        return "invalid comparison report"
    for system in systems:
        if not isinstance(system, dict):
            return "invalid comparison report"
        if not isinstance(system.get("system"), str) or not system["system"].strip():
            return "invalid comparison report"
        for field in ["passed_tasks", "failed_tasks", "total_tasks"]:
            if not _valid_non_negative_int(system.get(field)):
                return "invalid comparison report"
        if system["passed_tasks"] + system["failed_tasks"] != system["total_tasks"]:
            return "invalid comparison report"
    if not isinstance(data.get("tasks"), list):
        return "invalid comparison report"
    provenance = data.get("provenance")
    if not isinstance(provenance, dict):
        return "invalid comparison report"
    for field in ["complete", "all_live", "claim_eligible"]:
        if not isinstance(provenance.get(field), bool):
            return "invalid comparison report"
    warnings = provenance.get("warnings")
    if warnings is not None and not (isinstance(warnings, list) and all(isinstance(item, str) for item in warnings)):
        return "invalid comparison report"
    return None


def _live_parity_runbook_shape_error(data: Dict) -> Optional[str]:
    try:
        from .benchmark import validate_live_parity_runbook

        validate_live_parity_runbook(data, source="dashboard live parity runbook")
    except ValidationError:
        return "invalid live parity runbook"
    return None


def _run_manifest_shape_error(data: Dict) -> Optional[str]:
    try:
        from .benchmark import validate_run_manifest

        validate_run_manifest(data, source="dashboard run manifest")
    except ValidationError:
        return "invalid run manifest"
    return None


def _evidence_bundle_shape_error(data: Dict) -> Optional[str]:
    try:
        from .evidence import validate_evidence_bundle

        validate_evidence_bundle(data, source="dashboard evidence bundle")
    except ValidationError:
        return "invalid evidence bundle"
    return None


def _valid_non_negative_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _summarize_steps(raw_steps, workflow_steps=None) -> List[Dict]:
    if not isinstance(raw_steps, dict):
        return []
    workflow_meta = _workflow_step_meta(workflow_steps)
    steps = []
    for step_id, raw in raw_steps.items():
        if not isinstance(raw, dict):
            raw = {}
        meta = workflow_meta.get(step_id, {})
        kind = _string_or_empty(raw.get("kind") or meta.get("kind"))
        status = _string_or_unknown(raw.get("status"))
        approval_id = _approval_id_for_step(step_id, meta) if kind == "manual_gate" else ""
        steps.append(
            {
                "id": redact_text(_string_or_unknown(step_id)),
                "step_handle": step_handle(step_id),
                "phase": _phase_id(meta.get("phase") or raw.get("phase")),
                "kind": kind,
                "status": status,
                "detail": _step_detail(raw.get("detail"), kind, status),
                "approval_id": approval_id,
                "approval_required": kind == "manual_gate" and status != "completed",
                "approval_satisfied": kind == "manual_gate" and status == "completed",
                "approval_command": _approval_command_hint(approval_id) if kind == "manual_gate" and status != "completed" else "",
                "started_at_utc": _string_or_empty(raw.get("started_at_utc")),
                "finished_at_utc": _string_or_empty(raw.get("finished_at_utc")),
                "updated_at_utc": _string_or_empty(raw.get("updated_at_utc")),
                "duration_ms": _optional_int(raw.get("duration_ms")),
                "input_tokens": _optional_int(raw.get("input_tokens")),
                "output_tokens": _optional_int(raw.get("output_tokens")),
                "total_tokens": _optional_int(raw.get("total_tokens")),
                "cost_usd": raw.get("cost_usd") if _is_number(raw.get("cost_usd")) else None,
                "agent_invocations": _optional_int(raw.get("agent_invocations")),
                "agent_items_total": _optional_int(raw.get("agent_items_total")),
                "agent_items_cached": _optional_int(raw.get("agent_items_cached")),
                "agent_items_failed": _optional_int(raw.get("agent_items_failed")),
                "agent_packets_total": _optional_int(raw.get("agent_packets_total")),
                "agent_packets_cached": _optional_int(raw.get("agent_packets_cached")),
                "agent_items_per_packet_max": _optional_int(raw.get("agent_items_per_packet_max")),
                "agent_max_packets": _optional_int(raw.get("agent_max_packets")),
                "agent_workers": _optional_int(raw.get("agent_workers")),
                "agent_profile": _string_or_empty(raw.get("agent_profile")),
                "agent_profile_skill_count": _optional_int(
                    raw.get("agent_profile_skill_count")
                ),
                "agent_profile_skill_resource_count": _optional_int(
                    raw.get("agent_profile_skill_resource_count")
                ),
                "agent_profile_skill_resource_bytes": _optional_int(
                    raw.get("agent_profile_skill_resource_bytes")
                ),
                "agent_profile_skill_set_sha256": _string_or_empty(
                    raw.get("agent_profile_skill_set_sha256")
                ),
                "agent_profile_skill_script_count": _optional_int(
                    raw.get("agent_profile_skill_script_count")
                ),
                "agent_profile_skill_mcp_dependency_count": _optional_int(
                    raw.get("agent_profile_skill_mcp_dependency_count")
                ),
                "agent_profile_skill_mcp_tool_count": _optional_int(
                    raw.get("agent_profile_skill_mcp_tool_count")
                ),
                "agent_profile_skill_mcp_http_dependency_count": _optional_int(
                    raw.get("agent_profile_skill_mcp_http_dependency_count")
                ),
                "agent_profile_skill_mcp_stdio_dependency_count": _optional_int(
                    raw.get("agent_profile_skill_mcp_stdio_dependency_count")
                ),
                "agent_profile_skill_mcp_auth_dependency_count": _optional_int(
                    raw.get("agent_profile_skill_mcp_auth_dependency_count")
                ),
                "agent_profile_skill_mcp_auth_env_var_count": _optional_int(
                    raw.get("agent_profile_skill_mcp_auth_env_var_count")
                ),
                "agent_profile_skill_mcp_auth_header_count": _optional_int(
                    raw.get("agent_profile_skill_mcp_auth_header_count")
                ),
                "agent_profile_skill_mcp_sha256": _string_or_empty(
                    raw.get("agent_profile_skill_mcp_sha256")
                ),
                "agent_tool_policy": _string_or_empty(raw.get("agent_tool_policy")),
                "agent_tool_output_token_limit": _optional_int(raw.get("agent_tool_output_token_limit")),
                "agent_command_policy_sha256": _string_or_empty(
                    raw.get("agent_command_policy_sha256")
                ),
                "agent_restricted_hook_hash": _string_or_empty(
                    raw.get("agent_restricted_hook_hash")
                ),
                "agent_restricted_codex_version": _string_or_empty(
                    raw.get("agent_restricted_codex_version")
                ),
                "agent_restricted_external_hooks_disabled": _optional_int(
                    raw.get("agent_restricted_external_hooks_disabled")
                ),
                "agent_restricted_managed_hooks": _optional_int(
                    raw.get("agent_restricted_managed_hooks")
                ),
                "agent_memory_mode": _string_or_empty(raw.get("agent_memory_mode")),
                "agent_memory_selection": _string_or_empty(raw.get("agent_memory_selection")),
                "agent_memory_selection_policy": _string_or_empty(
                    raw.get("agent_memory_selection_policy")
                ),
                "agent_memory_query_sha256": _string_or_empty(
                    raw.get("agent_memory_query_sha256")
                ),
                "agent_memory_start_revision": _optional_int(raw.get("agent_memory_start_revision")),
                "agent_memory_revision": _optional_int(raw.get("agent_memory_revision")),
                "agent_memory_entry_count": _optional_int(raw.get("agent_memory_entry_count")),
                "agent_memory_snapshot_sha256": _string_or_empty(raw.get("agent_memory_snapshot_sha256")),
                "agent_memory_entry_sha256": _string_or_empty(raw.get("agent_memory_entry_sha256")),
                "agent_memory_changed": (
                    raw.get("agent_memory_changed")
                    if isinstance(raw.get("agent_memory_changed"), bool)
                    else None
                ),
                "agent_effort": _string_or_empty(raw.get("agent_effort")),
                "agent_max_tokens": _optional_int(raw.get("agent_max_tokens")),
                "agent_max_total_tokens": _optional_int(raw.get("agent_max_total_tokens")),
                "agent_budgeted_invocations": _optional_int(raw.get("agent_budgeted_invocations")),
                "agent_context_contract": (
                    "direct-artifact-v1"
                    if raw.get("agent_context_contract") == "direct-artifact-v1"
                    else ""
                ),
                "agent_context_source_count": _optional_int(
                    raw.get("agent_context_source_count")
                ),
                "agent_context_artifact_count": _optional_int(
                    raw.get("agent_context_artifact_count")
                ),
                "agent_context_bytes": _optional_int(raw.get("agent_context_bytes")),
                "agent_context_sha256": _goal_sha256(raw.get("agent_context_sha256")),
                "context_output_bytes": _optional_int(raw.get("context_output_bytes")),
                "context_output_sha256": _goal_sha256(raw.get("context_output_sha256")),
                "completion_verdict_contract": (
                    COMPLETION_VERDICT_CONTRACT
                    if raw.get("completion_verdict_contract")
                    == COMPLETION_VERDICT_CONTRACT
                    else ""
                ),
                "completion_verdict_valid": (
                    raw.get("completion_verdict_valid")
                    if isinstance(raw.get("completion_verdict_valid"), bool)
                    else None
                ),
                "completion_verdict_satisfied": (
                    raw.get("completion_verdict_satisfied")
                    if isinstance(raw.get("completion_verdict_satisfied"), bool)
                    else None
                ),
                "completion_verdict_confidence": (
                    raw.get("completion_verdict_confidence")
                    if raw.get("completion_verdict_confidence")
                    in {"low", "medium", "high"}
                    else ""
                ),
                "completion_verdict_evidence_count": _optional_int(
                    raw.get("completion_verdict_evidence_count")
                ),
                "completion_verdict_feedback_present": (
                    raw.get("completion_verdict_feedback_present")
                    if isinstance(
                        raw.get("completion_verdict_feedback_present"), bool
                    )
                    else None
                ),
                "completion_verdict_output_sha256": _goal_sha256(
                    raw.get("completion_verdict_output_sha256")
                ),
                "completion_verdict_output_bytes": _optional_int(
                    raw.get("completion_verdict_output_bytes")
                ),
                "completion_verdict_status": _completion_verdict_state_status(raw),
                "native_agents_enabled": (
                    raw.get("native_agents_enabled")
                    if isinstance(raw.get("native_agents_enabled"), bool)
                    else None
                ),
                "native_agent_max_threads": _optional_int(raw.get("native_agent_max_threads")),
                "native_agent_max_depth": _optional_int(raw.get("native_agent_max_depth")),
                "native_agent_max_total_tokens": _optional_int(
                    raw.get("native_agent_max_total_tokens")
                ),
                "native_agent_parent_completion_policy": _string_or_empty(
                    raw.get("native_agent_parent_completion_policy")
                ),
                "native_agent_parent_completion_checkpoint_tokens": _optional_int(
                    raw.get("native_agent_parent_completion_checkpoint_tokens")
                ),
                "native_agent_parent_integration_checkpoint_tokens": _optional_int(
                    raw.get("native_agent_parent_integration_checkpoint_tokens")
                ),
                "native_agent_usage_attributed": (
                    raw.get("native_agent_usage_attributed")
                    if isinstance(raw.get("native_agent_usage_attributed"), bool)
                    else None
                ),
                "native_agent_usage_status": _string_or_empty(
                    raw.get("native_agent_usage_status")
                ),
                "native_agent_usage_session_count": _optional_int(
                    raw.get("native_agent_usage_session_count")
                ),
                "native_agent_usage_child_count": _optional_int(
                    raw.get("native_agent_usage_child_count")
                ),
                "native_agent_usage_input_tokens": _optional_int(
                    raw.get("native_agent_usage_input_tokens")
                ),
                "native_agent_usage_cached_input_tokens": _optional_int(
                    raw.get("native_agent_usage_cached_input_tokens")
                ),
                "native_agent_usage_output_tokens": _optional_int(
                    raw.get("native_agent_usage_output_tokens")
                ),
                "native_agent_usage_total_tokens": _optional_int(
                    raw.get("native_agent_usage_total_tokens")
                ),
                "native_agent_usage_rollout_tokens": _optional_int(
                    raw.get("native_agent_usage_rollout_tokens")
                ),
                "runtime_budget_exhausted": (
                    raw.get("runtime_budget_exhausted")
                    if isinstance(raw.get("runtime_budget_exhausted"), bool)
                    else None
                ),
                "partial_output_preserved": (
                    raw.get("partial_output_preserved")
                    if isinstance(raw.get("partial_output_preserved"), bool)
                    else None
                ),
                "partial_output_source": _string_or_empty(
                    raw.get("partial_output_source")
                ),
                "partial_output_sha256": _string_or_empty(
                    raw.get("partial_output_sha256")
                ),
                "partial_output_bytes": _optional_int(
                    raw.get("partial_output_bytes")
                ),
                "partial_output_status": (
                    "pending" if raw.get("partial_output_preserved") is True else "none"
                ),
                "agent_team_members": _optional_int(raw.get("agent_team_members")),
                "agent_team_profile_skill_profiles": _optional_int(
                    raw.get("agent_team_profile_skill_profiles")
                ),
                "agent_team_profile_skill_count": _optional_int(
                    raw.get("agent_team_profile_skill_count")
                ),
                "agent_team_profile_skill_resource_count": _optional_int(
                    raw.get("agent_team_profile_skill_resource_count")
                ),
                "agent_team_profile_skill_resource_bytes": _optional_int(
                    raw.get("agent_team_profile_skill_resource_bytes")
                ),
                "agent_team_profile_skill_sets_sha256": _string_or_empty(
                    raw.get("agent_team_profile_skill_sets_sha256")
                ),
                "agent_team_profile_skill_script_count": _optional_int(
                    raw.get("agent_team_profile_skill_script_count")
                ),
                "agent_team_profile_skill_mcp_dependency_count": _optional_int(
                    raw.get("agent_team_profile_skill_mcp_dependency_count")
                ),
                "agent_team_profile_skill_mcp_tool_count": _optional_int(
                    raw.get("agent_team_profile_skill_mcp_tool_count")
                ),
                "agent_team_profile_skill_mcp_http_dependency_count": _optional_int(
                    raw.get("agent_team_profile_skill_mcp_http_dependency_count")
                ),
                "agent_team_profile_skill_mcp_stdio_dependency_count": _optional_int(
                    raw.get("agent_team_profile_skill_mcp_stdio_dependency_count")
                ),
                "agent_team_profile_skill_mcp_auth_dependency_count": _optional_int(
                    raw.get("agent_team_profile_skill_mcp_auth_dependency_count")
                ),
                "agent_team_profile_skill_mcp_auth_env_var_count": _optional_int(
                    raw.get("agent_team_profile_skill_mcp_auth_env_var_count")
                ),
                "agent_team_profile_skill_mcp_auth_header_count": _optional_int(
                    raw.get("agent_team_profile_skill_mcp_auth_header_count")
                ),
                "agent_team_lead": _string_or_empty(raw.get("agent_team_lead")),
                "agent_team_tasks": _optional_int(raw.get("agent_team_tasks")),
                "agent_team_tasks_completed": _optional_int(raw.get("agent_team_tasks_completed")),
                "agent_team_round": _optional_int(raw.get("agent_team_round")),
                "agent_team_max_rounds": _optional_int(raw.get("agent_team_max_rounds")),
                "agent_team_turns": _optional_int(raw.get("agent_team_turns")),
                "agent_team_messages": _optional_int(raw.get("agent_team_messages")),
                "agent_team_operator_questions": _optional_int(
                    raw.get("agent_team_operator_questions")
                ),
                "agent_team_max_operator_questions": _optional_int(
                    raw.get("agent_team_max_operator_questions")
                ),
                "agent_team_pending_operator_questions": _optional_int(
                    raw.get("agent_team_pending_operator_questions")
                ),
                "agent_team_sessions": _optional_int(raw.get("agent_team_sessions")),
                "agent_team_authorized_tokens": _optional_int(
                    raw.get("agent_team_authorized_tokens")
                ),
            }
        )
    order = {step_id: index for index, step_id in enumerate(workflow_meta.keys())}
    steps.sort(key=lambda step: (order.get(step["id"], len(order)), step["id"]))
    return steps


def _completion_verdict_state_status(raw: Dict) -> str:
    if raw.get("completion_verdict_contract") != COMPLETION_VERDICT_CONTRACT:
        return "none"
    valid = raw.get("completion_verdict_valid")
    if valid is False:
        return "invalid"
    if valid is True:
        satisfied = raw.get("completion_verdict_satisfied")
        confidence = raw.get("completion_verdict_confidence")
        evidence_count = raw.get("completion_verdict_evidence_count")
        feedback_present = raw.get("completion_verdict_feedback_present")
        digest = raw.get("completion_verdict_output_sha256")
        size = raw.get("completion_verdict_output_bytes")
        if (
            not isinstance(satisfied, bool)
            or confidence not in {"low", "medium", "high"}
            or not isinstance(evidence_count, int)
            or isinstance(evidence_count, bool)
            or evidence_count < 0
            or not isinstance(feedback_present, bool)
            or not _goal_sha256(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 1 <= size <= MAX_MODEL_VERDICT_BYTES
        ):
            return "invalid"
        return "satisfied" if satisfied else "unsatisfied"
    if raw.get("status") == "completed":
        return "invalid"
    if raw.get("status") in {"running", "pending", "planned"}:
        return "pending"
    return "unavailable"


def _attach_codex_step_checkpoints(
    run_dir: Path,
    steps: List[Dict],
    *,
    workflow_fingerprint_value: Optional[str] = None,
) -> None:
    for step in steps:
        kind = step.get("kind")
        if kind not in {"codex_exec", "agent_map"}:
            continue
        if kind == "agent_map":
            try:
                packet_progress = list_codex_packet_progress(
                    run_dir,
                    step["id"],
                    limit=DETAIL_AGENT_PACKET_LIMIT,
                )
                if workflow_fingerprint_value is not None and any(
                    value.get("workflow_fingerprint") not in {None, workflow_fingerprint_value}
                    for value in packet_progress
                ):
                    raise ValidationError("Codex packet progress workflow binding changed")
                if packet_progress:
                    step["codex_packet_progress"] = packet_progress
            except (OSError, ValidationError) as exc:
                step["codex_packet_progress"] = [
                    {"status": "invalid", "error": exc.__class__.__name__}
                ]
            continue
        try:
            checkpoint = load_codex_step_checkpoint_from_run_dir(run_dir, step["id"])
        except FileNotFoundError:
            checkpoint = None
        except (OSError, ValidationError) as exc:
            checkpoint = None
            step["codex_checkpoint"] = {
                "status": "invalid",
                "error": exc.__class__.__name__,
            }
        if checkpoint is not None:
            step["codex_checkpoint"] = {
                "status": checkpoint["status"],
                "mode": checkpoint["mode"],
                "resume_count": checkpoint["resume_count"],
                "updated_at_utc": checkpoint["updated_at_utc"],
                "session_id_sha256": hashlib.sha256(checkpoint["session_id"].encode("utf-8")).hexdigest(),
                "invocation_base_prompt_sha256": checkpoint.get(
                    "invocation_base_prompt_sha256"
                ),
                "invocation_effective_prompt_sha256": checkpoint.get(
                    "invocation_effective_prompt_sha256"
                ),
                "lifecycle_context_receipt_sha256": checkpoint.get(
                    "lifecycle_context_receipt_sha256"
                ),
            }
        try:
            terminal_path = codex_step_terminal_path_from_run_dir(
                run_dir,
                step["id"],
            )
            if terminal_path.exists() or terminal_path.is_symlink():
                validation = (
                    {"workflow_fingerprint": workflow_fingerprint_value}
                    if workflow_fingerprint_value is not None
                    else {}
                )
                terminal = load_codex_step_terminal(terminal_path, **validation)
                if terminal["step_id"] != step["id"]:
                    raise ValidationError("Codex step terminal path binding changed")
                step["codex_terminal"] = codex_step_terminal_summary(terminal)
        except (OSError, ValidationError) as exc:
            step["codex_terminal"] = {
                "status": "invalid",
                "error": exc.__class__.__name__,
            }
        try:
            progress_path = codex_step_progress_path_from_run_dir(run_dir, step["id"])
            if progress_path.exists() or progress_path.is_symlink():
                validation = {
                    "step_id": step["id"],
                    "scope": "step",
                }
                if workflow_fingerprint_value is not None:
                    validation["workflow_fingerprint"] = workflow_fingerprint_value
                progress = load_codex_progress(progress_path, **validation)
                step["codex_progress"] = codex_progress_summary(progress)
        except (OSError, ValidationError) as exc:
            step["codex_progress"] = {
                "status": "invalid",
                "error": exc.__class__.__name__,
            }


def _approval_id_for_step(step_id: str, meta: Dict) -> str:
    return APPROVAL_ID_PLACEHOLDER


def _approval_command_hint(approval_id: str) -> str:
    return "python3 -m conductor_runtime run RUN_WORKFLOW --resume RUN_DIR --approve %s" % APPROVAL_ID_PLACEHOLDER


def _step_detail(raw_detail, kind: str, status: str) -> str:
    if kind == "manual_gate":
        if status == "completed":
            return "approval %s present" % APPROVAL_ID_PLACEHOLDER
        if status == "blocked":
            return "missing approval %s" % APPROVAL_ID_PLACEHOLDER
        return "manual gate approval pending"
    return redact_text(_string_or_empty(raw_detail))


def _approval_summary(steps: List[Dict]) -> Dict:
    pending = []
    satisfied = 0
    for step in steps:
        if not isinstance(step, dict) or step.get("kind") != "manual_gate":
            continue
        if step.get("approval_satisfied"):
            satisfied += 1
            continue
        if step.get("approval_required"):
            pending.append(
                {
                    "step_id": _string_or_unknown(step.get("id")),
                    "status": _string_or_unknown(step.get("status")),
                    "approval_id": _string_or_empty(step.get("approval_id")),
                    "command": _string_or_empty(step.get("approval_command")),
                }
            )
    return {
        "pending_count": len(pending),
        "satisfied_count": satisfied,
        "pending": pending,
        "guidance": (
            "Manual gates require an explicit CLI resume with --approve; the live control token does not grant approval."
            if pending
            else ""
        ),
    }


def _runtime_summary(workflow_steps, steps: List[Dict], run_status: str = "unknown") -> Dict:
    workflow_meta = _workflow_step_meta(workflow_steps)
    by_id = {step["id"]: step for step in steps if isinstance(step, dict) and isinstance(step.get("id"), str)}
    if not workflow_meta:
        counts = _runtime_counts([])
        return {
            "schema": "conductor.runtime_summary.v1",
            "available": False,
            "total_steps": 0,
            "recorded_steps": len(by_id),
            "counts": counts,
            "ready": [],
            "waiting": [],
            "operator_actions": [],
            "failed_recovery": [],
            "active": [],
            "guidance": "Workflow metadata is unavailable; runtime action summary cannot be computed.",
        }

    ready = []
    waiting = []
    operator_actions = []
    failed_recovery = []
    active = []
    classified = []

    for step_id, meta in workflow_meta.items():
        state_step = by_id.get(step_id, {})
        status = _runtime_status(state_step)
        item = _runtime_item(step_id, meta, state_step, status)
        dependency_ids = item["depends_on"]
        waiting_on = _runtime_waiting_on(
            dependency_ids,
            by_id,
            workflow_meta,
            planned_satisfies=run_status in {"needs_resume", "planned"},
            planned_manual_gate_satisfies=run_status == "planned",
        )
        if waiting_on:
            item["waiting_on"] = waiting_on
            item["category"] = "waiting"
            waiting.append(item)
        elif status in {"running", "pause_requested", "stop_requested", "restart_requested"}:
            item["category"] = "active"
            active.append(item)
        elif status == "failed":
            item["category"] = "failed_recovery"
            item["command"] = _step_recovery_command("retry-step", step_id, "diagnostic follow-up")
            failed_recovery.append(item)
        elif status == "blocked" and item["kind"] != "manual_gate":
            item["category"] = "failed_recovery"
            item["command"] = _step_recovery_command("reset-step", step_id, "resolved blocker")
            failed_recovery.append(item)
        elif item["kind"] == "manual_gate" and status not in {"completed", "skipped"} and (status != "planned" or run_status == "needs_resume"):
            item["category"] = "operator_action"
            item["action"] = "manual_gate_approval"
            item["command"] = _approval_command_hint(APPROVAL_ID_PLACEHOLDER)
            operator_actions.append(item)
        elif status in {"pending", "unknown"} or (status == "planned" and run_status == "needs_resume"):
            item["category"] = "ready"
            item["command"] = "python3 -m conductor_runtime run RUN_WORKFLOW --resume RUN_DIR"
            ready.append(item)
        else:
            item["category"] = status
        classified.append(item)

    counts = _runtime_counts(classified)
    return {
        "schema": "conductor.runtime_summary.v1",
        "available": True,
        "total_steps": len(workflow_meta),
        "recorded_steps": len([step_id for step_id in workflow_meta if step_id in by_id]),
        "counts": counts,
        "ready": ready[:10],
        "waiting": waiting[:10],
        "operator_actions": operator_actions[:10],
        "failed_recovery": failed_recovery[:10],
        "active": active[:10],
        "truncated": any(len(group) > 10 for group in [ready, waiting, operator_actions, failed_recovery, active]),
        "guidance": _runtime_guidance(ready, waiting, operator_actions, failed_recovery, active),
    }


def _runtime_status(state_step: Dict) -> str:
    status = _string_or_empty(state_step.get("status") if isinstance(state_step, dict) else "")
    return status or "pending"


def _runtime_item(step_id: str, meta: Dict, state_step: Dict, status: str) -> Dict:
    depends_on = meta.get("depends_on") if isinstance(meta.get("depends_on"), list) else []
    phase_value = meta.get("phase")
    if (phase_value is None or phase_value == "") and isinstance(state_step, dict):
        phase_value = state_step.get("phase")
    return {
        "step_id": redact_text(_string_or_unknown(step_id)),
        "step_handle": step_handle(step_id),
        "phase": redact_text(_phase_id(phase_value)),
        "kind": redact_text(_string_or_empty((state_step.get("kind") if isinstance(state_step, dict) else "") or meta.get("kind"))),
        "status": redact_text(_string_or_unknown(status)),
        "detail": _runtime_detail(state_step, status),
        "depends_on": [redact_text(item) for item in depends_on if isinstance(item, str)],
    }


def _runtime_detail(state_step: Dict, status: str) -> str:
    if not isinstance(state_step, dict) or not state_step:
        return ""
    return redact_text(_string_or_empty(state_step.get("detail"))) if status not in {"pending", "unknown"} else ""


def _runtime_waiting_on(
    dependency_ids: List[str],
    by_id: Dict[str, Dict],
    workflow_meta: Dict[str, Dict],
    planned_satisfies: bool = False,
    planned_manual_gate_satisfies: bool = False,
) -> List[Dict]:
    waiting = []
    satisfied = {"completed", "skipped"}
    if planned_satisfies:
        satisfied.add("planned")
    for dependency_id in dependency_ids:
        if not isinstance(dependency_id, str):
            continue
        dependency = by_id.get(dependency_id, {})
        status = _runtime_status(dependency)
        dependency_meta = workflow_meta.get(dependency_id, {})
        dependency_kind = _string_or_empty((dependency.get("kind") if isinstance(dependency, dict) else "") or dependency_meta.get("kind"))
        if status == "planned" and dependency_kind == "manual_gate" and not planned_manual_gate_satisfies:
            waiting.append({"step_id": redact_text(dependency_id), "status": redact_text(_string_or_unknown(status))})
            continue
        if status not in satisfied:
            waiting.append({"step_id": redact_text(dependency_id), "status": redact_text(_string_or_unknown(status))})
    return waiting


def _step_recovery_command(action: str, step_id: str, reason: str) -> str:
    return "python3 -m conductor_runtime %s RUN_DIR %s --reason %s" % (
        action,
        shlex.quote(redact_text(step_id)),
        shlex.quote(reason),
    )


def _runtime_counts(items: List[Dict]) -> Dict[str, int]:
    counts = {
        "ready": 0,
        "waiting": 0,
        "operator_action": 0,
        "failed_recovery": 0,
        "active": 0,
        "completed": 0,
        "skipped": 0,
        "planned": 0,
    }
    for item in items:
        category = item.get("category") if isinstance(item, dict) else ""
        status = item.get("status") if isinstance(item, dict) else ""
        if category in counts:
            counts[category] += 1
        elif status in counts:
            counts[status] += 1
    return counts


def _runtime_guidance(ready: List[Dict], waiting: List[Dict], operator_actions: List[Dict], failed_recovery: List[Dict], active: List[Dict]) -> str:
    if failed_recovery:
        return "Review failed or blocked steps before using retry/reset recovery commands."
    if operator_actions:
        return "Operator action is required before the run can continue."
    if active:
        return "A step is active; inspect runner liveness before issuing controls."
    if ready:
        return "Ready steps can run on the next explicit resume."
    if waiting:
        return "Some steps are waiting for upstream dependencies."
    return "No pending runtime actions detected."


def _agent_team_details(run_dir: Path, workflow: Dict) -> List[Dict]:
    records = []
    steps = workflow.get("steps") if isinstance(workflow, dict) else None
    if not isinstance(steps, list):
        return records
    try:
        fingerprint = workflow_fingerprint(workflow)
    except (TypeError, ValueError):
        fingerprint = None
    for step in steps:
        if not isinstance(step, dict) or step.get("kind") != "agent_team":
            continue
        capture_dir = step.get("capture_dir")
        if not isinstance(capture_dir, str) or not capture_dir:
            records.append({"step_id": _string_or_unknown(step.get("id")), "status": "invalid", "error": "invalid capture directory"})
            continue
        try:
            require_no_path_escape(capture_dir)
            path = run_dir / "artifacts" / capture_dir / "team-state.json"
            state = load_agent_team_state(path)
            intent_path = run_dir / "artifacts" / capture_dir / "team-merge-intent.json"
            pending_path = run_dir / "artifacts" / capture_dir / "team-merge-pending.json"
            intent = None
            transaction = None
            if step.get("sandbox", "read-only") == "workspace-write" and (
                pending_path.exists() or pending_path.is_symlink()
            ):
                transaction = load_agent_team_merge_transaction(
                    pending_path,
                    step=step,
                    workflow_fingerprint=fingerprint,
                )
                state = transaction["candidate_state"]
            else:
                if step.get("sandbox", "read-only") == "workspace-write" and (
                    intent_path.exists() or intent_path.is_symlink()
                ):
                    intent = load_agent_team_merge_intent(
                        intent_path,
                        step=step,
                        workflow_fingerprint=fingerprint,
                    )
                validate_agent_team_state(
                    state,
                    step=step,
                    workflow_fingerprint=fingerprint,
                )
                if intent is not None:
                    verify_agent_team_state_outputs(
                        intent["candidate_state"],
                        run_dir / "artifacts",
                        step,
                    )
            verify_agent_team_state_outputs(state, run_dir / "artifacts", step)
            summary = agent_team_state_summary(state)
            terminal_dir = run_dir / "artifacts" / capture_dir / "turn-terminals"
            terminals = []
            if terminal_dir.exists() or terminal_dir.is_symlink():
                reject_symlink_path(
                    terminal_dir,
                    "agent team turn terminal directory",
                )
                if not terminal_dir.is_dir():
                    raise ValidationError(
                        "agent team turn terminal directory is invalid"
                    )
                expected_names = {
                    "%s.json" % member["id"] for member in step["members"]
                }
                for terminal_path in sorted(terminal_dir.iterdir()):
                    reject_symlink_path(
                        terminal_path,
                        "agent team turn terminal",
                    )
                    if (
                        terminal_path.name not in expected_names
                        or not terminal_path.is_file()
                    ):
                        raise ValidationError(
                            "agent team turn terminal directory has an unknown entry"
                        )
                    terminals.append(
                        agent_team_turn_terminal_summary(
                            load_agent_team_turn_terminal(
                                terminal_path,
                                step=step,
                                workflow_fingerprint=fingerprint,
                            )
                        )
                    )
            summary["turn_terminal_pending_count"] = len(terminals)
            summary["turn_terminal_pending"] = terminals
            completion_dir = run_dir / "artifacts" / capture_dir / "turn-completions"
            completions = []
            if completion_dir.exists() or completion_dir.is_symlink():
                reject_symlink_path(
                    completion_dir,
                    "agent team turn completion directory",
                )
                if not completion_dir.is_dir():
                    raise ValidationError(
                        "agent team turn completion directory is invalid"
                    )
                expected_names = {
                    "%s.json" % member["id"] for member in step["members"]
                }
                for completion_path in sorted(completion_dir.iterdir()):
                    reject_symlink_path(
                        completion_path,
                        "agent team turn completion",
                    )
                    if (
                        completion_path.name not in expected_names
                        or not completion_path.is_file()
                    ):
                        raise ValidationError(
                            "agent team turn completion directory has an unknown entry"
                        )
                    completions.append(
                        agent_team_turn_completion_summary(
                            load_agent_team_turn_completion(
                                completion_path,
                                step=step,
                                workflow_fingerprint=fingerprint,
                            )
                        )
                    )
            summary["turn_completion_pending_count"] = len(completions)
            summary["turn_completion_pending"] = completions
            if (terminals or completions) and transaction is None and intent is None:
                summary["status"] = "recovery-pending"
            operator_path = run_dir / "artifacts" / capture_dir / "operator-inbox.json"
            if operator_path.exists() or operator_path.is_symlink():
                operator_inbox = load_agent_team_operator_inbox(operator_path)
                verify_agent_team_operator_inbox(operator_inbox, state)
                summary["operator_inbox"] = agent_team_operator_inbox_summary(
                    operator_inbox
                )
            else:
                summary["operator_inbox"] = None
            chat_path = run_dir / "artifacts" / capture_dir / "operator-chat.json"
            if step.get("operator_chat") is not None and (
                chat_path.exists() or chat_path.is_symlink()
            ):
                operator_chat = load_agent_team_operator_chat(chat_path)
                verify_agent_team_operator_chat(operator_chat, state)
                summary["operator_chat"] = agent_team_operator_chat_summary(
                    operator_chat
                )
            else:
                summary["operator_chat"] = None
            if step.get("sandbox", "read-only") == "workspace-write":
                if transaction is not None:
                    merge_ledger = transaction["candidate_ledger"]
                    summary["recovery_pending"] = agent_team_merge_transaction_summary(
                        transaction
                    )
                    summary["status"] = "recovery-pending"
                else:
                    merge_ledger = load_agent_team_merge_ledger(
                        run_dir / "artifacts" / capture_dir / "team-merge-ledger.json",
                        step=step,
                        workflow_fingerprint=fingerprint,
                        generation=state["generation"],
                        state=state,
                    )
                    if intent is not None:
                        summary["recovery_pending"] = agent_team_merge_intent_summary(
                            intent
                        )
                        summary["status"] = "recovery-pending"
                    else:
                        summary["recovery_pending"] = None
                summary["merge"] = agent_team_merge_summary(merge_ledger)
                recovery_dir = run_dir / "artifacts" / capture_dir / "merge-recoveries"
                recoveries = []
                if recovery_dir.is_dir() and not recovery_dir.is_symlink():
                    for recovery_path in sorted(recovery_dir.glob("*.json")):
                        recoveries.append(
                            agent_team_merge_recovery_summary(
                                load_agent_team_merge_recovery(recovery_path)
                            )
                        )
                summary["merge_recovery_count"] = len(recoveries)
                summary["latest_merge_recovery"] = recoveries[-1] if recoveries else None
            else:
                summary["merge"] = None
                summary["recovery_pending"] = None
                summary["merge_recovery_count"] = 0
                summary["latest_merge_recovery"] = None
            summary.update(
                _agent_team_quality_retry_details(
                    run_dir,
                    capture_dir,
                    step["id"],
                )
            )
            summary.update(
                _agent_team_plan_approval_details(
                    run_dir,
                    step,
                    state,
                )
            )
            if step.get("operator_console") is not None:
                transcripts = list_agent_team_transcript_summaries(
                    run_dir / "artifacts",
                    step,
                    state["workflow_fingerprint"],
                    state["generation"],
                )
                summary["operator_console"] = {
                    "max_events": step["operator_console"]["max_events"],
                    "max_bytes": step["operator_console"]["max_bytes"],
                    "transcript_count": len(transcripts),
                    "transcripts": transcripts,
                }
            else:
                summary["operator_console"] = None
            summary["recent_messages"] = [
                {
                    "id": message["id"],
                    "round": message["round"],
                    "from": message["from"],
                    "to": message["to"],
                    "body_sha256": message["body_sha256"],
                }
                for message in state["messages"][-20:]
            ]
            summary["report"] = _string_or_empty(step.get("report"))
            summary["error"] = None
            records.append(summary)
        except FileNotFoundError:
            records.append(
                {
                    "step_id": _string_or_unknown(step.get("id")),
                    "status": "not-started",
                    "error": None,
                }
            )
        except (OSError, ValidationError) as exc:
            records.append(
                {
                    "step_id": _string_or_unknown(step.get("id")),
                    "status": "invalid",
                    "error": exc.__class__.__name__,
                }
            )
    return records


def _agent_team_quality_retry_details(
    run_dir: Path,
    capture_dir: str,
    step_id: str,
) -> Dict:
    base = run_dir / "artifacts" / capture_dir / "quality-retries"
    pending, pending_count, pending_truncated = _load_agent_team_quality_retry_directory(
        base / "pending",
        step_id,
        newest_only=False,
    )
    history, history_count, history_truncated = _load_agent_team_quality_retry_directory(
        base / "history",
        step_id,
        newest_only=True,
    )
    status_counts = {}
    for checkpoint in history:
        status = checkpoint["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "quality_retry_history_count": history_count,
        "quality_retry_history_display_count": len(history),
        "quality_retry_history_truncated": history_truncated,
        "quality_retry_status_counts": status_counts,
        "latest_quality_retry": history[-1] if history else None,
        "quality_retry_pending_count": pending_count,
        "quality_retry_pending_truncated": pending_truncated,
        "quality_retry_pending": pending,
    }


def _agent_team_plan_approval_details(run_dir: Path, step: Dict, state: Dict) -> Dict:
    policy = step.get("plan_approval")
    if not isinstance(policy, dict):
        return {
            "plan_approval_count": 0,
            "plan_approval_approved_count": 0,
            "plan_approval_rejection_count": 0,
            "plan_approvals": [],
        }
    values = []
    for task_id in policy.get("task_ids", []):
        path = (
            run_dir
            / "artifacts"
            / step["capture_dir"]
            / "plan-approvals"
            / ("generation-%09d" % state["generation"])
            / task_id
            / "approval.json"
        )
        approval = load_agent_team_plan_approval(
            path,
            step=step,
            workflow_fingerprint=state["workflow_fingerprint"],
            generation=state["generation"],
        )
        verify_agent_team_plan_approval_outputs(approval, run_dir / "artifacts")
        values.append(agent_team_plan_approval_summary(approval))
    return {
        "plan_approval_count": len(values),
        "plan_approval_approved_count": sum(
            value["status"] == "approved" for value in values
        ),
        "plan_approval_rejection_count": sum(
            value["rejection_count"] for value in values
        ),
        "plan_approvals": values,
    }


def _load_agent_team_quality_retry_directory(
    directory: Path,
    step_id: str,
    *,
    newest_only: bool,
) -> Tuple[List[Dict], int, bool]:
    entries = _scan_directory_entries_no_follow(
        directory,
        "agent team quality retry directory",
    )
    if entries is None:
        return [], 0, False
    names = sorted(
        name
        for name, child_kind, _mtime in entries
        if child_kind == "file" and name.endswith(".json")
    )
    total = len(names)
    truncated = total > DETAIL_AGENT_TEAM_QUALITY_RETRY_LIMIT
    selected = (
        names[-DETAIL_AGENT_TEAM_QUALITY_RETRY_LIMIT:]
        if newest_only
        else names[:DETAIL_AGENT_TEAM_QUALITY_RETRY_LIMIT]
    )
    summaries = []
    for name in selected:
        checkpoint, _file_sha256 = load_agent_team_quality_retry(directory / name)
        if checkpoint["step_id"] != step_id:
            raise ValidationError("agent team quality retry dashboard binding changed")
        summaries.append(agent_team_quality_retry_summary(checkpoint))
    return summaries, total, truncated


def _agent_map_details(
    run_dir: Path,
    workflow_steps,
    steps: List[Dict],
    packet_generations,
    max_text_bytes: int,
    max_packets: int,
    workflow_fingerprint_value: Optional[str] = None,
) -> List[Dict]:
    if not isinstance(workflow_steps, list):
        return []
    step_by_id = {step["id"]: step for step in steps}
    groups = []
    for workflow_step in workflow_steps:
        if not isinstance(workflow_step, dict) or workflow_step.get("kind") != "agent_map":
            continue
        step_id = workflow_step.get("id")
        if not isinstance(step_id, str) or not step_id:
            continue
        state_step = step_by_id.get(step_id, {})
        capture_dir = workflow_step.get("capture_dir", step_id)
        if not isinstance(capture_dir, str) or not capture_dir:
            capture_dir = step_id
        group = {
            "step_id": redact_text(_string_or_unknown(step_id)),
            "step_handle": step_handle(step_id),
            "phase": _phase_id(workflow_step.get("phase") or state_step.get("phase")),
            "status": _string_or_unknown(state_step.get("status")),
            "detail": redact_text(_string_or_empty(state_step.get("detail"))),
            "capture_dir": redact_text(capture_dir),
            "agent_invocations": state_step.get("agent_invocations"),
            "agent_items_total": state_step.get("agent_items_total"),
            "agent_items_cached": state_step.get("agent_items_cached"),
            "agent_packets_total": state_step.get("agent_packets_total"),
            "agent_packets_cached": state_step.get("agent_packets_cached"),
            "agent_items_per_packet_max": state_step.get("agent_items_per_packet_max"),
            "agent_max_packets": state_step.get("agent_max_packets"),
            "agent_workers": state_step.get("agent_workers"),
            "cache_present": False,
            "cache_entries": 0,
            "cache_error": None,
            "terminal_pending_count": 0,
            "terminal_pending": [],
            "terminal_error": None,
            "packets": [],
            "truncated": False,
            "error": None,
        }
        try:
            require_no_path_escape(capture_dir)
        except ValidationError as exc:
            group["error"] = str(exc.__class__.__name__)
            groups.append(group)
            continue
        cache_entries, cache_error = _agent_map_cache_entries(run_dir, capture_dir)
        group["cache_present"] = cache_entries is not None
        group["cache_entries"] = len(cache_entries or {})
        group["cache_error"] = cache_error
        trace_by_output, trace_meta = _agent_map_trace_entries(run_dir, capture_dir, step_id)
        group.update(trace_meta)
        terminals, terminal_by_index, terminal_error = _agent_map_terminal_entries(
            run_dir,
            capture_dir,
            workflow_step,
            workflow_fingerprint_value,
        )
        group["terminal_pending_count"] = len(terminals)
        group["terminal_pending"] = terminals
        group["terminal_error"] = terminal_error
        if terminals:
            group["status"] = "recovery-pending"
        items, item_error, item_truncated = _agent_map_items(run_dir, workflow_step, max_packets)
        if item_error:
            group["error"] = item_error
        group["truncated"] = item_truncated or bool(trace_meta.get("trace_truncated"))
        cache_by_output = _cache_entries_by_output(cache_entries or {})
        for index, packet in enumerate(items, start=1):
            output_relative = _agent_output_relative(capture_dir, packet.label, index)
            cache_entry = cache_by_output.get(output_relative, {})
            stdout_name = "%s.%03d.stdout.log" % (step_id, index)
            stderr_name = "%s.%03d.stderr.log" % (step_id, index)
            output = _detail_file(run_dir / "artifacts" / output_relative, output_relative, max_text_bytes=max_text_bytes)
            stdout = _detail_file(run_dir / "logs" / stdout_name, stdout_name, max_text_bytes=max_text_bytes)
            stderr = _detail_file(run_dir / "logs" / stderr_name, stderr_name, max_text_bytes=max_text_bytes)
            group["packets"].append(
                {
                    "index": index,
                    "item": redact_text(packet.label),
                    "item_count": len(packet.items),
                    "status": _agent_packet_status(
                        output,
                        stdout,
                        stderr,
                        cache_entry,
                        terminal_by_index.get(index),
                    ),
                    "terminal": terminal_by_index.get(index, {}),
                    "cached": bool(cache_entry),
                    "retry_generation": _agent_packet_retry_generation(
                        packet_generations,
                        step_id,
                        index,
                    ),
                    "cache_updated_at_utc": (
                        redact_text(_string_or_empty(cache_entry.get("updated_at_utc")))
                        if isinstance(cache_entry, dict)
                        else ""
                    ),
                    "trace": trace_by_output.get(output_relative, {}),
                    "output": output,
                    "stdout": stdout,
                    "stderr": stderr,
                }
            )
        groups.append(group)
    return groups


def _agent_map_terminal_entries(
    run_dir: Path,
    capture_dir: str,
    step: Dict,
    workflow_fingerprint_value: Optional[str],
) -> Tuple[List[Dict], Dict[int, Dict], Optional[str]]:
    directory = run_dir / "artifacts" / capture_dir / ".agent-map-turn-terminals"
    if not directory.exists() and not directory.is_symlink():
        return [], {}, None
    try:
        reject_symlink_path(directory, "agent_map packet terminal directory")
        if not directory.is_dir():
            raise ValidationError("agent_map packet terminal directory is invalid")
        summaries = []
        by_index = {}
        for path in sorted(directory.iterdir(), key=lambda value: value.name):
            reject_symlink_path(path, "agent_map packet terminal")
            stem = path.stem
            if (
                path.suffix != ".json"
                or len(stem) != 6
                or not stem.isdigit()
                or int(stem) < 1
                or int(stem) > MAX_AGENT_PACKETS
                or path.name != "%06d.json" % int(stem)
                or not path.is_file()
            ):
                raise ValidationError(
                    "agent_map packet terminal directory has an unknown entry"
                )
            expected = (
                {"workflow_fingerprint": workflow_fingerprint_value}
                if workflow_fingerprint_value is not None
                else None
            )
            terminal = load_agent_map_packet_terminal(
                path,
                step=step,
                expected=expected,
            )
            if terminal["index"] != int(stem):
                raise ValidationError("agent_map packet terminal path binding changed")
            summary = agent_map_packet_terminal_summary(terminal)
            summaries.append(summary)
            by_index[terminal["index"]] = summary
        return summaries, by_index, None
    except (OSError, ValidationError) as exc:
        return [], {}, exc.__class__.__name__


def _agent_packet_retry_generation(raw, step_id: str, packet_index: int) -> int:
    if not isinstance(raw, dict):
        return 0
    records = raw.get(step_id)
    if not isinstance(records, dict):
        return 0
    record = records.get(str(packet_index))
    if not isinstance(record, dict):
        return 0
    generation = record.get("generation")
    return generation if isinstance(generation, int) and not isinstance(generation, bool) and generation > 0 else 0


def _agent_map_trace_entries(run_dir: Path, capture_dir: str, step_id: str) -> Tuple[Dict[str, Dict], Dict]:
    meta = {
        "trace_present": False,
        "trace_entries": 0,
        "trace_error": None,
        "trace_truncated": False,
    }
    path = run_dir / "artifacts" / capture_dir / ".agent-map-trace.jsonl"
    if path.is_symlink():
        meta["trace_error"] = "symlink skipped"
        return {}, meta
    if not path.is_file():
        return {}, meta
    meta["trace_present"] = True
    try:
        data = read_regular_file_prefix_no_follow(path, "agent_map trace", DETAIL_AGENT_TRACE_BYTES + 1)
    except ValidationError as exc:
        meta["trace_error"] = "too large" if "must be at most" in str(exc) else exc.__class__.__name__
        return {}, meta
    except (OSError, UnicodeDecodeError) as exc:
        meta["trace_error"] = exc.__class__.__name__
        return {}, meta
    bytes_truncated = len(data) > DETAIL_AGENT_TRACE_BYTES
    if bytes_truncated:
        meta["trace_truncated"] = True
        return {}, meta
    text = data[:DETAIL_AGENT_TRACE_BYTES].decode("utf-8", errors="replace")
    lines = text.splitlines()
    by_output: Dict[str, Dict] = {}
    for line in lines:
        if not line.strip():
            continue
        if meta["trace_entries"] >= DETAIL_AGENT_TRACE_LIMIT:
            meta["trace_truncated"] = True
            meta["trace_entries"] = 0
            return {}, meta
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            meta["trace_error"] = "JSONDecodeError"
            break
        output, entry = _sanitize_agent_trace(raw, step_id)
        if output:
            by_output[output] = entry
            meta["trace_entries"] += 1
    return by_output, meta


def _sanitize_agent_trace(raw, expected_step_id: str) -> Tuple[Optional[str], Dict]:
    if not isinstance(raw, dict) or raw.get("schema") != AGENT_MAP_TRACE_SCHEMA:
        return None, {}
    if raw.get("step_id") != expected_step_id:
        return None, {}
    output = raw.get("output")
    if not isinstance(output, str) or not output:
        return None, {}
    entry = {
        "status": _agent_trace_status(raw.get("status")),
        "detail": _trace_string(raw.get("detail")),
        "cached": raw.get("cached") if isinstance(raw.get("cached"), bool) else False,
        "started_at_utc": _trace_string(raw.get("started_at_utc")),
        "finished_at_utc": _trace_string(raw.get("finished_at_utc")),
        "duration_ms": _optional_int(raw.get("duration_ms")),
        "recorded_at_utc": _trace_string(raw.get("recorded_at_utc")),
        "cache_updated_at_utc": _trace_string(raw.get("cache_updated_at_utc")),
        "returncode": raw.get("returncode") if isinstance(raw.get("returncode"), int) and not isinstance(raw.get("returncode"), bool) else None,
        "stdout_bytes": _optional_int(raw.get("stdout_bytes")),
        "stderr_bytes": _optional_int(raw.get("stderr_bytes")),
        "stdout_truncated": raw.get("stdout_truncated") if isinstance(raw.get("stdout_truncated"), bool) else None,
        "stderr_truncated": raw.get("stderr_truncated") if isinstance(raw.get("stderr_truncated"), bool) else None,
        "timed_out": raw.get("timed_out") if isinstance(raw.get("timed_out"), bool) else None,
    }
    return output, entry


def _agent_trace_status(value) -> str:
    if value in {"cached", "completed", "failed", "no_output", "timed_out"}:
        return value
    return "unknown"


def _trace_string(value) -> str:
    if not isinstance(value, str):
        return ""
    return redact_text(value)[:200]


def _agent_map_items(run_dir: Path, step: Dict, max_packets: int) -> Tuple[List[AgentPacket], Optional[str], bool]:
    display_limit = max(1, min(max_packets, DETAIL_AGENT_PACKET_LIMIT))
    source_limit = MAX_AGENT_PACKETS if step.get("max_packets") is not None else display_limit + 1
    try:
        if isinstance(step.get("items"), list):
            items = clean_packet_items(step["items"], "dashboard agent_map items", source_limit)
        elif isinstance(step.get("items_artifact"), str):
            require_no_path_escape(step["items_artifact"])
            path = run_dir / "artifacts" / step["items_artifact"]
            items = read_packet_items_file(path, "dashboard agent_map items_artifact", source_limit)
        elif isinstance(step.get("items_file"), str):
            return [], "workspace item source is unavailable in run detail", False
        else:
            return [], "agent_map item source is unavailable", False
    except (OSError, ValidationError) as exc:
        return [], exc.__class__.__name__, False
    try:
        packets = packetize_agent_items(items, step.get("max_packets"))
    except ValidationError as exc:
        return [], exc.__class__.__name__, False
    truncated = len(packets) > display_limit
    return packets[:display_limit], None, truncated


def _agent_map_cache_entries(run_dir: Path, capture_dir: str) -> Tuple[Optional[Dict], Optional[str]]:
    path = run_dir / "artifacts" / capture_dir / ".agent-map-cache.json"
    data, error = _read_json(path)
    if error == "missing":
        return None, None
    if error:
        return None, error
    if not isinstance(data, dict) or data.get("schema") != "conductor.agent_map_cache.v1":
        return None, "unsupported schema"
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return None, "invalid entries"
    return entries, None


def _cache_entries_by_output(entries: Dict) -> Dict[str, Dict]:
    by_output = {}
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        output = entry.get("output")
        if isinstance(output, str):
            by_output[output] = entry
    return by_output


def _agent_packet_status(
    output: Dict,
    stdout: Dict,
    stderr: Dict,
    cache_entry: Dict,
    terminal: Optional[Dict] = None,
) -> str:
    if terminal:
        return "recovery-pending"
    if output.get("present"):
        return "completed"
    if cache_entry:
        return "cached-output-missing"
    if stdout.get("present") or stderr.get("present"):
        return "ran-without-output"
    return "missing"


def _agent_output_relative(capture_dir: str, item: str, index: int) -> str:
    capture_name = "%03d-%s.md" % (index, _safe_capture_name(item))
    return "%s/%s" % (capture_dir, capture_name)


def _safe_capture_name(value: str) -> str:
    safe = "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe[:60] or "item"


def _workflow_step_meta(workflow_steps) -> Dict[str, Dict]:
    if not isinstance(workflow_steps, list):
        return {}
    meta = {}
    for step in workflow_steps:
        if not isinstance(step, dict):
            continue
        step_id = step.get("id")
        if isinstance(step_id, str) and step_id:
            meta[step_id] = step
    return meta


def _summarize_phases(workflow_steps, steps: List[Dict]) -> List[Dict]:
    workflow_meta = _workflow_step_meta(workflow_steps)
    by_id = {step["id"]: step for step in steps}
    phases: Dict[str, Dict] = {}
    phase_order: List[str] = []

    def phase_for_step(step_id: str, fallback=None) -> str:
        meta = workflow_meta.get(step_id, {})
        if meta:
            return _phase_id(meta.get("phase"))
        if isinstance(fallback, dict):
            return _phase_id(fallback.get("phase"))
        return "default"

    def ensure_phase(phase_id: str) -> Dict:
        if phase_id not in phases:
            phases[phase_id] = {
                "id": phase_id,
                "status": "pending",
                "steps_total": 0,
                "steps_recorded": 0,
                "step_counts": {},
                "agent_invocations": 0,
                "agent_packets_total": 0,
                "agent_packets_cached": 0,
                "agent_items_total": 0,
                "agent_items_cached": 0,
                "agent_workers_max": 0,
                "duration_ms": None,
                "started_at_utc": "",
                "finished_at_utc": "",
                "usage": _usage_summary({}),
            }
            phase_order.append(phase_id)
        return phases[phase_id]

    if workflow_meta:
        for step_id, meta in workflow_meta.items():
            phase = ensure_phase(phase_for_step(step_id))
            step = by_id.get(step_id)
            status = _string_or_unknown(step.get("status") if step else None)
            if status == "unknown":
                status = "pending"
            _add_step_to_phase(phase, step, status)
        for step in steps:
            if step["id"] in workflow_meta:
                continue
            phase = ensure_phase(_phase_id(step.get("phase")))
            _add_step_to_phase(phase, step, _string_or_unknown(step.get("status")))
    else:
        for step in steps:
            phase = ensure_phase(phase_for_step(step["id"], fallback=step))
            _add_step_to_phase(phase, step, _string_or_unknown(step.get("status")))

    for phase in phases.values():
        phase["status"] = _phase_status(phase["step_counts"], phase["steps_total"])
        phase["usage"] = _phase_usage([step for step in steps if step.get("phase") == phase["id"]])
    return [phases[phase_id] for phase_id in phase_order]


def _add_step_to_phase(phase: Dict, step: Optional[Dict], status: str) -> None:
    phase["steps_total"] += 1
    phase["step_counts"][status] = phase["step_counts"].get(status, 0) + 1
    if not step:
        return
    phase["steps_recorded"] += 1
    phase["agent_invocations"] += step.get("agent_invocations") or 0
    phase["agent_packets_total"] += step.get("agent_packets_total") or step.get("agent_items_total") or 0
    phase["agent_packets_cached"] += step.get("agent_packets_cached") or step.get("agent_items_cached") or 0
    phase["agent_items_total"] += step.get("agent_items_total") or 0
    phase["agent_items_cached"] += step.get("agent_items_cached") or 0
    phase["agent_workers_max"] = max(phase["agent_workers_max"], step.get("agent_workers") or 0)
    if step.get("duration_ms") is not None:
        phase["duration_ms"] = (phase["duration_ms"] or 0) + step["duration_ms"]
    started = step.get("started_at_utc") or ""
    if started and (not phase["started_at_utc"] or started < phase["started_at_utc"]):
        phase["started_at_utc"] = started
    finished = step.get("finished_at_utc") or ""
    if finished and finished > phase["finished_at_utc"]:
        phase["finished_at_utc"] = finished


def _phase_usage(steps: List[Dict]) -> Dict:
    input_tokens = _sum_optional_ints(step.get("input_tokens") for step in steps)
    output_tokens = _sum_optional_ints(step.get("output_tokens") for step in steps)
    total_tokens = _sum_optional_ints(_step_total_tokens(step) for step in steps)
    costs = [step.get("cost_usd") for step in steps if _is_number(step.get("cost_usd"))]
    cost_usd = sum(costs) if costs else None
    return {
        "status": "recorded" if total_tokens is not None or cost_usd is not None else "unavailable",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
    }


def _step_total_tokens(step: Dict):
    total_tokens = step.get("total_tokens")
    if isinstance(total_tokens, int) and not isinstance(total_tokens, bool) and total_tokens >= 0:
        return total_tokens
    input_tokens = step.get("input_tokens")
    output_tokens = step.get("output_tokens")
    if (
        isinstance(input_tokens, int)
        and not isinstance(input_tokens, bool)
        and input_tokens >= 0
        and isinstance(output_tokens, int)
        and not isinstance(output_tokens, bool)
        and output_tokens >= 0
    ):
        return input_tokens + output_tokens
    return None


def _sum_optional_ints(values) -> Optional[int]:
    total = 0
    seen = False
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            total += value
            seen = True
    return total if seen else None


def _phase_status(counts: Dict[str, int], total: int) -> str:
    if not counts or total <= 0:
        return "pending"
    if counts.get("failed"):
        return "failed"
    if counts.get("blocked"):
        return "blocked"
    if counts.get("running"):
        return "running"
    if counts.get("restart_requested") or counts.get("pause_requested") or counts.get("stop_requested"):
        return "running"
    completed_like = counts.get("completed", 0) + counts.get("skipped", 0)
    if completed_like == total:
        return "completed"
    planned_like = completed_like + counts.get("planned", 0)
    if planned_like == total:
        return "planned"
    return "pending"


def _phase_id(value) -> str:
    if isinstance(value, str) and value.strip():
        return redact_text(value.strip())
    return "default"


def _summarize_hook_events(raw_events) -> List[Dict]:
    if not isinstance(raw_events, list):
        return []
    events = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        events.append(
            {
                "id": _string_or_unknown(raw.get("id")),
                "event": _string_or_unknown(raw.get("event")),
                "step_id": _string_or_empty(raw.get("step_id")),
                "generation": _optional_int(raw.get("generation")),
                "round": _optional_int(raw.get("round")),
                "quality_retry_index": _optional_int(raw.get("quality_retry_index")),
                "member_id": _string_or_empty(raw.get("member_id")),
                "task_id": _string_or_empty(raw.get("task_id")),
                "task_origin": _string_or_empty(raw.get("task_origin")),
                "hook_input_sha256": _string_or_empty(raw.get("hook_input_sha256")),
                "workspace_fingerprint_sha256": _string_or_empty(
                    raw.get("workspace_fingerprint_sha256")
                ),
                "changed_file_count": _optional_int(raw.get("changed_file_count")),
                "status": _string_or_unknown(raw.get("status")),
                "detail": _string_or_empty(raw.get("detail")),
                "on_failure": _string_or_empty(raw.get("on_failure")),
                "started_at_utc": _string_or_empty(raw.get("started_at_utc")),
                "finished_at_utc": _string_or_empty(raw.get("finished_at_utc")),
                "duration_ms": _optional_int(raw.get("duration_ms")),
                "returncode": _optional_int(raw.get("returncode")),
                "timed_out": raw.get("timed_out") if isinstance(raw.get("timed_out"), bool) else None,
            }
        )
    return events


def _summarize_agent_native_tool_hooks(raw_summary) -> Dict:
    empty = {
        "status": "not-configured",
        "gate_count": 0,
        "prepared_gate_count": 0,
        "settled_gate_count": 0,
        "failed_gate_count": 0,
        "receipt_count": 0,
        "passed_count": 0,
        "denied_count": 0,
        "warning_count": 0,
        "verifier_binding_count": 0,
        "command_policy_gate_count": 0,
        "gate_sha256s": [],
        "bridge_sha256s": [],
        "hook_set_sha256s": [],
        "updated_at_utc": "",
        "summary_sha256": "",
        "error": None,
    }
    if raw_summary is None:
        return empty
    try:
        validate_agent_native_tool_summary(raw_summary)
    except (TypeError, ValidationError) as exc:
        invalid = dict(empty)
        invalid["status"] = "invalid"
        invalid["error"] = redact_text(exc.__class__.__name__)
        return invalid
    return {
        key: raw_summary[key]
        for key in empty
        if key != "error"
    } | {"error": None}


def _summarize_session_trace_events(raw_events, *, private_step_ids=()) -> List[Dict]:
    if not isinstance(raw_events, list):
        return []
    private_step_ids = set(private_step_ids)
    events = []
    for raw in raw_events[-DETAIL_SESSION_TRACE_LIMIT:]:
        if not isinstance(raw, dict):
            continue
        step_id = _trace_string(raw.get("step_id"))
        session_id = _trace_string(raw.get("session_id"))
        private_session_hash = None
        if step_id in private_step_ids and session_id:
            private_session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
            session_id = ""
        events.append(
            {
                "provider": _trace_string(raw.get("provider")) or "generic",
                "event": _trace_string(raw.get("event")) or "event",
                "status": _trace_string(raw.get("status")) or "unknown",
                "source": _trace_string(raw.get("source")),
                "session_id": session_id,
                "session_id_sha256": private_session_hash,
                "agent_id": _trace_string(raw.get("agent_id")),
                "step_id": step_id,
                "model": _trace_string(raw.get("model")),
                "role": _trace_string(raw.get("role")),
                "tool": _trace_string(raw.get("tool")),
                "detail": _trace_string(raw.get("detail")),
                "started_at_utc": _trace_string(raw.get("started_at_utc")),
                "finished_at_utc": _trace_string(raw.get("finished_at_utc")),
                "recorded_at_utc": _trace_string(raw.get("recorded_at_utc")),
                "imported_at_utc": _trace_string(raw.get("imported_at_utc")),
                "duration_ms": _optional_int(raw.get("duration_ms")),
                "input_tokens": _optional_int(raw.get("input_tokens")),
                "output_tokens": _optional_int(raw.get("output_tokens")),
                "total_tokens": _optional_int(raw.get("total_tokens")),
                "cost_usd": raw.get("cost_usd") if _is_number(raw.get("cost_usd")) else None,
            }
        )
    return events


def _summarize_session_trace_summary(raw_summary, raw_events) -> Dict:
    if isinstance(raw_summary, dict):
        by_provider = _session_trace_count_map(raw_summary.get("by_provider"), default_key="generic")
        by_status = _session_trace_count_map(raw_summary.get("by_status"), default_key="unknown")
        count = _optional_int(raw_summary.get("count"))
        if count is None and not by_provider and not by_status:
            return _session_trace_summary_from_events(raw_events)
        if count is None:
            count = sum(by_provider.values()) or sum(by_status.values())
        return {
            "count": count,
            "by_provider": by_provider,
            "by_status": by_status,
        }
    return _session_trace_summary_from_events(raw_events)


def _session_trace_summary_from_events(raw_events) -> Dict:
    by_provider: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    count = 0
    if isinstance(raw_events, list):
        for raw in raw_events:
            if not isinstance(raw, dict):
                continue
            count += 1
            provider = _trace_string(raw.get("provider")) or "generic"
            status = _trace_string(raw.get("status")) or "unknown"
            by_provider[provider] = by_provider.get(provider, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1
    return {
        "count": count,
        "by_provider": by_provider,
        "by_status": by_status,
    }


def _session_trace_count_map(raw_counts, default_key: str) -> Dict[str, int]:
    if not isinstance(raw_counts, dict):
        return {}
    counts: Dict[str, int] = {}
    for key, value in raw_counts.items():
        count = _optional_int(value)
        if count is None:
            continue
        key_text = _trace_string(key) if isinstance(key, str) else redact_text(str(key))[:200]
        key_text = key_text or default_key
        counts[key_text] = counts.get(key_text, 0) + count
    return counts


def _step_counts(steps: List[Dict]) -> Dict[str, int]:
    counts = {}
    for step in steps:
        status = _string_or_unknown(step.get("status"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _phase_counts(phases: List[Dict]) -> Dict[str, int]:
    counts = {}
    for phase in phases:
        status = _string_or_unknown(phase.get("status"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _hook_counts(events: List[Dict]) -> Dict[str, int]:
    counts = {}
    for event in events:
        status = _string_or_unknown(event.get("status"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _session_trace_counts(events: List[Dict]) -> Dict[str, int]:
    counts = {}
    for event in events:
        provider = _string_or_unknown(event.get("provider"))
        counts[provider] = counts.get(provider, 0) + 1
    return counts


def _agent_metrics(steps: List[Dict]) -> Dict:
    metrics = {
        "agent_invocations": 0,
        "agent_items_total": 0,
        "agent_items_cached": 0,
        "agent_workers_max": 0,
    }
    for step in steps:
        metrics["agent_invocations"] += step.get("agent_invocations") or 0
        metrics["agent_items_total"] += step.get("agent_items_total") or 0
        metrics["agent_items_cached"] += step.get("agent_items_cached") or 0
        metrics["agent_workers_max"] = max(metrics["agent_workers_max"], step.get("agent_workers") or 0)
    return metrics


def _usage_summary(raw_usage) -> Dict:
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    status = usage.get("status") if isinstance(usage.get("status"), str) and usage.get("status").strip() else "unavailable"
    return {
        "status": status,
        "input_tokens": _optional_int(usage.get("input_tokens")),
        "output_tokens": _optional_int(usage.get("output_tokens")),
        "total_tokens": _optional_int(usage.get("total_tokens")),
        "cost_usd": usage.get("cost_usd") if _is_number(usage.get("cost_usd")) else None,
    }


def _summarize_standard_files(run_dir: Path) -> List[Dict]:
    files = []
    for name in STANDARD_FILES:
        path = run_dir / name
        present = path.is_file() and not path.is_symlink()
        size = 0
        if present:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
        files.append({"name": name, "present": present, "bytes": size})
    return files


def _read_json(path: Path) -> Tuple[Optional[Dict], Optional[str]]:
    if path.is_symlink():
        return None, "symlink skipped"
    if not path.is_file():
        return None, "missing"
    try:
        return json.loads(read_regular_text_file_no_follow(path, "dashboard json", MAX_JSON_BYTES)), None
    except ValidationError as exc:
        if "must be at most" in str(exc):
            return None, "too large"
        return None, exc.__class__.__name__
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return None, exc.__class__.__name__


def _read_excerpt(path: Path, max_text_bytes: int) -> Dict:
    if path.is_symlink():
        return {"present": False, "text": "", "truncated": False, "error": "symlink skipped"}
    if not path.is_file():
        return {"present": False, "text": "", "truncated": False, "error": "missing"}
    try:
        data = read_regular_file_prefix_no_follow(path, "dashboard excerpt", max_text_bytes + 1)
    except (OSError, ValidationError) as exc:
        return {"present": False, "text": "", "truncated": False, "error": exc.__class__.__name__}
    truncated = len(data) > max_text_bytes
    text = data[:max_text_bytes].decode("utf-8", errors="replace")
    if truncated:
        text += "\n[truncated at %s bytes]" % max_text_bytes
    return {"present": True, "text": redact_text(text), "truncated": truncated, "error": None}


def _count_files(path: Path) -> Tuple[int, bool]:
    if path.is_symlink() or not path.is_dir():
        return 0, False
    count = 0
    visited = 0
    stack = [path]
    while stack:
        current = stack.pop()
        entries = _scan_directory_entries_no_follow(current, "count directory")
        if entries is None:
            continue
        for name, kind, _mtime in entries:
            if kind == "symlink":
                continue
            visited += 1
            if visited > FILE_SCAN_LIMIT:
                return count, True
            if kind == "file":
                if count >= FILE_COUNT_LIMIT:
                    return count, True
                count += 1
            elif kind == "dir":
                stack.append(current / name)
    return count, False


def _scan_directory_entries_no_follow(path: Path, label: str) -> Optional[List[Tuple[str, str, float]]]:
    try:
        reject_symlink_path(path, label)
        fd = open_dir_no_follow(path, label)
    except (OSError, ValidationError):
        return None
    try:
        entries = []
        for name in os.listdir(fd):
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                kind = "symlink"
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
            elif stat.S_ISDIR(info.st_mode):
                kind = "dir"
            else:
                kind = "other"
            entries.append((name, kind, info.st_mtime))
        return entries
    except (OSError, ValidationError):
        return None
    finally:
        os.close(fd)


def _open_child_dir_no_follow(parent_fd: int, name: str, label: str) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        raise ValidationError("%s does not exist: %s" % (label, name))
    except OSError as exc:
        raise ValidationError("failed to open %s %s: %s" % (label, name, exc.__class__.__name__))
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise ValidationError("%s must be a directory: %s" % (label, name))
        return fd
    except Exception:
        os.close(fd)
        raise


def _render_dashboard(
    title: str,
    generated_at: str,
    runs_dir: Path,
    benchmarks_dir: Path,
    goals_dir: Path,
    routines_dir: Path,
    auto_dir: Path,
    runs: List[Dict],
    auto_runs: List[Dict],
    reports: List[Dict],
    goals: List[Dict],
    run_details: List[Dict],
    goal_details: List[Dict],
    routines: List[Dict],
    routine_supervisor: Dict,
    routine_service: Dict,
) -> str:
    evidence_complete_runs = sum(1 for run in runs if run["evidence_complete"])
    attention_runs = sum(1 for run in runs if _run_needs_attention(run))
    eligible_claims = sum(1 for report in reports if report.get("evidence_status") == "claim-eligible-report")
    report_attention = sum(1 for report in reports if report.get("evidence_attention"))
    goal_attention = sum(1 for goal in goals if goal.get("evidence_attention"))
    auto_attention = sum(1 for item in auto_runs if item.get("needs_attention"))
    rows = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>%s</title>" % _h(title),
        "<style>%s</style>" % _css(),
        "</head>",
        "<body>",
        "<main>",
        "<header>",
        "<h1>%s</h1>" % _h(title),
        '<p class="muted">Generated %s from runs <code>%s</code>, auto receipts <code>%s</code>, goals <code>%s</code>, routines <code>%s</code>, and reports <code>%s</code>.</p>'
        % (
            _h(generated_at),
            _h(_display_path(runs_dir)),
            _h(_display_path(auto_dir)),
            _h(_display_path(goals_dir)),
            _h(_display_path(routines_dir)),
            _h(_display_path(benchmarks_dir)),
        ),
        "</header>",
        '<section class="metrics" aria-label="Summary">',
        _metric("Runs", str(len(runs))),
        _metric("Auto launches", str(len(auto_runs))),
        _metric("Auto needing attention", str(auto_attention)),
        _metric("Evidence-complete", str(evidence_complete_runs)),
        _metric("Needs attention", str(attention_runs)),
        _metric("Goals", str(len(goals))),
        _metric("Goals needing attention", str(goal_attention)),
        _metric("Reports", str(len(reports))),
        _metric("Report-declared eligible comparisons", str(eligible_claims)),
        _metric("Reports needing attention", str(report_attention)),
        "</section>",
        _render_runs(runs),
        _render_auto_orchestrations(auto_runs),
        _render_goals(goals),
        _render_goal_details(goal_details),
        _render_routines(routines),
        _render_routine_service(routine_service),
        _render_routine_supervisor(routine_supervisor),
        _render_run_details(run_details),
        _render_reports(reports),
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(rows) + "\n"


def _render_routine_supervisor(supervisor: Dict) -> str:
    if not isinstance(supervisor, dict) or not supervisor:
        return '<section><h2>Routine Supervisor</h2><p class="empty">No detached supervisor launches found.</p></section>'
    status = supervisor.get("status") or "unknown"
    liveness = supervisor.get("liveness") or "unknown"
    rows = [
        ("Lifecycle", _badge(status)),
        ("Liveness", _badge(liveness)),
        ("Scheduler", _h(supervisor.get("supervisor_status") or "unknown")),
        ("Worker", _h("pid %s" % (supervisor.get("pid") or "unknown"))),
        ("Cycles", _h(supervisor.get("cycles_completed") or 0)),
        ("Results", _h("%s completed / %s blocked / %s failed" % (
            supervisor.get("completed_count") or 0,
            supervisor.get("blocked_count") or 0,
            supervisor.get("failed_count") or 0,
        ))),
        ("Updated", _h(supervisor.get("updated_at_utc") or "unknown")),
        ("Metadata", _h(supervisor.get("metadata") or "unknown")),
    ]
    return '<section><h2>Routine Supervisor</h2><table><tbody>%s</tbody></table></section>' % "".join(
        "<tr><th>%s</th><td>%s</td></tr>" % (_h(label), value) for label, value in rows
    )


def _render_routine_service(service: Dict) -> str:
    if not isinstance(service, dict) or service.get("effective_status") == "not-installed":
        return '<section><h2>Routine Service</h2><p class="empty">No restart-persistent routine service installed.</p></section>'
    state = service.get("state") if isinstance(service.get("state"), dict) else {}
    rows = [
        ("Status", _badge(service.get("effective_status") or "unknown")),
        ("Liveness", _badge(service.get("liveness") or "unknown")),
        ("Platform", _h(service.get("platform") or "unknown")),
        ("Bindings", _h("valid" if service.get("bindings_valid") else "invalid")),
        ("Descriptor", _h(
            "valid" if service.get("descriptor_valid") else ("invalid" if service.get("descriptor_present") else "missing")
        )),
        ("Staged descriptor", _h(
            "valid"
            if service.get("staged_descriptor_valid")
            else ("invalid" if service.get("staged_descriptor_present") else "absent")
        )),
        ("Expiration", _h(service.get("expires_at_utc") or "unknown")),
        ("Cycles", _h(state.get("cycles_completed") or 0)),
        ("Results", _h("%s completed / %s failed" % (
            state.get("results_completed") or 0,
            state.get("results_failed") or 0,
        ))),
        ("Heartbeat", _h(state.get("heartbeat_at_utc") or "not recorded")),
    ]
    if service.get("error"):
        rows.append(("Attention", '<span class="warn">%s</span>' % _h(service.get("error"))))
    return '<section><h2>Routine Service</h2><table><tbody>%s</tbody></table></section>' % "".join(
        "<tr><th>%s</th><td>%s</td></tr>" % (_h(label), value) for label, value in rows
    )


def _render_routines(routines: List[Dict]) -> str:
    if not routines:
        return '<section><h2>Routines</h2><p class="empty">No routine manifests found.</p></section>'
    rows = []
    for routine in routines:
        state = "Paused" if routine.get("paused") else "Active"
        severity = "active" if routine.get("paused") else "ok"
        if routine.get("error") or routine.get("control_error") or routine.get("state_error"):
            state = "Needs attention"
            severity = "failed"
        rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                _h(routine.get("name") or "unknown"),
                _badge(state.lower().replace(" ", "-"), severity=severity),
                _h(routine.get("schedule") or "unknown"),
                _h(routine.get("workflow") or "unknown"),
                _badge(routine.get("last_status") or "never"),
                _h("%s runs / %s history" % (routine.get("executions") or 0, routine.get("history_count") or 0)),
            )
        )
    return (
        '<section><h2>Routines</h2><table><thead><tr><th>Routine</th><th>Control</th><th>Schedule</th>'
        '<th>Workflow</th><th>Latest</th><th>History</th></tr></thead><tbody>%s</tbody></table></section>'
        % "".join(rows)
    )


def _render_runs(runs: List[Dict]) -> str:
    if not runs:
        return '<section><h2>Runs</h2><p class="empty">No run directories found.</p></section>'
    rows = [
        "<section>",
        "<h2>Runs</h2>",
        '<table class="runs">',
        "<thead><tr><th>Run</th><th>Status</th><th>Timing</th><th>Workflow</th><th>Steps</th><th>Evidence</th><th>Final report excerpt</th></tr></thead>",
        "<tbody>",
    ]
    for run in runs:
        rows.append(
            "<tr>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "</tr>"
            % (
                _run_cell(run),
                _badge(run["status"]),
                _timing_cell(run),
                _workflow_cell(run),
                _steps_cell(run),
                _evidence_cell(run),
                _excerpt_cell(run["final_report_excerpt"]),
            )
        )
    rows.extend(["</tbody>", "</table>", "</section>"])
    return "\n".join(rows)


def _render_auto_orchestrations(auto_runs: List[Dict]) -> str:
    if not auto_runs:
        return '<section><h2>Automatic Orchestration</h2><p class="empty">No auto receipts found.</p></section>'
    rows = [
        "<section>",
        "<h2>Automatic Orchestration</h2>",
        "<table>",
        "<thead><tr><th>Receipt</th><th>Effective status</th><th>Handoff</th><th>Route</th><th>Mode</th><th>Child</th><th>Bounds</th></tr></thead>",
        "<tbody>",
    ]
    for item in auto_runs:
        child = item.get("child") if isinstance(item.get("child"), dict) else {}
        child_rows = [
            "<div>%s</div>" % _h(child.get("resolution") or "unknown"),
            '<div class="muted">%s / %s</div>'
            % (_h(child.get("kind") or "no child"), _h(item.get("liveness") or "unknown")),
        ]
        if child.get("error"):
            child_rows.append('<div class="warn">%s</div>' % _h(child.get("error")))
        bounds = "%s worker(s) / %s iteration(s)" % (
            item.get("max_workers") or 0,
            item.get("max_iterations") or 0,
        )
        if item.get("native_agent_max_threads") is not None:
            bounds += " / native max %s children, depth 1, %s shared weighted rollout tokens" % (
                item["native_agent_max_threads"],
                item.get("native_agent_max_total_tokens") or "unknown",
            )
        rows.append(
            "<tr><td><strong>%s</strong><div class=\"muted\">%s</div></td>"
            "<td>%s</td><td>%s</td><td>%s<div class=\"muted\">%s</div></td>"
            "<td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                _h(item.get("name") or "unknown"),
                _h(item.get("updated_at_utc") or item.get("mtime_utc") or "unknown"),
                _badge(item.get("effective_status") or "unknown"),
                _badge(item.get("status") or "unknown"),
                _h(item.get("route") or "unknown"),
                _h(item.get("verifier_kind") or "none"),
                _h(item.get("execution_mode") or "unknown"),
                "".join(child_rows),
                _h(bounds),
            )
        )
    rows.extend(["</tbody>", "</table>", "</section>"])
    return "\n".join(rows)


def _render_goals(goals: List[Dict]) -> str:
    if not goals:
        return '<section><h2>Goals</h2><p class="empty">No goal artifacts found.</p></section>'
    rows = [
        "<section>",
        "<h2>Goals</h2>",
        '<table class="goals">',
        "<thead><tr><th>Goal</th><th>Status</th><th>Workflow</th><th>Iterations</th><th>Latest</th><th>Operator action</th></tr></thead>",
        "<tbody>",
    ]
    for goal in goals:
        rows.append(
            "<tr>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "</tr>"
            % (
                _goal_cell(goal),
                _badge(goal.get("status") or "unknown"),
                _goal_workflow_cell(goal),
                _goal_iterations_cell(goal),
                _goal_latest_cell(goal),
                _goal_action_cell(goal),
            )
        )
    rows.extend(["</tbody>", "</table>", "</section>"])
    return "\n".join(rows)


def _render_goal_details(goal_details: List[Dict]) -> str:
    if not goal_details:
        return ""
    rows = ["<section>", "<h2>Goal Details</h2>"]
    for detail in goal_details:
        handle = detail.get("goal_handle") or goal_handle(str(detail.get("name") or detail.get("goal_id") or "unknown"))
        rows.extend(
            [
                '<section class="detail-section" id="%s">' % _h(_goal_detail_anchor(handle)),
                "<h3><code>%s</code></h3>" % _h(detail.get("goal_id") or detail.get("name") or "unknown"),
                _render_goal_detail_summary(detail),
                _render_goal_detail_iterations(detail),
                "</section>",
            ]
        )
    rows.append("</section>")
    return "\n".join(rows)


def _render_reports(reports: List[Dict]) -> str:
    if not reports:
        return '<section><h2>Benchmark Reports</h2><p class="empty">No benchmark reports found.</p></section>'
    rows = [
        "<section>",
        "<h2>Benchmark Reports</h2>",
        "<table>",
        "<thead><tr><th>Report</th><th>Type</th><th>Evidence</th><th>Suite</th><th>System</th><th>Result</th><th>Provenance</th><th>Warnings</th></tr></thead>",
        "<tbody>",
    ]
    for report in reports:
        rows.append(
            "<tr>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "</tr>"
            % (
                _h(report["name"]),
                _h(report["kind"]),
                _report_evidence_cell(report),
                _h(report["suite"]),
                _h(report["system"]),
                _report_result_cell(report),
                _provenance_cell(report),
                _warnings_cell(report),
            )
        )
    rows.extend(["</tbody>", "</table>", "</section>"])
    return "\n".join(rows)


def _run_cell(run: Dict) -> str:
    lines = ["<strong>%s</strong>" % _h(run["id"])]
    lines.append('<div class="muted">Updated %s</div>' % _h(run["mtime_utc"]))
    lines.append('<div><a href="#%s">Details</a></div>' % _h(_detail_anchor(run["id"])))
    if run["state_error"]:
        lines.append('<div class="warn">state.json: %s</div>' % _h(run["state_error"]))
    if run["workflow_error"]:
        lines.append('<div class="warn">workflow.json: %s</div>' % _h(run["workflow_error"]))
    return "".join(lines)


def _goal_cell(goal: Dict) -> str:
    lines = ["<strong>%s</strong>" % _h(goal.get("goal_id") or goal.get("name") or "unknown")]
    if goal.get("updated_at_utc"):
        lines.append('<div class="muted">Updated %s</div>' % _h(goal.get("updated_at_utc")))
    elif goal.get("mtime_utc"):
        lines.append('<div class="muted">Updated %s</div>' % _h(goal.get("mtime_utc")))
    if goal.get("goal_handle"):
        lines.append('<div><a href="#%s">Details</a></div>' % _h(_goal_detail_anchor(goal.get("goal_handle"))))
    if goal.get("error"):
        lines.append('<div class="warn">goal artifact: %s</div>' % _h(goal.get("error")))
    return "".join(lines)


def _goal_workflow_cell(goal: Dict) -> str:
    lines = ["<div>%s</div>" % _h(goal.get("workflow") or "unknown")]
    if goal.get("started_at_utc"):
        lines.append('<div class="muted">Started %s</div>' % _h(goal.get("started_at_utc")))
    if goal.get("finished_at_utc"):
        lines.append('<div class="muted">Finished %s</div>' % _h(goal.get("finished_at_utc")))
    return "".join(lines)


def _goal_iterations_cell(goal: Dict) -> str:
    iterations = _safe_int(goal.get("iterations"))
    max_iterations = goal.get("max_iterations")
    if isinstance(max_iterations, int) and not isinstance(max_iterations, bool):
        text = "%s/%s iterations" % (iterations, max_iterations)
    else:
        text = "%s iterations" % iterations
    rows = ["<div>%s</div>" % _h(text)]
    remaining = goal.get("remaining_iterations")
    if isinstance(remaining, int) and not isinstance(remaining, bool):
        rows.append('<div class="muted">%s remaining under current cap</div>' % _h(remaining))
    return "".join(rows)


def _goal_latest_cell(goal: Dict) -> str:
    latest = goal.get("last_iteration") if isinstance(goal.get("last_iteration"), dict) else {}
    rows = []
    if latest:
        status = latest.get("status") or "unknown"
        iteration = latest.get("iteration")
        label = "iteration %s" % iteration if iteration is not None else "latest iteration"
        rows.append("<div>%s %s</div>" % (_h(label), _badge(status)))
        if latest.get("run_id"):
            rows.append('<div class="muted">run %s</div>' % _h(latest.get("run_id")))
    else:
        rows.append('<span class="muted">No iterations recorded</span>')
    if goal.get("last_check_status"):
        check = goal.get("last_check_status")
        returncode = goal.get("last_check_returncode")
        suffix = " rc %s" % returncode if returncode is not None else ""
        rows.append('<div class="muted">Verifier: %s%s</div>' % (_h(check), _h(suffix)))
    planner_session = goal.get("planner_session") if isinstance(goal.get("planner_session"), dict) else {}
    if goal.get("planning_mode") == "deterministic-direct":
        rows.append('<div class="muted">Planning: %s</div>' % _h(_goal_planning_text(goal)))
    elif goal.get("planning_mode") == "progressive":
        rows.append('<div class="muted">Planning: %s</div>' % _h(_goal_planning_text(goal)))
        if planner_session:
            rows.append('<div class="muted">Planner: %s</div>' % _h(_planner_session_text(planner_session)))
    elif planner_session:
        rows.append('<div class="muted">Planner: %s</div>' % _h(_planner_session_text(planner_session)))
    background = goal.get("background") if isinstance(goal.get("background"), dict) else {}
    if background:
        rows.append(
            '<div class="muted">Worker: %s / %s</div>'
            % (_h(background.get("status") or "unknown"), _h(background.get("liveness") or "unknown"))
        )
    return "".join(rows)


def _goal_action_cell(goal: Dict) -> str:
    if goal.get("status") == "completed":
        return '<span class="muted">No action needed.</span>'
    if goal.get("status") in {"waiting", "monitoring"}:
        wake = goal.get("next_wake") if isinstance(goal.get("next_wake"), dict) else {}
        scheduled = wake.get("scheduled_for_utc") or "the persisted wake time"
        activity = (
            "Listening for a monitor event until"
            if goal.get("status") == "monitoring"
            else "Waiting until"
        )
        return '<span class="muted">%s %s; use the detached-goal controls to stop the worker.</span>' % (
            _h(activity),
            _h(scheduled),
        )
    if goal.get("status") == "invalid":
        return '<span class="muted">Inspect or replace the invalid goal artifact.</span>'
    path = goal.get("path")
    if isinstance(path, Path):
        rendered_path = _display_path(path)
    else:
        rendered_path = goal.get("name") or "GOAL_JSON"
    note = "Resume with the same workflow and verifier."
    if goal.get("status") == "max_iterations_exhausted":
        note = "Resume with a higher --max-iterations cap if more attempts are warranted."
    command = "run-model-goal" if goal.get("workflow") == "adaptive-model-workflow" else "run-goal"
    return "<code>%s --resume-goal %s</code><div class=\"muted\">%s</div>" % (
        _h(command),
        _h(rendered_path),
        _h(note),
    )


def _render_goal_detail_summary(detail: Dict) -> str:
    latest = detail.get("last_iteration") if isinstance(detail.get("last_iteration"), dict) else {}
    rows = [
        ("Status", _badge(detail.get("status") or "unknown")),
        ("Workflow", _h(detail.get("workflow") or "unknown")),
        ("Updated", _h(detail.get("updated_at_utc") or detail.get("mtime_utc") or "unknown")),
        ("Iterations", _h(_goal_iterations_text(detail))),
        ("Planning", _h(_goal_planning_text(detail))),
        (
            "Planner session",
            _h(
                "not used by deterministic direct planning"
                if detail.get("planning_mode") == "deterministic-direct"
                else _planner_session_text(detail.get("planner_session"))
            ),
        ),
        ("Verifier policy", _h(_goal_verifier_policy_text(detail.get("verifier_policy")))),
        ("Dynamic interval", _h(_dynamic_interval_text(detail))),
        ("Latest", _goal_detail_latest_text(latest, detail)),
        ("Operator action", _goal_action_cell(detail)),
        ("Verifier output", _h(detail.get("output_policy") or "raw verifier output omitted")),
    ]
    if detail.get("error"):
        rows.append(("Goal artifact", '<span class="warn">%s</span>' % _h(detail.get("error"))))
    background = detail.get("background") if isinstance(detail.get("background"), dict) else {}
    if background:
        rows.append(
            (
                "Detached worker",
                _h("%s / %s / pid %s" % (
                    background.get("status") or "unknown",
                    background.get("liveness") or "unknown",
                    background.get("pid") or "unknown",
                )),
            )
        )
    return "<table><tbody>%s</tbody></table>" % "".join(
        "<tr><th>%s</th><td>%s</td></tr>" % (_h(label), value) for label, value in rows
    )


def _render_goal_detail_iterations(detail: Dict) -> str:
    iterations = detail.get("iterations_detail") if isinstance(detail.get("iterations_detail"), list) else []
    if not iterations:
        return '<p class="empty">No goal iterations recorded.</p>'
    rows = [
        "<h4>Iterations</h4>",
        "<table>",
        "<thead><tr><th>Iteration</th><th>Status</th><th>Run</th><th>Verifier</th><th>Timing</th><th>Output summary</th></tr></thead>",
        "<tbody>",
    ]
    for iteration in iterations:
        check = iteration.get("check") if isinstance(iteration.get("check"), dict) else {}
        rows.append(
            "<tr>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "</tr>"
            % (
                _h(iteration.get("iteration") or "unknown"),
                _badge(iteration.get("status") or "unknown"),
                _goal_iteration_run_cell(iteration),
                _goal_iteration_check_cell(check),
                _goal_iteration_timing_cell(iteration, check),
                _goal_iteration_output_cell(check),
            )
        )
    rows.extend(["</tbody>", "</table>"])
    if detail.get("iterations_truncated"):
        rows.append(
            '<p class="muted">Showing the latest %s of %s iterations.</p>'
            % (_h(detail.get("iteration_display_count")), _h(detail.get("iterations")))
        )
    return "\n".join(rows)


def _goal_iterations_text(goal: Dict) -> str:
    iterations = _safe_int(goal.get("iterations"))
    max_iterations = goal.get("max_iterations")
    if isinstance(max_iterations, int) and not isinstance(max_iterations, bool):
        return "%s/%s iterations" % (iterations, max_iterations)
    return "%s iterations" % iterations


def _planner_session_text(raw) -> str:
    session = raw if isinstance(raw, dict) else {}
    if not session:
        return "unavailable"
    mode = str(session.get("mode") or "unavailable")
    availability = "available" if session.get("available") is True else "unavailable"
    turns = session.get("turns")
    if not isinstance(turns, int) or isinstance(turns, bool):
        turns = 0
    return "%s / %s / %s turns" % (mode, availability, turns)


def _goal_planning_text(goal: Dict) -> str:
    calls = _safe_int(goal.get("planning_provider_calls"))
    call_text = "%s planner call%s" % (calls, "" if calls == 1 else "s")
    if goal.get("planning_mode") == "deterministic-direct":
        return "deterministic direct / 0 planner calls per iteration"
    if goal.get("planning_mode") == "model-authored":
        return "model-authored workflow per iteration / %s" % call_text
    if goal.get("planning_mode") == "progressive":
        return "direct first / model escalation after verifier failure / %s" % call_text
    return "unknown"


def _dynamic_interval_text(goal: Dict) -> str:
    config = (
        goal.get("dynamic_interval")
        if isinstance(goal.get("dynamic_interval"), dict)
        else {}
    )
    if not config:
        return "disabled"
    wake = goal.get("next_wake") if isinstance(goal.get("next_wake"), dict) else {}
    text = "model-selected %s-%s minutes / no extra provider call" % (
        config.get("min_delay_minutes"),
        config.get("max_delay_minutes"),
    )
    if wake and wake.get("status") in {"scheduled", "waiting"}:
        text += " / next wake %s" % (wake.get("scheduled_for_utc") or "unknown")
    monitor = goal.get("monitor") if isinstance(goal.get("monitor"), dict) else {}
    wake_monitor = wake.get("monitor") if isinstance(wake.get("monitor"), dict) else {}
    if monitor:
        text += " / event monitor %s" % (wake_monitor.get("status") or "configured")
    return text


def _goal_verifier_policy_text(raw) -> str:
    policy = raw if isinstance(raw, dict) else {}
    if not policy:
        return "command-only or legacy model verifier"
    return "%s / max %s weighted tokens / %s / %s / %s" % (
        policy.get("effort") or "unknown effort",
        policy.get("max_tokens") if policy.get("max_tokens") is not None else "unknown",
        policy.get("token_cap_enforcement") or "unknown enforcement",
        "isolated" if policy.get("isolated_profile") is True else "standard profile",
        "ephemeral" if policy.get("ephemeral_session") is True else "persistent session",
    )


def _goal_detail_latest_text(latest: Dict, detail: Dict) -> str:
    if not latest:
        return '<span class="muted">No iterations recorded</span>'
    status = latest.get("status") or "unknown"
    iteration = latest.get("iteration")
    label = "iteration %s" % iteration if iteration is not None else "latest iteration"
    rows = ["%s %s" % (_h(label), _badge(status))]
    check_status = detail.get("last_check_status")
    if check_status:
        returncode = detail.get("last_check_returncode")
        suffix = " rc %s" % returncode if returncode is not None else ""
        rows.append('<div class="muted">Verifier: %s%s</div>' % (_h(check_status), _h(suffix)))
    return "".join(rows)


def _goal_iteration_run_cell(iteration: Dict) -> str:
    rows = []
    if iteration.get("run_id"):
        rows.append("<div>%s</div>" % _h(iteration.get("run_id")))
    if iteration.get("run_status"):
        rows.append('<div class="muted">run status: %s</div>' % _h(iteration.get("run_status")))
    if iteration.get("error_class"):
        rows.append('<div class="warn">error: %s</div>' % _h(iteration.get("error_class")))
    planner_session = iteration.get("planner_session") if isinstance(iteration.get("planner_session"), dict) else {}
    if planner_session:
        turn = planner_session.get("turn") or "unknown"
        rows.append(
            '<div class="muted">planner session: %s, turn %s</div>'
            % (_h(planner_session.get("mode") or "unknown"), _h(turn))
        )
    if iteration.get("planning_mode") in {"deterministic-direct", "model-authored"}:
        rows.append(
            '<div class="muted">planning: %s, %s provider calls</div>'
            % (
                _h(iteration.get("planning_mode")),
                _h(iteration.get("planning_provider_calls") or 0),
            )
        )
    return "".join(rows) or '<span class="muted">unknown</span>'


def _goal_iteration_check_cell(check: Dict) -> str:
    status = check.get("status") or "unknown"
    rows = [_badge(status)]
    if check.get("kind"):
        rows.append('<div class="muted">%s verifier</div>' % _h(check.get("kind")))
    if check.get("returncode") is not None:
        rows.append('<div class="muted">return code %s</div>' % _h(check.get("returncode")))
    if check.get("timed_out") is True:
        rows.append('<div class="warn">timed out</div>')
    if check.get("kind") == "command+model":
        command = check.get("command") if isinstance(check.get("command"), dict) else {}
        model = check.get("model") if isinstance(check.get("model"), dict) else {}
        rows.append('<div class="muted">command %s</div>' % _h(command.get("status") or "unknown"))
        rows.append(
            '<div class="muted">model %s</div>'
            % _h(model.get("status") or ("skipped" if check.get("short_circuited") else "unknown"))
        )
        rows.append(
            '<div class="muted">model verifier calls: %s</div>'
            % _h(check.get("model_provider_calls") or 0)
        )
    elif check.get("kind") == "workflow-completion":
        completion = (
            check.get("completion_verdict")
            if isinstance(check.get("completion_verdict"), dict)
            else {}
        )
        rows.append(
            '<div class="muted">provider calls: %s</div>'
            % _h(check.get("provider_calls") or 0)
        )
        if completion.get("confidence"):
            rows.append(
                '<div class="muted">%s confidence / %s evidence item(s)</div>'
                % (
                    _h(completion.get("confidence")),
                    _h(completion.get("evidence_count") or 0),
                )
            )
    verdict = check.get("verdict") if isinstance(check.get("verdict"), dict) else {}
    if verdict.get("confidence"):
        rows.append('<div class="muted">%s confidence</div>' % _h(verdict.get("confidence")))
    if check.get("error"):
        rows.append('<div class="warn">%s</div>' % _h(check.get("error")))
    return "".join(rows)


def _goal_iteration_timing_cell(iteration: Dict, check: Dict) -> str:
    rows = []
    if iteration.get("started_at_utc"):
        rows.append('<div>Run started %s</div>' % _h(iteration.get("started_at_utc")))
    if iteration.get("finished_at_utc"):
        rows.append('<div>Run finished %s</div>' % _h(iteration.get("finished_at_utc")))
    if check.get("started_at_utc"):
        rows.append('<div class="muted">Check started %s</div>' % _h(check.get("started_at_utc")))
    if check.get("finished_at_utc"):
        rows.append('<div class="muted">Check finished %s</div>' % _h(check.get("finished_at_utc")))
    wait = iteration.get("dynamic_wait") if isinstance(iteration.get("dynamic_wait"), dict) else {}
    if wait:
        rows.append(
            '<div class="muted">%s minute model-selected wait / %s / wake %s</div>'
            % (
                _h(wait.get("delay_minutes") or "unknown"),
                _h(wait.get("status") or "unknown"),
                _h(wait.get("scheduled_for_utc") or "unknown"),
            )
        )
        monitor = wait.get("monitor") if isinstance(wait.get("monitor"), dict) else {}
        if monitor:
            event = ""
            if monitor.get("event_present") is True:
                event = " / %s chars / hash %s" % (
                    monitor.get("event_chars") or 0,
                    (monitor.get("event_sha256") or "unknown")[:12],
                )
            rows.append(
                '<div class="muted">event monitor %s%s</div>'
                % (_h(monitor.get("status") or "unknown"), _h(event))
            )
    return "".join(rows) or '<span class="muted">unknown</span>'


def _goal_iteration_output_cell(check: Dict) -> str:
    parts = []
    stdout_bytes = check.get("stdout_bytes")
    stderr_bytes = check.get("stderr_bytes")
    if stdout_bytes is not None or stderr_bytes is not None:
        parts.append("%s/%s bytes out/err" % (stdout_bytes or 0, stderr_bytes or 0))
    if check.get("stdout_truncated") is True or check.get("stderr_truncated") is True:
        parts.append("truncated")
    if check.get("output_excerpts_omitted") is True:
        parts.append("excerpts omitted")
    verdict = check.get("verdict") if isinstance(check.get("verdict"), dict) else {}
    feedback = verdict.get("feedback") or check.get("feedback")
    evidence = verdict.get("evidence") if isinstance(verdict.get("evidence"), list) else []
    usage = check.get("usage") if isinstance(check.get("usage"), dict) else {}
    if usage.get("total_tokens") is not None:
        parts.append("%s tokens" % usage.get("total_tokens"))
    completion = (
        check.get("completion_verdict")
        if isinstance(check.get("completion_verdict"), dict)
        else {}
    )
    if completion:
        parts.append("%s verdict bytes" % (completion.get("output_bytes") or 0))
        parts.append(
            "hash %s"
            % (_string_or_empty(completion.get("output_sha256"))[:12] or "unknown")
        )
    rows = ['<span class="muted">%s</span>' % _h(" / ".join(parts) if parts else "not recorded")]
    if feedback:
        rows.append('<div>%s</div>' % _h(feedback))
    if evidence:
        rows.append('<div class="muted">%s evidence item(s)</div>' % _h(len(evidence)))
    return "".join(rows)


def _goal_detail_anchor(handle: str) -> str:
    return "goal-detail-%s" % _class_token(handle)


def _workflow_cell(run: Dict) -> str:
    lines = ["<div>%s</div>" % _h(run["workflow"])]
    if run["workflow_source"]:
        lines.append('<div class="muted">%s</div>' % _h(run["workflow_source"]))
    if run["started_at_utc"]:
        lines.append('<div class="muted">Started %s</div>' % _h(run["started_at_utc"]))
    return "".join(lines)


def _timing_cell(run: Dict) -> str:
    lines = []
    if run.get("duration_ms") is not None:
        lines.append("<div>Duration: %s</div>" % _h(_format_duration(run["duration_ms"])))
    elif run.get("started_at_utc"):
        lines.append('<div class="muted">Duration: running or unavailable</div>')
    else:
        lines.append('<div class="muted">Duration unavailable</div>')
    if run.get("finished_at_utc"):
        lines.append('<div class="muted">Finished %s</div>' % _h(run["finished_at_utc"]))
    runner_label = _runner_label(run.get("runner") or {})
    if runner_label != "unavailable":
        lines.append('<div class="muted">Runner: %s</div>' % _h(runner_label))
    background_label = _background_label(run.get("background") or {})
    if background_label != "unavailable":
        lines.append('<div class="muted">Background: %s</div>' % _h(background_label))
    launch_label = _launch_label(run.get("launch") or {})
    if launch_label != "unavailable":
        lines.append('<div class="muted">Launch: %s</div>' % _h(launch_label))
    agent = run.get("agent_metrics") or {}
    if agent.get("agent_invocations") or agent.get("agent_items_total"):
        lines.append(
            '<div class="muted">Agents: %s calls, %s items, %s cached</div>'
            % (
                _h(agent.get("agent_invocations", 0)),
                _h(agent.get("agent_items_total", 0)),
                _h(agent.get("agent_items_cached", 0)),
            )
        )
    usage = run.get("usage") or {}
    lines.append('<div class="muted">Tokens/cost: %s</div>' % _h(_usage_label(usage)))
    hook_counts = run.get("hook_counts") or {}
    if hook_counts:
        summary = ", ".join("%s %s" % (count, status) for status, count in sorted(hook_counts.items()))
        lines.append('<div class="muted">Hooks: %s</div>' % _h(summary))
    if run.get("session_trace_count"):
        lines.append('<div class="muted">Session traces: %s</div>' % _h(_session_trace_count_label(run)))
    approvals = run.get("approvals") if isinstance(run.get("approvals"), dict) else {}
    pending_approvals = approvals.get("pending_count")
    if isinstance(pending_approvals, int) and not isinstance(pending_approvals, bool) and pending_approvals > 0:
        lines.append('<div class="warn">Approvals needed: %s manual gate(s)</div>' % _h(pending_approvals))
    runtime = run.get("runtime") if isinstance(run.get("runtime"), dict) else {}
    runtime_label = _runtime_summary_label(runtime)
    if runtime_label:
        lines.append('<div class="muted">Runtime actions: %s</div>' % _h(runtime_label))
    return "".join(lines)


def _runtime_summary_label(runtime: Dict) -> str:
    if not isinstance(runtime, dict) or not runtime.get("available"):
        return ""
    counts = runtime.get("counts") if isinstance(runtime.get("counts"), dict) else {}
    parts = []
    for key, label in [
        ("operator_action", "operator"),
        ("failed_recovery", "recovery"),
        ("ready", "ready"),
        ("waiting", "waiting"),
        ("active", "active"),
    ]:
        value = counts.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            parts.append("%s %s" % (value, label))
    return ", ".join(parts)


def _session_trace_count_label(run: Dict) -> str:
    count = run.get("session_trace_count")
    counts = run.get("session_trace_counts") if isinstance(run.get("session_trace_counts"), dict) else {}
    summary = ", ".join("%s %s" % (count, provider) for provider, count in sorted(counts.items()))
    if summary:
        return "%s total (%s)" % (count, summary)
    return "%s total" % count


def _runner_label(runner: Dict) -> str:
    status = _string_or_unknown(runner.get("status"))
    if status == "unavailable":
        return "unavailable"
    parts = [status]
    liveness = _string_or_empty(runner.get("liveness"))
    if liveness and liveness not in {"unavailable", "finished"}:
        parts.append(liveness)
    heartbeat_age = runner.get("heartbeat_age_seconds")
    if isinstance(heartbeat_age, int) and not isinstance(heartbeat_age, bool):
        parts.append("age %s" % _format_duration(heartbeat_age * 1000))
    mode = _string_or_empty(runner.get("mode"))
    if mode:
        parts.append(mode)
    pid = runner.get("pid")
    if isinstance(pid, int):
        parts.append("pid %s" % pid)
    active_process_count = runner.get("active_process_count")
    if isinstance(active_process_count, int) and not isinstance(active_process_count, bool) and active_process_count > 0:
        parts.append("%s active child%s" % (active_process_count, "" if active_process_count == 1 else "ren"))
    heartbeat = _string_or_empty(runner.get("heartbeat_at_utc"))
    if heartbeat:
        parts.append("heartbeat %s" % heartbeat)
    return ", ".join(parts)


def _launch_label(launch: Dict) -> str:
    if not isinstance(launch, dict):
        return "unavailable"
    status = _string_or_unknown(launch.get("status"))
    if status == "unavailable":
        if not launch.get("error") or launch.get("error") == "missing":
            return "unavailable"
    parts = [status]
    preflight = _string_or_empty(launch.get("preflight_status"))
    if preflight:
        parts.append("preflight %s" % preflight)
    if launch.get("no_process_started") is True:
        parts.append("no process started")
    external = launch.get("external_supervisor") if isinstance(launch.get("external_supervisor"), dict) else {}
    if external.get("approval_tokens_required_at_launch") is True:
        parts.append("approval tokens required")
    policy = launch.get("policy") if isinstance(launch.get("policy"), dict) else {}
    approval_count = policy.get("approval_count")
    if isinstance(approval_count, int) and not isinstance(approval_count, bool) and approval_count > 0:
        parts.append("%s approval(s)" % approval_count)
    if launch.get("error"):
        parts.append(_string_or_empty(launch.get("error")) or "error")
    return ", ".join(parts)


def _background_label(background: Dict) -> str:
    if not isinstance(background, dict):
        return "unavailable"
    status = _string_or_unknown(background.get("status"))
    if status == "unavailable" and background.get("error") in {None, "", "missing"}:
        return "unavailable"
    parts = [status]
    run_status = _string_or_empty(background.get("run_status"))
    if run_status and run_status != "unknown":
        parts.append("run %s" % run_status)
    pid = background.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        parts.append("pid %s" % pid)
    if background.get("detached_session") is True:
        parts.append("isolated")
    if background.get("error_class"):
        parts.append(_string_or_empty(background.get("error_class")))
    if background.get("error") and background.get("error") != "missing":
        parts.append(_string_or_empty(background.get("error")))
    return ", ".join(parts)


def _steps_cell(run: Dict) -> str:
    phases = run.get("phases") or []
    phase_prefix = _phase_summary_cell(phases)
    if not run["steps"]:
        if phase_prefix:
            return phase_prefix
        return '<span class="muted">No steps recorded</span>'
    summary = ", ".join("%s %s" % (count, status) for status, count in sorted(run["step_counts"].items()))
    rows = [phase_prefix, '<details><summary>%s</summary><ul class="steps">' % _h(summary)]
    for step in run["steps"]:
        detail = ""
        if step["detail"]:
            detail = '<div class="muted">%s</div>' % _h(step["detail"])
        updated = ""
        if step["updated_at_utc"]:
            updated = '<div class="muted">%s</div>' % _h(step["updated_at_utc"])
        meta_parts = []
        if step.get("kind"):
            meta_parts.append(step["kind"])
        if step.get("phase") and step.get("phase") != "default":
            meta_parts.append("phase %s" % step["phase"])
        if step.get("duration_ms") is not None:
            meta_parts.append(_format_duration(step["duration_ms"]))
        if step.get("agent_items_total"):
            meta_parts.append(
                "%s agent item(s), %s cached"
                % (step.get("agent_items_total"), step.get("agent_items_cached") or 0)
            )
        meta = ""
        if meta_parts:
            meta = '<div class="muted">%s</div>' % _h(" / ".join(meta_parts))
        rows.append("<li>%s <strong>%s</strong>%s%s%s</li>" % (_badge(step["status"]), _h(step["id"]), meta, detail, updated))
    rows.append("</ul></details>")
    return "".join(rows)


def _phase_summary_cell(phases: List[Dict]) -> str:
    if not phases:
        return ""
    summary = ", ".join("%s %s" % (count, status) for status, count in sorted(_phase_counts(phases).items()))
    rows = ['<details><summary>Phases: %s</summary><ul class="steps">' % _h(summary)]
    for phase in phases:
        rows.append(
            "<li>%s <strong>%s</strong><div class=\"muted\">%s steps / %s agent item(s) / %s</div></li>"
            % (
                _badge(phase.get("status", "unknown")),
                _h(phase.get("id", "default")),
                _h(phase.get("steps_total", 0)),
                _h(phase.get("agent_items_total", 0)),
                _h(_usage_label(phase.get("usage") or {})),
            )
        )
    rows.append("</ul></details>")
    return "".join(rows)


def _evidence_cell(run: Dict) -> str:
    present = [item["name"] for item in run["standard_files"] if item["present"]]
    missing = [item["name"] for item in run["standard_files"] if not item["present"]]
    artifact_count = "%s%s" % (run["artifact_count"], "+" if run["artifact_count_truncated"] else "")
    log_count = "%s%s" % (run["log_count"], "+" if run["log_count_truncated"] else "")
    rows = [
        "<div>Standard files: %s/%s</div>" % (len(present), len(run["standard_files"])),
        '<div class="muted">Artifacts: %s, logs: %s</div>' % (_h(artifact_count), _h(log_count)),
    ]
    if not run["evidence_complete"]:
        rows.append('<div class="warn">Completion evidence incomplete</div>')
    if missing:
        rows.append('<div class="warn">Missing: %s</div>' % _h(", ".join(missing)))
    return "".join(rows)


def _excerpt_cell(excerpt: Dict) -> str:
    if not excerpt["present"]:
        return '<span class="muted">No final report yet</span>'
    text = excerpt["text"].strip()
    if not text:
        return '<span class="muted">Final report is empty</span>'
    return "<details><summary>View excerpt</summary><pre>%s</pre></details>" % _h(text)


def _render_run_details(details: List[Dict]) -> str:
    if not details:
        return ""
    rows = ["<section>", "<h2>Run Details</h2>"]
    for detail in details:
        rows.append(_render_one_run_detail(detail))
    rows.append("</section>")
    return "\n".join(rows)


def _render_one_run_detail(detail: Dict) -> str:
    run_id = detail.get("id", "unknown")
    lines = [
        '<article class="run-detail" id="%s">' % _h(_detail_anchor(run_id)),
        "<h3>%s</h3>" % _h(run_id),
    ]
    if detail.get("error"):
        lines.append('<p class="warn">%s</p>' % _h(detail["error"]))
    lines.append(
        '<p class="muted">Status %s · Workflow %s · Duration %s · Tokens/cost %s · Runner %s · Background %s · Launch %s</p>'
        % (
            _h(detail.get("status", "unknown")),
            _h(detail.get("workflow", "unknown")),
            _h(_format_duration(detail.get("duration_ms"))),
            _h(_usage_label(detail.get("usage") or {})),
            _h(_runner_label(detail.get("runner") or {})),
            _h(_background_label(detail.get("background") or {})),
            _h(_launch_label(detail.get("launch") or {})),
        )
    )
    lines.append(_render_detail_launch(detail.get("launch") or {}))
    lines.append(_render_detail_background(detail.get("background") or {}))
    lines.append(_render_detail_approvals(detail.get("approvals") or {}))
    lines.append(_render_detail_runtime(detail.get("runtime") or {}))
    lines.append(
        _render_detail_agent_native_tool_hooks(
            detail.get("agent_native_tool_hooks") or {}
        )
    )
    lines.append(_render_detail_diagnostics(detail.get("diagnostics") or {}))
    lines.append(_render_detail_steps(detail.get("steps") or []))
    lines.append(_render_detail_phases(detail.get("phases") or []))
    lines.append(_render_detail_hooks(detail.get("hook_events") or []))
    lines.append(_render_detail_session_traces(detail.get("session_trace_events") or [], detail.get("session_trace_summary") or {}))
    lines.append(_render_detail_agent_teams(detail.get("agent_teams") or []))
    lines.append(_render_detail_agent_maps(detail.get("agent_maps") or []))
    lines.append(_render_detail_file_table("Standard Artifacts", detail.get("standard_files") or []))
    lines.append(_render_detail_group("Artifacts", detail.get("artifacts") or _empty_detail_group()))
    lines.append(_render_detail_group("Logs", detail.get("logs") or _empty_detail_group()))
    lines.append("</article>")
    return "\n".join(lines)


def _render_detail_agent_native_tool_hooks(summary: Dict) -> str:
    if not isinstance(summary, dict) or summary.get("status") == "not-configured":
        return ""
    if summary.get("status") == "invalid":
        return (
            "<h4>Native Tool Gates</h4>"
            '<p class="warn">Gate summary is invalid.</p>'
        )
    return (
        "<h4>Native Tool Gates</h4>"
        '<p class="muted">Status %s · Gates %d (%d settled, %d prepared, %d failed) '
        "· Calls %d (%d passed, %d denied) · Warnings %d · Verifier bindings %d "
        "· Restricted-policy bindings %d</p>"
        % (
            _h(summary.get("status", "unknown")),
            summary.get("gate_count", 0),
            summary.get("settled_gate_count", 0),
            summary.get("prepared_gate_count", 0),
            summary.get("failed_gate_count", 0),
            summary.get("receipt_count", 0),
            summary.get("passed_count", 0),
            summary.get("denied_count", 0),
            summary.get("warning_count", 0),
            summary.get("verifier_binding_count", 0),
            summary.get("command_policy_gate_count", 0),
        )
    )


def _render_detail_diagnostics(diagnostics: Dict) -> str:
    if not isinstance(diagnostics, dict):
        return "<h4>Diagnostics</h4><p class=\"muted\">Diagnostics unavailable.</p>"
    issues = diagnostics.get("issues") if isinstance(diagnostics.get("issues"), list) else []
    rows = [
        "<h4>Diagnostics</h4>",
        '<p class="muted">%s</p>' % _h(diagnostics.get("summary") or "No current diagnostic issues."),
    ]
    if not issues:
        rows.append('<p class="muted">No current diagnostic issues.</p>')
        return "\n".join(rows)
    rows.extend(
        [
            '<table class="detail-table">',
            "<thead><tr><th>Severity</th><th>Code</th><th>Message</th><th>Recommendation</th><th>Command</th></tr></thead>",
            "<tbody>",
        ]
    )
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        message = issue.get("message") or ""
        if issue.get("detail"):
            message = "%s %s" % (message, issue.get("detail"))
        command = issue.get("command") or ""
        command_html = "<code>%s</code>" % _h(command) if command else '<span class="muted">none</span>'
        rows.append(
            "<tr><td>%s</td><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                _badge(issue.get("severity") or "unknown"),
                _h(issue.get("code") or "unknown"),
                _h(message),
                _h(issue.get("recommendation") or ""),
                command_html,
            )
        )
    rows.extend(["</tbody>", "</table>"])
    return "\n".join(rows)


def _render_detail_approvals(approvals: Dict) -> str:
    if not isinstance(approvals, dict) or not approvals.get("pending"):
        return ""
    pending = approvals.get("pending") if isinstance(approvals.get("pending"), list) else []
    rows = [
        "<h4>Approval Guidance</h4>",
        '<p class="muted">%s</p>' % _h(
            approvals.get("guidance")
            or "Manual gates require an explicit CLI resume with --approve; the live control token does not grant approval."
        ),
        '<table class="detail-table">',
        "<thead><tr><th>Step</th><th>Status</th><th>Approval</th><th>Resume guidance</th></tr></thead>",
        "<tbody>",
    ]
    for item in pending:
        if not isinstance(item, dict):
            continue
        command = item.get("command") or "python3 -m conductor_runtime run RUN_WORKFLOW --resume RUN_DIR --approve <approval-id>"
        rows.append(
            "<tr><td><code>%s</code></td><td>%s</td><td><code>%s</code></td><td><code>%s</code></td></tr>"
            % (
                _h(item.get("step_id") or "unknown"),
                _h(item.get("status") or "unknown"),
                _h(item.get("approval_id") or "<approval-id>"),
                _h(command),
            )
        )
    rows.extend(["</tbody>", "</table>"])
    return "\n".join(rows)


def _render_detail_runtime(runtime: Dict) -> str:
    if not isinstance(runtime, dict) or not runtime.get("available"):
        return ""
    rows = [
        "<h4>Runtime Actions</h4>",
        '<p class="muted">%s</p>' % _h(runtime.get("guidance") or "No pending runtime actions detected."),
        '<table class="detail-table">',
        "<thead><tr><th>Category</th><th>Step</th><th>Status</th><th>Waiting/deps</th><th>Detail</th><th>Command</th></tr></thead>",
        "<tbody>",
    ]
    groups = [
        ("Operator action", runtime.get("operator_actions")),
        ("Failed recovery", runtime.get("failed_recovery")),
        ("Ready", runtime.get("ready")),
        ("Waiting", runtime.get("waiting")),
        ("Active", runtime.get("active")),
    ]
    rendered = 0
    for label, items in groups:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            rendered += 1
            rows.append(
                "<tr><td>%s</td><td><code>%s</code><div class=\"muted\">%s / %s</div></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (
                    _h(label),
                    _h(item.get("step_id") or "unknown"),
                    _h(item.get("phase") or "default"),
                    _h(item.get("kind") or "unknown"),
                    _badge(item.get("status") or "unknown"),
                    _h(_runtime_waiting_label(item)),
                    _h(item.get("detail") or ""),
                    _runtime_command_html(item),
                )
            )
    if rendered == 0:
        rows.append('<tr><td colspan="6" class="muted">No pending runtime actions.</td></tr>')
    rows.extend(["</tbody>", "</table>"])
    return "\n".join(rows)


def _runtime_waiting_label(item: Dict) -> str:
    waiting = item.get("waiting_on") if isinstance(item.get("waiting_on"), list) else []
    if waiting:
        parts = []
        for dependency in waiting[:6]:
            if isinstance(dependency, dict):
                parts.append("%s:%s" % (_string_or_unknown(dependency.get("step_id")), _string_or_unknown(dependency.get("status"))))
        if len(waiting) > 6:
            parts.append("+%d more" % (len(waiting) - 6))
        return ", ".join(parts)
    depends_on = item.get("depends_on") if isinstance(item.get("depends_on"), list) else []
    if depends_on:
        return "depends on %s" % ", ".join(_string_or_unknown(dep) for dep in depends_on[:6])
    return ""


def _runtime_command_html(item: Dict) -> str:
    command = item.get("command") if isinstance(item.get("command"), str) else ""
    if not command:
        return '<span class="muted">none</span>'
    return "<code>%s</code>" % _h(command)


def _render_detail_background(background: Dict) -> str:
    if not isinstance(background, dict) or _background_label(background) == "unavailable":
        return ""
    rows = [
        "<h4>Background Worker</h4>",
        '<table class="detail-table">',
        "<thead><tr><th>Field</th><th>Value</th></tr></thead>",
        "<tbody>",
        "<tr><td>Status</td><td>%s</td></tr>" % _h(_background_label(background)),
    ]
    if background.get("created_at_utc"):
        rows.append("<tr><td>Created</td><td><code>%s</code></td></tr>" % _h(background["created_at_utc"]))
    if background.get("finished_at_utc"):
        rows.append("<tr><td>Finished</td><td><code>%s</code></td></tr>" % _h(background["finished_at_utc"]))
    rows.append(
        "<tr><td>Handoff</td><td>anonymous stdin pipe; payload persisted %s; daemon installed %s</td></tr>"
        % (_h(_bool_text(background.get("payload_persisted"))), _h(_bool_text(background.get("daemon_installed"))))
    )
    rows.append(
        "<tr><td>Approvals</td><td>%s supplied; values persisted %s</td></tr>"
        % (
            _h(background.get("approval_count") if background.get("approval_count") is not None else "unknown"),
            _h(_bool_text(background.get("approval_values_persisted"))),
        )
    )
    if background.get("exit_code") is not None:
        rows.append("<tr><td>Exit code</td><td>%s</td></tr>" % _h(background["exit_code"]))
    if background.get("error"):
        rows.append("<tr><td>Error</td><td class=\"warn\">%s</td></tr>" % _h(background["error"]))
    rows.extend(["</tbody>", "</table>"])
    return "\n".join(rows)


def _render_detail_launch(launch: Dict) -> str:
    if not isinstance(launch, dict) or _launch_label(launch) == "unavailable":
        return ""
    policy = launch.get("policy") if isinstance(launch.get("policy"), dict) else {}
    external = launch.get("external_supervisor") if isinstance(launch.get("external_supervisor"), dict) else {}
    rows = [
        "<h4>Launch</h4>",
        '<table class="detail-table">',
        "<thead><tr><th>Field</th><th>Value</th></tr></thead>",
        "<tbody>",
        "<tr><td>Status</td><td>%s</td></tr>" % _h(_launch_label(launch)),
        "<tr><td>Manifest</td><td><code>%s</code></td></tr>" % _h(launch.get("manifest") or ""),
    ]
    if launch.get("created_at_utc"):
        rows.append("<tr><td>Created</td><td><code>%s</code></td></tr>" % _h(launch.get("created_at_utc")))
    if launch.get("workflow"):
        rows.append("<tr><td>Workflow</td><td>%s</td></tr>" % _h(launch.get("workflow")))
    if launch.get("process_model"):
        rows.append("<tr><td>Process model</td><td>%s</td></tr>" % _h(launch.get("process_model")))
    if launch.get("steps_planned") is not None or launch.get("dry_run") is not None:
        rows.append(
            "<tr><td>Preflight</td><td>%s</td></tr>"
            % _h(
                "%s, dry-run %s, %s planned step(s)"
                % (
                    launch.get("preflight_status") or "unknown",
                    _bool_text(launch.get("dry_run")),
                    launch.get("steps_planned") if launch.get("steps_planned") is not None else "unknown",
                )
            )
        )
    if launch.get("max_workers") is not None:
        rows.append("<tr><td>Max workers</td><td>%s</td></tr>" % _h(launch.get("max_workers")))
    if policy:
        rows.append("<tr><td>Policy</td><td>%s</td></tr>" % _h(_launch_policy_label(policy)))
    if external:
        rows.append("<tr><td>Supervisor contract</td><td>%s</td></tr>" % _h(_launch_supervisor_label(external)))
    if launch.get("resume_command"):
        suffix = " (truncated)" if launch.get("resume_command_truncated") else ""
        rows.append(
            "<tr><td>Resume command%s</td><td><code>%s</code></td></tr>"
            % (_h(suffix), _h(launch.get("resume_command")))
        )
    if launch.get("resume_command_error"):
        rows.append("<tr><td>Resume command</td><td class=\"warn\">%s</td></tr>" % _h(launch.get("resume_command_error")))
    if launch.get("error"):
        rows.append("<tr><td>Error</td><td class=\"warn\">%s</td></tr>" % _h(launch.get("error")))
    rows.extend(["</tbody>", "</table>"])
    return "\n".join(rows)


def _launch_policy_label(policy: Dict) -> str:
    enabled = []
    for key, label in [
        ("allow_writes", "writes"),
        ("allow_destructive", "destructive"),
        ("allow_network", "network"),
        ("allow_agent", "agent"),
        ("allow_parallel", "parallel"),
    ]:
        if policy.get(key) is True:
            enabled.append(label)
    approval_count = policy.get("approval_count")
    if isinstance(approval_count, int) and not isinstance(approval_count, bool):
        enabled.append("%s approval(s)" % approval_count)
    persisted = policy.get("approval_values_persisted")
    if persisted is not None:
        enabled.append("approval values persisted: %s" % _bool_text(persisted))
    return ", ".join(enabled) if enabled else "default"


def _launch_supervisor_label(external: Dict) -> str:
    parts = []
    if external.get("must_not_assume_daemon") is True:
        parts.append("no daemon assumption")
    if external.get("requires_operator_or_supervisor_approval") is True:
        parts.append("operator/supervisor approval required")
    if external.get("approval_tokens_required_at_launch") is True:
        parts.append("approval tokens required at launch")
    if external.get("start_in_separate_session_recommended") is True:
        parts.append("separate session recommended")
    return ", ".join(parts) if parts else "unavailable"


def _render_detail_agent_teams(agent_teams: List[Dict]) -> str:
    if not agent_teams:
        return '<h4>Agent Teams</h4><p class="muted">No coordinated agent-team state recorded.</p>'
    sections = ["<h4>Agent Teams</h4>"]
    for team in agent_teams:
        status = _string_or_unknown(team.get("status"))
        error = _string_or_empty(team.get("error"))
        sections.append(
            '<section class="detail-group"><h5>%s</h5><p class="muted">%s</p>'
            % (
                _h(team.get("step_id")),
                _h(
                    "status %s, generation %s%s"
                    % (status, team.get("generation", 0), ", error %s" % error if error else "")
                ),
            )
        )
        if status not in {"invalid", "not-started"}:
            task_counts = team.get("task_counts") if isinstance(team.get("task_counts"), dict) else {}
            sections.append(
                "<p>Lead <code>%s</code> | round %s/%s | %s members | %s/%s tasks completed | "
                "%s turns | %s interrupted | %s messages | %s/%s authorized tokens</p>"
                % (
                    _h(team.get("lead")),
                    _h(team.get("round")),
                    _h(team.get("max_rounds")),
                    _h(team.get("member_count")),
                    _h(task_counts.get("completed")),
                    _h(sum(value for value in task_counts.values() if isinstance(value, int))),
                    _h(team.get("turn_count")),
                    _h(team.get("interruption_count", 0)),
                    _h(team.get("message_count")),
                    _h(team.get("authorized_tokens")),
                    _h(team.get("max_total_tokens")),
                )
            )
            operator = team.get("operator_inbox") if isinstance(team.get("operator_inbox"), dict) else {}
            operator_counts = operator.get("status_counts") if isinstance(operator.get("status_counts"), dict) else {}
            if team.get("max_operator_tasks") or operator:
                sections.append(
                    "<p>Operator follow-ups: %s/%s accepted | %s queued | %s superseded</p>"
                    % (
                        _h(team.get("operator_tasks_added", 0)),
                        _h(team.get("max_operator_tasks", 0)),
                        _h(operator_counts.get("pending", 0)),
                        _h(operator_counts.get("superseded", 0)),
                    )
                )
            if team.get("max_message_tasks"):
                sections.append(
                    "<p>Active teammate responses: %s/%s message task(s)</p>"
                    % (
                        _h(team.get("message_tasks_added", 0)),
                        _h(team.get("max_message_tasks", 0)),
                    )
                )
            operator_console = (
                team.get("operator_console")
                if isinstance(team.get("operator_console"), dict)
                else None
            )
            if operator_console is not None:
                transcripts = (
                    operator_console.get("transcripts")
                    if isinstance(operator_console.get("transcripts"), list)
                    else []
                )
                sections.append(
                    "<p>Private teammate transcripts: %s | limits %s events / %s bytes | "
                    "content omitted from this dashboard</p>"
                    % (
                        _h(operator_console.get("transcript_count", len(transcripts))),
                        _h(operator_console.get("max_events")),
                        _h(operator_console.get("max_bytes")),
                    )
                )
                if transcripts:
                    transcript_rows = []
                    for transcript in transcripts:
                        transcript_rows.append(
                            "<tr><td><code>%s</code></td><td><code>%s</code></td>"
                            "<td><code>%s</code></td><td>%s</td><td>%s</td><td><code>%s</code></td></tr>"
                            % (
                                _h(transcript.get("member_id")),
                                _h(transcript.get("task_id")),
                                _h(transcript.get("transcript_id")),
                                _h(transcript.get("status")),
                                _h(transcript.get("event_count")),
                                _h(transcript.get("file_sha256")),
                            )
                        )
                    sections.append(
                        "<table><thead><tr><th>Member</th><th>Task</th><th>Transcript</th>"
                        "<th>Status</th><th>Events</th><th>File SHA-256</th></tr></thead>"
                        "<tbody>%s</tbody></table>" % "".join(transcript_rows)
                    )
            plan_approval_count = team.get("plan_approval_count") or 0
            if plan_approval_count:
                sections.append(
                    "<p>Plan approvals: %s/%s approved | %s rejection(s) before approval | "
                    "in-flight provider replay after process loss: disabled</p>"
                    % (
                        _h(team.get("plan_approval_approved_count", 0)),
                        _h(plan_approval_count),
                        _h(team.get("plan_approval_rejection_count", 0)),
                    )
                )
                for approval in team.get("plan_approvals", [])[:16]:
                    sections.append(
                        "<p>Task <code>%s</code> | planner <code>%s</code> | lead <code>%s</code> | "
                        "status %s | revision %s/%s | plan/review output retained by hash only in this view</p>"
                        % (
                            _h(approval.get("task_id")),
                            _h(approval.get("member_id")),
                            _h(approval.get("lead_member_id")),
                            _h(approval.get("status")),
                            _h(approval.get("revision")),
                            _h((approval.get("max_revisions") or 0) + 1),
                        )
                    )
            quality_retry_count = team.get("quality_retry_history_count") or 0
            quality_retry_pending_count = team.get("quality_retry_pending_count") or 0
            if quality_retry_count or quality_retry_pending_count:
                quality_counts = (
                    team.get("quality_retry_status_counts")
                    if isinstance(team.get("quality_retry_status_counts"), dict)
                    else {}
                )
                quality_statuses = ", ".join(
                    "%s %s" % (count, status)
                    for status, count in sorted(quality_counts.items())
                ) or "no terminal outcomes"
                sections.append(
                    "<p>Automatic quality retries: %s (%s) | pending recovery: %s | "
                    "automatic provider replay after process loss: disabled%s</p>"
                    % (
                        _h(quality_retry_count),
                        _h(quality_statuses),
                        _h(quality_retry_pending_count),
                        " | history display truncated"
                        if team.get("quality_retry_history_truncated")
                        else "",
                    )
                )
                pending_quality = (
                    team.get("quality_retry_pending")
                    if isinstance(team.get("quality_retry_pending"), list)
                    else []
                )
                for checkpoint in pending_quality[:8]:
                    sections.append(
                        "<p><strong>Quality retry recovery pending</strong> | status %s | "
                        "member/retry task <code>%s/%s</code>%s | retry %s/%s | hook <code>%s</code></p>"
                        % (
                            _h(checkpoint.get("status")),
                            _h(checkpoint.get("member_id")),
                            _h(checkpoint.get("task_id")),
                            (
                                " | event task <code>%s</code>"
                                % _h(checkpoint.get("event_task_id"))
                                if checkpoint.get("event_task_id") != checkpoint.get("task_id")
                                else ""
                            ),
                            _h(checkpoint.get("retry_index")),
                            _h(checkpoint.get("max_retries")),
                            _h(checkpoint.get("hook_id")),
                        )
                    )
            merge = team.get("merge") if isinstance(team.get("merge"), dict) else None
            if merge is not None:
                sections.append(
                    "<p>Isolated write | <code>%s</code> | %s accepted merges | %s serialized conflicts | "
                    "%s applied files | %s deduplicated files</p>"
                    % (
                        _h(merge.get("strategy")),
                        _h(merge.get("merge_count")),
                        _h(merge.get("conflict_count")),
                        _h(merge.get("applied_file_count")),
                        _h(merge.get("deduplicated_file_count")),
                    )
                )
            pending = (
                team.get("recovery_pending")
                if isinstance(team.get("recovery_pending"), dict)
                else None
            )
            if pending is not None:
                if pending.get("intent_sha256"):
                    sections.append(
                        "<p><strong>Accepted turn recovery pending</strong> | intent <code>%s</code> | "
                        "member/task <code>%s/%s</code> | merge decision pending | provider replay not required</p>"
                        % (
                            _h(pending.get("intent_sha256")),
                            _h(pending.get("member_id")),
                            _h(pending.get("task_id")),
                        )
                    )
                else:
                    sections.append(
                        "<p><strong>Merge recovery pending</strong> | transaction <code>%s</code> | "
                        "member/task <code>%s/%s</code> | %s changed files | provider replay not required</p>"
                        % (
                            _h(pending.get("transaction_sha256")),
                            _h(pending.get("member_id")),
                            _h(pending.get("task_id")),
                            _h(pending.get("changed_file_count")),
                        )
                    )
            terminals = (
                team.get("turn_terminal_pending")
                if isinstance(team.get("turn_terminal_pending"), list)
                else []
            )
            for terminal in terminals:
                if not isinstance(terminal, dict):
                    continue
                sections.append(
                    "<p><strong>Local terminal turn recovery pending</strong> | terminal "
                    "<code>%s</code> | member/task <code>%s/%s</code> | quality retry %s | "
                    "provider replay not required</p>"
                    % (
                        _h(terminal.get("terminal_sha256")),
                        _h(terminal.get("member_id")),
                        _h(terminal.get("task_id")),
                        _h(terminal.get("quality_retry_index")),
                    )
                )
            completions = (
                team.get("turn_completion_pending")
                if isinstance(team.get("turn_completion_pending"), list)
                else []
            )
            for completion in completions:
                if not isinstance(completion, dict):
                    continue
                sections.append(
                    "<p><strong>Provider-completed turn recovery pending</strong> | completion "
                    "<code>%s</code> | member/task <code>%s/%s</code> | quality retry %s | "
                    "provider replay not required</p>"
                    % (
                        _h(completion.get("completion_sha256")),
                        _h(completion.get("member_id")),
                        _h(completion.get("task_id")),
                        _h(completion.get("quality_retry_index")),
                    )
                )
            if team.get("merge_recovery_count"):
                latest = (
                    team.get("latest_merge_recovery")
                    if isinstance(team.get("latest_merge_recovery"), dict)
                    else {}
                )
                sections.append(
                    "<p>%s exactly-once merge recoveries | latest source checkpoint <code>%s</code> | "
                    "provider replayed: no</p>"
                    % (
                        _h(team.get("merge_recovery_count")),
                        _h(latest.get("source_checkpoint")),
                    )
                )
            tasks = team.get("tasks") if isinstance(team.get("tasks"), list) else []
            if tasks:
                rows = []
                for task in tasks:
                    rows.append(
                        "<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td></tr>"
                        % (
                            _h(task.get("id")),
                            _h(task.get("status")),
                            _h(task.get("claimed_by") or task.get("assignee") or "unassigned"),
                            _h(task.get("attempts")),
                        )
                    )
                sections.append(
                    "<table><thead><tr><th>Task</th><th>Status</th><th>Owner</th><th>Attempts</th></tr></thead>"
                    "<tbody>%s</tbody></table>" % "".join(rows)
                )
        sections.append("</section>")
    return "".join(sections)


def _render_detail_agent_maps(agent_maps: List[Dict]) -> str:
    if not agent_maps:
        return '<h4>Agent Packets</h4><p class="muted">No agent_map packet details recorded.</p>'
    rows = ["<h4>Agent Packets</h4>"]
    for group in agent_maps:
        rows.append(
            "<h5><code>%s</code> %s</h5>"
            % (_h(group.get("step_id", "unknown")), _badge(group.get("status", "unknown")))
        )
        meta = [
            "phase %s" % (group.get("phase") or "default"),
            "capture %s" % (group.get("capture_dir") or ""),
            "%s calls" % (group.get("agent_invocations") or 0),
            "%s packet(s), %s cached" % (
                group.get("agent_packets_total") or len(group.get("packets") or []),
                group.get("agent_packets_cached") or 0,
            ),
            "%s item(s)" % (group.get("agent_items_total") or len(group.get("packets") or [])),
            "%s cached item(s)" % (group.get("agent_items_cached") or 0),
            "%s worker(s)" % (group.get("agent_workers") or 0),
            "%s cache entries" % (group.get("cache_entries") or 0),
            "%s trace entries" % (group.get("trace_entries") or 0),
            "%s terminal recovery pending" % (group.get("terminal_pending_count") or 0),
        ]
        rows.append('<p class="muted">%s</p>' % _h(" / ".join(meta)))
        if group.get("error"):
            rows.append('<p class="warn">%s</p>' % _h(group["error"]))
        if group.get("cache_error"):
            rows.append('<p class="warn">cache: %s</p>' % _h(group["cache_error"]))
        if group.get("trace_error"):
            rows.append('<p class="warn">trace: %s</p>' % _h(group["trace_error"]))
        if group.get("terminal_error"):
            rows.append('<p class="warn">terminal recovery: %s</p>' % _h(group["terminal_error"]))
        if group.get("terminal_pending_count"):
            rows.append(
                '<p class="warn">Local packet terminal recovery pending; provider replay is not required.</p>'
            )
        if group.get("truncated"):
            rows.append('<p class="warn">Packet listing truncated.</p>')
        rows.append(_render_detail_agent_packet_table(group.get("packets") or []))
    return "\n".join(rows)


def _render_detail_agent_packet_table(packets: List[Dict]) -> str:
    if not packets:
        return '<p class="muted">No packet rows available.</p>'
    rows = [
        '<table class="detail-table">',
        "<thead><tr><th>Packet</th><th>Status</th><th>Trace</th><th>Cache</th><th>Output</th><th>Stdout</th><th>Stderr</th></tr></thead>",
        "<tbody>",
    ]
    for packet in packets:
        rows.append(
            "<tr><td><code>%03d</code><div>%s</div></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                packet.get("index") or 0,
                _h(packet.get("item") or ""),
                _badge(packet.get("status") or "unknown"),
                _h(_agent_packet_trace_text(packet)),
                _h(_agent_packet_cache_text(packet)),
                _detail_excerpt((packet.get("output") or {}).get("excerpt") or {}),
                _detail_excerpt((packet.get("stdout") or {}).get("excerpt") or {}),
                _detail_excerpt((packet.get("stderr") or {}).get("excerpt") or {}),
            )
        )
    rows.extend(["</tbody>", "</table>"])
    return "\n".join(rows)


def _agent_packet_cache_text(packet: Dict) -> str:
    generation = packet.get("retry_generation")
    generation_text = " / packet retry %s" % generation if isinstance(generation, int) and generation > 0 else ""
    if not packet.get("cached"):
        return "not cached%s" % generation_text
    updated = packet.get("cache_updated_at_utc") or ""
    return "cached%s%s" % ((" %s" % updated) if updated else "", generation_text)


def _agent_packet_trace_text(packet: Dict) -> str:
    trace = packet.get("trace") if isinstance(packet.get("trace"), dict) else {}
    if not trace:
        return "not recorded"
    parts = [_string_or_unknown(trace.get("status"))]
    duration = trace.get("duration_ms")
    if duration is not None:
        parts.append(_format_duration(duration))
    if trace.get("cached"):
        parts.append("cache reuse")
    returncode = trace.get("returncode")
    if returncode is not None:
        parts.append("rc %s" % returncode)
    if trace.get("timed_out"):
        parts.append("timed out")
    stdout_bytes = trace.get("stdout_bytes")
    stderr_bytes = trace.get("stderr_bytes")
    if stdout_bytes is not None or stderr_bytes is not None:
        parts.append("%s/%s bytes out/err" % (stdout_bytes or 0, stderr_bytes or 0))
    if trace.get("detail") and trace.get("detail") not in {"completed", "reused cached output"}:
        parts.append(trace["detail"])
    return " / ".join(parts)


def _render_detail_phases(phases: List[Dict]) -> str:
    if not phases:
        return "<h4>Phases</h4><p class=\"muted\">No phase summary available.</p>"
    rows = [
        "<h4>Phases</h4>",
        '<table class="detail-table">',
        "<thead><tr><th>Phase</th><th>Status</th><th>Steps</th><th>Agents</th><th>Timing</th><th>Tokens/cost</th></tr></thead>",
        "<tbody>",
    ]
    for phase in phases:
        rows.append(
            "<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                _h(phase.get("id", "default")),
                _badge(phase.get("status", "unknown")),
                _h(_phase_steps_text(phase)),
                _h(_phase_agents_text(phase)),
                _h(_phase_timing_text(phase)),
                _h(_usage_label(phase.get("usage") or {})),
            )
        )
    rows.extend(["</tbody>", "</table>"])
    return "\n".join(rows)


def _render_detail_steps(steps: List[Dict]) -> str:
    if not steps:
        return '<p class="muted">No recorded steps.</p>'
    rows = [
        '<table class="detail-table">',
        "<thead><tr><th>Step</th><th>Status</th><th>Phase</th><th>Kind</th><th>Timing</th><th>Detail</th></tr></thead>",
        "<tbody>",
    ]
    for step in steps:
        rows.append(
            "<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                _h(step.get("id", "unknown")),
                _badge(step.get("status", "unknown")),
                _h(step.get("phase") or "default"),
                _h(step.get("kind") or ""),
                _h(_step_timing_text(step)),
                _h(_step_detail_with_codex_checkpoint(step)),
            )
        )
    rows.extend(["</tbody>", "</table>"])
    return "\n".join(rows)


def _step_detail_with_codex_checkpoint(step: Dict) -> str:
    detail = _string_or_empty(step.get("detail"))
    if step.get("partial_output_status") == "invalid":
        partial_text = "failed-turn output evidence invalid"
        detail = "%s | %s" % (detail, partial_text) if detail else partial_text
    elif step.get("partial_output_status") == "valid":
        partial_text = "failed-turn output preserved: %s bytes from %s, hash %s"
        partial_text %= (
            step.get("partial_output_bytes", "unknown"),
            _string_or_unknown(step.get("partial_output_source")),
            _string_or_unknown(step.get("partial_output_sha256"))[:12],
        )
        detail = "%s | %s" % (detail, partial_text) if detail else partial_text
    budget_text = _step_agent_budget_text(step)
    if budget_text:
        detail = "%s | %s" % (detail, budget_text) if detail else budget_text
    progress = step.get("codex_progress")
    if isinstance(progress, dict):
        if progress.get("status") == "invalid":
            progress_text = "Codex progress invalid (%s)" % _string_or_unknown(
                progress.get("error")
            )
        else:
            progress_text = (
                "Codex progress %s, %s events, %s/%s items, last %s"
                % (
                    _string_or_unknown(progress.get("status")),
                    _optional_int(progress.get("event_count")) or 0,
                    _optional_int(progress.get("item_completed_count")) or 0,
                    _optional_int(progress.get("item_started_count")) or 0,
                    _string_or_unknown(progress.get("last_event")),
                )
            )
            native_count = _optional_int(progress.get("native_agent_count")) or 0
            collab_count = _optional_int(progress.get("collab_tool_call_count")) or 0
            if progress.get("native_agents_enabled_by_runner") is True:
                progress_text += ", native enabled max %s depth %s, %s" % (
                    _optional_int(progress.get("native_agent_max_threads")) or "unknown",
                    _optional_int(progress.get("native_agent_max_depth")) or "unknown",
                    _native_usage_text(progress),
                )
            if native_count or collab_count:
                progress_text += (
                    ", %s native agents (%s active, %s errors), %s collaboration calls%s"
                    % (
                        native_count,
                        _optional_int(progress.get("native_agent_active_count")) or 0,
                        _optional_int(progress.get("native_agent_error_count")) or 0,
                        collab_count,
                        (
                            ""
                            if progress.get("native_agents_enabled_by_runner") is True
                            else ", nested usage unattributed"
                        ),
                    )
                )
                if progress.get("native_agent_observation_truncated") is True:
                    progress_text += ", native-agent observation truncated"
        detail = "%s | %s" % (detail, progress_text) if detail else progress_text
    packet_progress = step.get("codex_packet_progress")
    if isinstance(packet_progress, list) and packet_progress:
        invalid = [value for value in packet_progress if value.get("status") == "invalid"]
        if invalid:
            packet_text = "Codex packet progress invalid (%s)" % _string_or_unknown(
                invalid[0].get("error")
            )
        else:
            active = sum(value.get("status") == "active" for value in packet_progress)
            completed = sum(value.get("status") == "completed" for value in packet_progress)
            failed = len(packet_progress) - active - completed
            events = sum(_optional_int(value.get("event_count")) or 0 for value in packet_progress)
            packet_text = (
                "Codex packet progress %s active, %s completed, %s failed, %s events"
                % (active, completed, failed, events)
            )
            native_count = sum(
                _optional_int(value.get("native_agent_count")) or 0
                for value in packet_progress
            )
            collab_count = sum(
                _optional_int(value.get("collab_tool_call_count")) or 0
                for value in packet_progress
            )
            if native_count or collab_count:
                packet_text += (
                    ", %s native agents, %s collaboration calls, nested usage unattributed"
                    % (native_count, collab_count)
                )
                if any(
                    value.get("native_agent_observation_truncated") is True
                    for value in packet_progress
                ):
                    packet_text += ", native-agent observation truncated"
        detail = "%s | %s" % (detail, packet_text) if detail else packet_text
    terminal = step.get("codex_terminal")
    if isinstance(terminal, dict):
        if terminal.get("status") == "invalid":
            terminal_text = "Codex local terminal invalid (%s)" % _string_or_unknown(
                terminal.get("error")
            )
        else:
            terminal_text = "Codex local terminal recovery pending, provider replay not required"
        detail = "%s | %s" % (detail, terminal_text) if detail else terminal_text
    checkpoint = step.get("codex_checkpoint")
    if not isinstance(checkpoint, dict):
        return detail
    status = _string_or_unknown(checkpoint.get("status"))
    if status == "invalid":
        checkpoint_text = "Codex checkpoint invalid (%s)" % _string_or_unknown(checkpoint.get("error"))
    else:
        mode = _string_or_unknown(checkpoint.get("mode"))
        count = _optional_int(checkpoint.get("resume_count"))
        session_hash = _string_or_empty(checkpoint.get("session_id_sha256"))[:12]
        checkpoint_text = "Codex checkpoint %s, %s, %s resume(s), session %s" % (
            status,
            mode,
            count if count is not None else "unknown",
            session_hash or "unknown",
        )
    return "%s | %s" % (detail, checkpoint_text) if detail else checkpoint_text


def _attach_codex_partial_outputs(run_dir: Path, steps: List[Dict], workflow_steps) -> None:
    workflow_meta = _workflow_step_meta(workflow_steps)
    for step in steps:
        if step.get("partial_output_status") != "pending":
            continue
        try:
            step_id = step["id"]
            meta = workflow_meta.get(step_id, {})
            if step.get("kind") != "codex_exec" or step.get("status") != "failed":
                raise ValidationError("partial output requires a failed Codex step")
            source = step.get("partial_output_source")
            digest = step.get("partial_output_sha256")
            size = step.get("partial_output_bytes")
            if source not in {"agent-messages", "final-message-file"}:
                raise ValidationError("partial output source is invalid")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValidationError("partial output hash is invalid")
            if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_JSON_BYTES:
                raise ValidationError("partial output size is invalid")
            capture = meta.get("capture", "%s.md" % step_id)
            require_no_path_escape(capture)
            payload = read_regular_file_bytes_no_follow(
                run_dir / "artifacts" / capture,
                "Codex partial output",
                MAX_JSON_BYTES,
            )
            if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
                raise ValidationError("partial output artifact changed")
        except (FileNotFoundError, OSError, TypeError, ValidationError):
            step["partial_output_status"] = "invalid"
        else:
            step["partial_output_status"] = "valid"


def _attach_codex_completion_verdicts(
    run_dir: Path,
    steps: List[Dict],
    workflow_steps,
) -> None:
    workflow_meta = _workflow_step_meta(workflow_steps)
    for step in steps:
        if step.get("completion_verdict_status") not in {
            "satisfied",
            "unsatisfied",
            "invalid",
        }:
            continue
        try:
            step_id = step["id"]
            meta = workflow_meta.get(step_id, {})
            if (
                step.get("kind") != "codex_exec"
                or meta.get("completion_verdict") != COMPLETION_VERDICT_CONTRACT
            ):
                raise ValidationError("completion verdict requires a contracted Codex step")
            digest = step.get("completion_verdict_output_sha256")
            size = step.get("completion_verdict_output_bytes")
            if not digest or not isinstance(size, int) or isinstance(size, bool):
                raise ValidationError("completion verdict output receipt is invalid")
            capture = meta.get("capture", "%s.md" % step_id)
            require_no_path_escape(capture)
            record = load_model_verdict_record(
                run_dir / "artifacts" / capture,
                "completion verdict output",
            )
            if record["size"] != size or record["sha256"] != digest:
                raise ValidationError("completion verdict output changed")
            verdict = record["verdict"]
            if (
                verdict["satisfied"] is not step.get("completion_verdict_satisfied")
                or verdict["confidence"] != step.get("completion_verdict_confidence")
                or len(verdict["evidence"])
                != step.get("completion_verdict_evidence_count")
                or bool(verdict["feedback"].strip())
                is not step.get("completion_verdict_feedback_present")
            ):
                raise ValidationError("completion verdict state changed")
            expected_status = "satisfied" if verdict["satisfied"] else "unsatisfied"
            if step.get("completion_verdict_status") != expected_status:
                raise ValidationError("completion verdict status changed")
        except (
            FileNotFoundError,
            OSError,
            TypeError,
            ValidationError,
        ):
            step["completion_verdict_status"] = "invalid"


def _step_agent_budget_text(step: Dict) -> str:
    parts = []
    if step.get("agent_profile"):
        parts.append("profile %s" % _string_or_empty(step["agent_profile"]))
    if step.get("agent_profile_skill_count") is not None:
        skill_text = "%s profile Skill(s)" % step["agent_profile_skill_count"]
        if step.get("agent_profile_skill_set_sha256"):
            skill_text += " set %s" % _string_or_empty(
                step["agent_profile_skill_set_sha256"]
            )[:12]
        if step.get("agent_profile_skill_resource_count") is not None:
            skill_text += ", %s resource(s), %s bytes" % (
                step["agent_profile_skill_resource_count"],
                step.get("agent_profile_skill_resource_bytes", 0),
            )
        if step.get("agent_profile_skill_script_count") is not None:
            skill_text += ", %s script route(s)" % step[
                "agent_profile_skill_script_count"
            ]
        if step.get("agent_profile_skill_mcp_dependency_count") is not None:
            skill_text += ", %s MCP dependency(s), %s MCP tool(s)" % (
                step["agent_profile_skill_mcp_dependency_count"],
                step.get("agent_profile_skill_mcp_tool_count", 0),
            )
            skill_text += ", %s authenticated, %s credential env var(s), %s env header(s)" % (
                step.get("agent_profile_skill_mcp_auth_dependency_count", 0),
                step.get("agent_profile_skill_mcp_auth_env_var_count", 0),
                step.get("agent_profile_skill_mcp_auth_header_count", 0),
            )
            skill_text += ", %s HTTPS, %s stdio" % (
                step.get("agent_profile_skill_mcp_http_dependency_count", 0),
                step.get("agent_profile_skill_mcp_stdio_dependency_count", 0),
            )
            if step.get("agent_profile_skill_mcp_sha256"):
                skill_text += " MCP set %s" % _string_or_empty(
                    step["agent_profile_skill_mcp_sha256"]
                )[:12]
        parts.append(skill_text)
    if step.get("agent_tool_policy"):
        parts.append("tools %s" % _string_or_empty(step["agent_tool_policy"]))
    if step.get("agent_tool_output_token_limit") is not None:
        parts.append("tool output max %s tokens" % step["agent_tool_output_token_limit"])
    if step.get("agent_command_policy_sha256"):
        parts.append(
            "command policy %s"
            % _string_or_empty(step["agent_command_policy_sha256"])[:12]
        )
    if step.get("agent_restricted_hook_hash"):
        parts.append(
            "hook %s" % _string_or_empty(step["agent_restricted_hook_hash"])[:19]
        )
    if step.get("agent_restricted_codex_version"):
        parts.append(
            "CLI %s" % _string_or_empty(step["agent_restricted_codex_version"])
        )
    if step.get("agent_restricted_external_hooks_disabled") is not None:
        parts.append(
            "%s external hook(s) disabled"
            % step["agent_restricted_external_hooks_disabled"]
        )
    if step.get("agent_restricted_managed_hooks") is not None:
        parts.append("%s managed hook(s) retained" % step["agent_restricted_managed_hooks"])
    if step.get("agent_memory_mode"):
        memory_text = "memory %s" % _string_or_empty(step["agent_memory_mode"])
        if step.get("agent_memory_selection"):
            memory_text += "/%s" % _string_or_empty(step["agent_memory_selection"])
        if step.get("agent_memory_revision") is not None:
            memory_text += " revision %s" % step["agent_memory_revision"]
        if step.get("agent_memory_entry_count") is not None:
            memory_text += " (%s entries)" % step["agent_memory_entry_count"]
        parts.append(memory_text)
    if step.get("agent_memory_start_revision") is not None:
        parts.append("memory started at revision %s" % step["agent_memory_start_revision"])
    if step.get("agent_memory_snapshot_sha256"):
        parts.append("memory snapshot %s" % _string_or_empty(step["agent_memory_snapshot_sha256"])[:12])
    if step.get("agent_memory_query_sha256"):
        parts.append("memory query %s" % _string_or_empty(step["agent_memory_query_sha256"])[:12])
    if step.get("agent_memory_entry_sha256"):
        parts.append("memory entry %s" % _string_or_empty(step["agent_memory_entry_sha256"])[:12])
    if step.get("agent_team_members") is not None:
        parts.append(
            "team %s member(s), lead %s, %s/%s task(s), round %s/%s, %s message(s)"
            % (
                step["agent_team_members"],
                _string_or_unknown(step.get("agent_team_lead")),
                step.get("agent_team_tasks_completed", 0),
                step.get("agent_team_tasks", 0),
                step.get("agent_team_round", 0),
                step.get("agent_team_max_rounds", 0),
                step.get("agent_team_messages", 0),
            )
        )
    if step.get("agent_team_profile_skill_count") is not None:
        skill_text = "%s team profile Skill binding(s) across %s profile(s)" % (
            step["agent_team_profile_skill_count"],
            step.get("agent_team_profile_skill_profiles", 0),
        )
        if step.get("agent_team_profile_skill_sets_sha256"):
            skill_text += " set %s" % _string_or_empty(
                step["agent_team_profile_skill_sets_sha256"]
            )[:12]
        if step.get("agent_team_profile_skill_resource_count") is not None:
            skill_text += ", %s resource(s), %s bytes" % (
                step["agent_team_profile_skill_resource_count"],
                step.get("agent_team_profile_skill_resource_bytes", 0),
            )
        if step.get("agent_team_profile_skill_script_count") is not None:
            skill_text += ", %s script route(s)" % step[
                "agent_team_profile_skill_script_count"
            ]
        if step.get("agent_team_profile_skill_mcp_dependency_count") is not None:
            skill_text += ", %s MCP dependency(s), %s MCP tool(s)" % (
                step["agent_team_profile_skill_mcp_dependency_count"],
                step.get("agent_team_profile_skill_mcp_tool_count", 0),
            )
            skill_text += ", %s authenticated, %s credential env var(s), %s env header(s)" % (
                step.get("agent_team_profile_skill_mcp_auth_dependency_count", 0),
                step.get("agent_team_profile_skill_mcp_auth_env_var_count", 0),
                step.get("agent_team_profile_skill_mcp_auth_header_count", 0),
            )
            skill_text += ", %s HTTPS, %s stdio" % (
                step.get("agent_team_profile_skill_mcp_http_dependency_count", 0),
                step.get("agent_team_profile_skill_mcp_stdio_dependency_count", 0),
            )
        parts.append(skill_text)
    if step.get("agent_team_authorized_tokens") is not None:
        parts.append("%s team-authorized rollout tokens" % step["agent_team_authorized_tokens"])
    if step.get("agent_effort"):
        parts.append("effort %s" % _string_or_empty(step["agent_effort"]))
    if step.get("agent_max_tokens") is not None:
        parts.append("max %s weighted rollout tokens/call" % step["agent_max_tokens"])
    if step.get("agent_max_total_tokens") is not None:
        scope = "team" if step.get("kind") == "agent_team" else "map attempt"
        parts.append("max %s weighted rollout tokens/%s" % (step["agent_max_total_tokens"], scope))
    if step.get("agent_max_packets") is not None:
        parts.append("max %s packet(s)" % step["agent_max_packets"])
    if step.get("agent_budgeted_invocations") is not None:
        parts.append("%s budgeted call(s)" % step["agent_budgeted_invocations"])
    if step.get("agent_context_contract"):
        parts.append(
            "dependency evidence %s source(s)/%s artifact(s)/%s bytes, hash %s"
            % (
                step.get("agent_context_source_count", "unknown"),
                step.get("agent_context_artifact_count", "unknown"),
                step.get("agent_context_bytes", "unknown"),
                _string_or_empty(step.get("agent_context_sha256"))[:12] or "unknown",
            )
        )
    if step.get("completion_verdict_contract"):
        verdict_text = "completion verdict %s" % step.get(
            "completion_verdict_status", "unavailable"
        )
        if step.get("completion_verdict_confidence"):
            verdict_text += ", confidence %s" % step["completion_verdict_confidence"]
        if step.get("completion_verdict_evidence_count") is not None:
            verdict_text += ", %s evidence item(s)" % step[
                "completion_verdict_evidence_count"
            ]
        if step.get("completion_verdict_feedback_present") is True:
            verdict_text += ", actionable feedback retained privately"
        verdict_hash = _string_or_empty(
            step.get("completion_verdict_output_sha256")
        )[:12]
        if verdict_hash:
            verdict_text += ", hash %s" % verdict_hash
        parts.append(verdict_text)
    if step.get("native_agents_enabled") is True:
        usage_text = "nested usage %s" % (
            step.get("native_agent_usage_status") or "unavailable"
        )
        if step.get("native_agent_usage_attributed") is True:
            usage_text = "%s weighted/%s gross tokens across %s sessions/%s children" % (
                step.get("native_agent_usage_rollout_tokens", "unknown"),
                step.get("native_agent_usage_total_tokens", "unknown"),
                step.get("native_agent_usage_session_count", "unknown"),
                step.get("native_agent_usage_child_count", "unknown"),
            )
        parts.append(
            "native agents max %s children, depth %s, %s shared weighted rollout tokens, "
            "parent integration checkpoint %s and synthesis checkpoint %s remaining, %s"
            % (
                step.get("native_agent_max_threads", "unknown"),
                step.get("native_agent_max_depth", "unknown"),
                step.get("native_agent_max_total_tokens", "unknown"),
                step.get("native_agent_parent_integration_checkpoint_tokens", "unknown"),
                step.get("native_agent_parent_completion_checkpoint_tokens", "unknown"),
                usage_text,
            )
        )
    return "Agent budget: %s" % ", ".join(parts) if parts else ""


def _native_usage_text(progress: Dict) -> str:
    if progress.get("native_agent_usage_status") == "complete":
        return "%s weighted/%s gross tokens across %s sessions/%s children" % (
            _optional_int(progress.get("native_agent_usage_rollout_tokens")) or 0,
            _optional_int(progress.get("native_agent_usage_total_tokens")) or 0,
            _optional_int(progress.get("native_agent_usage_session_count")) or 0,
            _optional_int(progress.get("native_agent_usage_child_count")) or 0,
        )
    return "nested usage %s" % _string_or_unknown(
        progress.get("native_agent_usage_status")
    )


def _phase_steps_text(phase: Dict) -> str:
    counts = phase.get("step_counts") if isinstance(phase.get("step_counts"), dict) else {}
    summary = ", ".join("%s %s" % (count, status) for status, count in sorted(counts.items()))
    total = phase.get("steps_total") or 0
    recorded = phase.get("steps_recorded") or 0
    return "%s/%s recorded%s" % (recorded, total, (": %s" % summary) if summary else "")


def _phase_agents_text(phase: Dict) -> str:
    return "%s calls / %s packet(s), %s item(s), %s cached / max %s worker(s)" % (
        phase.get("agent_invocations") or 0,
        phase.get("agent_packets_total") or 0,
        phase.get("agent_items_total") or 0,
        phase.get("agent_items_cached") or 0,
        phase.get("agent_workers_max") or 0,
    )


def _phase_timing_text(phase: Dict) -> str:
    parts = []
    if phase.get("duration_ms") is not None:
        parts.append(_format_duration(phase.get("duration_ms")))
    if phase.get("started_at_utc"):
        parts.append("started %s" % phase["started_at_utc"])
    if phase.get("finished_at_utc"):
        parts.append("finished %s" % phase["finished_at_utc"])
    return " / ".join(parts) or "unknown"


def _render_detail_hooks(events: List[Dict]) -> str:
    if not events:
        return '<h4>Hooks</h4><p class="muted">No hook events recorded.</p>'
    rows = [
        "<h4>Hooks</h4>",
        '<table class="detail-table">',
        "<thead><tr><th>Hook</th><th>Event</th><th>Status</th><th>Timing</th><th>Detail</th></tr></thead>",
        "<tbody>",
    ]
    for event in events:
        rows.append(
            "<tr><td><code>%s</code>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                _h(event.get("id", "unknown")),
                _hook_step_text(event),
                _h(event.get("event", "unknown")),
                _badge(event.get("status", "unknown")),
                _h(_hook_timing_text(event)),
                _h(event.get("detail") or ""),
            )
        )
    rows.extend(["</tbody>", "</table>"])
    return "\n".join(rows)


def _render_detail_session_traces(events: List[Dict], summary: Dict = None) -> str:
    summary = summary if isinstance(summary, dict) else {}
    total = _optional_int(summary.get("count"))
    if total is None:
        total = len(events)
    if not events and not total:
        return '<h4>Session Traces</h4><p class="muted">No imported session trace events recorded.</p>'
    label = _session_trace_count_label(
        {
            "session_trace_count": total,
            "session_trace_counts": summary.get("by_provider") if isinstance(summary.get("by_provider"), dict) else {},
        }
    )
    rows = [
        "<h4>Session Traces</h4>",
        '<p class="muted">%s%s</p>'
        % (
            _h(label),
            _h("; showing latest %s" % len(events)) if total > len(events) else "",
        ),
    ]
    if not events:
        rows.append('<p class="muted">No displayable session trace rows.</p>')
        return "\n".join(rows)
    rows.extend([
        '<table class="detail-table">',
        "<thead><tr><th>Event</th><th>Status</th><th>Actor</th><th>Timing</th><th>Tokens/cost</th><th>Detail</th></tr></thead>",
        "<tbody>",
    ])
    for event in events:
        rows.append(
            "<tr><td><code>%s</code><div class=\"muted\">%s</div></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                _h(event.get("event") or "event"),
                _h(event.get("provider") or "generic"),
                _badge(event.get("status") or "unknown"),
                _h(_session_trace_actor_text(event)),
                _h(_session_trace_timing_text(event)),
                _h(_session_trace_usage_text(event)),
                _h(event.get("detail") or ""),
            )
        )
    rows.extend(["</tbody>", "</table>"])
    return "\n".join(rows)


def _session_trace_actor_text(event: Dict) -> str:
    parts = []
    for label, field in [("model", "model"), ("session", "session_id"), ("agent", "agent_id"), ("step", "step_id"), ("role", "role"), ("tool", "tool"), ("source", "source")]:
        value = event.get(field)
        if value:
            parts.append("%s %s" % (label, value))
    if event.get("session_id_sha256"):
        parts.append("session hash %s" % event["session_id_sha256"][:12])
    return " / ".join(parts) or "unknown"


def _session_trace_timing_text(event: Dict) -> str:
    parts = []
    duration = _format_duration(event.get("duration_ms"))
    if duration:
        parts.append(duration)
    if event.get("started_at_utc"):
        parts.append("started %s" % event["started_at_utc"])
    if event.get("finished_at_utc"):
        parts.append("finished %s" % event["finished_at_utc"])
    if event.get("recorded_at_utc"):
        parts.append("recorded %s" % event["recorded_at_utc"])
    if event.get("imported_at_utc"):
        parts.append("imported %s" % event["imported_at_utc"])
    return " / ".join(parts) or "unknown"


def _session_trace_usage_text(event: Dict) -> str:
    usage = {
        "status": "recorded",
        "input_tokens": event.get("input_tokens"),
        "output_tokens": event.get("output_tokens"),
        "total_tokens": event.get("total_tokens"),
        "cost_usd": event.get("cost_usd"),
    }
    if any(usage.get(field) is not None for field in ["input_tokens", "output_tokens", "total_tokens", "cost_usd"]):
        return _usage_label(usage)
    return "unavailable"


def _render_detail_group(title: str, group: Dict) -> str:
    prefix = ""
    if group.get("error"):
        prefix = '<p class="warn">%s: %s</p>' % (_h(title), _h(group["error"]))
    suffix = ""
    if group.get("truncated"):
        suffix = '<p class="warn">%s listing truncated.</p>' % _h(title)
    return prefix + _render_detail_file_table(title, group.get("files") or []) + suffix


def _render_detail_file_table(title: str, files: List[Dict]) -> str:
    if not files:
        return "<h4>%s</h4><p class=\"muted\">No files found.</p>" % _h(title)
    rows = [
        "<h4>%s</h4>" % _h(title),
        '<table class="detail-table">',
        "<thead><tr><th>File</th><th>Bytes</th><th>Updated</th><th>Excerpt</th></tr></thead>",
        "<tbody>",
    ]
    for item in files:
        rows.append(
            "<tr><td><code>%s</code>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                _h(item.get("name") or "unknown"),
                _file_error(item),
                _h(item.get("bytes", 0)),
                _h(item.get("mtime_utc") or ""),
                _detail_excerpt(item.get("excerpt") or {}),
            )
        )
    rows.extend(["</tbody>", "</table>"])
    return "\n".join(rows)


def _file_error(item: Dict) -> str:
    if not item.get("error"):
        return ""
    return '<div class="warn">%s</div>' % _h(item["error"])


def _detail_excerpt(excerpt: Dict) -> str:
    if excerpt.get("error"):
        return '<span class="muted">%s</span>' % _h(excerpt["error"])
    text = str(excerpt.get("text") or "").strip()
    if not text:
        return '<span class="muted">No excerpt</span>'
    return "<details><summary>View excerpt</summary><pre>%s</pre></details>" % _h(text)


def _step_timing_text(step: Dict) -> str:
    parts = []
    if step.get("duration_ms") is not None:
        parts.append(_format_duration(step.get("duration_ms")))
    if step.get("started_at_utc"):
        parts.append("started %s" % step["started_at_utc"])
    if step.get("finished_at_utc"):
        parts.append("finished %s" % step["finished_at_utc"])
    if step.get("agent_items_total"):
        parts.append(
            "%s agent item(s) in %s packet(s), %s cached"
            % (
                step.get("agent_items_total"),
                step.get("agent_packets_total") or step.get("agent_items_total"),
                step.get("agent_items_cached") or 0,
            )
        )
    return " / ".join(parts)


def _hook_timing_text(event: Dict) -> str:
    parts = []
    if event.get("duration_ms") is not None:
        parts.append(_format_duration(event.get("duration_ms")))
    if event.get("returncode") is not None:
        parts.append("exit %s" % event.get("returncode"))
    if event.get("timed_out"):
        parts.append("timed out")
    if event.get("on_failure"):
        parts.append("on failure: %s" % event["on_failure"])
    return " / ".join(parts)


def _hook_step_text(event: Dict) -> str:
    lines = []
    if event.get("step_id"):
        lines.append("step %s" % event["step_id"])
    if event.get("member_id") or event.get("task_id"):
        identity = "%s / %s" % (
            event.get("member_id") or "member",
            event.get("task_id") or "task",
        )
        position = []
        if event.get("generation") is not None:
            position.append("generation %s" % event["generation"])
        if event.get("round") is not None:
            position.append("round %s" % event["round"])
        if isinstance(event.get("quality_retry_index"), int) and event["quality_retry_index"] > 0:
            position.append("quality retry %s" % event["quality_retry_index"])
        if position:
            identity += " (%s)" % ", ".join(position)
        lines.append(identity)
    if event.get("hook_input_sha256"):
        lines.append("input %s" % event["hook_input_sha256"][:12])
    return "".join('<div class="muted">%s</div>' % _h(line) for line in lines)


def _detail_anchor(run_id: str) -> str:
    return "run-detail-%s" % run_handle(str(run_id))


def _report_result_cell(report: Dict) -> str:
    lines = ["<div>%s</div>" % _h(report["result"])]
    if report["failed_tasks"] is not None:
        severity = "ok" if report["failed_tasks"] == 0 else "failed"
        lines.append(_badge("%s failed" % report["failed_tasks"], severity=severity))
    if report["duration_ms"] is not None:
        lines.append('<div class="muted">%s ms</div>' % _h(report["duration_ms"]))
    provider = report.get("provider") if isinstance(report.get("provider"), dict) else {}
    if provider:
        for status in provider.get("statuses") or []:
            severity = "ok" if status == "success" else "failed"
            lines.append(_badge("provider %s" % status, severity=severity))
        if provider.get("main_models"):
            lines.append('<div class="muted">Main model: %s</div>' % _h(", ".join(provider["main_models"])))
        if provider.get("helper_models"):
            lines.append('<div class="muted">Helpers: %s</div>' % _h(", ".join(provider["helper_models"])))
        if provider.get("model_bindings"):
            lines.append('<div class="muted">Model binding: %s</div>' % _h(", ".join(provider["model_bindings"])))
        if provider.get("service_tiers"):
            lines.append('<div class="muted">Service tier: %s</div>' % _h(", ".join(provider["service_tiers"])))
        if provider.get("service_tier_bindings"):
            lines.append(
                '<div class="muted">Service-tier binding: %s</div>'
                % _h(", ".join(provider["service_tier_bindings"]))
            )
        if provider.get("execution_modes"):
            lines.append('<div class="muted">Execution mode: %s</div>' % _h(", ".join(provider["execution_modes"])))
        if provider.get("cost_usd") is not None:
            lines.append('<div class="muted">Cost: $%.6f</div>' % provider["cost_usd"])
        if provider.get("budget_overshoot_usd"):
            lines.append('<div class="warn">Cap overshoot: $%.6f</div>' % provider["budget_overshoot_usd"])
        if provider.get("partial_output_preserved"):
            lines.append('<div class="muted">Partial assistant output preserved.</div>')
        if provider.get("total_tokens") is not None:
            lines.append('<div class="muted">Gross terminal tokens: %s</div>' % _h(provider["total_tokens"]))
        if provider.get("token_accountings"):
            lines.append('<div class="muted">Token accounting: %s</div>' % _h(", ".join(provider["token_accountings"])))
        if provider.get("cache_creation_input_tokens") is not None:
            lines.append(
                '<div class="muted">Claude tokens: input %s + cache-write %s + cache-read %s + output %s</div>'
                % (
                    _h(provider.get("input_tokens")),
                    _h(provider.get("cache_creation_input_tokens")),
                    _h(provider.get("cache_read_input_tokens")),
                    _h(provider.get("output_tokens")),
                )
            )
        if provider.get("rollout_budget_tokens") is not None:
            lines.append(
                '<div class="muted">Rollout-budget tokens: %s</div>'
                % _h(provider["rollout_budget_tokens"])
            )
        if provider.get("rollout_budget_overshoot"):
            lines.append(
                '<div class="warn">Rollout cap overshoot: %s</div>'
                % _h(provider["rollout_budget_overshoot"])
            )
        if provider.get("token_overshoot"):
            lines.append(
                '<div class="warn">Legacy gross token cap overshoot: %s</div>'
                % _h(provider["token_overshoot"])
            )
        if provider.get("source_unchanged") is not None:
            lines.append('<div class="muted">Source unchanged: %s</div>' % _h(_bool_text(provider["source_unchanged"])))
        if provider.get("stage_unchanged") is not None:
            lines.append('<div class="muted">Read-only stage unchanged: %s</div>' % _h(_bool_text(provider["stage_unchanged"])))
        if provider.get("quality_score") is not None:
            lines.append(
                '<div class="muted">Quality score: %s/%s</div>'
                % (_h(provider["quality_score"]), _h(provider["quality_max_score"]))
            )
            lines.append(
                '<div class="muted">Independent reviewer: %s</div>'
                % _h(_bool_text(provider.get("quality_independent")))
            )
        for status in provider.get("staged_statuses") or []:
            severity = "ok" if status == "success" else "failed"
            lines.append(_badge("staged %s" % status, severity=severity))
        if provider.get("verification_statuses"):
            lines.append(
                '<div class="muted">Verifier: %s</div>'
                % _h(", ".join(provider["verification_statuses"]))
            )
        if provider.get("change_count") is not None:
            lines.append('<div class="muted">Staged changes: %s</div>' % _h(provider["change_count"]))
        if provider.get("patches_written"):
            lines.append('<div class="muted">Patches written: %s</div>' % _h(provider["patches_written"]))
    if report["error"]:
        lines.append('<div class="warn">%s</div>' % _h(report["error"]))
    return "".join(lines)


def _report_evidence_status(report: Dict) -> Dict:
    if report.get("error"):
        return {
            "evidence_status": "invalid-report",
            "evidence_label": "invalid report",
            "evidence_attention": True,
        }
    kind = report.get("kind")
    if kind == "benchmark":
        failed = report.get("failed_tasks")
        if isinstance(failed, int) and not isinstance(failed, bool) and failed > 0:
            return {
                "evidence_status": "benchmark-failed",
                "evidence_label": "benchmark failed",
                "evidence_attention": True,
            }
        return {
            "evidence_status": "benchmark-evidence",
            "evidence_label": "local benchmark evidence",
            "evidence_attention": False,
        }
    if kind == "comparison":
        failed = report.get("failed_tasks")
        if isinstance(failed, int) and not isinstance(failed, bool) and failed > 0:
            return {
                "evidence_status": "comparison-failed",
                "evidence_label": "comparison has failed tasks",
                "evidence_attention": True,
            }
        if report.get("claim_eligible") is True and report.get("provenance_complete") is True and report.get("all_live") is True:
            return {
                "evidence_status": "claim-eligible-report",
                "evidence_label": "report-declared claim eligible",
                "evidence_attention": False,
            }
        if report.get("claim_eligible") is True:
            return {
                "evidence_status": "claim-inconsistent",
                "evidence_label": "inconsistent claim provenance",
                "evidence_attention": True,
            }
        if report.get("claim_eligible") is False:
            return {
                "evidence_status": "claim-blocked",
                "evidence_label": "claim-blocked comparison",
                "evidence_attention": True,
            }
        return {
            "evidence_status": "claim-unknown",
            "evidence_label": "claim status unknown",
            "evidence_attention": True,
        }
    if kind == "runbook":
        return {
            "evidence_status": "planned-not-evidence",
            "evidence_label": "planned runbook, not evidence",
            "evidence_attention": False,
        }
    if kind == "run-manifest":
        if report.get("all_live") is True:
            return {
                "evidence_status": "live-provenance",
                "evidence_label": "live run provenance input",
                "evidence_attention": False,
            }
        return {
            "evidence_status": "planned-not-evidence",
            "evidence_label": "non-live provenance template",
            "evidence_attention": False,
        }
    if kind == "evidence-bundle":
        return {
            "evidence_status": "evidence-index",
            "evidence_label": "validated evidence index",
            "evidence_attention": False,
        }
    return {
        "evidence_status": "unknown-report",
        "evidence_label": "unknown report",
        "evidence_attention": True,
    }


def _report_evidence_cell(report: Dict) -> str:
    status = _string_or_unknown(report.get("evidence_status"))
    label = _string_or_unknown(report.get("evidence_label"))
    severity = "failed" if report.get("evidence_attention") else "ok"
    rows = [_badge(label, severity=severity)]
    if status == "claim-blocked":
        rows.append('<div class="muted">Not eligible for parity or superiority claims.</div>')
    elif status == "claim-eligible-report":
        rows.append('<div class="muted">Report declares complete live claim-eligible provenance.</div>')
    elif status == "benchmark-evidence":
        rows.append('<div class="muted">Single-system report; not a comparison claim.</div>')
    elif status == "planned-not-evidence":
        rows.append('<div class="muted">Operator instructions only; no live execution evidence.</div>')
    elif status == "live-provenance":
        rows.append('<div class="muted">Provenance input only; parity still requires a live comparison report.</div>')
    elif status == "evidence-index":
        rows.append('<div class="muted">Validated evidence index; claim status comes from indexed reports.</div>')
    elif status in {"comparison-failed", "invalid-report", "unknown-report", "claim-unknown", "claim-inconsistent"}:
        rows.append('<div class="muted">Review report provenance before use.</div>')
    return "".join(rows)


def _provenance_cell(report: Dict) -> str:
    if report["kind"] != "comparison":
        return '<span class="muted">Not applicable</span>'
    claim = report.get("claim_eligible")
    complete = report.get("provenance_complete")
    all_live = report.get("all_live")
    rows = [
        "<div>Claim eligible: %s</div>" % _bool_badge(claim),
        '<div class="muted">Complete: %s, live: %s</div>' % (_h(_bool_text(complete)), _h(_bool_text(all_live))),
    ]
    return "".join(rows)


def _warnings_cell(report: Dict) -> str:
    warnings = report.get("warnings") or []
    if not warnings:
        return '<span class="muted">None</span>'
    return "<ul>%s</ul>" % "".join("<li>%s</li>" % _h(item) for item in warnings)


def _metric(label: str, value: str) -> str:
    return '<div class="metric"><div class="metric-value">%s</div><div class="metric-label">%s</div></div>' % (
        _h(value),
        _h(label),
    )


def _badge(status: str, severity: Optional[str] = None) -> str:
    severity = severity or _status_severity(status)
    return '<span class="badge %s">%s</span>' % (_class_token(severity), _h(status))


def _bool_badge(value) -> str:
    if value is True:
        return _badge("true", severity="ok")
    if value is False:
        return _badge("false", severity="failed")
    return _badge("unknown", severity="unknown")


def _status_severity(status: str) -> str:
    normalized = str(status).lower()
    if normalized in {"completed", "passed", "planned", "0 failed"}:
        return "ok"
    if normalized in {"failed", "blocked", "invalid", "workflow_failed"} or normalized.endswith(" failed") and not normalized.startswith("0 "):
        return "failed"
    if normalized in {
        "running",
        "created",
        "pending",
        "needs_resume",
        "paused",
        "pause_requested",
        "stop_requested",
        "restart_requested",
        "max_iterations_exhausted",
        "goal_not_satisfied",
        "completion_not_satisfied",
    }:
        return "active"
    return "unknown"


def _run_has_completion_evidence(status: str, steps: List[Dict], standard_files: List[Dict], final_report: Dict) -> bool:
    if status != "completed":
        return False
    if not steps:
        return False
    if any(step["status"] not in {"completed", "skipped"} for step in steps):
        return False
    if any(not item["present"] for item in standard_files):
        return False
    return _final_report_has_substance(final_report)


def _run_needs_attention(run: Dict) -> bool:
    if run["state_error"] or run["workflow_error"]:
        return True
    if run["status"] in {"failed", "blocked", "running", "created", "unknown", "planned"}:
        return True
    return not run["evidence_complete"]


def _final_report_has_substance(final_report: Dict) -> bool:
    if not final_report.get("present"):
        return False
    text = str(final_report.get("text") or "").strip()
    if not text:
        return False
    lower = text.lower()
    pending_markers = [
        "summary:\n\npending",
        "changes:\n\npending",
        "verification performed:\n\npending",
        "verification not performed:\n\npending",
        "risks and caveats:\n\npending",
    ]
    if sum(1 for marker in pending_markers if marker in lower) >= 2:
        return False
    if lower in {"pending", "pending.", "# final report pending", "# final report\n\npending."}:
        return False
    return True


def _css() -> str:
    return """
:root {
  color-scheme: light;
  --bg: #f7f8fa;
  --panel: #ffffff;
  --text: #1f2933;
  --muted: #667085;
  --line: #d9dee7;
  --ok-bg: #dff3e8;
  --ok-text: #166534;
  --failed-bg: #fde8e8;
  --failed-text: #991b1b;
  --active-bg: #e0f2fe;
  --active-text: #075985;
  --unknown-bg: #ebeef3;
  --unknown-text: #475467;
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.45;
}
main {
  width: min(1440px, 100%);
  margin: 0 auto;
  padding: 24px;
}
header {
  margin-bottom: 20px;
}
h1 {
  margin: 0 0 6px;
  font-size: 30px;
  font-weight: 700;
}
h2 {
  margin: 28px 0 12px;
  font-size: 20px;
}
code {
  background: #eef1f5;
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 1px 4px;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px;
}
.metric {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px 14px;
}
.metric-value {
  font-size: 24px;
  font-weight: 700;
}
.metric-label, .muted {
  color: var(--muted);
  font-size: 13px;
}
table {
  width: 100%;
  border-collapse: collapse;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
th, td {
  border-bottom: 1px solid var(--line);
  padding: 10px 12px;
  text-align: left;
  vertical-align: top;
  overflow-wrap: anywhere;
}
th {
  background: #eef1f5;
  font-size: 12px;
  letter-spacing: 0;
  text-transform: uppercase;
  color: #344054;
}
tr:last-child td {
  border-bottom: 0;
}
.runs th:nth-child(1), .runs td:nth-child(1) {
  width: 19%;
}
.runs th:nth-child(2), .runs td:nth-child(2) {
  width: 10%;
}
.runs th:nth-child(3), .runs td:nth-child(3) {
  width: 16%;
}
.runs th:nth-child(4), .runs td:nth-child(4) {
  width: 24%;
}
.runs th:nth-child(5), .runs td:nth-child(5) {
  width: 16%;
}
.badge {
  display: inline-block;
  min-width: 64px;
  margin: 1px 4px 1px 0;
  padding: 2px 7px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  text-align: center;
  white-space: nowrap;
}
.badge.ok {
  background: var(--ok-bg);
  color: var(--ok-text);
}
.badge.failed {
  background: var(--failed-bg);
  color: var(--failed-text);
}
.badge.active {
  background: var(--active-bg);
  color: var(--active-text);
}
.badge.unknown {
  background: var(--unknown-bg);
  color: var(--unknown-text);
}
details summary {
  cursor: pointer;
}
pre {
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  background: #f3f5f8;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 10px;
}
ul {
  margin: 6px 0 0;
  padding-left: 18px;
}
.steps {
  padding-left: 0;
  list-style: none;
}
.warn {
  color: #b45309;
  font-size: 13px;
}
.empty {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
}
@media (max-width: 860px) {
  main {
    padding: 14px;
  }
  table, thead, tbody, tr, th, td {
    display: block;
  }
  thead {
    display: none;
  }
  tr {
    border-bottom: 1px solid var(--line);
  }
  td {
    border-bottom: 0;
  }
}
"""


def _validate_positive_int(value: int, name: str) -> None:
    if not isinstance(value, int) or value <= 0:
        raise ValidationError("%s must be a positive integer" % name)


def _default_goals_dir(runs_dir: Path) -> Path:
    return Path(runs_dir).parent / "goals"


def _safe_int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _optional_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _optional_signed_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _format_duration(value) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "unknown"
    if value < 1000:
        return "%d ms" % value
    seconds = value / 1000.0
    if seconds < 60:
        return "%.1f s" % seconds
    minutes = int(seconds // 60)
    remaining = int(seconds % 60)
    return "%dm %02ds" % (minutes, remaining)


def _usage_label(usage: Dict) -> str:
    if usage.get("total_tokens") is not None:
        label = "%s tokens" % usage["total_tokens"]
        if usage.get("cost_usd") is not None:
            label += ", $%.4f" % usage["cost_usd"]
        return label
    return usage.get("status") or "unavailable"


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _string_or_unknown(value) -> str:
    if isinstance(value, str) and value.strip():
        return value
    return "unknown"


def _string_or_empty(value) -> str:
    if isinstance(value, str):
        return value
    return ""


def _bool_text(value) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def _utc_from_timestamp(timestamp: float) -> str:
    return utc_from_timestamp(timestamp).isoformat(timespec="seconds") + "Z"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _class_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9_-]+", "-", str(value).lower()).strip("-")
    return token or "unknown"


def _h(value) -> str:
    return escape(redact_text(str(value)), quote=True)
