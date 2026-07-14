import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Dict, Optional

from conductor_runtime import __version__
from conductor_extras.runtime.artifacts import RunArtifacts
from conductor_extras.runtime.agent_map_packet_terminal import (
    agent_map_packet_terminal_summary,
    load_agent_map_packet_terminal,
)
from conductor_extras.runtime.codex_step_terminal import (
    codex_step_terminal_summary,
    load_codex_step_terminal,
)
from conductor_extras.runtime.codex_progress import codex_progress_summary, load_codex_progress
from conductor_extras.runtime.codex_hook_preflight import restricted_codex_version_range
from conductor_extras.runtime.desktop_notification import (
    RUN_NOTIFICATION_STATUSES,
    desktop_notification_status_for_execution,
    load_desktop_notification_receipt,
    send_artifact_desktop_notification,
    send_run_desktop_notification,
)
from conductor_extras.runtime.agent_profiles import (
    AGENT_PROFILE_SCHEMA,
    MAX_AGENT_PROFILE_INSTRUCTIONS_BYTES,
    agent_profile_prompt_summary,
    agent_profile_skill_resource_summary,
    agent_profile_summary,
    iter_agent_profile_files,
    load_agent_profile,
    validate_agent_profile,
    write_agent_profile,
)
from conductor_extras.runtime.agent_memory import (
    AGENT_MEMORY_SELECTIONS,
    AGENT_MEMORY_WRITE_APPROVAL,
    MAX_AGENT_MEMORY_ENTRY_BYTES,
    agent_memory_path,
    agent_memory_summary,
    append_agent_memory,
    list_agent_memories,
    load_agent_memory,
    load_agent_memory_file,
    remove_agent_memory_entry,
)
from conductor_extras.runtime.agent_team import (
    agent_team_interruption_summary,
    agent_team_state_summary,
    load_agent_team_interruption,
    load_agent_team_state,
)
from conductor_extras.runtime.agent_team_operator import (
    agent_team_operator_inbox_summary,
    load_agent_team_operator_inbox,
)
from conductor_extras.runtime.agent_team_chat import (
    agent_team_operator_chat_summary,
    load_agent_team_operator_chat,
)
from conductor_extras.runtime.agent_team_transcript import (
    agent_team_transcript_summary,
    load_agent_team_transcript,
)
from conductor_extras.runtime.agent_team_merge import (
    agent_team_merge_intent_summary,
    agent_team_merge_recovery_summary,
    agent_team_merge_summary,
    agent_team_merge_transaction_summary,
    load_agent_team_merge_intent,
    load_agent_team_merge_ledger,
    load_agent_team_merge_recovery,
    load_agent_team_merge_transaction,
)
from conductor_extras.runtime.agent_team_quality_retry import (
    agent_team_quality_retry_summary,
    load_agent_team_quality_retry,
)
from conductor_extras.runtime.agent_team_turn_completion import (
    agent_team_turn_completion_summary,
    load_agent_team_turn_completion,
)
from conductor_extras.runtime.agent_team_turn_terminal import (
    agent_team_turn_terminal_summary,
    load_agent_team_turn_terminal,
)
from conductor_extras.runtime.agent_team_plan_approval import (
    agent_team_plan_approval_summary,
    load_agent_team_plan,
    load_agent_team_plan_approval,
    load_agent_team_plan_review,
)
from conductor_extras.runtime.auto_orchestrator import (
    AUTO_REASONING_EFFORT,
    AUTO_REASONING_EFFORTS,
    AUTO_STRATEGIES,
    collect_auto_orchestrations,
    load_auto_orchestration_receipt,
    run_auto_orchestration,
    summarize_auto_orchestration,
)
from conductor_extras.runtime.auto_topology_campaign import (
    AUTO_TOPOLOGIES,
    auto_topology_campaign_status,
    build_auto_topology_campaign,
    load_auto_topology_campaign,
    write_auto_topology_campaign,
)
from conductor_extras.runtime.auto_topology_run import run_auto_topology_arm
from conductor_extras.runtime.readonly_parity_campaign import (
    READONLY_PARITY_SYSTEMS,
    build_readonly_parity_campaign,
    load_readonly_parity_campaign,
    readonly_parity_campaign_status,
    write_readonly_parity_campaign,
)
from conductor_extras.runtime.readonly_parity_run import run_readonly_parity_arm
from conductor_extras.runtime.benchmark import (
    BUDGET_CAP_ENFORCEMENT_LEVELS,
    DEFAULT_LIVE_PARITY_RUN_ID,
    build_live_parity_runbook,
    build_run_manifest_from_report,
    compare_benchmark_reports,
    load_benchmark_suite,
    load_benchmark_report,
    load_comparison_manifest,
    load_live_parity_runbook,
    load_parity_tasks,
    load_run_manifest,
    run_benchmark_suite,
    validate_benchmark_report,
    write_live_parity_runbook,
    write_run_manifest,
)
from conductor_extras.runtime.benchmark_quality import (
    load_benchmark_quality_evidence,
    load_benchmark_score_input,
    score_benchmark_report,
)
from conductor_extras.runtime.background_run import (
    load_background_run,
    runtime_self_command,
    run_background_worker,
    start_background_run,
    wait_for_background_run,
)
from conductor_extras.runtime.background_goal import (
    background_model_goal_liveness,
    latest_background_model_goal,
    load_background_model_goal,
    run_background_model_goal_worker,
    start_background_model_goal,
    terminate_background_model_goal,
    wait_for_background_model_goal,
)
from conductor_extras.runtime.background_supervisor import (
    background_routine_supervisor_liveness,
    background_routine_supervisor_routines_dir,
    latest_background_routine_supervisor,
    load_background_routine_supervisor,
    run_background_routine_supervisor_worker,
    start_background_routine_supervisor,
    terminate_background_routine_supervisor,
    wait_for_background_routine_supervisor,
)
from conductor_extras.runtime.cloud_tasks import apply_cloud_task, cloud_task_status, review_cloud_task, submit_cloud_task
from conductor_extras.runtime.claude_live import (
    CLAUDE_PAID_RUN_APPROVAL,
    load_claude_provider_evidence,
    run_claude_readonly_task,
)
from conductor_extras.runtime.claude_staged import (
    CLAUDE_STAGED_WRITE_APPROVAL,
    load_claude_staged_evidence,
    run_claude_staged_task,
)
from conductor_extras.runtime.codex_live import (
    CODEX_DEFAULT_SERVICE_TIER,
    CODEX_LIVE_RUN_APPROVAL,
    CODEX_REASONING_EFFORTS,
    CODEX_SERVICE_TIERS,
    DEFAULT_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
    load_codex_provider_evidence,
    run_codex_readonly_task,
)
from conductor_extras.runtime.codex_config import (
    CODEX_PROFILE_TOOL_POLICIES,
    DEFAULT_CODEX_NATIVE_AGENT_THREADS,
    MAX_CODEX_NATIVE_AGENT_THREADS,
    MIN_CODEX_NATIVE_AGENT_THREADS,
)
from conductor_extras.runtime.codex_staged import (
    CODEX_STAGED_WRITE_APPROVAL,
    load_codex_staged_evidence,
    run_codex_staged_task,
)
from conductor_extras.runtime.codex_staged_repair import (
    CODEX_STAGED_REPAIR_STATE_SCHEMA,
    load_codex_staged_repair_evidence,
    load_codex_staged_repair_state,
    run_codex_staged_repair,
)
from conductor_extras.runtime.staged_delivery import (
    verified_repair_stage_delivery as _verified_repair_stage_delivery,
    verified_stage_delivery as _verified_stage_delivery,
)
from conductor_extras.runtime.staged_apply import (
    STAGED_APPLY_APPROVAL,
    STAGED_APPLY_DELETE_APPROVAL,
    apply_verified_stage,
    default_staged_apply_receipt_path,
    load_staged_apply_receipt,
    preflight_new_verified_stage_apply,
)
from conductor_extras.runtime.dashboard import (
    DEFAULT_MAX_AUTO,
    DEFAULT_MAX_GOALS,
    DEFAULT_MAX_REPORTS,
    DEFAULT_MAX_RUNS,
    DEFAULT_MAX_TEXT_BYTES,
    collect_run_detail,
    collect_runs,
    run_handle,
    summarize_runner,
    write_dashboard,
)
from conductor_runtime.errors import ConductorError, ValidationError
from conductor_extras.runtime.evidence import (
    build_evidence_bundle,
    collect_evidence_report_records,
    load_evidence_bundle,
    require_claim_eligible,
    write_evidence_bundle,
)
from conductor_extras.runtime.goal_loop import (
    iter_goal_state_paths,
    load_check_prompt_file,
    load_goal_state,
    parse_check_command_json,
    run_goal_loop,
)
from conductor_extras.runtime.live_server import DEFAULT_LIVE_HOST, DEFAULT_LIVE_PORT, serve_live_dashboard
from conductor_extras.runtime.model_planner import (
    DEFAULT_MODEL_WORKFLOW_REASONING_EFFORT,
    MAX_MODEL_WORKFLOW_ITEMS,
    MAX_MODEL_WORKFLOW_STEPS,  # noqa: F401 - compatibility re-export
    MODEL_WORKFLOW_REASONING_EFFORTS,
    default_model_workflow_output,
    load_model_workflow_receipt,
    plan_model_workflow,
)
from conductor_extras.runtime.model_orchestrator import (
    default_model_workflow_run_receipt,
    load_model_workflow_run_receipt,
    run_model_workflow,
    run_reviewed_model_workflow,
)
from conductor_extras.runtime.model_goal_loop import run_model_goal_loop
from conductor_extras.runtime.agent_packets import BALANCED_AGENT_MAP_MAX_PACKETS
from conductor_extras.runtime.parity_campaign import (
    PARITY_CAMPAIGN_SYSTEMS,
    build_parity_campaign,
    load_parity_campaign,
    write_parity_campaign,
)
from conductor_extras.runtime.parity_campaign_status import campaign_status
from conductor_extras.runtime.parity_campaign_run import run_parity_campaign_trial
from conductor_extras.runtime.planner import MAX_PACKET_ITEMS, MAX_TASK_CHARS, PLAN_KINDS, PLAN_PROFILES, build_workflow_plan, write_workflow_plan
from conductor_extras.runtime.paths import (
    default_agent_memory_dir,
    default_benchmarks_dir,
    default_cloud_dir,
    default_dashboard_path,
    default_generated_dir,
    default_goals_dir,
    default_live_parity_report_dir,
    default_live_parity_workflow_dir,
    default_routines_dir,
    default_runs_dir,
)
from conductor_extras.runtime.prepared_run import prepare_run
from conductor_runtime.redaction import redact_json_value, redact_terminal_text, redact_text
from conductor_extras.runtime.runner import DEFAULT_OUTPUT_LIMIT_BYTES, WorkflowRunner
from conductor_extras.runtime.run_console import (
    RUN_CONSOLE_MAX_REFRESH_MS,
    RUN_CONSOLE_MAX_TIMEOUT_SECONDS,
    RUN_CONSOLE_MAX_WIDTH,
    RUN_CONSOLE_MIN_REFRESH_MS,
    RUN_CONSOLE_MIN_WIDTH,
    build_run_console_snapshot,
    render_run_console,
    run_console_is_terminal,
)
from conductor_extras.runtime.run_control import (
    list_team_inbox,
    list_team_plans,
    list_team_questions,
    list_team_transcripts,
    queue_team_task,
    read_team_console_snapshot,
    read_team_question,
    read_team_plan,
    read_team_transcript,
    recover_run,
    record_session_trace,
    record_session_trace_with_usage,
    pause_run,
    record_usage,
    reset_step,
    retry_packet,
    restart_run,
    resume_run,
    retry_step,
    reply_team_question,
    review_team_plan,
    save_run_workflow,
    skip_step,
    stop_run,
    terminate_run,
)
from conductor_extras.runtime.team_console import (
    TEAM_CONSOLE_DEFAULT_MESSAGE_CHARS,
    TEAM_CONSOLE_LAYOUTS,
    TEAM_CONSOLE_MAX_DISPLAY_EVENTS,
    TEAM_CONSOLE_MAX_HISTORY,
    TEAM_CONSOLE_MAX_MESSAGE_CHARS,
    TEAM_CONSOLE_MAX_WIDTH,
    TEAM_CONSOLE_MIN_MESSAGE_CHARS,
    TEAM_CONSOLE_MIN_WIDTH,
    TEAM_CONSOLE_REFRESH_MAX_MS,
    TEAM_CONSOLE_REFRESH_MIN_MS,
    TEAM_CONSOLE_TIMEOUT_MAX_SECONDS,
    render_team_console,
    team_console_is_terminal,
    validate_team_console_snapshot,
)
from conductor_extras.runtime.routines import (
    build_routine_manifest,
    build_schedule,
    iter_routine_manifest_paths,
    load_routine_manifest,
    routine_list_record,
    write_routine_manifest,
)
from conductor_extras.runtime.routine_supervisor import (
    execute_routine_manifest,
    load_routine_controls,
    load_routine_controls_file,
    load_supervisor_state,
    load_supervisor_state_file,
    routine_control_record,
    routine_status,
    set_routine_paused,
    supervise_routines,
    update_routine_schedule,
)
from conductor_extras.runtime.routine_service import (
    ROUTINE_SERVICE_INSTALL_APPROVAL,
    ROUTINE_SERVICE_UNINSTALL_APPROVAL,  # noqa: F401 - compatibility re-export
    ROUTINE_SERVICE_UPDATE_APPROVAL,
    build_routine_service_grant,
    install_routine_service,
    load_routine_service_grant,
    load_routine_service_state,
    run_routine_service_worker,
    status_routine_service,
    uninstall_routine_service,
)
from conductor_extras.runtime.saved_workflows import (
    apply_saved_workflow_args,
    iter_saved_workflow_entries,
    load_saved_workflow,
    parse_saved_workflow_args,
    resolve_saved_workflow,
    validate_saved_workflow_command_name,
    validate_saved_workflow_script_destination,
    write_saved_workflow_json,
    write_saved_workflow_script,
)
from conductor_extras.runtime.schemas import SCHEMA_VERSION as SCHEMA_EXPORT_SCHEMA, schema_json, schema_types
from conductor_extras.runtime.session_trace_import import aggregate_session_trace_usage, load_session_trace_import
from conductor_extras.runtime.security import (
    RuntimePolicy,
    read_regular_file_bytes_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    validate_approval_tokens,
    write_new_text_file_no_follow,
    write_text_file_no_follow,
)
from conductor_extras.runtime.usage_import import load_usage_import
from conductor_extras.runtime.workflow import (
    MAX_WORKFLOW_JSON_BYTES,
    iter_workflow_files,
    load_workflow,
    validate_workflow,
    workflow_summary,
)
from conductor_extras.runtime.staged_workspace import require_path_outside_workspace
from conductor_extras.runtime.system_doctor import (
    CODEX_HOST_REPAIR_APPROVAL,
    inspect_codex_installation,
    repair_codex_host_link,
)


MAX_STATUS_JSON_BYTES = 2 * 1024 * 1024
STAGED_EXIT_CONTRACTS = ("strict", "verified-stage")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="conductor-runtime", description="Run Codex Conductor workflows.")
    parser.add_argument("--version", action="version", version="conductor-runtime %s" % __version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate workflow JSON files.")
    validate.add_argument("paths", nargs="+", type=Path)

    validate_profile = subparsers.add_parser(
        "validate-agent-profile",
        help="Validate reusable policy-bounded agent profile JSON files.",
    )
    validate_profile.add_argument("paths", nargs="+", type=Path)

    inspect_profile = subparsers.add_parser(
        "inspect-agent-profile",
        help="Print a redacted summary of one reusable agent profile.",
    )
    inspect_profile.add_argument("profile", type=Path)
    inspect_profile.add_argument(
        "--workspace",
        type=Path,
        help="Bind and summarize the selected Skill resource trees under this workspace.",
    )
    inspect_profile.add_argument("--json", action="store_true")

    write_profile = subparsers.add_parser(
        "write-agent-profile",
        help="Write a strict reusable agent profile without launching a provider.",
    )
    write_profile.add_argument("output", type=Path)
    write_profile.add_argument("--name", required=True)
    write_profile.add_argument("--description", default="")
    profile_instructions = write_profile.add_mutually_exclusive_group(required=True)
    profile_instructions.add_argument("--instructions")
    profile_instructions.add_argument("--instructions-file", type=Path)
    write_profile.add_argument("--model")
    write_profile.add_argument("--effort", choices=sorted(CODEX_REASONING_EFFORTS))
    write_profile.add_argument("--max-tokens", type=int)
    write_profile.add_argument("--sandbox", choices=["read-only", "workspace-write"], default="read-only")
    write_profile.add_argument(
        "--tool-policy",
        choices=sorted(CODEX_PROFILE_TOOL_POLICIES),
        default="standard",
    )
    write_profile.add_argument("--tool-output-token-limit", type=int)
    write_profile.add_argument(
        "--allow-command-json",
        action="append",
        default=[],
        help="Allow one exact argv JSON array; repeat for multiple rules.",
    )
    write_profile.add_argument(
        "--allow-command-prefix-json",
        action="append",
        default=[],
        help="Allow one argv-prefix JSON array plus trailing args; repeat for multiple rules.",
    )
    write_profile.add_argument("--allow-apply-patch", action="store_true")
    write_profile.add_argument("--risk", choices=["low", "medium", "high"], default="medium")
    write_profile.add_argument("--max-workers", type=int)
    write_profile.add_argument("--timeout-seconds", type=int)
    write_profile.add_argument("--context-file", action="append", default=[])
    write_profile.add_argument(
        "--skill",
        action="append",
        default=[],
        help=(
            "Select one workspace-relative Skill directory; SKILL.md is preloaded and its bounded "
            "resource tree is available on demand. Repeat as needed."
        ),
    )
    write_profile.add_argument(
        "--allow-skill-script-json",
        action="append",
        default=[],
        help=(
            "Authorize one selected Skill script under restricted policy as JSON with skill, script, "
            "and args or args_prefix; repeat for multiple routes."
        ),
    )
    write_profile.add_argument(
        "--allow-skill-mcp-json",
        action="append",
        default=[],
        help=(
            "Authorize one parsed selected-Skill HTTPS MCP dependency as JSON with skill, name, url, "
            "and exact tools; repeat for multiple dependencies."
        ),
    )
    write_profile.add_argument("--memory-mode", choices=["read-only", "read-write"])
    write_profile.add_argument("--memory-selection", choices=sorted(AGENT_MEMORY_SELECTIONS))
    write_profile.add_argument("--memory-max-entries", type=int)
    write_profile.add_argument("--memory-max-bytes", type=int)

    bind_profile = subparsers.add_parser(
        "bind-agent-profile",
        help="Bind one profile into selected Codex workflow steps without launching a provider.",
    )
    bind_profile.add_argument("workflow", type=Path)
    bind_profile.add_argument("profile", type=Path)
    bind_profile.add_argument("output", type=Path)
    bind_profile.add_argument("--step", action="append", required=True)

    validate_memory = subparsers.add_parser(
        "validate-agent-memory",
        help="Validate strict revisioned agent memory store JSON files.",
    )
    validate_memory.add_argument("paths", nargs="+", type=Path)

    validate_team_state = subparsers.add_parser(
        "validate-agent-team-state",
        help="Validate strict resumable agent team state JSON files.",
    )
    validate_team_state.add_argument("paths", nargs="+", type=Path)

    inspect_team_state = subparsers.add_parser(
        "inspect-agent-team-state",
        help="Inspect bounded agent team progress without message bodies or session ids.",
    )
    inspect_team_state.add_argument("path", type=Path)
    inspect_team_state.add_argument("--json", action="store_true")

    validate_team_operator = subparsers.add_parser(
        "validate-agent-team-operator-inbox",
        help="Validate strict operator-to-teammate task inbox files.",
    )
    validate_team_operator.add_argument("paths", nargs="+", type=Path)

    inspect_team_operator = subparsers.add_parser(
        "inspect-agent-team-operator-inbox",
        help="Inspect operator task delivery status without instruction text.",
    )
    inspect_team_operator.add_argument("path", type=Path)
    inspect_team_operator.add_argument("--json", action="store_true")

    validate_team_chat = subparsers.add_parser(
        "validate-agent-team-operator-chat",
        help="Validate strict teammate-to-operator question and reply files.",
    )
    validate_team_chat.add_argument("paths", nargs="+", type=Path)

    inspect_team_chat = subparsers.add_parser(
        "inspect-agent-team-operator-chat",
        help="Inspect operator-chat routing and lifecycle without question or reply text.",
    )
    inspect_team_chat.add_argument("path", type=Path)
    inspect_team_chat.add_argument("--json", action="store_true")

    validate_team_transcript = subparsers.add_parser(
        "validate-agent-team-transcript",
        help="Validate strict private redacted teammate transcript files.",
    )
    validate_team_transcript.add_argument("paths", nargs="+", type=Path)

    inspect_team_transcript = subparsers.add_parser(
        "inspect-agent-team-transcript",
        help="Inspect teammate transcript identity, bounds, and hashes without message text.",
    )
    inspect_team_transcript.add_argument("path", type=Path)
    inspect_team_transcript.add_argument("--json", action="store_true")

    validate_team_interruption = subparsers.add_parser(
        "validate-agent-team-interruption",
        help="Validate strict hash-only teammate interruption artifacts.",
    )
    validate_team_interruption.add_argument("paths", nargs="+", type=Path)

    inspect_team_interruption = subparsers.add_parser(
        "inspect-agent-team-interruption",
        help="Inspect one teammate interruption without instruction or session text.",
    )
    inspect_team_interruption.add_argument("path", type=Path)
    inspect_team_interruption.add_argument("--json", action="store_true")

    validate_team_plan = subparsers.add_parser(
        "validate-agent-team-plan",
        help="Validate strict read-only teammate plan outputs.",
    )
    validate_team_plan.add_argument("paths", nargs="+", type=Path)

    validate_team_plan_review = subparsers.add_parser(
        "validate-agent-team-plan-review",
        help="Validate strict configured lead or operator plan-review outputs.",
    )
    validate_team_plan_review.add_argument("paths", nargs="+", type=Path)

    validate_team_plan_approval = subparsers.add_parser(
        "validate-agent-team-plan-approval",
        help="Validate strict teammate plan-approval lifecycle state.",
    )
    validate_team_plan_approval.add_argument("paths", nargs="+", type=Path)

    inspect_team_plan_approval = subparsers.add_parser(
        "inspect-agent-team-plan-approval",
        help="Inspect plan approval without plan, feedback, or session text.",
    )
    inspect_team_plan_approval.add_argument("path", type=Path)
    inspect_team_plan_approval.add_argument("--json", action="store_true")

    queue_team = subparsers.add_parser(
        "queue-team-task",
        help="Queue a bounded named-teammate follow-up, optionally interrupting its current turn.",
    )
    queue_team.add_argument("run_dir", type=Path)
    queue_team.add_argument("step_id")
    queue_team.add_argument("--member", required=True)
    queue_team_instruction = queue_team.add_mutually_exclusive_group(required=True)
    queue_team_instruction.add_argument("--instruction")
    queue_team_instruction.add_argument("--instruction-file", type=Path)
    queue_team.add_argument(
        "--interrupt-current",
        action="store_true",
        help="Stop the named member's active provider turn and deliver this redirect next.",
    )

    list_team = subparsers.add_parser(
        "list-team-inbox",
        help="List queued and accepted operator teammate tasks without instruction text.",
    )
    list_team.add_argument("run_dir", type=Path)
    list_team.add_argument("step_id")
    list_team.add_argument("--json", action="store_true")

    list_questions = subparsers.add_parser(
        "list-team-questions",
        help="List teammate questions for the operator without their text.",
    )
    list_questions.add_argument("run_dir", type=Path)
    list_questions.add_argument("step_id")
    list_questions.add_argument("--json", action="store_true")

    read_question = subparsers.add_parser(
        "read-team-question",
        help="Explicitly read one private teammate question and any operator reply.",
    )
    read_question.add_argument("run_dir", type=Path)
    read_question.add_argument("step_id")
    read_question.add_argument("question_id")
    read_question.add_argument("--json", action="store_true")

    reply_question = subparsers.add_parser(
        "reply-team-question",
        help="Answer one pending teammate question and wake that teammate in its existing session.",
    )
    reply_question.add_argument("run_dir", type=Path)
    reply_question.add_argument("step_id")
    reply_question.add_argument("question_id")
    reply_value = reply_question.add_mutually_exclusive_group(required=True)
    reply_value.add_argument("--reply")
    reply_value.add_argument("--reply-file", type=Path)

    list_plans = subparsers.add_parser(
        "list-team-plans",
        help="List teammate plan approval status and hashes without plan or feedback text.",
    )
    list_plans.add_argument("run_dir", type=Path)
    list_plans.add_argument("step_id")
    list_plans.add_argument("--json", action="store_true")

    read_plan = subparsers.add_parser(
        "read-team-plan",
        help="Explicitly read one private teammate plan and its latest review.",
    )
    read_plan.add_argument("run_dir", type=Path)
    read_plan.add_argument("step_id")
    read_plan.add_argument("task_id")
    read_plan.add_argument("--json", action="store_true")

    list_transcripts = subparsers.add_parser(
        "list-team-transcripts",
        help="List private teammate transcripts without assistant message text.",
    )
    list_transcripts.add_argument("run_dir", type=Path)
    list_transcripts.add_argument("step_id")
    list_transcripts.add_argument("--json", action="store_true")

    read_transcript = subparsers.add_parser(
        "read-team-transcript",
        help="Explicitly read or follow one private redacted teammate transcript.",
    )
    read_transcript.add_argument("run_dir", type=Path)
    read_transcript.add_argument("step_id")
    read_transcript.add_argument("transcript_id")
    transcript_mode = read_transcript.add_mutually_exclusive_group()
    transcript_mode.add_argument("--json", action="store_true")
    transcript_mode.add_argument("--follow", action="store_true")
    read_transcript.add_argument("--timeout-seconds", type=int, default=900)

    team_console = subparsers.add_parser(
        "team-console",
        help="Explicitly display or follow private redacted transcripts for selected teammates.",
    )
    team_console.add_argument("run_dir", type=Path)
    team_console.add_argument("step_id")
    team_console.add_argument("--member", action="append", dest="members")
    console_mode = team_console.add_mutually_exclusive_group()
    console_mode.add_argument("--json", action="store_true")
    console_mode.add_argument("--follow", action="store_true")
    team_console.add_argument(
        "--layout",
        choices=sorted(TEAM_CONSOLE_LAYOUTS),
        default="auto",
    )
    team_console.add_argument("--width", type=int)
    team_console.add_argument("--history", type=int, default=1)
    team_console.add_argument("--max-events", type=int, default=8)
    team_console.add_argument(
        "--max-message-chars",
        type=int,
        default=TEAM_CONSOLE_DEFAULT_MESSAGE_CHARS,
    )
    team_console.add_argument("--refresh-ms", type=int, default=250)
    team_console.add_argument("--timeout-seconds", type=int, default=900)
    team_console.add_argument("--no-clear", action="store_true")

    review_plan = subparsers.add_parser(
        "review-team-plan",
        help="Approve or reject one operator-reviewed teammate plan.",
    )
    review_plan.add_argument("run_dir", type=Path)
    review_plan.add_argument("step_id")
    review_plan.add_argument("task_id")
    review_plan.add_argument("--decision", choices=["approve", "reject"], required=True)
    review_feedback = review_plan.add_mutually_exclusive_group(required=True)
    review_feedback.add_argument("--feedback")
    review_feedback.add_argument("--feedback-file", type=Path)

    validate_team_merge = subparsers.add_parser(
        "validate-agent-team-merge-ledger",
        help="Validate strict deterministic agent-team merge ledgers.",
    )
    validate_team_merge.add_argument("paths", nargs="+", type=Path)

    inspect_team_merge = subparsers.add_parser(
        "inspect-agent-team-merge-ledger",
        help="Inspect deterministic team merge counts and fingerprints without file contents.",
    )
    inspect_team_merge.add_argument("path", type=Path)
    inspect_team_merge.add_argument("--json", action="store_true")

    validate_map_terminal = subparsers.add_parser(
        "validate-agent-map-packet-terminal",
        help="Validate a strict locally observed terminal agent-map packet receipt.",
    )
    validate_map_terminal.add_argument("paths", nargs="+", type=Path)

    inspect_map_terminal = subparsers.add_parser(
        "inspect-agent-map-packet-terminal",
        help="Inspect packet recovery identity without private output or session data.",
    )
    inspect_map_terminal.add_argument("path", type=Path)
    inspect_map_terminal.add_argument("--json", action="store_true")

    validate_codex_terminal = subparsers.add_parser(
        "validate-codex-step-terminal",
        help="Validate a strict locally observed terminal Codex step receipt.",
    )
    validate_codex_terminal.add_argument("paths", nargs="+", type=Path)

    inspect_codex_terminal = subparsers.add_parser(
        "inspect-codex-step-terminal",
        help="Inspect Codex step recovery identity without private output or session data.",
    )
    inspect_codex_terminal.add_argument("path", type=Path)
    inspect_codex_terminal.add_argument("--json", action="store_true")

    validate_codex_progress = subparsers.add_parser(
        "validate-codex-progress",
        help="Validate strict content-free Codex progress evidence.",
    )
    validate_codex_progress.add_argument("paths", nargs="+", type=Path)

    inspect_codex_progress = subparsers.add_parser(
        "inspect-codex-progress",
        help="Inspect bounded Codex event and item counters without provider content.",
    )
    inspect_codex_progress.add_argument("path", type=Path)
    inspect_codex_progress.add_argument("--json", action="store_true")

    validate_team_completion = subparsers.add_parser(
        "validate-agent-team-turn-completion",
        help="Validate a strict provider-completed teammate turn checkpoint.",
    )
    validate_team_completion.add_argument("paths", nargs="+", type=Path)

    inspect_team_completion = subparsers.add_parser(
        "inspect-agent-team-turn-completion",
        help="Inspect completed-turn recovery identity without private output or session data.",
    )
    inspect_team_completion.add_argument("path", type=Path)
    inspect_team_completion.add_argument("--json", action="store_true")

    validate_team_terminal = subparsers.add_parser(
        "validate-agent-team-turn-terminal",
        help="Validate a strict locally observed terminal teammate turn receipt.",
    )
    validate_team_terminal.add_argument("paths", nargs="+", type=Path)

    inspect_team_terminal = subparsers.add_parser(
        "inspect-agent-team-turn-terminal",
        help="Inspect terminal-turn recovery identity without private output or session data.",
    )
    inspect_team_terminal.add_argument("path", type=Path)
    inspect_team_terminal.add_argument("--json", action="store_true")

    validate_team_intent = subparsers.add_parser(
        "validate-agent-team-merge-intent",
        help="Validate a strict accepted-turn checkpoint written before merge planning.",
    )
    validate_team_intent.add_argument("paths", nargs="+", type=Path)

    inspect_team_intent = subparsers.add_parser(
        "inspect-agent-team-merge-intent",
        help="Inspect accepted-turn recovery identity without private state or file contents.",
    )
    inspect_team_intent.add_argument("path", type=Path)
    inspect_team_intent.add_argument("--json", action="store_true")

    validate_team_transaction = subparsers.add_parser(
        "validate-agent-team-merge-transaction",
        help="Validate a strict pending write-team merge transaction.",
    )
    validate_team_transaction.add_argument("paths", nargs="+", type=Path)

    inspect_team_transaction = subparsers.add_parser(
        "inspect-agent-team-merge-transaction",
        help="Inspect pending merge recovery identity without source contents.",
    )
    inspect_team_transaction.add_argument("path", type=Path)
    inspect_team_transaction.add_argument("--json", action="store_true")

    validate_team_recovery = subparsers.add_parser(
        "validate-agent-team-merge-recovery",
        help="Validate strict exactly-once write-team recovery receipts.",
    )
    validate_team_recovery.add_argument("paths", nargs="+", type=Path)

    inspect_team_recovery = subparsers.add_parser(
        "inspect-agent-team-merge-recovery",
        help="Inspect merge recovery checkpoints and no-replay evidence.",
    )
    inspect_team_recovery.add_argument("path", type=Path)
    inspect_team_recovery.add_argument("--json", action="store_true")

    validate_team_quality_retry = subparsers.add_parser(
        "validate-agent-team-quality-retry",
        help="Validate strict bounded agent-team quality retry checkpoints.",
    )
    validate_team_quality_retry.add_argument("paths", nargs="+", type=Path)

    inspect_team_quality_retry = subparsers.add_parser(
        "inspect-agent-team-quality-retry",
        help="Inspect retry identity, bounds, hashes, and no-replay status without feedback text.",
    )
    inspect_team_quality_retry.add_argument("path", type=Path)
    inspect_team_quality_retry.add_argument("--json", action="store_true")

    list_memory = subparsers.add_parser(
        "list-agent-memory",
        help="List workspace-scoped reusable agent memory stores.",
    )
    list_memory.add_argument("--workspace", type=Path, default=Path("."))
    list_memory.add_argument("--memory-dir", type=Path)
    list_memory.add_argument("--json", action="store_true")

    inspect_memory = subparsers.add_parser(
        "inspect-agent-memory",
        help="Inspect one workspace-scoped agent memory store without printing entry content.",
    )
    inspect_memory.add_argument("profile")
    inspect_memory.add_argument("--workspace", type=Path, default=Path("."))
    inspect_memory.add_argument("--memory-dir", type=Path)
    inspect_memory.add_argument("--json", action="store_true")

    remember_memory = subparsers.add_parser(
        "remember-agent-memory",
        help="Append one redacted entry to external workspace-scoped agent memory.",
    )
    remember_memory.add_argument("profile")
    remember_memory.add_argument("--workspace", type=Path, default=Path("."))
    remember_memory.add_argument("--memory-dir", type=Path)
    memory_content = remember_memory.add_mutually_exclusive_group(required=True)
    memory_content.add_argument("--content")
    memory_content.add_argument("--content-file", type=Path)
    remember_memory.add_argument("--entry-id")
    remember_memory.add_argument("--tag", action="append", default=[])
    remember_memory.add_argument("--expected-revision", type=int)

    forget_memory = subparsers.add_parser(
        "forget-agent-memory",
        help="Remove one explicitly named entry from external workspace-scoped agent memory.",
    )
    forget_memory.add_argument("profile")
    forget_memory.add_argument("entry_id")
    forget_memory.add_argument("--workspace", type=Path, default=Path("."))
    forget_memory.add_argument("--memory-dir", type=Path)
    forget_memory.add_argument("--expected-revision", type=int)

    validate_saved = subparsers.add_parser("validate-saved-workflows", help="Validate safe saved workflow scripts.")
    validate_saved.add_argument("saved_workflows", nargs="*", help="Saved workflow command names or workspace-relative paths.")
    validate_saved.add_argument("--workspace", type=Path, default=Path("."))
    validate_saved.add_argument(
        "--keep-going",
        action="store_true",
        help="Report every invalid saved workflow instead of stopping at the first error.",
    )
    _add_saved_workflow_scope_args(validate_saved)

    validate_model_receipt = subparsers.add_parser(
        "validate-model-workflow-receipt",
        help="Validate model-authored workflow draft receipts.",
    )
    validate_model_receipt.add_argument("paths", nargs="+", type=Path)

    validate_model_run_receipt = subparsers.add_parser(
        "validate-model-workflow-run-receipt",
        help="Validate foreground or detached model workflow plan-and-run receipts.",
    )
    validate_model_run_receipt.add_argument("paths", nargs="+", type=Path)

    validate_auto_receipt = subparsers.add_parser(
        "validate-auto-receipt",
        help="Validate deterministic auto-orchestration routing receipts.",
    )
    validate_auto_receipt.add_argument("paths", nargs="+", type=Path)

    list_auto = subparsers.add_parser(
        "list-auto",
        help="List recent automatic orchestration receipts with resolved child lifecycle status.",
    )
    list_auto.add_argument("--workspace", type=Path, default=Path("."))
    list_auto.add_argument("--generated-dir", type=Path)
    list_auto.add_argument("--auto-dir", type=Path)
    list_auto.add_argument("--runs-dir", type=Path)
    list_auto.add_argument("--goals-dir", type=Path)
    list_auto.add_argument("--max-auto", type=int, default=50)
    list_auto.add_argument("--json", action="store_true")

    status_auto = subparsers.add_parser(
        "status-auto",
        help="Inspect one automatic orchestration receipt and resolve its child lifecycle artifact.",
    )
    status_auto.add_argument("receipt", type=Path)
    status_auto.add_argument("--workspace", type=Path, default=Path("."))
    status_auto.add_argument("--generated-dir", type=Path)
    status_auto.add_argument("--runs-dir", type=Path)
    status_auto.add_argument("--goals-dir", type=Path)
    status_auto.add_argument("--child-artifact", type=Path)
    status_auto.add_argument("--json", action="store_true")

    validate_background = subparsers.add_parser(
        "validate-background-run",
        help="Validate detached background-run metadata artifacts.",
    )
    validate_background.add_argument("paths", nargs="+", type=Path)

    validate_desktop_notification = subparsers.add_parser(
        "validate-desktop-notification",
        help="Validate content-free desktop-notification delivery receipts.",
    )
    validate_desktop_notification.add_argument("paths", nargs="+", type=Path)

    validate_background_goal = subparsers.add_parser(
        "validate-background-model-goal",
        help="Validate detached adaptive model-goal worker metadata.",
    )
    validate_background_goal.add_argument("paths", nargs="+", type=Path)

    validate_background_supervisor = subparsers.add_parser(
        "validate-background-routine-supervisor",
        help="Validate detached routine-supervisor lifecycle metadata.",
    )
    validate_background_supervisor.add_argument("paths", nargs="+", type=Path)

    inspect = subparsers.add_parser("inspect", help="Print a workflow summary.")
    inspect.add_argument("workflow", type=Path)

    run = subparsers.add_parser("run", help="Run or dry-run a workflow.")
    run.add_argument("workflow", type=Path)
    run.add_argument("--workspace", type=Path, default=Path("."))
    run.add_argument("--runs-dir", type=Path)
    run.add_argument("--run-id")
    run.add_argument("--resume", type=Path)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument(
        "--print-result",
        action="store_true",
        help="Print the terminal result value after a completed result-contract workflow.",
    )
    run.add_argument(
        "--desktop-notify",
        action="store_true",
        help="Send one content-free desktop notification when this execution finishes.",
    )
    run.add_argument("--allow-writes", action="store_true")
    run.add_argument("--allow-destructive", action="store_true")
    run.add_argument("--allow-network", action="store_true")
    run.add_argument("--allow-agent", action="store_true")
    run.add_argument("--allow-parallel", action="store_true")
    run.add_argument("--approve", action="append", default=[])
    run.add_argument("--max-workers", type=int)

    goal = subparsers.add_parser(
        "run-goal",
        help="Run a workflow in a bounded foreground loop until its configured verifier chain passes.",
    )
    goal.add_argument("workflow", type=Path)
    goal.add_argument("--workspace", type=Path, default=Path("."))
    goal.add_argument("--runs-dir", type=Path)
    goal.add_argument("--goals-dir", type=Path)
    goal.add_argument("--goal-id")
    goal.add_argument("--resume-goal", type=Path, help="Resume an existing goal artifact with remaining iterations.")
    goal.add_argument(
        "--desktop-notify",
        action="store_true",
        help="Send one content-free desktop notification when this goal attempt finishes.",
    )
    goal.add_argument(
        "--check-command-json",
        help='Verifier argv as JSON; combine with a model criterion to run command then model.',
    )
    goal_model_verifier = goal.add_mutually_exclusive_group()
    goal_model_verifier.add_argument(
        "--check-prompt",
        help="Strict completion criteria for a read-only Codex model verifier; the prompt is not persisted.",
    )
    goal_model_verifier.add_argument(
        "--check-prompt-file",
        type=Path,
        help="Workspace-relative file containing strict completion criteria for a read-only Codex model verifier.",
    )
    goal.add_argument("--check-model", help="Optional Codex model override for model verification.")
    goal.add_argument(
        "--workflow-completion",
        action="store_true",
        help=(
            "Use one authoritative terminal strict completion verdict already in the workflow; "
            "cannot be combined with an external verifier."
        ),
    )
    goal.add_argument("--max-iterations", type=int, default=3)
    goal.add_argument("--check-timeout-seconds", type=int, default=120)
    goal.add_argument("--output-limit-bytes", type=int, default=DEFAULT_OUTPUT_LIMIT_BYTES)
    goal.add_argument("--allow-writes", action="store_true")
    goal.add_argument("--allow-destructive", action="store_true")
    goal.add_argument("--allow-network", action="store_true")
    goal.add_argument("--allow-agent", action="store_true")
    goal.add_argument("--allow-parallel", action="store_true")
    goal.add_argument("--approve", action="append", default=[])
    goal.add_argument("--max-workers", type=int)

    status_goal = subparsers.add_parser("status-goal", help="Print a goal-loop artifact state.")
    status_goal.add_argument("goal_json", type=Path)

    list_goals = subparsers.add_parser("list-goals", help="List local goal-loop artifacts.")
    list_goals.add_argument("--goals-dir", type=Path)
    list_goals.add_argument("--max-goals", type=int, default=50)
    list_goals.add_argument("--json", action="store_true", help="Emit a redacted JSON envelope.")

    prepare = subparsers.add_parser("prepare-run", help="Preflight a workflow into a resumable run and launch manifest.")
    prepare.add_argument("workflow", type=Path)
    prepare.add_argument("--workspace", type=Path, default=Path("."))
    prepare.add_argument("--runs-dir", type=Path)
    prepare.add_argument("--run-id")
    prepare.add_argument("--allow-writes", action="store_true")
    prepare.add_argument("--allow-destructive", action="store_true")
    prepare.add_argument("--allow-network", action="store_true")
    prepare.add_argument("--allow-agent", action="store_true")
    prepare.add_argument("--allow-parallel", action="store_true")
    prepare.add_argument("--approve", action="append", default=[])
    prepare.add_argument("--max-workers", type=int)

    background = subparsers.add_parser(
        "start-background-run",
        help="Preflight and start one explicit detached workflow worker.",
    )
    background.add_argument("workflow", type=Path)
    background.add_argument("--workspace", type=Path, default=Path("."))
    background.add_argument("--runs-dir", type=Path)
    background.add_argument("--run-id")
    background.add_argument(
        "--desktop-notify",
        action="store_true",
        help="Have the detached worker send one content-free desktop notification when it finishes.",
    )
    background.add_argument("--allow-writes", action="store_true")
    background.add_argument("--allow-destructive", action="store_true")
    background.add_argument("--allow-network", action="store_true")
    background.add_argument("--allow-agent", action="store_true")
    background.add_argument("--allow-parallel", action="store_true")
    background.add_argument("--approve", action="append", default=[])
    background.add_argument("--max-workers", type=int)

    background_status = subparsers.add_parser(
        "status-background-run",
        help="Print validated detached worker, runner, and run-state status.",
    )
    background_status.add_argument("run_dir", type=Path)

    background_wait = subparsers.add_parser(
        "wait-background-run",
        help="Wait for a detached worker to finish and print its final metadata.",
    )
    background_wait.add_argument("run_dir", type=Path)
    background_wait.add_argument("--timeout-seconds", type=int, default=3600)
    background_wait.add_argument("--poll-interval-ms", type=int, default=250)

    status = subparsers.add_parser("status", help="Print run state.")
    status.add_argument("run_dir", type=Path)
    status.add_argument("--with-runner", action="store_true", help="Wrap state with runner heartbeat/liveness metadata.")

    list_cmd = subparsers.add_parser("list", help="List workflow templates.")
    list_cmd.add_argument("paths", nargs="*", type=Path, default=[Path("conductor-workflows")])

    list_runs = subparsers.add_parser("list-runs", help="List recent workflow runs with runner liveness.")
    list_runs.add_argument("--runs-dir", type=Path)
    list_runs.add_argument("--max-runs", type=int, default=DEFAULT_MAX_RUNS)
    list_runs.add_argument("--max-text-bytes", type=int, default=DEFAULT_MAX_TEXT_BYTES)
    list_runs.add_argument("--status", action="append", default=[], help="Only include runs with this status.")
    list_runs.add_argument("--json", action="store_true", help="Emit a redacted JSON envelope.")

    diagnose = subparsers.add_parser("diagnose-run", help="Diagnose a run and suggest audited recovery actions.")
    diagnose.add_argument("run_dir", type=Path)
    diagnose.add_argument("--max-text-bytes", type=int, default=DEFAULT_MAX_TEXT_BYTES)
    diagnose.add_argument("--json", action="store_true", help="Emit a redacted JSON envelope.")

    run_console = subparsers.add_parser(
        "run-console",
        help="Display or follow content-free workflow and Codex progress.",
    )
    run_console.add_argument("run_dir", type=Path)
    run_console_mode = run_console.add_mutually_exclusive_group()
    run_console_mode.add_argument("--json", action="store_true")
    run_console_mode.add_argument("--follow", action="store_true")
    run_console.add_argument("--width", type=int)
    run_console.add_argument("--refresh-ms", type=int, default=500)
    run_console.add_argument("--timeout-seconds", type=int, default=3600)
    run_console.add_argument("--no-clear", action="store_true")

    saved_list = subparsers.add_parser("list-saved-workflows", help="List safe saved workflow scripts.")
    saved_list.add_argument("--workspace", type=Path, default=Path("."))
    _add_saved_workflow_scope_args(saved_list)

    saved_inspect = subparsers.add_parser("inspect-saved-workflow", help="Inspect a safe saved workflow command.")
    saved_inspect.add_argument("saved_workflow")
    saved_inspect.add_argument("--workspace", type=Path, default=Path("."))
    _add_saved_workflow_scope_args(saved_inspect)
    _add_saved_workflow_args(saved_inspect)

    saved_export = subparsers.add_parser("export-saved-workflow", help="Export a safe saved workflow command to JSON.")
    saved_export.add_argument("saved_workflow")
    saved_export.add_argument("destination", type=Path)
    saved_export.add_argument("--workspace", type=Path, default=Path("."))
    _add_saved_workflow_scope_args(saved_export)
    _add_saved_workflow_args(saved_export)

    saved_run = subparsers.add_parser("run-saved-workflow", help="Run or dry-run a safe saved workflow command.")
    saved_run.add_argument("saved_workflow")
    saved_run.add_argument("--workspace", type=Path, default=Path("."))
    saved_run.add_argument("--runs-dir", type=Path)
    saved_run.add_argument("--run-id")
    saved_run.add_argument("--resume", type=Path)
    saved_run.add_argument("--dry-run", action="store_true")
    saved_run.add_argument(
        "--print-result",
        action="store_true",
        help="Print the consolidated saved-workflow result after completion.",
    )
    saved_run.add_argument(
        "--desktop-notify",
        action="store_true",
        help="Send one content-free desktop notification when this execution finishes.",
    )
    saved_run.add_argument("--allow-writes", action="store_true")
    saved_run.add_argument("--allow-destructive", action="store_true")
    saved_run.add_argument("--allow-network", action="store_true")
    saved_run.add_argument("--allow-agent", action="store_true")
    saved_run.add_argument("--allow-parallel", action="store_true")
    saved_run.add_argument("--approve", action="append", default=[])
    saved_run.add_argument("--max-workers", type=int)
    _add_saved_workflow_scope_args(saved_run)
    _add_saved_workflow_args(saved_run)

    init = subparsers.add_parser("init", help="Copy a workflow template to a destination.")
    init.add_argument("template", type=Path)
    init.add_argument("destination", type=Path)

    doctor = subparsers.add_parser("doctor", help="Check local runtime prerequisites.")
    doctor.add_argument(
        "--repair-codex-host",
        action="store_true",
        help="Create a missing Codex companion-host link inside the user home.",
    )
    doctor.add_argument("--approve", action="append", default=[])
    doctor.add_argument("--json", action="store_true")

    schema_cmd = subparsers.add_parser("schema", help="List or print public Conductor JSON Schemas.")
    schema_cmd.add_argument("schema_type", nargs="?", help="Schema name or artifact schema id to print.")
    schema_cmd.add_argument("--list", action="store_true", help="List available schema types.")
    schema_cmd.add_argument("--json", action="store_true", help="Emit the schema list as JSON with --list.")

    write_schema = subparsers.add_parser("write-schema", help="Write one public Conductor JSON Schema to a new file.")
    write_schema.add_argument("schema_type", help="Schema name or artifact schema id to write.")
    write_schema.add_argument("destination", type=Path)

    benchmark = subparsers.add_parser("benchmark", help="Run a benchmark suite and write a JSON report.")
    benchmark.add_argument("suite", type=Path)
    benchmark.add_argument("--workspace", type=Path, default=Path("."))
    benchmark.add_argument("--output", type=Path)
    benchmark.add_argument("--allow-writes", action="store_true")
    benchmark.add_argument("--allow-destructive", action="store_true")
    benchmark.add_argument("--allow-network", action="store_true")
    benchmark.add_argument("--approve", action="append", default=[])
    benchmark.add_argument("--output-limit-bytes", type=int, default=1024 * 1024)
    benchmark.add_argument("--system", default="conductor")

    validate_report = subparsers.add_parser(
        "validate-benchmark-report",
        help="Validate strict benchmark reports, including aggregate staged-repair reports.",
    )
    validate_report.add_argument("paths", nargs="+", type=Path)

    score_report = subparsers.add_parser(
        "score-benchmark-report",
        help="Attach hash-bound rubric quality evidence to an unscored benchmark report.",
    )
    score_report.add_argument("report", type=Path)
    score_report.add_argument("parity_tasks", type=Path)
    score_report.add_argument("score_input", type=Path)
    score_report.add_argument("output", type=Path)

    validate_quality = subparsers.add_parser(
        "validate-benchmark-quality-evidence",
        help="Validate standalone benchmark quality evidence artifacts.",
    )
    validate_quality.add_argument("paths", nargs="+", type=Path)

    validate_score_input = subparsers.add_parser(
        "validate-benchmark-score-input",
        help="Validate strict benchmark score input artifacts.",
    )
    validate_score_input.add_argument("paths", nargs="+", type=Path)

    claude_task = subparsers.add_parser(
        "run-claude-readonly-task",
        help="Run one paid, pinned Claude Sonnet Ultracode parity task with read-only tools.",
    )
    claude_task.add_argument("parity_tasks", type=Path)
    claude_task.add_argument("task_id")
    claude_task.add_argument("--workspace", type=Path, default=Path("."))
    claude_task.add_argument("--output", type=Path, required=True)
    claude_task.add_argument("--max-budget-usd", type=float, required=True)
    claude_task.add_argument("--max-turns", type=int, default=8)
    claude_task.add_argument("--timeout-seconds", type=int, default=900)
    claude_task.add_argument("--output-limit-bytes", type=int, default=1024 * 1024)
    claude_task.add_argument("--allow-agent", action="store_true")
    claude_task.add_argument("--allow-network", action="store_true")
    claude_task.add_argument(
        "--approve",
        action="append",
        default=[],
        help="Approval token; this command requires %s." % CLAUDE_PAID_RUN_APPROVAL,
    )

    validate_claude_evidence = subparsers.add_parser(
        "validate-claude-provider-evidence",
        help="Validate strict Claude live-run provider evidence artifacts.",
    )
    validate_claude_evidence.add_argument("paths", nargs="+", type=Path)

    claude_staged = subparsers.add_parser(
        "run-claude-staged-task",
        help="Run one paid, source-isolated Claude Sonnet Ultracode edit and emit a verified patch.",
    )
    claude_staged.add_argument("parity_tasks", type=Path)
    claude_staged.add_argument("task_id")
    claude_staged.add_argument("--workspace", type=Path, default=Path("."))
    claude_staged.add_argument("--output", type=Path, required=True)
    claude_staged.add_argument("--stage-dir", type=Path)
    claude_staged.add_argument("--patch-output", type=Path)
    claude_staged.add_argument(
        "--check-command-json",
        required=True,
        help='Deterministic verifier argv as JSON, for example ["python3","-B","-m","unittest","discover"].',
    )
    claude_staged.add_argument("--max-budget-usd", type=float, required=True)
    claude_staged.add_argument("--max-turns", type=int, default=12)
    claude_staged.add_argument("--timeout-seconds", type=int, default=1800)
    claude_staged.add_argument("--check-timeout-seconds", type=int, default=600)
    claude_staged.add_argument("--output-limit-bytes", type=int, default=1024 * 1024)
    claude_staged.add_argument("--check-output-limit-bytes", type=int, default=1024 * 1024)
    claude_staged.add_argument(
        "--exit-contract",
        choices=STAGED_EXIT_CONTRACTS,
        default=None,
        help=(
            "Process exit semantics: strict requires provider and staged success; verified-stage accepts a retained "
            "verifier-passing patch while the report and provider failure remain unchanged. Defaults to strict, "
            "or verified-stage when --apply-verified-stage is used."
        ),
    )
    _add_inline_verified_apply_args(claude_staged)
    claude_staged.add_argument("--allow-agent", action="store_true")
    claude_staged.add_argument("--allow-network", action="store_true")
    claude_staged.add_argument("--allow-writes", action="store_true")
    claude_staged.add_argument(
        "--approve",
        action="append",
        default=[],
        help="Approval token; requires claude-paid-run and %s." % CLAUDE_STAGED_WRITE_APPROVAL,
    )

    validate_claude_staged = subparsers.add_parser(
        "validate-claude-staged-evidence",
        help="Validate strict Claude staged-write evidence artifacts.",
    )
    validate_claude_staged.add_argument("paths", nargs="+", type=Path)

    codex_task = subparsers.add_parser(
        "run-codex-readonly-task",
        help="Run one pinned, source-isolated Codex parity task in a read-only sandbox.",
    )
    codex_task.add_argument("parity_tasks", type=Path)
    codex_task.add_argument("task_id")
    codex_task.add_argument("--workspace", type=Path, default=Path("."))
    codex_task.add_argument("--output", type=Path, required=True)
    codex_task.add_argument("--stage-dir", type=Path)
    codex_task.add_argument("--model", required=True)
    codex_task.add_argument("--effort", choices=sorted(CODEX_REASONING_EFFORTS), required=True)
    codex_task.add_argument(
        "--service-tier",
        choices=sorted(CODEX_SERVICE_TIERS),
        default=CODEX_DEFAULT_SERVICE_TIER,
        help="Codex routing tier; priority may reduce latency while consuming increased usage.",
    )
    codex_task.add_argument("--max-tokens", type=int, required=True)
    codex_task.add_argument("--tool-output-token-limit", type=int, default=DEFAULT_CODEX_TOOL_OUTPUT_TOKEN_LIMIT)
    codex_task.add_argument("--timeout-seconds", type=int, default=900)
    codex_task.add_argument("--output-limit-bytes", type=int, default=1024 * 1024)
    codex_task.add_argument("--allow-agent", action="store_true")
    codex_task.add_argument("--allow-network", action="store_true")
    codex_task.add_argument(
        "--approve",
        action="append",
        default=[],
        help="Approval token; this command requires %s." % CODEX_LIVE_RUN_APPROVAL,
    )

    validate_codex_evidence = subparsers.add_parser(
        "validate-codex-provider-evidence",
        help="Validate strict Codex isolated live-run provider evidence artifacts.",
    )
    validate_codex_evidence.add_argument("paths", nargs="+", type=Path)

    codex_staged = subparsers.add_parser(
        "run-codex-staged-task",
        help="Run one source-isolated Codex implementation task and emit a verified patch.",
    )
    codex_staged.add_argument("parity_tasks", type=Path)
    codex_staged.add_argument("task_id")
    codex_staged.add_argument("--workspace", type=Path, default=Path("."))
    codex_staged.add_argument("--output", type=Path, required=True)
    codex_staged.add_argument("--stage-dir", type=Path)
    codex_staged.add_argument("--patch-output", type=Path)
    codex_staged.add_argument(
        "--check-command-json",
        required=True,
        help='Deterministic verifier argv as JSON, for example ["python3","-B","-m","unittest","discover"].',
    )
    codex_staged.add_argument("--model", required=True)
    codex_staged.add_argument("--effort", choices=sorted(CODEX_REASONING_EFFORTS), required=True)
    codex_staged.add_argument(
        "--service-tier",
        choices=sorted(CODEX_SERVICE_TIERS),
        default=CODEX_DEFAULT_SERVICE_TIER,
        help="Codex routing tier; priority may reduce latency while consuming increased usage.",
    )
    codex_staged.add_argument("--max-tokens", type=int, required=True)
    codex_staged.add_argument("--tool-output-token-limit", type=int, default=DEFAULT_CODEX_TOOL_OUTPUT_TOKEN_LIMIT)
    codex_staged.add_argument("--timeout-seconds", type=int, default=1800)
    codex_staged.add_argument("--check-timeout-seconds", type=int, default=600)
    codex_staged.add_argument("--output-limit-bytes", type=int, default=1024 * 1024)
    codex_staged.add_argument("--check-output-limit-bytes", type=int, default=1024 * 1024)
    codex_staged.add_argument(
        "--exit-contract",
        choices=STAGED_EXIT_CONTRACTS,
        default=None,
        help=(
            "Process exit semantics: strict requires provider and staged success; verified-stage accepts a retained "
            "verifier-passing patch while the report and provider failure remain unchanged. Defaults to strict, "
            "or verified-stage when --apply-verified-stage is used."
        ),
    )
    _add_inline_verified_apply_args(codex_staged)
    codex_staged.add_argument("--allow-agent", action="store_true")
    codex_staged.add_argument("--allow-network", action="store_true")
    codex_staged.add_argument("--allow-writes", action="store_true")
    codex_staged.add_argument(
        "--approve",
        action="append",
        default=[],
        help="Approval token; requires codex-live-run and %s." % CODEX_STAGED_WRITE_APPROVAL,
    )

    validate_codex_staged = subparsers.add_parser(
        "validate-codex-staged-evidence",
        help="Validate strict Codex staged-write evidence artifacts.",
    )
    validate_codex_staged.add_argument("paths", nargs="+", type=Path)

    codex_repair = subparsers.add_parser(
        "run-codex-staged-repair",
        help="Run bounded source-isolated Codex repair attempts with deterministic verifier feedback.",
    )
    codex_repair.add_argument("parity_tasks", type=Path)
    codex_repair.add_argument("task_id")
    codex_repair.add_argument("--workspace", type=Path, default=Path("."))
    codex_repair.add_argument("--repair-dir", type=Path, required=True)
    codex_repair.add_argument(
        "--check-command-json",
        required=True,
        help='Deterministic verifier argv as JSON, for example ["python3","-B","-m","unittest","discover"].',
    )
    codex_repair.add_argument("--model", required=True)
    codex_repair.add_argument("--effort", choices=sorted(CODEX_REASONING_EFFORTS), required=True)
    codex_repair.add_argument("--max-attempts", type=int, required=True)
    codex_repair.add_argument(
        "--attempts-this-run",
        type=int,
        help="Pause cleanly after this many new attempts when another retry remains.",
    )
    codex_repair.add_argument(
        "--resume",
        action="store_true",
        help="Resume a validated repair checkpoint without replaying completed attempts.",
    )
    codex_repair.add_argument(
        "--exit-contract",
        choices=STAGED_EXIT_CONTRACTS,
        default=None,
        help=(
            "Process exit semantics: strict requires repair success; verified-stage accepts a retained "
            "cumulative patch whose final stage passed verification, without relabeling repair failure. Defaults "
            "to strict, or verified-stage when --apply-verified-stage is used."
        ),
    )
    _add_inline_verified_apply_args(codex_repair)
    codex_repair.add_argument("--max-tokens-per-attempt", type=int, required=True)
    codex_repair.add_argument("--provider-timeout-seconds", type=int, required=True)
    codex_repair.add_argument("--check-timeout-seconds", type=int, required=True)
    codex_repair.add_argument("--tool-output-token-limit", type=int, default=DEFAULT_CODEX_TOOL_OUTPUT_TOKEN_LIMIT)
    codex_repair.add_argument("--output-limit-bytes", type=int, default=1024 * 1024)
    codex_repair.add_argument("--check-output-limit-bytes", type=int, default=1024 * 1024)
    codex_repair.add_argument("--allow-agent", action="store_true")
    codex_repair.add_argument("--allow-network", action="store_true")
    codex_repair.add_argument("--allow-writes", action="store_true")
    codex_repair.add_argument(
        "--approve",
        action="append",
        default=[],
        help="Approval token; requires codex-live-run and %s." % CODEX_STAGED_WRITE_APPROVAL,
    )

    validate_codex_repair = subparsers.add_parser(
        "validate-codex-staged-repair",
        help="Validate strict Codex staged repair evidence and its hash-linked attempt reports.",
    )
    validate_codex_repair.add_argument("paths", nargs="+", type=Path)

    validate_codex_repair_state = subparsers.add_parser(
        "validate-codex-staged-repair-state",
        help="Validate strict staged-repair checkpoints and their hash-linked artifacts.",
    )
    validate_codex_repair_state.add_argument("paths", nargs="+", type=Path)

    staged_apply = subparsers.add_parser(
        "apply-verified-stage",
        help="Transactionally apply a retained, verifier-passing staged delivery without another model call.",
    )
    staged_apply.add_argument("evidence", type=Path)
    staged_apply.add_argument("--workspace", type=Path, default=Path("."))
    staged_apply.add_argument("--stage-dir", type=Path)
    staged_apply.add_argument("--patch", type=Path)
    staged_apply.add_argument("--receipt", type=Path)
    staged_apply.add_argument("--allow-writes", action="store_true")
    staged_apply.add_argument("--allow-destructive", action="store_true")
    staged_apply.add_argument(
        "--approve",
        action="append",
        default=[],
        help=(
            "Approval token; requires %s, plus %s when the verified delta deletes files."
            % (STAGED_APPLY_APPROVAL, STAGED_APPLY_DELETE_APPROVAL)
        ),
    )

    validate_staged_apply = subparsers.add_parser(
        "validate-staged-apply",
        help="Validate hash-bound verified-stage apply receipts.",
    )
    validate_staged_apply.add_argument("paths", nargs="+", type=Path)

    compare = subparsers.add_parser("compare", help="Compare benchmark reports from multiple systems.")
    compare.add_argument("manifest", type=Path)
    compare.add_argument("--base-dir", type=Path, default=Path("."))
    compare.add_argument("--output", type=Path)
    compare.add_argument(
        "--require-claim-eligible",
        action="store_true",
        help="Exit nonzero unless all compared systems have equivalent live run provenance.",
    )

    comparison_manifest = subparsers.add_parser("validate-comparison", help="Validate benchmark comparison manifest files.")
    comparison_manifest.add_argument("paths", nargs="+", type=Path)

    parity_tasks = subparsers.add_parser("validate-parity-tasks", help="Validate parity task fixture files.")
    parity_tasks.add_argument("paths", nargs="+", type=Path)

    parity_campaign_plan = subparsers.add_parser(
        "plan-parity-campaign",
        help="Print a seed-randomized, balanced matched-provider campaign without launching tools.",
    )
    parity_campaign_plan.add_argument("parity_tasks", type=Path)
    _add_parity_campaign_args(parity_campaign_plan)

    parity_campaign_write = subparsers.add_parser(
        "write-parity-campaign",
        help="Write a seed-randomized matched-provider campaign without launching tools.",
    )
    parity_campaign_write.add_argument("parity_tasks", type=Path)
    parity_campaign_write.add_argument("output", type=Path)
    _add_parity_campaign_args(parity_campaign_write)

    parity_campaign_validate = subparsers.add_parser(
        "validate-parity-campaign",
        help="Validate preregistered matched-provider campaign files.",
    )
    parity_campaign_validate.add_argument("paths", nargs="+", type=Path)

    parity_campaign_status = subparsers.add_parser(
        "status-parity-campaign",
        help="Validate planned campaign evidence and summarize paired coverage and uncertainty.",
    )
    parity_campaign_status.add_argument("campaign", type=Path)
    parity_campaign_status.add_argument("--json", action="store_true")

    parity_campaign_run = subparsers.add_parser(
        "run-parity-campaign-trial",
        help="Run and finalize one preregistered paired implementation trial in its randomized order.",
    )
    parity_campaign_run.add_argument("campaign", type=Path)
    parity_campaign_run.add_argument("parity_tasks", type=Path)
    parity_campaign_trial = parity_campaign_run.add_mutually_exclusive_group(required=True)
    parity_campaign_trial.add_argument("--trial-id")
    parity_campaign_trial.add_argument("--next", action="store_true", dest="next_trial")
    parity_campaign_run.add_argument("--fixture-root", type=Path)
    parity_campaign_run.add_argument("--resume", action="store_true")
    parity_campaign_run.add_argument("--dry-run", action="store_true")
    parity_campaign_run.add_argument("--json", action="store_true")
    parity_campaign_run.add_argument("--allow-agent", action="store_true")
    parity_campaign_run.add_argument("--allow-network", action="store_true")
    parity_campaign_run.add_argument("--allow-writes", action="store_true")
    parity_campaign_run.add_argument("--allow-destructive", action="store_true")
    parity_campaign_run.add_argument("--allow-parallel", action="store_true")
    parity_campaign_run.add_argument("--approve", action="append", default=[])

    auto_topology_plan = subparsers.add_parser(
        "plan-auto-topology-campaign",
        help="Print a randomized matched direct/progressive/plan-first campaign without launching tools.",
    )
    auto_topology_plan.add_argument("parity_tasks", type=Path)
    _add_auto_topology_campaign_args(auto_topology_plan)

    auto_topology_write = subparsers.add_parser(
        "write-auto-topology-campaign",
        help="Write a preregistered matched auto-topology campaign without launching tools.",
    )
    auto_topology_write.add_argument("parity_tasks", type=Path)
    auto_topology_write.add_argument("output", type=Path)
    _add_auto_topology_campaign_args(auto_topology_write)

    auto_topology_validate = subparsers.add_parser(
        "validate-auto-topology-campaign",
        help="Validate preregistered matched auto-topology campaign files.",
    )
    auto_topology_validate.add_argument("paths", nargs="+", type=Path)

    auto_topology_status = subparsers.add_parser(
        "status-auto-topology-campaign",
        help="Validate and summarize matched topology quality, escalation, latency, tokens, and cost.",
    )
    auto_topology_status.add_argument("campaign", type=Path)
    auto_topology_status.add_argument("--json", action="store_true")

    auto_topology_run = subparsers.add_parser(
        "run-auto-topology-arm",
        help="Run and finalize one preregistered auto-topology arm with exactly-once launch evidence.",
    )
    auto_topology_run.add_argument("campaign", type=Path)
    auto_topology_run.add_argument("parity_tasks", type=Path)
    auto_topology_selection = auto_topology_run.add_mutually_exclusive_group(required=True)
    auto_topology_selection.add_argument("--next", action="store_true", dest="next_arm")
    auto_topology_selection.add_argument("--cohort-id")
    auto_topology_run.add_argument("--topology", choices=AUTO_TOPOLOGIES)
    auto_topology_run.add_argument("--fixture-root", type=Path)
    auto_topology_run.add_argument("--resume", action="store_true")
    auto_topology_run.add_argument("--dry-run", action="store_true")
    auto_topology_run.add_argument("--json", action="store_true")
    auto_topology_run.add_argument("--allow-agent", action="store_true")
    auto_topology_run.add_argument("--allow-writes", action="store_true")
    auto_topology_run.add_argument("--allow-network", action="store_true")
    auto_topology_run.add_argument("--allow-destructive", action="store_true")
    auto_topology_run.add_argument("--allow-parallel", action="store_true")
    auto_topology_run.add_argument("--approve", action="append", default=[])

    readonly_parity_plan = subparsers.add_parser(
        "plan-readonly-parity-campaign",
        help="Print a matched or product-profile Codex/Sonnet campaign without launching providers.",
    )
    readonly_parity_plan.add_argument("parity_tasks", type=Path)
    _add_readonly_parity_campaign_args(readonly_parity_plan)

    readonly_parity_write = subparsers.add_parser(
        "write-readonly-parity-campaign",
        help="Write a preregistered matched or product-profile read-only campaign without launching providers.",
    )
    readonly_parity_write.add_argument("parity_tasks", type=Path)
    readonly_parity_write.add_argument("output", type=Path)
    _add_readonly_parity_campaign_args(readonly_parity_write)

    readonly_parity_validate = subparsers.add_parser(
        "validate-readonly-parity-campaign",
        help="Validate preregistered read-only parity campaign files.",
    )
    readonly_parity_validate.add_argument("paths", nargs="+", type=Path)

    readonly_parity_status = subparsers.add_parser(
        "status-readonly-parity-campaign",
        help="Validate and summarize hidden quality, topology, latency, token, and cost evidence.",
    )
    readonly_parity_status.add_argument("campaign", type=Path)
    readonly_parity_status.add_argument("--json", action="store_true")

    readonly_parity_run = subparsers.add_parser(
        "run-readonly-parity-arm",
        help="Run and finalize one exactly-once serial Codex, native Codex, or Sonnet Ultracode arm.",
    )
    readonly_parity_run.add_argument("campaign", type=Path)
    readonly_parity_run.add_argument("parity_tasks", type=Path)
    readonly_selection = readonly_parity_run.add_mutually_exclusive_group(required=True)
    readonly_selection.add_argument("--next", action="store_true", dest="next_arm")
    readonly_selection.add_argument("--cohort-id")
    readonly_parity_run.add_argument("--system", choices=READONLY_PARITY_SYSTEMS)
    readonly_parity_run.add_argument("--fixture-root", type=Path)
    readonly_parity_run.add_argument("--resume", action="store_true")
    readonly_parity_run.add_argument("--dry-run", action="store_true")
    readonly_parity_run.add_argument("--json", action="store_true")
    readonly_parity_run.add_argument("--allow-agent", action="store_true")
    readonly_parity_run.add_argument("--allow-network", action="store_true")
    readonly_parity_run.add_argument("--allow-parallel", action="store_true")
    readonly_parity_run.add_argument("--approve", action="append", default=[])

    parity_runbook_plan = subparsers.add_parser(
        "plan-live-parity-runbook",
        help="Print a planned live parity runbook from parity task fixtures without launching tools.",
    )
    parity_runbook_plan.add_argument("parity_tasks", type=Path)
    _add_live_parity_runbook_args(parity_runbook_plan)

    parity_runbook_write = subparsers.add_parser(
        "write-live-parity-runbook",
        help="Write a planned live parity runbook from parity task fixtures without launching tools.",
    )
    parity_runbook_write.add_argument("parity_tasks", type=Path)
    parity_runbook_write.add_argument("output", type=Path)
    _add_live_parity_runbook_args(parity_runbook_write)

    parity_runbook_validate = subparsers.add_parser(
        "validate-live-parity-runbook",
        help="Validate planned live parity runbook files.",
    )
    parity_runbook_validate.add_argument("paths", nargs="+", type=Path)

    run_manifest = subparsers.add_parser("validate-run-manifest", help="Validate benchmark run manifest files.")
    run_manifest.add_argument("paths", nargs="+", type=Path)

    write_manifest = subparsers.add_parser("write-run-manifest", help="Generate a run manifest from a benchmark report.")
    write_manifest.add_argument("report", type=Path)
    write_manifest.add_argument("output", type=Path)
    write_manifest.add_argument("--run-id", required=True)
    write_manifest.add_argument("--system")
    write_manifest.add_argument("--live-run", action="store_true")
    write_manifest.add_argument("--repository-state", required=True)
    write_manifest.add_argument("--repository-commit")
    write_manifest.add_argument("--repository-branch")
    write_manifest.add_argument("--repository-dirty")
    write_manifest.add_argument("--model-provider", required=True)
    write_manifest.add_argument("--model-name", required=True)
    write_manifest.add_argument("--model-effort", required=True)
    write_manifest.add_argument("--model-version")
    write_manifest.add_argument("--max-minutes", type=int, required=True)
    write_manifest.add_argument("--max-agents", type=int, required=True)
    write_manifest.add_argument("--max-live-tool-cost", choices=["none", "low", "medium", "high"], required=True)
    write_manifest.add_argument("--max-cost-usd", type=float)
    write_manifest.add_argument("--max-tokens", type=int)
    write_manifest.add_argument("--cost-cap-enforcement", choices=sorted(BUDGET_CAP_ENFORCEMENT_LEVELS))
    write_manifest.add_argument("--token-cap-enforcement", choices=sorted(BUDGET_CAP_ENFORCEMENT_LEVELS))
    write_manifest.add_argument("--allow-writes", action="store_true")
    write_manifest.add_argument("--allow-destructive", action="store_true")
    write_manifest.add_argument("--allow-network", action="store_true")
    write_manifest.add_argument("--allow-agent", action="store_true")
    write_manifest.add_argument("--allow-parallel", action="store_true")
    write_manifest.add_argument("--approved-token", action="append", default=[])
    write_manifest.add_argument("--task-max-score", type=int, default=10)

    evidence = subparsers.add_parser("evidence-bundle", help="Print a redacted local parity evidence bundle.")
    _add_evidence_bundle_args(evidence)
    evidence.add_argument(
        "--require-claim-eligible",
        action="store_true",
        help="Exit nonzero unless the bundle contains a claim-eligible live comparison report.",
    )

    write_evidence = subparsers.add_parser("write-evidence-bundle", help="Write a redacted local parity evidence bundle.")
    write_evidence.add_argument("output", type=Path)
    _add_evidence_bundle_args(write_evidence)
    write_evidence.add_argument(
        "--require-claim-eligible",
        action="store_true",
        help="Exit nonzero unless the bundle contains a claim-eligible live comparison report.",
    )

    validate_evidence = subparsers.add_parser("validate-evidence-bundle", help="Validate local parity evidence bundle files.")
    validate_evidence.add_argument("paths", nargs="+", type=Path)
    validate_evidence.add_argument("--benchmarks-dir", type=Path)
    validate_evidence.add_argument("--max-reports", type=int, default=DEFAULT_MAX_REPORTS)
    validate_evidence.add_argument(
        "--require-claim-eligible",
        action="store_true",
        help="Exit nonzero unless every bundle contains a claim-eligible live comparison report backed by --benchmarks-dir.",
    )

    routine_manifest = subparsers.add_parser("validate-routine-manifest", help="Validate routine manifest files.")
    routine_manifest.add_argument("paths", nargs="+", type=Path)

    routine_controls = subparsers.add_parser("validate-routine-controls", help="Validate external routine control files.")
    routine_controls.add_argument("paths", nargs="+", type=Path)

    routine_state = subparsers.add_parser("validate-routine-state", help="Validate external routine execution history files.")
    routine_state.add_argument("paths", nargs="+", type=Path)

    routine_service_grant = subparsers.add_parser(
        "validate-routine-service-grant",
        help="Validate durable routine-service grant files.",
    )
    routine_service_grant.add_argument("paths", nargs="+", type=Path)

    routine_service_state = subparsers.add_parser(
        "validate-routine-service-state",
        help="Validate durable routine-service lifecycle state files.",
    )
    routine_service_state.add_argument("paths", nargs="+", type=Path)

    write_routine = subparsers.add_parser(
        "write-routine-manifest",
        help="Write a recurring routine manifest for an external scheduler without launching it.",
    )
    write_routine.add_argument("output", type=Path)
    write_routine.add_argument("--name", required=True)
    routine_target = write_routine.add_mutually_exclusive_group(required=True)
    routine_target.add_argument("--workflow", type=Path)
    routine_target.add_argument("--saved-workflow")
    write_routine.add_argument("--workspace", type=Path, default=Path("."))
    write_routine.add_argument("--runs-dir", type=Path)
    routine_schedule = write_routine.add_mutually_exclusive_group(required=True)
    routine_schedule.add_argument("--manual", action="store_true")
    routine_schedule.add_argument("--interval-minutes", type=int)
    routine_schedule.add_argument("--daily-at")
    routine_schedule.add_argument("--weekdays-at")
    routine_schedule.add_argument("--weekly-at")
    routine_schedule.add_argument("--monthly-at")
    routine_schedule.add_argument("--once-at")
    routine_schedule.add_argument(
        "--cron",
        help="Strict five-field numeric cron expression: minute hour day-of-month month day-of-week.",
    )
    write_routine.add_argument("--timezone", default="UTC")
    write_routine.add_argument(
        "--desktop-notify",
        action="store_true",
        help="Notify after each real routine execution, including scheduled service runs.",
    )
    write_routine.add_argument("--allow-writes", action="store_true")
    write_routine.add_argument("--allow-destructive", action="store_true")
    write_routine.add_argument("--allow-network", action="store_true")
    write_routine.add_argument("--allow-agent", action="store_true")
    write_routine.add_argument("--allow-parallel", action="store_true")
    write_routine.add_argument("--approve", action="append", default=[])
    write_routine.add_argument("--max-workers", type=int)
    write_routine.add_argument("--max-minutes", type=int)
    write_routine.add_argument("--max-cost-usd", type=float)
    write_routine.add_argument("--max-tokens", type=int)
    _add_saved_workflow_scope_args(write_routine)
    _add_saved_workflow_args(write_routine)

    list_routines = subparsers.add_parser("list-routines", help="List local routine manifests.")
    list_routines.add_argument("--routines-dir", type=Path)
    list_routines.add_argument("--max-routines", type=int, default=50)
    list_routines.add_argument("--json", action="store_true", help="Emit a redacted JSON envelope.")

    status_routine = subparsers.add_parser("status-routine", help="Show one routine's control state and bounded history.")
    status_routine.add_argument("routine_manifest", type=Path)

    run_routine = subparsers.add_parser("run-routine", help="Run one approved routine manifest now.")
    run_routine.add_argument("routine_manifest", type=Path)
    run_routine.add_argument("--approve", action="append", default=[])
    run_routine.add_argument("--output-limit-bytes", type=int, default=DEFAULT_OUTPUT_LIMIT_BYTES)

    run_routine_now = subparsers.add_parser("run-routine-now", help="Run one approved routine immediately, even if paused.")
    run_routine_now.add_argument("routine_manifest", type=Path)
    run_routine_now.add_argument("--approve", action="append", default=[])
    run_routine_now.add_argument("--output-limit-bytes", type=int, default=DEFAULT_OUTPUT_LIMIT_BYTES)

    pause_routine = subparsers.add_parser("pause-routine", help="Pause future scheduled runs without stopping an active run.")
    pause_routine.add_argument("routine_manifest", type=Path)
    pause_routine.add_argument("--reason", default="")

    resume_routine = subparsers.add_parser("resume-routine", help="Resume future scheduled runs after explicit approval.")
    resume_routine.add_argument("routine_manifest", type=Path)
    resume_routine.add_argument("--approve", action="append", default=[])
    resume_routine.add_argument("--reason", default="")

    update_routine = subparsers.add_parser(
        "update-routine-schedule",
        help="Atomically replace one routine schedule after explicit approval.",
    )
    update_routine.add_argument("routine_manifest", type=Path)
    update_schedule = update_routine.add_mutually_exclusive_group(required=True)
    update_schedule.add_argument("--manual", action="store_true")
    update_schedule.add_argument("--interval-minutes", type=int)
    update_schedule.add_argument("--daily-at")
    update_schedule.add_argument("--weekdays-at")
    update_schedule.add_argument("--weekly-at")
    update_schedule.add_argument("--monthly-at")
    update_schedule.add_argument("--once-at")
    update_schedule.add_argument(
        "--cron",
        help="Strict five-field numeric cron expression: minute hour day-of-month month day-of-week.",
    )
    update_routine.add_argument("--timezone", default="UTC")
    update_routine.add_argument("--approve", action="append", default=[])
    update_routine.add_argument("--reason", default="")

    supervise = subparsers.add_parser(
        "supervise-routines",
        help="Run an operator-visible foreground routine scheduler.",
    )
    supervise.add_argument("--routines-dir", type=Path)
    supervise.add_argument("--approve", action="append", default=[])
    supervise.add_argument("--poll-seconds", type=int, default=60)
    supervise.add_argument(
        "--max-cycles",
        type=int,
        help="Cycle cap; defaults to 1 in foreground and 0 (until terminated) in background.",
    )
    supervise.add_argument("--max-routines", type=int, default=50)
    supervise.add_argument("--output-limit-bytes", type=int, default=DEFAULT_OUTPUT_LIMIT_BYTES)
    supervise.add_argument(
        "--background",
        action="store_true",
        help="Launch one visible session-isolated scheduler after approval handoff.",
    )

    background_supervisor_status = subparsers.add_parser(
        "status-background-routine-supervisor",
        help="Print detached routine-supervisor status, liveness, and routine state.",
    )
    background_supervisor_status.add_argument("metadata_json", type=Path)

    background_supervisor_wait = subparsers.add_parser(
        "wait-background-routine-supervisor",
        help="Wait for a bounded detached routine supervisor to finish.",
    )
    background_supervisor_wait.add_argument("metadata_json", type=Path)
    background_supervisor_wait.add_argument("--timeout-seconds", type=int, default=3600)
    background_supervisor_wait.add_argument("--poll-interval-ms", type=int, default=250)

    background_supervisor_terminate = subparsers.add_parser(
        "terminate-background-routine-supervisor",
        help="Signal a validated active detached routine supervisor.",
    )
    background_supervisor_terminate.add_argument("metadata_json", type=Path)
    background_supervisor_terminate.add_argument(
        "--force",
        action="store_true",
        help="Escalate only if graceful child cleanup exceeds the bounded grace period.",
    )

    install_service = subparsers.add_parser(
        "install-routine-service",
        help="Install and optionally start an explicit hash-bound user routine service.",
    )
    _add_routine_service_install_args(install_service)

    update_service = subparsers.add_parser(
        "update-routine-service",
        help="Replace and restart an existing hash-bound user routine service.",
    )
    _add_routine_service_install_args(update_service)

    status_service = subparsers.add_parser(
        "status-routine-service",
        help="Inspect an installed routine service, grant bindings, heartbeat, and liveness.",
    )
    status_service.add_argument("--routines-dir", type=Path)
    status_service.add_argument("--json", action="store_true")
    status_service.add_argument("--home", type=Path, help=argparse.SUPPRESS)

    uninstall_service = subparsers.add_parser(
        "uninstall-routine-service",
        help="Stop and remove one explicit user routine service while retaining execution history.",
    )
    uninstall_service.add_argument("--routines-dir", type=Path)
    uninstall_service.add_argument("--allow-service-install", action="store_true")
    uninstall_service.add_argument("--approve", action="append", default=[])
    uninstall_service.add_argument("--home", type=Path, help=argparse.SUPPRESS)

    cloud_submit = subparsers.add_parser("cloud-submit", help="Submit an approved Codex Cloud task and record it.")
    cloud_task_source = cloud_submit.add_mutually_exclusive_group(required=True)
    cloud_task_source.add_argument("--task")
    cloud_task_source.add_argument("--task-file", type=Path)
    cloud_submit.add_argument("--env", required=True, dest="environment_id")
    cloud_submit.add_argument("--workspace", type=Path, default=Path("."))
    cloud_submit.add_argument("--cloud-dir", type=Path)
    cloud_submit.add_argument("--branch", default="")
    cloud_submit.add_argument("--attempts", type=int, default=1)
    cloud_submit.add_argument("--timeout-seconds", type=int, default=300)
    cloud_submit.add_argument("--output-limit-bytes", type=int, default=DEFAULT_OUTPUT_LIMIT_BYTES)
    cloud_submit.add_argument("--allow-network", action="store_true")
    cloud_submit.add_argument("--approve", action="append", default=[])

    cloud_status = subparsers.add_parser("cloud-status", help="Fetch an approved Codex Cloud task status.")
    _add_cloud_read_args(cloud_status)

    cloud_review = subparsers.add_parser("cloud-review", help="Fetch and record a reviewed Codex Cloud diff.")
    _add_cloud_read_args(cloud_review)
    cloud_review.add_argument("--cloud-dir", type=Path)
    cloud_review.add_argument("--attempt", type=int)

    cloud_apply = subparsers.add_parser("cloud-apply", help="Apply a reviewed, unchanged Codex Cloud diff.")
    _add_cloud_read_args(cloud_apply)
    cloud_apply.add_argument("--review-receipt", type=Path, required=True)
    cloud_apply.add_argument("--attempt", type=int)
    cloud_apply.add_argument("--allow-writes", action="store_true")

    dashboard = subparsers.add_parser("dashboard", help="Generate a static HTML run and benchmark dashboard.")
    dashboard.add_argument("--runs-dir", type=Path)
    dashboard.add_argument("--benchmarks-dir", type=Path)
    dashboard.add_argument("--goals-dir", type=Path)
    dashboard.add_argument("--routines-dir", type=Path)
    dashboard.add_argument("--generated-dir", type=Path)
    dashboard.add_argument("--auto-dir", type=Path)
    dashboard.add_argument("--output", type=Path)
    dashboard.add_argument("--title", default="Codex Conductor Dashboard")
    dashboard.add_argument("--max-runs", type=int, default=DEFAULT_MAX_RUNS)
    dashboard.add_argument("--max-reports", type=int, default=DEFAULT_MAX_REPORTS)
    dashboard.add_argument("--max-goals", type=int, default=DEFAULT_MAX_GOALS)
    dashboard.add_argument("--max-auto", type=int, default=DEFAULT_MAX_AUTO)
    dashboard.add_argument("--max-text-bytes", type=int, default=DEFAULT_MAX_TEXT_BYTES)

    serve = subparsers.add_parser("serve", help="Serve a loopback live dashboard with token-gated run controls.")
    serve.add_argument("--runs-dir", type=Path)
    serve.add_argument("--benchmarks-dir", type=Path)
    serve.add_argument("--goals-dir", type=Path)
    serve.add_argument("--routines-dir", type=Path)
    serve.add_argument("--generated-dir", type=Path)
    serve.add_argument("--auto-dir", type=Path)
    serve.add_argument("--host", default=DEFAULT_LIVE_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_LIVE_PORT)
    serve.add_argument("--title", default="Codex Conductor Live")
    serve.add_argument("--max-runs", type=int, default=DEFAULT_MAX_RUNS)
    serve.add_argument("--max-reports", type=int, default=DEFAULT_MAX_REPORTS)
    serve.add_argument("--max-goals", type=int, default=DEFAULT_MAX_GOALS)
    serve.add_argument("--max-auto", type=int, default=DEFAULT_MAX_AUTO)
    serve.add_argument("--max-text-bytes", type=int, default=DEFAULT_MAX_TEXT_BYTES)
    serve.add_argument("--control-token")
    serve.add_argument("--allow-remote", action="store_true", help="Allow binding to a non-loopback host.")

    retry = subparsers.add_parser("retry-step", help="Reset a failed or blocked step so --resume can retry it.")
    retry.add_argument("run_dir", type=Path)
    retry.add_argument("step_id")
    retry.add_argument("--cascade", action="store_true", help="Also reset transitive downstream dependents.")
    retry.add_argument("--reason", default="")

    reset = subparsers.add_parser("reset-step", help="Reset a step so --resume can re-run it.")
    reset.add_argument("run_dir", type=Path)
    reset.add_argument("step_id")
    reset.add_argument("--cascade", action="store_true", help="Also reset transitive downstream dependents.")
    reset.add_argument("--reason", default="")

    retry_packet_parser = subparsers.add_parser(
        "retry-packet",
        help="Rerun one agent_map packet while retaining compatible sibling packet cache entries.",
    )
    retry_packet_parser.add_argument("run_dir", type=Path)
    retry_packet_parser.add_argument("step_id")
    retry_packet_parser.add_argument("packet_index", type=int)
    retry_packet_parser.add_argument("--cascade", action="store_true", help="Also reset transitive downstream dependents.")
    retry_packet_parser.add_argument("--reason", default="")

    recover = subparsers.add_parser(
        "recover-run",
        help="Reconcile an interrupted run after proving its runner and tracked children are inactive.",
    )
    recover.add_argument("run_dir", type=Path)
    recover_resolution = recover.add_mutually_exclusive_group()
    recover_resolution.add_argument(
        "--retry-running",
        action="store_true",
        help="Reset the interrupted running step to pending instead of recording it as failed.",
    )
    recover_resolution.add_argument(
        "--resume-codex",
        action="store_true",
        help="Continue an interrupted codex_exec step on its checkpointed Codex thread.",
    )
    recover.add_argument("--reason", required=True)

    skip = subparsers.add_parser("skip-step", help="Mark a non-gate step skipped with an audit reason.")
    skip.add_argument("run_dir", type=Path)
    skip.add_argument("step_id")
    skip.add_argument("--cascade", action="store_true", help="Reset transitive downstream dependents.")
    skip.add_argument("--reason", required=True)

    pause = subparsers.add_parser("pause-run", help="Request a run pause at the next step boundary.")
    pause.add_argument("run_dir", type=Path)
    pause.add_argument("--reason", default="")

    resume = subparsers.add_parser("resume-run", help="Clear a paused run so a later run --resume can continue.")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--reason", default="")

    stop = subparsers.add_parser("stop-run", help="Request a run stop at the next step boundary.")
    stop.add_argument("run_dir", type=Path)
    stop.add_argument("--reason", default="")

    restart = subparsers.add_parser("restart-run", help="Reset all workflow steps so --resume restarts the run.")
    restart.add_argument("run_dir", type=Path)
    restart.add_argument("--reason", default="")

    terminate = subparsers.add_parser("terminate-run", help="Signal a validated active session-isolated runner process group.")
    terminate.add_argument("run_dir", type=Path)
    terminate.add_argument("--reason", default="")
    terminate.add_argument("--force", action="store_true", help="Use SIGKILL instead of SIGTERM.")

    save_workflow = subparsers.add_parser("save-run-workflow", help="Export a run's validated workflow JSON for reuse.")
    save_workflow.add_argument("run_dir", type=Path)
    save_workflow.add_argument("destination", type=Path)

    usage = subparsers.add_parser("record-usage", help="Record measured token/cost usage for a run or step.")
    usage.add_argument("run_dir", type=Path)
    usage.add_argument("--step-id")
    usage.add_argument("--input-tokens", type=int)
    usage.add_argument("--output-tokens", type=int)
    usage.add_argument("--total-tokens", type=int)
    usage.add_argument("--cost-usd", type=float)
    usage.add_argument("--source", default="manual")
    usage.add_argument("--reason", default="")

    import_usage = subparsers.add_parser("import-usage", help="Import measured token/cost usage from a local JSON artifact.")
    import_usage.add_argument("run_dir", type=Path)
    import_usage.add_argument("usage_json", type=Path)
    import_usage.add_argument("--step-id")
    import_usage.add_argument("--provider", choices=["auto", "generic", "openai", "anthropic", "claude", "codex"], default="auto")
    import_usage.add_argument("--source")
    import_usage.add_argument("--reason", default="")

    import_trace = subparsers.add_parser("import-session-trace", help="Import redacted session trace events from a local JSON or JSONL artifact.")
    import_trace.add_argument("run_dir", type=Path)
    import_trace.add_argument("trace_json", type=Path)
    import_trace.add_argument("--step-id")
    import_trace.add_argument("--provider", choices=["auto", "generic", "openai", "anthropic", "claude", "codex"], default="auto")
    import_trace.add_argument("--source")
    import_trace.add_argument("--reason", default="")
    import_trace.add_argument(
        "--record-usage",
        action="store_true",
        help="Also record aggregate measured token/cost usage found in the local trace artifact.",
    )

    plan = subparsers.add_parser("plan-workflow", help="Generate a validated workflow draft from task text.")
    task_source = plan.add_mutually_exclusive_group(required=True)
    task_source.add_argument("--task", help="Task text to turn into a workflow draft.")
    task_source.add_argument("--task-file", type=Path, help="File containing task text.")
    plan.add_argument("--name")
    plan.add_argument("--kind", choices=sorted(PLAN_KINDS), default="auto")
    plan.add_argument("--profile", choices=sorted(PLAN_PROFILES), default="standard")
    plan.add_argument("--failing-command", action="append", default=[])
    plan.add_argument("--check-command", action="append", default=[])
    packet_source = plan.add_mutually_exclusive_group()
    packet_source.add_argument("--packet-item", action="append", default=[], help="Add one explicit packet item for a generated agent_map step.")
    packet_source.add_argument("--packet-items-file", type=Path, help="Workspace-relative line-oriented item file for a generated agent_map step.")
    packet_source.add_argument("--packet-glob", action="append", default=[], help="Add one workspace-relative glob whose matched files become packet items.")
    plan.add_argument("--packet-max-items", type=int, default=MAX_PACKET_ITEMS)
    plan.add_argument(
        "--packet-max-packets",
        type=int,
        default=BALANCED_AGENT_MAP_MAX_PACKETS,
        help="Maximum model packets for a generated agent_map; larger item sets are grouped safely.",
    )
    plan.add_argument("--no-agent", action="store_true", help="Generate a shell/artifact-only draft.")
    plan.add_argument("--write-capable", action="store_true", help="Include a gated workspace-write implementation step.")
    plan.add_argument("--max-workers", type=int, default=4)
    plan.add_argument("--output", type=Path)
    plan.add_argument(
        "--saved-command",
        help="Also write the generated workflow as a safe .claude/workflows command with this name.",
    )
    plan.add_argument(
        "--saved-output",
        type=Path,
        help="Destination .js/.mjs for --saved-command; defaults to .claude/workflows/<name>.js.",
    )

    model_plan = subparsers.add_parser(
        "plan-model-workflow",
        help="Use read-only Codex to author a validated workflow draft without executing it.",
    )
    model_task_source = model_plan.add_mutually_exclusive_group(required=True)
    model_task_source.add_argument("--task", help="Untrusted task text for the model workflow architect.")
    model_task_source.add_argument("--task-file", type=Path, help="File containing untrusted task text.")
    model_plan.add_argument("--workspace", type=Path, default=Path("."))
    model_plan.add_argument("--name", help="Optional lowercase workflow name.")
    model_plan.add_argument("--model", help="Optional Codex model override.")
    model_plan.add_argument(
        "--planner-effort",
        choices=sorted(MODEL_WORKFLOW_REASONING_EFFORTS),
        default=DEFAULT_MODEL_WORKFLOW_REASONING_EFFORT,
        help="Codex planning reasoning effort; defaults to high instead of inheriting user config.",
    )
    model_plan.add_argument("--write-capable", action="store_true", help="Allow a gated workspace-write Codex step in the draft.")
    model_plan.add_argument("--max-workers", type=int, default=4)
    model_plan.add_argument("--max-items", type=int, default=MAX_MODEL_WORKFLOW_ITEMS)
    model_plan.add_argument("--max-steps", type=int, default=32)
    model_plan.add_argument("--timeout-seconds", type=int, default=900)
    model_plan.add_argument("--output-limit-bytes", type=int, default=DEFAULT_OUTPUT_LIMIT_BYTES)
    model_plan.add_argument("--output", type=Path, help="Draft destination; defaults to external Conductor state.")
    model_plan.add_argument("--receipt", type=Path, help="Receipt destination; defaults beside the draft.")
    model_plan.add_argument(
        "--no-workspace-binding",
        action="store_true",
        help="Skip the default bounded workspace content binding; reviewed execution then needs an explicit override.",
    )
    model_plan.add_argument("--allow-agent", action="store_true", help="Explicitly allow the read-only Codex planning call.")
    model_plan.add_argument("--approve", action="append", default=[])

    model_run = subparsers.add_parser(
        "run-model-workflow",
        help="Plan, preflight, and explicitly execute a model-authored workflow in foreground or detached mode.",
    )
    model_run_task = model_run.add_mutually_exclusive_group(required=True)
    model_run_task.add_argument("--task", help="Untrusted task text for the model workflow architect.")
    model_run_task.add_argument("--task-file", type=Path, help="File containing untrusted task text.")
    model_run.add_argument("--workspace", type=Path, default=Path("."))
    model_run.add_argument("--runs-dir", type=Path)
    model_run.add_argument("--run-id")
    model_run.add_argument("--name", help="Optional lowercase workflow name.")
    model_run.add_argument("--model", help="Optional Codex model override for the planning call.")
    model_run.add_argument(
        "--planner-effort",
        choices=sorted(MODEL_WORKFLOW_REASONING_EFFORTS),
        default=DEFAULT_MODEL_WORKFLOW_REASONING_EFFORT,
        help="Codex planning reasoning effort; use xhigh only when its extra latency and usage are justified.",
    )
    model_run.add_argument(
        "--write-capable",
        action="store_true",
        help="Allow gated workspace-write Codex steps in the generated workflow.",
    )
    model_run.add_argument("--max-workers", type=int, default=1)
    model_run.add_argument("--max-items", type=int, default=MAX_MODEL_WORKFLOW_ITEMS)
    model_run.add_argument("--max-steps", type=int, default=32)
    model_run.add_argument("--planning-timeout-seconds", type=int, default=900)
    model_run.add_argument("--planning-output-limit-bytes", type=int, default=DEFAULT_OUTPUT_LIMIT_BYTES)
    model_run.add_argument("--output", type=Path, help="Draft destination; defaults to external Conductor state.")
    model_run.add_argument("--planning-receipt", type=Path, help="Planning receipt destination.")
    model_run.add_argument("--run-receipt", type=Path, help="Plan-and-run receipt destination.")
    model_run_mode = model_run.add_mutually_exclusive_group()
    model_run_mode.add_argument("--dry-run", action="store_true", help="Stop after full policy preflight without workflow execution.")
    model_run_mode.add_argument(
        "--background",
        action="store_true",
        help="Launch the fully preflighted generated workflow as one visible detached run.",
    )
    model_run.add_argument(
        "--desktop-notify",
        action="store_true",
        help="Send one content-free desktop notification when real execution finishes.",
    )
    model_run.add_argument("--allow-writes", action="store_true")
    model_run.add_argument("--allow-destructive", action="store_true")
    model_run.add_argument("--allow-network", action="store_true")
    model_run.add_argument("--allow-agent", action="store_true")
    model_run.add_argument("--allow-parallel", action="store_true")
    model_run.add_argument("--approve", action="append", default=[])

    reviewed_model_run = subparsers.add_parser(
        "run-reviewed-model-workflow",
        help="Preflight and execute an exact receipt-bound model workflow without another planning call.",
    )
    reviewed_model_run.add_argument("workflow", type=Path)
    reviewed_model_run.add_argument("planning_receipt", type=Path)
    reviewed_model_run.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Explicit current workspace for the reviewed graph.",
    )
    reviewed_model_run.add_argument("--runs-dir", type=Path)
    reviewed_model_run.add_argument("--run-id")
    reviewed_model_run.add_argument("--run-receipt", type=Path)
    reviewed_model_run_mode = reviewed_model_run.add_mutually_exclusive_group()
    reviewed_model_run_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the reviewed binding and stop after full policy preflight.",
    )
    reviewed_model_run_mode.add_argument(
        "--background",
        action="store_true",
        help="Launch the exact preflighted draft as one visible detached run.",
    )
    reviewed_model_run.add_argument(
        "--desktop-notify",
        action="store_true",
        help="Send one content-free desktop notification when real execution finishes.",
    )
    reviewed_model_run.add_argument("--allow-writes", action="store_true")
    reviewed_model_run.add_argument("--allow-destructive", action="store_true")
    reviewed_model_run.add_argument("--allow-network", action="store_true")
    reviewed_model_run.add_argument("--allow-agent", action="store_true")
    reviewed_model_run.add_argument("--allow-parallel", action="store_true")
    reviewed_model_run.add_argument(
        "--allow-unbound-workspace",
        action="store_true",
        help="Allow a legacy or explicitly unbound planning receipt; execution also requires --approve unbound-reviewed-workspace.",
    )
    reviewed_model_run.add_argument("--approve", action="append", default=[])

    model_goal = subparsers.add_parser(
        "run-model-goal",
        help="Replan and execute model-authored workflows until verification passes, optionally in one detached worker.",
    )
    model_goal_task = model_goal.add_mutually_exclusive_group(required=True)
    model_goal_task.add_argument("--task", help="Untrusted task text for adaptive model planning.")
    model_goal_task.add_argument("--task-file", type=Path, help="File containing untrusted task text.")
    model_goal.add_argument(
        "--check-command-json",
        help="Verifier argv as a JSON string array; combine with model criteria for an ordered chain.",
    )
    model_goal_model_verifier = model_goal.add_mutually_exclusive_group()
    model_goal_model_verifier.add_argument("--check-prompt", help="Strict criteria for a read-only Codex verifier.")
    model_goal_model_verifier.add_argument("--check-prompt-file", type=Path, help="Workspace-relative verifier criteria file.")
    model_goal.add_argument("--workspace", type=Path, default=Path("."))
    model_goal.add_argument("--runs-dir", type=Path)
    model_goal.add_argument("--goals-dir", type=Path)
    model_goal.add_argument("--goal-id")
    model_goal.add_argument("--resume-goal", type=Path, help="Resume a matching adaptive model goal with remaining iterations.")
    model_goal.add_argument("--name", help="Optional fixed lowercase workflow name for every iteration.")
    model_goal.add_argument("--planner-model", help="Optional Codex model override for each planning call.")
    model_goal.add_argument(
        "--planner-effort",
        choices=sorted(MODEL_WORKFLOW_REASONING_EFFORTS),
        default=DEFAULT_MODEL_WORKFLOW_REASONING_EFFORT,
        help="Codex planning reasoning effort for every iteration.",
    )
    model_goal.add_argument("--check-model", help="Optional Codex model override for model verification.")
    model_goal.add_argument("--write-capable", action="store_true")
    model_goal.add_argument("--max-iterations", type=int, default=3)
    model_goal.add_argument(
        "--dynamic-interval",
        action="store_true",
        help="Let the model-only verifier choose a persisted 1-60 minute delay before each retry without an extra provider call.",
    )
    model_goal.add_argument(
        "--min-delay-minutes",
        type=int,
        help="Minimum model-selected retry delay; requires --dynamic-interval (default: 1).",
    )
    model_goal.add_argument(
        "--max-delay-minutes",
        type=int,
        help="Maximum model-selected retry delay; requires --dynamic-interval (default: 60).",
    )
    model_goal.add_argument(
        "--monitor-command-json",
        help="Read-only argv that wakes a dynamic wait on its first non-empty stdout line; the selected delay remains the deadline.",
    )
    model_goal.add_argument("--max-workers", type=int, default=1)
    model_goal.add_argument("--max-items", type=int, default=MAX_MODEL_WORKFLOW_ITEMS)
    model_goal.add_argument("--max-steps", type=int, default=32)
    model_goal.add_argument("--planning-timeout-seconds", type=int, default=900)
    model_goal.add_argument("--planning-output-limit-bytes", type=int, default=DEFAULT_OUTPUT_LIMIT_BYTES)
    model_goal.add_argument("--check-timeout-seconds", type=int, default=120)
    model_goal.add_argument("--output-limit-bytes", type=int, default=DEFAULT_OUTPUT_LIMIT_BYTES)
    model_goal.add_argument(
        "--fresh-planner-session-per-iteration",
        action="store_true",
        help="Disable default same-session Codex planning continuity for each retry.",
    )
    model_goal.add_argument(
        "--background",
        action="store_true",
        help="Run all adaptive planning, execution, and verification cycles in one visible detached worker.",
    )
    model_goal.add_argument(
        "--desktop-notify",
        action="store_true",
        help="Send one content-free desktop notification when the adaptive goal finishes.",
    )
    model_goal.add_argument("--allow-writes", action="store_true")
    model_goal.add_argument("--allow-destructive", action="store_true")
    model_goal.add_argument("--allow-network", action="store_true")
    model_goal.add_argument("--allow-agent", action="store_true")
    model_goal.add_argument("--allow-parallel", action="store_true")
    model_goal.add_argument("--approve", action="append", default=[])

    auto = subparsers.add_parser(
        "auto",
        help="Route a task to direct execution or verifier-driven progressive decomposition.",
    )
    auto_task = auto.add_mutually_exclusive_group(required=True)
    auto_task.add_argument("--task", help="Untrusted task text for automatic orchestration.")
    auto_task.add_argument("--task-file", type=Path, help="File containing untrusted task text.")
    auto.add_argument("--strategy", choices=sorted(AUTO_STRATEGIES), default="auto")
    auto.add_argument(
        "--check-command-json",
        help="Adaptive verifier argv; combine with model criteria to short-circuit tests before model review.",
    )
    auto_model_verifier = auto.add_mutually_exclusive_group()
    auto_model_verifier.add_argument("--check-prompt", help="Strict criteria for an adaptive read-only Codex verifier.")
    auto_model_verifier.add_argument("--check-prompt-file", type=Path, help="Workspace-relative adaptive verifier criteria file.")
    auto.add_argument("--workspace", type=Path, default=Path("."))
    auto.add_argument("--runs-dir", type=Path)
    auto.add_argument("--goals-dir", type=Path)
    auto.add_argument("--output", type=Path, help="Optional workflow draft path for a one-shot workflow route.")
    auto.add_argument("--receipt", type=Path, help="Auto-routing receipt path; defaults to external Conductor state.")
    auto.add_argument("--run-id", help="Optional run id for a one-shot workflow route.")
    auto.add_argument("--goal-id", help="Optional goal id for the adaptive route.")
    auto.add_argument("--resume-goal", type=Path, help="Resume a matching adaptive goal artifact.")
    auto.add_argument("--name", help="Optional lowercase workflow name.")
    auto.add_argument("--planner-model", help="Optional Codex model override for planning or the direct worker.")
    auto.add_argument(
        "--planner-effort",
        choices=sorted(AUTO_REASONING_EFFORTS),
        default=AUTO_REASONING_EFFORT,
        help=(
            "Effort for planning or the direct worker. auto uses medium only for bounded verified tasks without "
            "broad or sensitive complexity markers; all other tasks use high."
        ),
    )
    auto.add_argument("--check-model", help="Optional Codex model override for adaptive verification.")
    auto.add_argument("--max-iterations", type=int, default=3)
    auto.add_argument(
        "--max-workers",
        type=int,
        help="Worker ceiling; with a verifier, values above one try direct first and unlock decomposition only after failure.",
    )
    auto.add_argument(
        "--native-agent-threads",
        type=int,
        help=(
            "Depth-1 concurrently open child-thread ceiling for --strategy native "
            "(parent excluded; default: %d, range: %d-%d)."
            % (
                DEFAULT_CODEX_NATIVE_AGENT_THREADS,
                MIN_CODEX_NATIVE_AGENT_THREADS,
                MAX_CODEX_NATIVE_AGENT_THREADS,
            )
        ),
    )
    auto.add_argument("--max-items", type=int, default=MAX_MODEL_WORKFLOW_ITEMS)
    auto.add_argument("--max-steps", type=int, default=32)
    auto.add_argument("--planning-timeout-seconds", type=int, default=900)
    auto.add_argument("--planning-output-limit-bytes", type=int, default=DEFAULT_OUTPUT_LIMIT_BYTES)
    auto.add_argument("--check-timeout-seconds", type=int, default=120)
    auto.add_argument("--output-limit-bytes", type=int, default=DEFAULT_OUTPUT_LIMIT_BYTES)
    auto_mode = auto.add_mutually_exclusive_group()
    auto_mode.add_argument(
        "--plan-only",
        action="store_true",
        help="Compile and preflight without execution; the direct route uses no provider call.",
    )
    auto_mode.add_argument(
        "--background",
        action="store_true",
        help="Run the approved workflow or adaptive goal in one visible detached worker.",
    )
    auto.add_argument(
        "--desktop-notify",
        action="store_true",
        help="Send one content-free desktop notification when real automatic execution finishes.",
    )
    auto.add_argument(
        "--fresh-planner-session-per-iteration",
        action="store_true",
        help="Disable same-session planning continuity for adaptive retries.",
    )
    auto.add_argument("--allow-writes", action="store_true")
    auto.add_argument("--allow-destructive", action="store_true")
    auto.add_argument("--allow-network", action="store_true")
    auto.add_argument("--allow-agent", action="store_true")
    auto.add_argument("--allow-parallel", action="store_true")
    auto.add_argument("--approve", action="append", default=[])

    background_goal_status = subparsers.add_parser(
        "status-background-model-goal",
        help="Print detached adaptive model-goal worker status and liveness.",
    )
    background_goal_status.add_argument("metadata_json", type=Path)

    background_goal_wait = subparsers.add_parser(
        "wait-background-model-goal",
        help="Wait for a detached adaptive model goal to finish.",
    )
    background_goal_wait.add_argument("metadata_json", type=Path)
    background_goal_wait.add_argument("--timeout-seconds", type=int, default=3600)
    background_goal_wait.add_argument("--poll-interval-ms", type=int, default=250)

    background_goal_terminate = subparsers.add_parser(
        "terminate-background-model-goal",
        help="Signal a validated active detached adaptive model-goal worker.",
    )
    background_goal_terminate.add_argument("metadata_json", type=Path)
    background_goal_terminate.add_argument(
        "--force",
        action="store_true",
        help="Escalate to SIGKILL only if graceful child cleanup does not finish within the bounded grace period.",
    )
    return parser


def _add_inline_verified_apply_args(parser) -> None:
    parser.add_argument(
        "--apply-verified-stage",
        action="store_true",
        help=(
            "After strict evidence is written, transactionally apply the accepted stage with no additional "
            "provider or verifier call. Requires verified-stage-apply approval."
        ),
    )
    parser.add_argument(
        "--apply-receipt",
        type=Path,
        help="External transaction receipt path for --apply-verified-stage.",
    )
    parser.add_argument(
        "--allow-destructive",
        action="store_true",
        help="Permit verified source-file deletions only with verified-stage-delete approval.",
    )


def _add_saved_workflow_scope_args(parser) -> None:
    parser.add_argument(
        "--project-root",
        type=Path,
        help="Resolve nested project saved workflows up to this root; default is the workspace only.",
    )
    parser.add_argument(
        "--include-personal",
        action="store_true",
        help="Also discover personal saved workflows under ~/.claude/workflows or --personal-workflows-dir.",
    )
    parser.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--personal-workflows-dir",
        type=Path,
        help="Override the personal saved workflow directory for discovery.",
    )


def _add_saved_workflow_args(parser) -> None:
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Render a saved workflow placeholder like {{args.KEY}} before validation and run/export.",
    )


def _add_cloud_read_args(parser) -> None:
    parser.add_argument("task_id")
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--output-limit-bytes", type=int, default=DEFAULT_OUTPUT_LIMIT_BYTES)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--approve", action="append", default=[])


def _add_parity_campaign_args(parser) -> None:
    parser.add_argument("--run-id", default="matched-parity-campaign")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--artifacts-dir", default="parity-campaign-artifacts")
    parser.add_argument("--codex-model", default="gpt-5.6-sol")
    parser.add_argument(
        "--codex-effort",
        choices=sorted(AUTO_REASONING_EFFORTS),
        default=AUTO_REASONING_EFFORT,
    )


def _add_auto_topology_campaign_args(parser) -> None:
    parser.add_argument("--run-id", default="matched-auto-topology-campaign")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--artifacts-dir", default="auto-topology-campaign-artifacts")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--effort", choices=sorted(CODEX_REASONING_EFFORTS), default="ultra")
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--parallel-workers", type=int, default=4)
    parser.add_argument("--fixture-root", type=Path)


def _add_readonly_parity_campaign_args(parser) -> None:
    parser.add_argument(
        "--run-id",
        help="Campaign id; defaults to a name matching the selected budget profile.",
    )
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--artifacts-dir", default="readonly-parity-campaign-artifacts")
    parser.add_argument("--codex-model", default="gpt-5.6-sol")
    parser.add_argument("--codex-effort", choices=sorted(CODEX_REASONING_EFFORTS), default="ultra")
    parser.add_argument(
        "--budget-profile",
        choices=["matched-v1", "product-v1"],
        default="matched-v1",
        help="Codex caps: matched-v1 uses 18k/18k; product-v1 uses 20k serial and 42k native.",
    )
    parser.add_argument("--fixture-root", type=Path)


def _add_live_parity_runbook_args(parser) -> None:
    parser.add_argument("--run-id", default=DEFAULT_LIVE_PARITY_RUN_ID)
    parser.add_argument("--report-dir")
    parser.add_argument("--workflow-dir")


def _add_evidence_bundle_args(parser) -> None:
    parser.add_argument("--runs-dir", type=Path)
    parser.add_argument("--benchmarks-dir", type=Path)
    parser.add_argument("--goals-dir", type=Path)
    parser.add_argument("--routines-dir", type=Path)
    parser.add_argument("--max-runs", type=int, default=DEFAULT_MAX_RUNS)
    parser.add_argument("--max-reports", type=int, default=DEFAULT_MAX_REPORTS)
    parser.add_argument("--max-goals", type=int, default=DEFAULT_MAX_GOALS)
    parser.add_argument("--max-routines", type=int, default=50)


def _add_routine_service_install_args(parser) -> None:
    parser.add_argument("--routines-dir", type=Path)
    parser.add_argument("--routine-manifest", action="append", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--platform", choices=["auto", "launchd", "systemd"], default="auto")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--max-routines", type=int, default=50)
    parser.add_argument("--output-limit-bytes", type=int, default=DEFAULT_OUTPUT_LIMIT_BYTES)
    parser.add_argument("--grant-days", type=int, default=30)
    parser.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--allow-service-install", action="store_true")
    parser.add_argument("--allow-writes", action="store_true")
    parser.add_argument("--allow-destructive", action="store_true")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--allow-agent", action="store_true")
    parser.add_argument("--allow-parallel", action="store_true")
    parser.add_argument("--approve", action="append", default=[])
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Stage the grant and descriptor under external Conductor state without writing OS service configuration.",
    )


def main(argv=None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if raw_argv and raw_argv[0] == "_background-worker":
        if len(raw_argv) not in {2, 3} or (len(raw_argv) == 3 and raw_argv[2] != "--desktop-notify"):
            print(
                "ERROR: internal background worker requires one run directory and optional --desktop-notify",
                file=sys.stderr,
            )
            return 2
        try:
            return run_background_worker(
                Path(raw_argv[1]),
                desktop_notify=len(raw_argv) == 3,
            )
        except (ConductorError, FileNotFoundError) as exc:
            print("ERROR: %s" % redact_text(str(exc)), file=sys.stderr)
            return 2
    if raw_argv and raw_argv[0] == "_background-model-goal-worker":
        if len(raw_argv) not in {2, 3} or (len(raw_argv) == 3 and raw_argv[2] != "--desktop-notify"):
            print(
                "ERROR: internal background model goal worker requires one metadata path and optional --desktop-notify",
                file=sys.stderr,
            )
            return 2
        try:
            return run_background_model_goal_worker(
                Path(raw_argv[1]),
                desktop_notify=len(raw_argv) == 3,
            )
        except (ConductorError, FileNotFoundError) as exc:
            print("ERROR: %s" % redact_text(str(exc)), file=sys.stderr)
            return 2
    if raw_argv and raw_argv[0] == "_background-routine-supervisor-worker":
        if len(raw_argv) != 2:
            print("ERROR: internal background routine supervisor requires one metadata path", file=sys.stderr)
            return 2
        try:
            return run_background_routine_supervisor_worker(Path(raw_argv[1]))
        except (ConductorError, FileNotFoundError) as exc:
            print("ERROR: %s" % redact_text(str(exc)), file=sys.stderr)
            return 2
    if raw_argv and raw_argv[0] == "_routine-service-worker":
        if len(raw_argv) != 2:
            print("ERROR: internal routine service worker requires one grant path", file=sys.stderr)
            return 2
        try:
            return run_routine_service_worker(Path(raw_argv[1]))
        except (ConductorError, FileNotFoundError) as exc:
            print("ERROR: %s" % redact_text(str(exc)), file=sys.stderr)
            return 2
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    _apply_default_storage_paths(args)
    try:
        if args.command == "validate":
            return _validate(args)
        if args.command == "validate-agent-profile":
            return _validate_agent_profile(args)
        if args.command == "inspect-agent-profile":
            return _inspect_agent_profile(args)
        if args.command == "write-agent-profile":
            return _write_agent_profile(args)
        if args.command == "bind-agent-profile":
            return _bind_agent_profile(args)
        if args.command == "validate-agent-memory":
            return _validate_agent_memory(args)
        if args.command == "validate-agent-team-state":
            return _validate_agent_team_state(args)
        if args.command == "inspect-agent-team-state":
            return _inspect_agent_team_state(args)
        if args.command == "validate-agent-team-operator-inbox":
            return _validate_agent_team_operator_inbox(args)
        if args.command == "inspect-agent-team-operator-inbox":
            return _inspect_agent_team_operator_inbox(args)
        if args.command == "validate-agent-team-operator-chat":
            return _validate_agent_team_operator_chat(args)
        if args.command == "inspect-agent-team-operator-chat":
            return _inspect_agent_team_operator_chat(args)
        if args.command == "validate-agent-team-transcript":
            return _validate_agent_team_transcript(args)
        if args.command == "inspect-agent-team-transcript":
            return _inspect_agent_team_transcript(args)
        if args.command == "validate-agent-team-interruption":
            return _validate_agent_team_interruption(args)
        if args.command == "inspect-agent-team-interruption":
            return _inspect_agent_team_interruption(args)
        if args.command == "validate-agent-team-plan":
            return _validate_agent_team_plan(args)
        if args.command == "validate-agent-team-plan-review":
            return _validate_agent_team_plan_review(args)
        if args.command == "validate-agent-team-plan-approval":
            return _validate_agent_team_plan_approval(args)
        if args.command == "inspect-agent-team-plan-approval":
            return _inspect_agent_team_plan_approval(args)
        if args.command == "queue-team-task":
            return _queue_team_task(args)
        if args.command == "list-team-inbox":
            return _list_team_inbox(args)
        if args.command == "list-team-questions":
            return _list_team_questions(args)
        if args.command == "read-team-question":
            return _read_team_question(args)
        if args.command == "reply-team-question":
            return _reply_team_question(args)
        if args.command == "list-team-plans":
            return _list_team_plans(args)
        if args.command == "read-team-plan":
            return _read_team_plan(args)
        if args.command == "list-team-transcripts":
            return _list_team_transcripts(args)
        if args.command == "read-team-transcript":
            return _read_team_transcript(args)
        if args.command == "team-console":
            return _team_console(args)
        if args.command == "review-team-plan":
            return _review_team_plan(args)
        if args.command == "validate-agent-team-merge-ledger":
            return _validate_agent_team_merge_ledger(args)
        if args.command == "inspect-agent-team-merge-ledger":
            return _inspect_agent_team_merge_ledger(args)
        if args.command == "validate-agent-map-packet-terminal":
            return _validate_agent_map_packet_terminal(args)
        if args.command == "inspect-agent-map-packet-terminal":
            return _inspect_agent_map_packet_terminal(args)
        if args.command == "validate-codex-step-terminal":
            return _validate_codex_step_terminal(args)
        if args.command == "inspect-codex-step-terminal":
            return _inspect_codex_step_terminal(args)
        if args.command == "validate-codex-progress":
            return _validate_codex_progress(args)
        if args.command == "inspect-codex-progress":
            return _inspect_codex_progress(args)
        if args.command == "validate-agent-team-turn-completion":
            return _validate_agent_team_turn_completion(args)
        if args.command == "inspect-agent-team-turn-completion":
            return _inspect_agent_team_turn_completion(args)
        if args.command == "validate-agent-team-turn-terminal":
            return _validate_agent_team_turn_terminal(args)
        if args.command == "inspect-agent-team-turn-terminal":
            return _inspect_agent_team_turn_terminal(args)
        if args.command == "validate-agent-team-merge-intent":
            return _validate_agent_team_merge_intent(args)
        if args.command == "inspect-agent-team-merge-intent":
            return _inspect_agent_team_merge_intent(args)
        if args.command == "validate-agent-team-merge-transaction":
            return _validate_agent_team_merge_transaction(args)
        if args.command == "inspect-agent-team-merge-transaction":
            return _inspect_agent_team_merge_transaction(args)
        if args.command == "validate-agent-team-merge-recovery":
            return _validate_agent_team_merge_recovery(args)
        if args.command == "inspect-agent-team-merge-recovery":
            return _inspect_agent_team_merge_recovery(args)
        if args.command == "validate-agent-team-quality-retry":
            return _validate_agent_team_quality_retry(args)
        if args.command == "inspect-agent-team-quality-retry":
            return _inspect_agent_team_quality_retry(args)
        if args.command == "list-agent-memory":
            return _list_agent_memory(args)
        if args.command == "inspect-agent-memory":
            return _inspect_agent_memory(args)
        if args.command == "remember-agent-memory":
            return _remember_agent_memory(args)
        if args.command == "forget-agent-memory":
            return _forget_agent_memory(args)
        if args.command == "validate-saved-workflows":
            return _validate_saved_workflows(args)
        if args.command == "validate-model-workflow-receipt":
            return _validate_model_workflow_receipt(args)
        if args.command == "validate-model-workflow-run-receipt":
            return _validate_model_workflow_run_receipt(args)
        if args.command == "validate-auto-receipt":
            return _validate_auto_receipt(args)
        if args.command == "list-auto":
            return _list_auto(args)
        if args.command == "status-auto":
            return _status_auto(args)
        if args.command == "validate-background-run":
            return _validate_background_run(args)
        if args.command == "validate-desktop-notification":
            return _validate_desktop_notification(args)
        if args.command == "validate-background-model-goal":
            return _validate_background_model_goal(args)
        if args.command == "validate-background-routine-supervisor":
            return _validate_background_routine_supervisor(args)
        if args.command == "inspect":
            return _inspect(args)
        if args.command == "run":
            return _run(args)
        if args.command == "run-goal":
            return _run_goal(args)
        if args.command == "status-goal":
            return _status_goal(args)
        if args.command == "list-goals":
            return _list_goals(args)
        if args.command == "prepare-run":
            return _prepare_run(args)
        if args.command == "start-background-run":
            return _start_background_run(args)
        if args.command == "status-background-run":
            return _status_background_run(args)
        if args.command == "wait-background-run":
            return _wait_background_run(args)
        if args.command == "status-background-model-goal":
            return _status_background_model_goal(args)
        if args.command == "wait-background-model-goal":
            return _wait_background_model_goal(args)
        if args.command == "terminate-background-model-goal":
            return _terminate_background_model_goal(args)
        if args.command == "status":
            return _status(args)
        if args.command == "list":
            return _list(args)
        if args.command == "list-runs":
            return _list_runs(args)
        if args.command == "diagnose-run":
            return _diagnose_run(args)
        if args.command == "run-console":
            return _run_console(args)
        if args.command == "list-saved-workflows":
            return _list_saved_workflows(args)
        if args.command == "inspect-saved-workflow":
            return _inspect_saved_workflow(args)
        if args.command == "export-saved-workflow":
            return _export_saved_workflow(args)
        if args.command == "run-saved-workflow":
            return _run_saved_workflow(args)
        if args.command == "init":
            return _init(args)
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "schema":
            return _schema(args)
        if args.command == "write-schema":
            return _write_schema(args)
        if args.command == "benchmark":
            return _benchmark(args)
        if args.command == "validate-benchmark-report":
            return _validate_benchmark_report(args)
        if args.command == "score-benchmark-report":
            return _score_benchmark_report(args)
        if args.command == "validate-benchmark-quality-evidence":
            return _validate_benchmark_quality_evidence(args)
        if args.command == "validate-benchmark-score-input":
            return _validate_benchmark_score_input(args)
        if args.command == "run-claude-readonly-task":
            return _run_claude_readonly_task(args)
        if args.command == "validate-claude-provider-evidence":
            return _validate_claude_provider_evidence(args)
        if args.command == "run-claude-staged-task":
            return _run_claude_staged_task(args)
        if args.command == "validate-claude-staged-evidence":
            return _validate_claude_staged_evidence(args)
        if args.command == "run-codex-readonly-task":
            return _run_codex_readonly_task(args)
        if args.command == "validate-codex-provider-evidence":
            return _validate_codex_provider_evidence(args)
        if args.command == "run-codex-staged-task":
            return _run_codex_staged_task(args)
        if args.command == "validate-codex-staged-evidence":
            return _validate_codex_staged_evidence(args)
        if args.command == "run-codex-staged-repair":
            return _run_codex_staged_repair(args)
        if args.command == "validate-codex-staged-repair":
            return _validate_codex_staged_repair(args)
        if args.command == "validate-codex-staged-repair-state":
            return _validate_codex_staged_repair_state(args)
        if args.command == "apply-verified-stage":
            return _apply_verified_stage(args)
        if args.command == "validate-staged-apply":
            return _validate_staged_apply(args)
        if args.command == "compare":
            return _compare(args)
        if args.command == "validate-comparison":
            return _validate_comparison(args)
        if args.command == "validate-parity-tasks":
            return _validate_parity_tasks(args)
        if args.command == "plan-parity-campaign":
            return _plan_parity_campaign(args)
        if args.command == "write-parity-campaign":
            return _write_parity_campaign(args)
        if args.command == "validate-parity-campaign":
            return _validate_parity_campaign(args)
        if args.command == "status-parity-campaign":
            return _status_parity_campaign(args)
        if args.command == "run-parity-campaign-trial":
            return _run_parity_campaign_trial(args)
        if args.command == "plan-auto-topology-campaign":
            return _plan_auto_topology_campaign(args)
        if args.command == "write-auto-topology-campaign":
            return _write_auto_topology_campaign(args)
        if args.command == "validate-auto-topology-campaign":
            return _validate_auto_topology_campaign(args)
        if args.command == "status-auto-topology-campaign":
            return _status_auto_topology_campaign(args)
        if args.command == "run-auto-topology-arm":
            return _run_auto_topology_arm(args)
        if args.command == "plan-readonly-parity-campaign":
            return _plan_readonly_parity_campaign(args)
        if args.command == "write-readonly-parity-campaign":
            return _write_readonly_parity_campaign(args)
        if args.command == "validate-readonly-parity-campaign":
            return _validate_readonly_parity_campaign(args)
        if args.command == "status-readonly-parity-campaign":
            return _status_readonly_parity_campaign(args)
        if args.command == "run-readonly-parity-arm":
            return _run_readonly_parity_arm(args)
        if args.command == "plan-live-parity-runbook":
            return _plan_live_parity_runbook(args)
        if args.command == "write-live-parity-runbook":
            return _write_live_parity_runbook(args)
        if args.command == "validate-live-parity-runbook":
            return _validate_live_parity_runbook(args)
        if args.command == "validate-run-manifest":
            return _validate_run_manifest(args)
        if args.command == "write-run-manifest":
            return _write_run_manifest(args)
        if args.command == "evidence-bundle":
            return _evidence_bundle(args)
        if args.command == "write-evidence-bundle":
            return _write_evidence_bundle(args)
        if args.command == "validate-evidence-bundle":
            return _validate_evidence_bundle(args)
        if args.command == "validate-routine-manifest":
            return _validate_routine_manifest(args)
        if args.command == "validate-routine-controls":
            return _validate_routine_controls(args)
        if args.command == "validate-routine-state":
            return _validate_routine_state(args)
        if args.command == "validate-routine-service-grant":
            return _validate_routine_service_grant(args)
        if args.command == "validate-routine-service-state":
            return _validate_routine_service_state(args)
        if args.command == "write-routine-manifest":
            return _write_routine_manifest(args)
        if args.command == "list-routines":
            return _list_routines(args)
        if args.command == "status-routine":
            return _status_routine(args)
        if args.command in {"run-routine", "run-routine-now"}:
            return _run_routine(args)
        if args.command == "pause-routine":
            return _pause_routine(args)
        if args.command == "resume-routine":
            return _resume_routine(args)
        if args.command == "update-routine-schedule":
            return _update_routine_schedule(args)
        if args.command == "supervise-routines":
            return _supervise_routines(args)
        if args.command == "status-background-routine-supervisor":
            return _status_background_routine_supervisor(args)
        if args.command == "wait-background-routine-supervisor":
            return _wait_background_routine_supervisor(args)
        if args.command == "terminate-background-routine-supervisor":
            return _terminate_background_routine_supervisor(args)
        if args.command in {"install-routine-service", "update-routine-service"}:
            return _install_routine_service(args)
        if args.command == "status-routine-service":
            return _status_routine_service(args)
        if args.command == "uninstall-routine-service":
            return _uninstall_routine_service(args)
        if args.command == "cloud-submit":
            return _cloud_submit(args)
        if args.command == "cloud-status":
            return _cloud_status(args)
        if args.command == "cloud-review":
            return _cloud_review(args)
        if args.command == "cloud-apply":
            return _cloud_apply(args)
        if args.command == "dashboard":
            return _dashboard(args)
        if args.command == "serve":
            return _serve(args)
        if args.command == "retry-step":
            return _retry_step(args)
        if args.command == "reset-step":
            return _reset_step(args)
        if args.command == "retry-packet":
            return _retry_packet(args)
        if args.command == "recover-run":
            return _recover_run(args)
        if args.command == "skip-step":
            return _skip_step(args)
        if args.command == "pause-run":
            return _pause_run(args)
        if args.command == "resume-run":
            return _resume_run(args)
        if args.command == "stop-run":
            return _stop_run(args)
        if args.command == "restart-run":
            return _restart_run(args)
        if args.command == "terminate-run":
            return _terminate_run(args)
        if args.command == "save-run-workflow":
            return _save_run_workflow(args)
        if args.command == "record-usage":
            return _record_usage(args)
        if args.command == "import-usage":
            return _import_usage(args)
        if args.command == "import-session-trace":
            return _import_session_trace(args)
        if args.command == "plan-workflow":
            return _plan_workflow(args)
        if args.command == "plan-model-workflow":
            return _plan_model_workflow(args)
        if args.command == "run-model-workflow":
            return _run_model_workflow(args)
        if args.command == "run-reviewed-model-workflow":
            return _run_reviewed_model_workflow(args)
        if args.command == "run-model-goal":
            return _run_model_goal(args)
        if args.command == "auto":
            return _auto(args)
    except ConductorError as exc:
        print("ERROR: %s" % redact_text(str(exc)), file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print("ERROR: %s" % redact_text(str(exc)), file=sys.stderr)
        return 2
    return 0


def _apply_default_storage_paths(args) -> None:
    workspace = getattr(args, "workspace", None)
    if hasattr(args, "runs_dir") and args.runs_dir is None:
        args.runs_dir = default_runs_dir(workspace)
    if hasattr(args, "goals_dir") and args.goals_dir is None:
        args.goals_dir = default_goals_dir(workspace)
    if hasattr(args, "benchmarks_dir") and args.benchmarks_dir is None:
        args.benchmarks_dir = default_benchmarks_dir(workspace)
    if hasattr(args, "routines_dir") and args.routines_dir is None:
        args.routines_dir = default_routines_dir(workspace)
    if hasattr(args, "generated_dir") and args.generated_dir is None:
        args.generated_dir = default_generated_dir(workspace)
    if hasattr(args, "auto_dir") and args.auto_dir is None:
        generated_dir = getattr(args, "generated_dir", None) or default_generated_dir(workspace)
        args.auto_dir = Path(generated_dir) / "auto"
    if hasattr(args, "cloud_dir") and args.cloud_dir is None:
        args.cloud_dir = default_cloud_dir(workspace)
    if getattr(args, "command", None) == "dashboard" and args.output is None:
        args.output = default_dashboard_path(workspace)
    if hasattr(args, "report_dir") and args.report_dir is None:
        args.report_dir = default_live_parity_report_dir(workspace)
    if hasattr(args, "workflow_dir") and args.workflow_dir is None:
        args.workflow_dir = default_live_parity_workflow_dir(workspace)


def _validate(args) -> int:
    files = iter_workflow_files(args.paths)
    if not files:
        raise ValidationError("no workflow files found")
    for path in files:
        load_workflow(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_agent_profile(args) -> int:
    files = iter_agent_profile_files(args.paths)
    if not files:
        raise ValidationError("no agent profile files found")
    for path in files:
        load_agent_profile(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_profile(args) -> int:
    profile = load_agent_profile(args.profile)
    summary = agent_profile_summary(profile)
    if args.workspace is not None:
        summary["skill_resources"] = agent_profile_skill_resource_summary(
            profile,
            args.workspace,
        )
        summary["prompt_compaction"] = agent_profile_prompt_summary(
            profile,
            args.workspace,
        )
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Agent profile: %s" % redact_text(summary["name"]))
    print("Description: %s" % redact_text(summary["description"] or "n/a"))
    print("Sandbox: %s" % summary["sandbox"])
    print("Tool policy: %s" % summary["tool_policy"])
    print(
        "Tool output ceiling: %s"
        % (
            summary["tool_output_token_limit"]
            if summary["tool_output_token_limit"] is not None
            else "runtime/default"
        )
    )
    if summary["tool_policy"] == "restricted":
        print("Codex CLI range: %s" % restricted_codex_version_range())
        print("Command rules: %d" % summary["command_rule_count"])
        print("Skill script routes: %d" % summary["skill_script_rule_count"])
        print("Skill MCP dependencies: %d" % summary["skill_mcp_dependency_count"])
        print("Skill MCP tools: %d" % summary["skill_mcp_tool_count"])
        print(
            "Skill MCP HTTPS dependencies: %d"
            % summary["skill_mcp_http_dependency_count"]
        )
        print(
            "Skill MCP stdio dependencies: %d"
            % summary["skill_mcp_stdio_dependency_count"]
        )
        print(
            "Authenticated Skill MCP dependencies: %d"
            % summary["skill_mcp_auth_dependency_count"]
        )
        print(
            "Skill MCP credential environment variables: %d"
            % summary["skill_mcp_auth_env_var_count"]
        )
        print(
            "Skill MCP environment-backed headers: %d"
            % summary["skill_mcp_auth_header_count"]
        )
        print(
            "Skill MCP dependencies sha256: %s"
            % (summary["skill_mcp_dependencies_sha256"] or "none")
        )
        print("Apply patch: %s" % ("allowed" if summary["allow_apply_patch"] else "denied"))
        print("Command policy sha256: %s" % summary["command_policy_sha256"])
    print("Risk floor: %s" % summary["risk"])
    print("Model: %s" % redact_text(summary["model"] or "workflow/default"))
    print("Effort: %s" % (summary["effort"] or "workflow/default"))
    print("Token ceiling: %s" % (summary["max_tokens"] if summary["max_tokens"] is not None else "workflow/default"))
    print("Worker ceiling: %s" % (summary["max_workers"] if summary["max_workers"] is not None else "workflow/default"))
    print("Instructions: %s bytes, sha256 %s" % (summary["instructions_bytes"], summary["instructions_sha256"]))
    print("Context files: %d" % len(summary["context_files"]))
    print("Skill entrypoints: %d" % len(summary["skills"]))
    if summary.get("skill_resources") is not None:
        resources = summary["skill_resources"]
        print(
            "Skill resources: %d files, %d bytes, set %s"
            % (
                resources["resource_count"],
                resources["resource_bytes"],
                resources["skill_set_sha256"] or "none",
            )
        )
    if summary.get("prompt_compaction") is not None:
        prompt = summary["prompt_compaction"]
        print(
            "Static profile prompt: %d bytes, sha256 %s"
            % (prompt["full_static_bytes"], prompt["full_static_sha256"])
        )
        print(
            "Resumed static profile prompt: %d bytes (%s, %d bytes saved)"
            % (
                prompt["resume_static_bytes"],
                prompt["resume_static_mode"],
                prompt["resume_static_saved_bytes"],
            )
        )
    memory = summary["memory"]
    if memory is None:
        print("Memory: disabled")
    else:
        print(
            "Memory: %s, %s selection, %d entries, %d bytes"
            % (
                memory["mode"],
                memory["selection"],
                memory["max_entries"],
                memory["max_bytes"],
            )
        )
    return 0


def _write_agent_profile(args) -> int:
    instructions = args.instructions
    if args.instructions_file is not None:
        instructions = read_regular_text_file_no_follow(
            args.instructions_file,
            "agent profile instructions",
            MAX_AGENT_PROFILE_INSTRUCTIONS_BYTES,
        )
    profile = {
        "schema": AGENT_PROFILE_SCHEMA,
        "name": args.name,
        "description": args.description,
        "instructions": instructions,
        "sandbox": args.sandbox,
        "tool_policy": args.tool_policy,
        "risk": args.risk,
    }
    optional = {
        "model": args.model,
        "effort": args.effort,
        "max_tokens": args.max_tokens,
        "max_workers": args.max_workers,
        "timeout_seconds": args.timeout_seconds,
        "tool_output_token_limit": args.tool_output_token_limit,
    }
    for key, value in optional.items():
        if value is not None:
            profile[key] = value
    command_rules = []
    for field, values in [
        ("argv", args.allow_command_json),
        ("argv_prefix", args.allow_command_prefix_json),
    ]:
        for raw in values:
            try:
                argv = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValidationError("--allow-command JSON is invalid: %s" % exc) from exc
            command_rules.append({field: argv})
    if command_rules:
        profile["command_allowlist"] = command_rules
    if args.allow_apply_patch:
        profile["allow_apply_patch"] = True
    if args.context_file:
        profile["context_files"] = list(args.context_file)
    if args.skill:
        profile["skills"] = list(args.skill)
    if args.allow_skill_script_json:
        skill_scripts = []
        for raw in args.allow_skill_script_json:
            try:
                rule = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValidationError("--allow-skill-script-json is invalid: %s" % exc) from exc
            skill_scripts.append(rule)
        profile["skill_scripts"] = skill_scripts
    if args.allow_skill_mcp_json:
        skill_mcp_dependencies = []
        for raw in args.allow_skill_mcp_json:
            try:
                dependency = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValidationError("--allow-skill-mcp-json is invalid: %s" % exc) from exc
            skill_mcp_dependencies.append(dependency)
        profile["skill_mcp_dependencies"] = skill_mcp_dependencies
    memory_settings_requested = (
        args.memory_selection is not None
        or args.memory_max_entries is not None
        or args.memory_max_bytes is not None
    )
    if memory_settings_requested and args.memory_mode is None:
        raise ValidationError("agent profile memory settings require --memory-mode")
    if args.memory_mode is not None:
        profile["memory"] = {"mode": args.memory_mode}
        if args.memory_selection is not None:
            profile["memory"]["selection"] = args.memory_selection
        if args.memory_max_entries is not None:
            profile["memory"]["max_entries"] = args.memory_max_entries
        if args.memory_max_bytes is not None:
            profile["memory"]["max_bytes"] = args.memory_max_bytes
    validate_agent_profile(profile, source=str(args.output))
    write_agent_profile(profile, args.output)
    print("Agent profile written: %s" % redact_text(str(args.output)))
    return 0


def _bind_agent_profile(args) -> int:
    workflow = load_workflow(args.workflow)
    profile = load_agent_profile(args.profile)
    if args.output.exists() or args.output.is_symlink():
        raise ValidationError("bound workflow output already exists: %s" % args.output)
    selected = list(args.step)
    if len(selected) != len(set(selected)):
        raise ValidationError("--step values must be unique")
    bound = json.loads(
        json.dumps(
            {key: value for key, value in workflow.items() if not key.startswith("_")},
            sort_keys=True,
        )
    )
    profiles = bound.setdefault("agent_profiles", [])
    existing = next((item for item in profiles if item.get("name") == profile["name"]), None)
    if existing is not None and existing != profile:
        raise ValidationError("workflow already contains a different agent profile named %s" % profile["name"])
    if existing is None:
        profiles.append(profile)
    steps = {step["id"]: step for step in bound["steps"]}
    for step_id in selected:
        step = steps.get(step_id)
        if step is None:
            raise ValidationError("workflow has no step named %s" % step_id)
        if step.get("kind") not in {"codex_exec", "agent_map"}:
            raise ValidationError("step %s is not a Codex agent step" % step_id)
        step["agent_profile"] = profile["name"]
    validate_workflow(bound, source=str(args.output))
    payload = json.dumps(bound, indent=2, sort_keys=True) + "\n"
    if len(payload.encode("utf-8")) > MAX_WORKFLOW_JSON_BYTES:
        raise ValidationError("bound workflow exceeds %d bytes" % MAX_WORKFLOW_JSON_BYTES)
    try:
        write_new_text_file_no_follow(args.output, "bound workflow output", payload)
    except FileExistsError:
        raise ValidationError("bound workflow output already exists: %s" % args.output)
    print("Bound profile %s to %d step(s): %s" % (profile["name"], len(selected), redact_text(str(args.output))))
    return 0


def _validate_agent_memory(args) -> int:
    for path in args.paths:
        load_agent_memory_file(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_agent_team_state(args) -> int:
    for path in args.paths:
        load_agent_team_state(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_team_state(args) -> int:
    summary = agent_team_state_summary(load_agent_team_state(args.path))
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Agent team: %s" % redact_text(summary["step_id"]))
    print("Status: %s" % summary["status"])
    print("Generation: %d" % summary["generation"])
    print("Lead: %s" % redact_text(summary["lead"]))
    print("Round: %d/%d" % (summary["round"], summary["max_rounds"]))
    print("Members: %d" % summary["member_count"])
    print("Tasks completed: %d/%d" % (
        summary["task_counts"]["completed"],
        sum(summary["task_counts"].values()),
    ))
    print("Turns: %d/%d" % (summary["turn_count"], summary["max_turns"]))
    print("Messages: %d" % summary["message_count"])
    print(
        "Operator tasks: %d/%d"
        % (summary["operator_tasks_added"], summary["max_operator_tasks"])
    )
    print(
        "Active message tasks: %d/%d"
        % (summary["message_tasks_added"], summary["max_message_tasks"])
    )
    print(
        "Operator questions: %d/%d (%d pending)"
        % (
            summary["operator_questions_added"],
            summary["max_operator_questions"],
            summary["pending_operator_questions"],
        )
    )
    print(
        "Authorized tokens: %d/%d"
        % (summary["authorized_tokens"], summary["max_total_tokens"])
    )
    return 0


def _validate_agent_team_operator_inbox(args) -> int:
    for path in args.paths:
        load_agent_team_operator_inbox(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_team_operator_inbox(args) -> int:
    summary = agent_team_operator_inbox_summary(
        load_agent_team_operator_inbox(args.path)
    )
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Agent team: %s" % redact_text(summary["step_id"]))
    print("Entries: %d" % summary["entry_count"])
    print("Pending: %d" % summary["status_counts"]["pending"])
    print("Accepted: %d" % summary["status_counts"]["accepted"])
    print("Superseded: %d" % summary["status_counts"]["superseded"])
    return 0


def _validate_agent_team_operator_chat(args) -> int:
    for path in args.paths:
        load_agent_team_operator_chat(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_team_operator_chat(args) -> int:
    summary = agent_team_operator_chat_summary(
        load_agent_team_operator_chat(args.path)
    )
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Agent team: %s" % redact_text(summary["step_id"]))
    print("Questions: %d" % summary["entry_count"])
    print("Pending: %d" % summary["status_counts"]["pending"])
    print("Answered: %d" % summary["status_counts"]["answered"])
    print("Delivered: %d" % summary["status_counts"]["delivered"])
    print("Superseded: %d" % summary["status_counts"]["superseded"])
    return 0


def _validate_agent_team_transcript(args) -> int:
    for path in args.paths:
        load_agent_team_transcript(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_team_transcript(args) -> int:
    summary = agent_team_transcript_summary(load_agent_team_transcript(args.path))
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Transcript: %s" % summary["transcript_id"])
    print("Agent team: %s" % redact_text(summary["step_id"]))
    print("Generation: %d" % summary["generation"])
    print("Round: %d" % summary["round"])
    print("Member: %s" % redact_text(summary["member_id"]))
    print("Task: %s" % redact_text(summary["task_id"]))
    print("Status: %s" % summary["status"])
    print("Events: %d (%d assistant messages)" % (
        summary["event_count"],
        summary["assistant_message_count"],
    ))
    print("Events dropped: %d" % summary["events_dropped"])
    print("Session SHA-256: %s" % (summary["session_id_sha256"] or "none"))
    print("Output SHA-256: %s" % (summary["output_sha256"] or "none"))
    return 0


def _validate_agent_team_interruption(args) -> int:
    for path in args.paths:
        load_agent_team_interruption(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_team_interruption(args) -> int:
    summary = agent_team_interruption_summary(
        load_agent_team_interruption(args.path)
    )
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Task: %s" % summary["task_id"])
    print("Status: %s" % summary["status"])
    print("Operator entry: %s" % summary["operator_entry_id"])
    print("Instruction SHA-256: %s" % summary["instruction_sha256"])
    print("Session SHA-256: %s" % summary["session_id_sha256"])
    return 0


def _validate_agent_team_plan(args) -> int:
    for path in args.paths:
        load_agent_team_plan(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_agent_team_plan_review(args) -> int:
    for path in args.paths:
        load_agent_team_plan_review(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_agent_team_plan_approval(args) -> int:
    for path in args.paths:
        load_agent_team_plan_approval(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_team_plan_approval(args) -> int:
    summary = agent_team_plan_approval_summary(
        load_agent_team_plan_approval(args.path)
    )
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Task: %s" % summary["task_id"])
    print("Member: %s" % summary["member_id"])
    print("Lead: %s" % summary["lead_member_id"])
    print("Status: %s" % summary["status"])
    print("Revision: %d/%d" % (summary["revision"], summary["max_revisions"] + 1))
    print("Rejections: %d" % summary["rejection_count"])
    return 0


def _queue_team_task(args) -> int:
    instruction = args.instruction
    if args.instruction_file is not None:
        instruction = read_regular_text_file_no_follow(
            args.instruction_file,
            "operator team instruction",
            16 * 1024,
        )
    entry = queue_team_task(
        args.run_dir,
        args.step_id,
        args.member,
        instruction,
        interrupt_current=args.interrupt_current,
    )
    print("Operator task: %s" % entry["task_id"])
    print("Member: %s" % entry["member_id"])
    print("Status: %s" % entry["status"])
    print("Delivery: %s" % entry["delivery"])
    if entry["delivery"] == "interrupt-current":
        print("Interrupt round: %s" % entry["interrupt_round"])
        print("Interrupt task: %s" % entry["interrupt_task_id"])
    print("Instruction SHA-256: %s" % entry["instruction_sha256"])
    return 0


def _list_team_inbox(args) -> int:
    summary = list_team_inbox(args.run_dir, args.step_id)
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Agent team: %s" % redact_text(summary["step_id"]))
    print("Status: %s" % summary["team_status"])
    print("Generation: %d" % summary["generation"])
    print("Operator tasks: %d/%d" % (
        summary["operator_tasks_added"],
        summary["max_operator_tasks"],
    ))
    print("Queued: %d" % summary["status_counts"]["pending"])
    print("Accepted: %d" % summary["status_counts"]["accepted"])
    print("Superseded: %d" % summary["status_counts"]["superseded"])
    return 0


def _list_team_questions(args) -> int:
    summary = list_team_questions(args.run_dir, args.step_id)
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Agent team: %s" % redact_text(summary["step_id"]))
    print("Status: %s" % summary["team_status"])
    print("Generation: %d" % summary["generation"])
    print(
        "Operator questions: %d/%d"
        % (summary["operator_questions_added"], summary["max_operator_questions"])
    )
    for entry in summary["entries"]:
        if entry["generation"] != summary["generation"]:
            continue
        print(
            "%s  %s  member=%s  question_sha256=%s"
            % (
                entry["id"],
                entry["status"],
                redact_text(entry["member_id"]),
                entry["question_sha256"],
            )
        )
    return 0


def _read_team_question(args) -> int:
    entry = read_team_question(
        args.run_dir,
        args.step_id,
        args.question_id,
    )
    if args.json:
        print(json.dumps(entry, indent=2, sort_keys=True))
        return 0
    print("Question: %s" % entry["id"])
    print("Member: %s" % redact_text(entry["member_id"]))
    print("Status: %s" % entry["status"])
    print("Question SHA-256: %s" % entry["question_sha256"])
    print("")
    print(entry["question"])
    if entry["reply"] is not None:
        print("")
        print("Operator reply:")
        print(entry["reply"])
    return 0


def _reply_team_question(args) -> int:
    reply = args.reply
    if args.reply_file is not None:
        reply = read_regular_text_file_no_follow(
            args.reply_file,
            "operator team reply",
            16 * 1024,
        )
    entry = reply_team_question(
        args.run_dir,
        args.step_id,
        args.question_id,
        reply,
    )
    print("Question: %s" % entry["id"])
    print("Member: %s" % redact_text(entry["member_id"]))
    print("Status: %s" % entry["status"])
    print("Reply SHA-256: %s" % entry["reply_sha256"])
    print("Response task: %s" % entry["response_task_id"])
    return 0


def _list_team_plans(args) -> int:
    summary = list_team_plans(args.run_dir, args.step_id)
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Agent team: %s" % redact_text(summary["step_id"]))
    print("Status: %s" % summary["team_status"])
    print("Generation: %d" % summary["generation"])
    print("Reviewer: %s" % summary["reviewer"])
    for value in summary["plans"]:
        print(
            "%s  %s  revision=%d/%d  plan_sha256=%s"
            % (
                value["task_id"],
                value["status"],
                value["revision"],
                value["max_revisions"] + 1,
                value["latest_plan_output_sha256"] or "none",
            )
        )
    return 0


def _read_team_plan(args) -> int:
    value = read_team_plan(args.run_dir, args.step_id, args.task_id)
    if args.json:
        print(json.dumps(value, indent=2, sort_keys=True))
        return 0
    print("Task: %s" % redact_text(value["task_id"]))
    print("Member: %s" % redact_text(value["member_id"]))
    print("Reviewer: %s" % value["reviewer"])
    print("Status: %s" % value["status"])
    print("")
    print("Criteria:")
    print(value["criteria"])
    print("")
    print("Plan:")
    print(json.dumps(value["plan"], indent=2, sort_keys=True))
    if value["review"] is not None:
        print("")
        print("Latest review:")
        print(json.dumps(value["review"], indent=2, sort_keys=True))
    return 0


def _list_team_transcripts(args) -> int:
    summary = list_team_transcripts(args.run_dir, args.step_id)
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Agent team: %s" % redact_text(summary["step_id"]))
    print("Status: %s" % summary["team_status"])
    print("Generation: %d" % summary["generation"])
    print("Console bounds: %d events / %d bytes" % (
        summary["max_events"],
        summary["max_bytes"],
    ))
    for value in summary["transcripts"]:
        print(
            "%s  %s  round=%d  member=%s  task=%s  events=%d  sha256=%s"
            % (
                value["transcript_id"],
                value["status"],
                value["round"],
                redact_text(value["member_id"]),
                redact_text(value["task_id"]),
                value["event_count"],
                value["file_sha256"],
            )
        )
    return 0


def _read_team_transcript(args) -> int:
    if (
        isinstance(args.timeout_seconds, bool)
        or not isinstance(args.timeout_seconds, int)
        or not 1 <= args.timeout_seconds <= 24 * 60 * 60
    ):
        raise ValidationError("read-team-transcript timeout-seconds must be from 1 to 86400")
    value = read_team_transcript(
        args.run_dir,
        args.step_id,
        args.transcript_id,
    )
    if args.json:
        print(json.dumps(value, indent=2, sort_keys=True))
        return 0
    print("Transcript: %s" % value["transcript_id"])
    print("Member: %s" % redact_text(value["member_id"]))
    print("Task: %s" % redact_text(value["task_id"]))
    print("Round: %d" % value["round"])
    print("")
    seen = 0
    deadline = time.monotonic() + args.timeout_seconds
    while True:
        for event in value["events"][seen:]:
            _print_team_transcript_event(event)
        seen = len(value["events"])
        if not args.follow or value["status"] != "active":
            break
        if time.monotonic() >= deadline:
            raise ValidationError("read-team-transcript follow timed out")
        time.sleep(0.25)
        value = read_team_transcript(
            args.run_dir,
            args.step_id,
            args.transcript_id,
        )
        if seen > len(value["events"]):
            raise ValidationError("agent team transcript event history changed while following")
    print("")
    print("Status: %s" % value["status"])
    if value["truncated"]:
        print("Truncated: yes (%d events dropped)" % value["events_dropped"])
    return 0


def _team_console(args) -> int:
    if (
        isinstance(args.refresh_ms, bool)
        or not isinstance(args.refresh_ms, int)
        or not TEAM_CONSOLE_REFRESH_MIN_MS
        <= args.refresh_ms
        <= TEAM_CONSOLE_REFRESH_MAX_MS
    ):
        raise ValidationError(
            "team-console refresh-ms must be from %d to %d"
            % (TEAM_CONSOLE_REFRESH_MIN_MS, TEAM_CONSOLE_REFRESH_MAX_MS)
        )
    if (
        isinstance(args.timeout_seconds, bool)
        or not isinstance(args.timeout_seconds, int)
        or not 1 <= args.timeout_seconds <= TEAM_CONSOLE_TIMEOUT_MAX_SECONDS
    ):
        raise ValidationError(
            "team-console timeout-seconds must be from 1 to %d"
            % TEAM_CONSOLE_TIMEOUT_MAX_SECONDS
        )
    if (
        isinstance(args.history, bool)
        or not isinstance(args.history, int)
        or not 1 <= args.history <= TEAM_CONSOLE_MAX_HISTORY
    ):
        raise ValidationError(
            "team-console history must be from 1 to %d"
            % TEAM_CONSOLE_MAX_HISTORY
        )
    if (
        isinstance(args.max_events, bool)
        or not isinstance(args.max_events, int)
        or not 1 <= args.max_events <= TEAM_CONSOLE_MAX_DISPLAY_EVENTS
    ):
        raise ValidationError(
            "team-console max-events must be from 1 to %d"
            % TEAM_CONSOLE_MAX_DISPLAY_EVENTS
        )
    if (
        isinstance(args.max_message_chars, bool)
        or not isinstance(args.max_message_chars, int)
        or not TEAM_CONSOLE_MIN_MESSAGE_CHARS
        <= args.max_message_chars
        <= TEAM_CONSOLE_MAX_MESSAGE_CHARS
    ):
        raise ValidationError(
            "team-console max-message-chars must be from %d to %d"
            % (TEAM_CONSOLE_MIN_MESSAGE_CHARS, TEAM_CONSOLE_MAX_MESSAGE_CHARS)
        )
    if args.width is None:
        width = shutil.get_terminal_size((120, 40)).columns
        width = max(TEAM_CONSOLE_MIN_WIDTH, min(TEAM_CONSOLE_MAX_WIDTH, width))
    else:
        width = args.width
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or not TEAM_CONSOLE_MIN_WIDTH <= width <= TEAM_CONSOLE_MAX_WIDTH
        ):
            raise ValidationError(
                "team-console width must be from %d to %d"
                % (TEAM_CONSOLE_MIN_WIDTH, TEAM_CONSOLE_MAX_WIDTH)
            )
    snapshot = read_team_console_snapshot(
        args.run_dir,
        args.step_id,
        member_ids=args.members,
    )
    validate_team_console_snapshot(snapshot)
    if args.json:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
        return 0
    deadline = time.monotonic() + args.timeout_seconds
    last_fingerprint = None
    use_clear = bool(
        args.follow
        and not args.no_clear
        and hasattr(sys.stdout, "isatty")
        and sys.stdout.isatty()
    )
    try:
        while True:
            fingerprint = json.dumps(
                snapshot,
                sort_keys=True,
                separators=(",", ":"),
            )
            if fingerprint != last_fingerprint:
                rendered = render_team_console(
                    snapshot,
                    width=width,
                    layout=args.layout,
                    history=args.history,
                    max_events=args.max_events,
                    max_message_chars=args.max_message_chars,
                )
                if use_clear:
                    sys.stdout.write("\x1b[2J\x1b[H")
                elif last_fingerprint is not None:
                    sys.stdout.write("\n")
                sys.stdout.write(rendered)
                sys.stdout.flush()
                last_fingerprint = fingerprint
            if not args.follow or team_console_is_terminal(snapshot):
                return 0
            if time.monotonic() >= deadline:
                raise ValidationError("team-console follow timed out")
            time.sleep(args.refresh_ms / 1000.0)
            snapshot = read_team_console_snapshot(
                args.run_dir,
                args.step_id,
                member_ids=args.members,
            )
            validate_team_console_snapshot(snapshot)
    except KeyboardInterrupt:
        if use_clear:
            sys.stdout.write("\n")
            sys.stdout.flush()
        return 130


def _print_team_transcript_event(event: Dict) -> None:
    prefix = "[%03d %s]" % (event["sequence"], event["at_utc"])
    if event["type"] == "assistant-message":
        print("%s teammate" % prefix)
        print(redact_terminal_text(event["text"]))
        return
    if event["type"] == "activity":
        print("%s %s %s" % (prefix, event["item_type"], event["status"]))
        return
    print("%s %s" % (prefix, event["type"]))


def _review_team_plan(args) -> int:
    feedback = args.feedback
    if args.feedback_file is not None:
        feedback = read_regular_text_file_no_follow(
            args.feedback_file,
            "operator plan review feedback",
            16 * 1024,
        )
    value = review_team_plan(
        args.run_dir,
        args.step_id,
        args.task_id,
        args.decision,
        feedback,
    )
    print("Task: %s" % redact_text(value["task_id"]))
    print("Status: %s" % value["status"])
    print("Decision: %s" % value["latest_decision"])
    print("Review SHA-256: %s" % value["latest_review_output_sha256"])
    return 0


def _validate_agent_team_merge_ledger(args) -> int:
    for path in args.paths:
        load_agent_team_merge_ledger(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_team_merge_ledger(args) -> int:
    summary = agent_team_merge_summary(load_agent_team_merge_ledger(args.path))
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Merge strategy: %s" % summary["strategy"])
    print("Events: %d" % summary["event_count"])
    print("Accepted merges: %d" % summary["merge_count"])
    print("Serialized conflicts: %d" % summary["conflict_count"])
    print("Changed files: %d" % summary["changed_file_count"])
    print("Applied files: %d" % summary["applied_file_count"])
    print("Deduplicated files: %d" % summary["deduplicated_file_count"])
    print("Current source fingerprint: %s" % summary["source_current_sha256"])
    return 0


def _validate_agent_map_packet_terminal(args) -> int:
    for path in args.paths:
        load_agent_map_packet_terminal(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_map_packet_terminal(args) -> int:
    summary = agent_map_packet_terminal_summary(
        load_agent_map_packet_terminal(args.path)
    )
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Packet terminal: %s" % summary["terminal_sha256"])
    print("Status: %s" % summary["status"])
    print("Step/index: %s/%d" % (summary["step_id"], summary["index"]))
    print("Cache generation: %d" % summary["cache_generation"])
    print("Packet generation: %d" % summary["packet_generation"])
    print("Provider replay required: no")
    return 0


def _validate_codex_step_terminal(args) -> int:
    for path in args.paths:
        load_codex_step_terminal(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_codex_step_terminal(args) -> int:
    summary = codex_step_terminal_summary(load_codex_step_terminal(args.path))
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Codex step terminal: %s" % summary["terminal_sha256"])
    print("Status: %s" % summary["status"])
    print("Step: %s" % summary["step_id"])
    print("Mode/resumes: %s/%d" % (summary["mode"], summary["resume_count"]))
    print("Workspace mode: %s" % summary["workspace_mode"])
    print("Provider replay required: no")
    return 0


def _validate_codex_progress(args) -> int:
    for path in args.paths:
        load_codex_progress(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_codex_progress(args) -> int:
    summary = codex_progress_summary(load_codex_progress(args.path))
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    target = summary["step_id"]
    if summary["packet_index"] is not None:
        target += "/packet-%d" % summary["packet_index"]
    print("Codex progress: %s" % summary["progress_sha256"])
    print("Target: %s" % target)
    print("Status/event: %s/%s" % (summary["status"], summary["last_event"]))
    print(
        "Items: %d started | %d completed | %d active | %d failed"
        % (
            summary["item_started_count"],
            summary["item_completed_count"],
            summary["active_item_count"],
            summary["failed_item_count"],
        )
    )
    if summary["native_agent_count"] or summary["collab_tool_call_count"]:
        print(
            "Native agents: %d observed | %d active | %d errors | %d collaboration calls | %d failed"
            % (
                summary["native_agent_count"],
                summary["native_agent_active_count"],
                summary["native_agent_error_count"],
                summary["collab_tool_call_count"],
                summary["collab_tool_failed_count"],
            )
        )
        print("Nested native-agent usage attributed: no")
    print("Raw provider content persisted: no")
    return 0


def _validate_agent_team_turn_completion(args) -> int:
    for path in args.paths:
        load_agent_team_turn_completion(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_team_turn_completion(args) -> int:
    summary = agent_team_turn_completion_summary(
        load_agent_team_turn_completion(args.path)
    )
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Turn completion: %s" % summary["completion_sha256"])
    print("Status: %s" % summary["status"])
    print("Generation: %d" % summary["generation"])
    print("Round: %d" % summary["round"])
    print("Member/task: %s/%s" % (summary["member_id"], summary["task_id"]))
    print("Quality retry: %d" % summary["quality_retry_index"])
    print("Provider replay required: no")
    return 0


def _validate_agent_team_turn_terminal(args) -> int:
    for path in args.paths:
        load_agent_team_turn_terminal(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_team_turn_terminal(args) -> int:
    summary = agent_team_turn_terminal_summary(
        load_agent_team_turn_terminal(args.path)
    )
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Turn terminal: %s" % summary["terminal_sha256"])
    print("Status: %s" % summary["status"])
    print("Generation: %d" % summary["generation"])
    print("Round: %d" % summary["round"])
    print("Member/task: %s/%s" % (summary["member_id"], summary["task_id"]))
    print("Quality retry: %d" % summary["quality_retry_index"])
    print("Provider replay required: no")
    return 0


def _validate_agent_team_merge_intent(args) -> int:
    for path in args.paths:
        load_agent_team_merge_intent(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_team_merge_intent(args) -> int:
    summary = agent_team_merge_intent_summary(
        load_agent_team_merge_intent(args.path)
    )
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Merge intent: %s" % summary["intent_sha256"])
    print("Status: %s" % summary["status"])
    print("Generation: %d" % summary["generation"])
    print("Round: %d" % summary["round"])
    print("Member/task: %s/%s" % (summary["member_id"], summary["task_id"]))
    print("Provider replay required: no")
    return 0


def _validate_agent_team_merge_transaction(args) -> int:
    for path in args.paths:
        load_agent_team_merge_transaction(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_team_merge_transaction(args) -> int:
    summary = agent_team_merge_transaction_summary(
        load_agent_team_merge_transaction(args.path)
    )
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Merge transaction: %s" % summary["transaction_sha256"])
    print("Status: %s" % summary["status"])
    print("Generation: %d" % summary["generation"])
    print("Round: %d" % summary["round"])
    print("Member/task: %s/%s" % (summary["member_id"], summary["task_id"]))
    print("Changed files: %d" % summary["changed_file_count"])
    print("Provider replay required: no")
    return 0


def _validate_agent_team_merge_recovery(args) -> int:
    for path in args.paths:
        load_agent_team_merge_recovery(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_team_merge_recovery(args) -> int:
    summary = agent_team_merge_recovery_summary(
        load_agent_team_merge_recovery(args.path)
    )
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Merge recovery: %s" % summary["transaction_sha256"])
    print("Source checkpoint: %s" % summary["source_checkpoint"])
    print("State checkpoint: %s" % summary["state_checkpoint"])
    print("Ledger checkpoint: %s" % summary["ledger_checkpoint"])
    print("Provider replayed: no")
    return 0


def _validate_agent_team_quality_retry(args) -> int:
    for path in args.paths:
        load_agent_team_quality_retry(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _inspect_agent_team_quality_retry(args) -> int:
    checkpoint, _file_sha256 = load_agent_team_quality_retry(args.path)
    summary = agent_team_quality_retry_summary(checkpoint)
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Quality retry: %s/%s" % (summary["member_id"], summary["task_id"]))
    print("Status: %s" % summary["status"])
    print("Hook/event: %s/%s" % (summary["hook_id"], summary["event"]))
    if summary["event_task_id"] != summary["task_id"]:
        print("Event task: %s" % summary["event_task_id"])
    print(
        "Retry: %d/%d (hook %d/%d)"
        % (
            summary["retry_index"],
            summary["max_retries"],
            summary["hook_retry_index"],
            summary["hook_max_retries"],
        )
    )
    print("Maximum tokens: %d" % summary["max_tokens"])
    print("Automatic provider replay allowed: no")
    return 0


def _list_agent_memory(args) -> int:
    memory_dir = _agent_memory_dir_for_cli(args)
    records = list_agent_memories(args.workspace, memory_dir=memory_dir)
    if args.json:
        print(json.dumps(redact_json_value({"memories": records}), indent=2, sort_keys=True))
        return 0
    if not records:
        print("No agent memory stores.")
        return 0
    for record in records:
        print(
            "%s: revision %d, %d entries, %d bytes"
            % (
                redact_text(record["profile"]),
                record["revision"],
                record["entry_count"],
                record["content_bytes"],
            )
        )
    return 0


def _inspect_agent_memory(args) -> int:
    memory_dir = _agent_memory_dir_for_cli(args)
    memory = load_agent_memory(
        args.workspace,
        args.profile,
        memory_dir=memory_dir,
        allow_missing=False,
    )
    summary = agent_memory_summary(memory)
    if args.json:
        print(json.dumps(redact_json_value(summary), indent=2, sort_keys=True))
        return 0
    print("Agent memory: %s" % redact_text(summary["profile"]))
    print("Revision: %d" % summary["revision"])
    print("Entries: %d" % summary["entry_count"])
    print("Content bytes: %d" % summary["content_bytes"])
    print("Store SHA-256: %s" % summary["store_sha256"])
    for entry in summary["entries"]:
        print(
            "- %s: %d bytes, sha256 %s, tags %s, source %s"
            % (
                redact_text(entry["id"]),
                entry["content_bytes"],
                entry["content_sha256"],
                ",".join(entry["tags"]) if entry["tags"] else "none",
                entry["source"]["kind"],
            )
        )
    return 0


def _remember_agent_memory(args) -> int:
    memory_dir = _agent_memory_dir_for_cli(args)
    content = args.content
    if args.content_file is not None:
        content = read_regular_text_file_no_follow(
            args.content_file,
            "agent memory content file",
            MAX_AGENT_MEMORY_ENTRY_BYTES,
        )
    result = append_agent_memory(
        args.workspace,
        args.profile,
        content,
        tags=list(args.tag),
        entry_id=args.entry_id,
        expected_revision=args.expected_revision,
        memory_dir=memory_dir,
    )
    memory = result["memory"]
    print("Agent memory: %s" % redact_text(args.profile))
    print("Entry: %s" % redact_text(result["entry"]["id"]))
    print("Revision: %d" % memory["revision"])
    print("Changed: %s" % str(bool(result["changed"])).lower())
    print("Store: %s" % redact_text(str(agent_memory_path(args.workspace, args.profile, memory_dir=memory_dir))))
    print("Workflow write approval: %s" % AGENT_MEMORY_WRITE_APPROVAL)
    return 0


def _forget_agent_memory(args) -> int:
    memory_dir = _agent_memory_dir_for_cli(args)
    result = remove_agent_memory_entry(
        args.workspace,
        args.profile,
        args.entry_id,
        expected_revision=args.expected_revision,
        memory_dir=memory_dir,
    )
    print("Agent memory: %s" % redact_text(args.profile))
    print("Removed entry: %s" % redact_text(result["removed_entry_id"]))
    print("Revision: %d" % result["memory"]["revision"])
    return 0


def _agent_memory_dir_for_cli(args) -> Path:
    workspace = Path(args.workspace).expanduser().resolve()
    memory_dir = (
        Path(args.memory_dir).expanduser()
        if args.memory_dir is not None
        else default_agent_memory_dir(workspace)
    )
    require_path_outside_workspace(workspace, memory_dir, "agent memory directory")
    return memory_dir


def _validate_saved_workflows(args) -> int:
    if args.saved_workflows:
        saved_workflows = []
        invalid = 0
        for identifier in args.saved_workflows:
            try:
                saved = _resolve_saved_workflow_for_cli(identifier, args)
            except ConductorError as exc:
                if not args.keep_going:
                    raise
                invalid += 1
                print(
                    "INVALID: %s (%s)"
                    % (redact_text(identifier), redact_text(str(exc)))
                )
                continue
            saved_workflows.append(saved)
        _reject_duplicate_saved_workflows(saved_workflows)
        for saved in saved_workflows:
            print(
                "OK: %s (%s, %s)"
                % (redact_text(str(saved.path)), redact_text(saved.command_name), redact_text(saved.scope))
            )
        return 2 if invalid else 0
    entries = iter_saved_workflow_entries(args.workspace, **_saved_workflow_scope_kwargs(args))
    if not entries:
        raise ValidationError("no saved workflow scripts found")
    saved_workflows = []
    invalid = 0
    for entry in entries:
        try:
            saved_workflows.append(
                load_saved_workflow(entry.path, scope=entry.scope, rank=entry.rank)
            )
        except ConductorError as exc:
            if not args.keep_going:
                raise
            invalid += 1
            print(
                "INVALID: %s (%s)"
                % (redact_text(str(entry.path)), redact_text(str(exc)))
            )
    _reject_duplicate_saved_workflows(saved_workflows)
    for saved in saved_workflows:
        print(
            "OK: %s (%s, %s)"
            % (redact_text(str(saved.path)), redact_text(saved.command_name), redact_text(saved.scope))
        )
    return 2 if invalid else 0


def _validate_model_workflow_receipt(args) -> int:
    for path in args.paths:
        load_model_workflow_receipt(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_model_workflow_run_receipt(args) -> int:
    for path in args.paths:
        load_model_workflow_run_receipt(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_auto_receipt(args) -> int:
    for path in args.paths:
        load_auto_orchestration_receipt(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _list_auto(args) -> int:
    records = collect_auto_orchestrations(
        args.auto_dir,
        generated_dir=args.generated_dir,
        goals_dir=args.goals_dir,
        runs_dir=args.runs_dir,
        max_records=args.max_auto,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "schema": "conductor.auto_orchestration_list.v1",
                    "count": len(records),
                    "auto_dir": str(args.auto_dir),
                    "records": records,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    for record in records:
        child = record.get("child") if isinstance(record.get("child"), dict) else {}
        print(
            "%s\t%s\t%s\t%s\t%s\t%s"
            % (
                _list_runs_text_field(record.get("name") or "unknown"),
                _list_runs_text_field(record.get("effective_status") or "unknown"),
                _list_runs_text_field(record.get("route") or "unknown"),
                _list_runs_text_field(record.get("execution_mode") or "unknown"),
                _list_runs_text_field(record.get("liveness") or "unknown"),
                _list_runs_text_field(child.get("resolution") or "unknown"),
            )
        )
    return 0


def _status_auto(args) -> int:
    summary = summarize_auto_orchestration(
        args.receipt,
        generated_dir=args.generated_dir,
        goals_dir=args.goals_dir,
        runs_dir=args.runs_dir,
        child_artifact=args.child_artifact,
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    child = summary.get("child") if isinstance(summary.get("child"), dict) else {}
    print("Auto receipt: %s" % redact_text(str(args.receipt)))
    print("Route: %s" % _list_runs_text_field(summary.get("route") or "unknown"))
    print("Execution mode: %s" % _list_runs_text_field(summary.get("execution_mode") or "unknown"))
    print("Handoff status: %s" % _list_runs_text_field(summary.get("status") or "unknown"))
    print("Effective status: %s" % _list_runs_text_field(summary.get("effective_status") or "unknown"))
    print("Child resolution: %s" % _list_runs_text_field(child.get("resolution") or "unknown"))
    print("Liveness: %s" % _list_runs_text_field(summary.get("liveness") or "unknown"))
    if child.get("error"):
        print("Child error: %s" % _list_runs_text_field(child.get("error")))
    return 1 if summary.get("needs_attention") else 0


def _validate_background_run(args) -> int:
    for path in args.paths:
        run_dir = path if path.is_dir() else path.parent
        if not path.is_dir() and path.name != "background.json":
            raise ValidationError("background metadata filename must be background.json: %s" % path)
        load_background_run(run_dir)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_desktop_notification(args) -> int:
    for path in args.paths:
        load_desktop_notification_receipt(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_background_model_goal(args) -> int:
    for path in args.paths:
        load_background_model_goal(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_background_routine_supervisor(args) -> int:
    for path in args.paths:
        load_background_routine_supervisor(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _reject_duplicate_saved_workflows(saved_workflows) -> None:
    by_name = {}
    for saved in saved_workflows:
        by_name.setdefault((saved.command_name, saved.rank), []).append(saved.path)
    duplicates = {
        name_rank: paths
        for name_rank, paths in by_name.items()
        if len(paths) > 1
    }
    if duplicates:
        parts = []
        for (name, _rank), paths in sorted(duplicates.items()):
            rendered_paths = ", ".join(redact_text(str(path)) for path in paths)
            parts.append("%s: %s" % (redact_text(name), rendered_paths))
        raise ValidationError("duplicate saved workflow command name(s): %s" % "; ".join(parts))


def _inspect(args) -> int:
    workflow = load_workflow(args.workflow)
    print(workflow_summary(workflow))
    return 0


def _run(args) -> int:
    if args.dry_run and args.desktop_notify:
        raise ValidationError("--desktop-notify requires real workflow execution, not --dry-run")
    workflow = load_workflow(args.workflow)
    _validate_print_result_request(workflow, args)
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        allow_network=args.allow_network,
        allow_agent=args.allow_agent,
        allow_parallel=args.allow_parallel,
        approvals=approvals,
    )
    runner = WorkflowRunner(
        workflow=workflow,
        workspace=args.workspace,
        base_run_dir=args.runs_dir,
        policy=policy,
        dry_run=args.dry_run,
        run_id=args.run_id,
        resume_dir=args.resume,
        max_workers=args.max_workers,
    )
    try:
        run = runner.execute()
    except Exception:
        _notify_run_fail_open(runner.run, args.desktop_notify)
        raise
    print("Run directory: %s" % run.run_dir)
    status = str(run.read_state().get("status") or "failed")
    print("Status: %s" % status)
    _print_workflow_result(run, workflow, status, args.print_result)
    _notify_run_fail_open(run, args.desktop_notify, status=status)
    return 0


def _validate_print_result_request(workflow: Dict, args) -> None:
    if not args.print_result:
        return
    if args.dry_run:
        raise ValidationError("--print-result requires real workflow execution, not --dry-run")
    if not workflow.get("result_artifact"):
        raise ValidationError("--print-result requires a workflow result_artifact contract")


def _print_workflow_result(run, workflow: Dict, status: str, print_result: bool) -> None:
    relative = workflow.get("result_artifact")
    if not isinstance(relative, str) or not relative:
        return
    path = run.resolve_artifact_path(relative)
    if status == "completed":
        print("Result artifact: %s" % redact_text(str(path)))
    elif status == "planned":
        print("Result artifact (planned): %s" % redact_text(str(path)))
    else:
        print("Result artifact (pending): %s" % redact_text(str(path)))
    if not print_result:
        return
    if status != "completed":
        raise ValidationError("--print-result requires a completed workflow")
    text = run.read_artifact(relative)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError("workflow result artifact is not valid JSON") from exc
    print("Result:")
    rendered = redact_terminal_text(value) if isinstance(value, str) else text
    sys.stdout.write(rendered)
    if not rendered.endswith("\n"):
        sys.stdout.write("\n")


def _run_goal(args) -> int:
    workflow = load_workflow(args.workflow)
    if args.workflow_completion and any(
        value is not None
        for value in (
            args.check_command_json,
            args.check_prompt,
            args.check_prompt_file,
            args.check_model,
        )
    ):
        raise ValidationError(
            "workflow completion verification cannot be combined with an external verifier"
        )
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        allow_network=args.allow_network,
        allow_agent=args.allow_agent,
        allow_parallel=args.allow_parallel,
        approvals=approvals,
    )
    check_command = parse_check_command_json(args.check_command_json) if args.check_command_json is not None else None
    check_prompt = args.check_prompt
    if args.check_prompt_file is not None:
        check_prompt = load_check_prompt_file(args.check_prompt_file, args.workspace)
    result = run_goal_loop(
        workflow=workflow,
        workspace=args.workspace,
        runs_dir=args.runs_dir,
        goals_dir=args.goals_dir,
        policy=policy,
        check_command=check_command,
        check_prompt=check_prompt,
        check_model=args.check_model,
        max_iterations=args.max_iterations,
        goal_id=args.goal_id,
        resume_goal=args.resume_goal,
        max_workers=args.max_workers,
        check_timeout_seconds=args.check_timeout_seconds,
        output_limit_bytes=args.output_limit_bytes,
        workflow_completion=args.workflow_completion,
    )
    print("Goal: %s" % redact_text(result.goal_id))
    print("Status: %s" % result.status)
    print("Iterations: %s" % result.iterations)
    print("Goal artifact: %s" % redact_text(str(result.goal_path)))
    for run_dir in result.run_dirs:
        print("Run directory: %s" % redact_text(str(run_dir)))
    _notify_execution_fail_open(
        args.desktop_notify,
        result.status,
        artifact_path=result.goal_path,
    )
    return 0 if result.status == "completed" else 1


def _status_goal(args) -> int:
    state = load_goal_state(args.goal_json)
    print(redact_text(json.dumps(state, indent=2, sort_keys=True) + "\n"), end="")
    return 0


def _list_goals(args) -> int:
    records = []
    for path in iter_goal_state_paths(args.goals_dir, max_goals=args.max_goals):
        try:
            state = load_goal_state(path)
            records.append(_goal_list_record(path, state))
        except ConductorError as exc:
            records.append(
                {
                    "path": str(path),
                    "goal_id": path.stem,
                    "status": "invalid",
                    "workflow": "unknown",
                    "iterations": 0,
                    "planner_session": {},
                    "updated_at_utc": "",
                    "error": str(exc),
                }
            )
    if args.json:
        payload = {
            "schema": "conductor.goal_list.v1",
            "goals_dir": str(args.goals_dir),
            "count": len(records),
            "goals": records,
        }
        print(json.dumps(redact_json_value(payload), indent=2, sort_keys=True))
        return 0
    for record in records:
        print(
            "%s\t%s\t%s\t%s\t%s\t%s\t%s"
            % (
                _list_runs_text_field(record["goal_id"]),
                _list_runs_text_field(record["status"]),
                _list_runs_text_field(record["workflow"]),
                _list_runs_text_field(record["iterations"]),
                _list_runs_text_field(_planner_session_list_label(record.get("planner_session"))),
                _list_runs_text_field(_background_goal_list_label(record.get("background"))),
                _list_runs_text_field(record["updated_at_utc"]),
            )
        )
    return 0


def _goal_list_record(path: Path, state: Dict) -> Dict:
    iterations = state.get("iterations") if isinstance(state.get("iterations"), list) else []
    planner = state.get("planner") if isinstance(state.get("planner"), dict) else {}
    session = planner.get("session") if isinstance(planner.get("session"), dict) else {}
    session_hash = session.get("id_sha256")
    if not isinstance(session_hash, str) or len(session_hash) != 64 or any(
        character not in "0123456789abcdef" for character in session_hash
    ):
        session_hash = ""
    session_mode = session.get("mode")
    if session_mode not in {"same-session", "fresh-per-iteration"}:
        session_mode = "unavailable"
    turns = session.get("turns")
    if not isinstance(turns, int) or isinstance(turns, bool) or turns < 0:
        turns = 0
    return {
        "path": str(path),
        "goal_id": str(state.get("goal_id") or path.stem),
        "status": str(state.get("status") or "unknown"),
        "workflow": str(state.get("workflow") or "unknown"),
        "iterations": len(iterations),
        "planner_session": {
            "mode": session_mode,
            "available": session_mode == "same-session" and isinstance(session.get("id"), str) and bool(session["id"]),
            "id_sha256": session_hash,
            "turns": turns,
        },
        "background": latest_background_model_goal(path.parent, str(state.get("goal_id") or path.stem)),
        "updated_at_utc": str(state.get("updated_at_utc") or state.get("finished_at_utc") or state.get("started_at_utc") or ""),
    }


def _planner_session_list_label(session: Dict) -> str:
    if not isinstance(session, dict) or not session:
        return "unavailable"
    mode = str(session.get("mode") or "unavailable")
    availability = "available" if session.get("available") is True else "unavailable"
    turns = session.get("turns")
    turn_label = str(turns) if isinstance(turns, int) and not isinstance(turns, bool) else "0"
    return "%s/%s/%s-turns" % (mode, availability, turn_label)


def _background_goal_list_label(background: Dict) -> str:
    if not isinstance(background, dict) or not background:
        return "unavailable"
    status = str(background.get("status") or "unknown")
    liveness = str(background.get("liveness") or "unknown")
    return "%s/%s" % (status, liveness)


def _prepare_run(args) -> int:
    workflow = load_workflow(args.workflow)
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        allow_network=args.allow_network,
        allow_agent=args.allow_agent,
        allow_parallel=args.allow_parallel,
        approvals=approvals,
    )
    result = prepare_run(
        workflow=workflow,
        workflow_path=args.workflow,
        workspace=args.workspace,
        runs_dir=args.runs_dir,
        policy=policy,
        run_id=args.run_id,
        max_workers=args.max_workers,
    )
    print("Prepared run directory: %s" % redact_text(result["run_dir"]))
    print("Status: %s" % result["status"])
    print("Launch manifest: %s" % redact_text(result["launch_manifest"]))
    print("Resume command: %s" % redact_text(result["resume_command"]))
    return 0


def _start_background_run(args) -> int:
    workflow = load_workflow(args.workflow)
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        allow_network=args.allow_network,
        allow_agent=args.allow_agent,
        allow_parallel=args.allow_parallel,
        approvals=validate_approval_tokens(args.approve),
    )
    result = start_background_run(
        workflow=workflow,
        workflow_path=args.workflow,
        workspace=args.workspace,
        runs_dir=args.runs_dir,
        policy=policy,
        run_id=args.run_id,
        max_workers=args.max_workers,
        desktop_notify=args.desktop_notify,
    )
    print("Background run directory: %s" % redact_text(str(result.run_dir)))
    print("Worker pid: %d" % result.pid)
    print("Status: %s" % result.metadata["status"])
    print("Metadata: %s" % redact_text(str(result.run_dir / "background.json")))
    return 0


def _status_background_run(args) -> int:
    metadata = load_background_run(args.run_dir)
    state_path = args.run_dir / "state.json"
    try:
        state = json.loads(read_regular_text_file_no_follow(state_path, "state.json", MAX_STATUS_JSON_BYTES))
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (state_path, exc))
    if not isinstance(state, dict):
        raise ValidationError("state.json must contain a JSON object")
    payload = {
        "schema": "conductor.background_status.v1",
        "background": metadata,
        "runner": summarize_runner(args.run_dir),
        "state": state,
    }
    print(json.dumps(redact_json_value(payload), indent=2, sort_keys=True))
    return 0


def _wait_background_run(args) -> int:
    if not isinstance(args.poll_interval_ms, int) or not 50 <= args.poll_interval_ms <= 60000:
        raise ValidationError("poll-interval-ms must be from 50 to 60000")
    metadata = wait_for_background_run(
        args.run_dir,
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_ms / 1000.0,
    )
    print(json.dumps(redact_json_value(metadata), indent=2, sort_keys=True))
    return 0 if metadata["exit_code"] == 0 and metadata["run_status"] == "completed" else 1


def _status_background_model_goal(args) -> int:
    metadata = load_background_model_goal(args.metadata_json)
    payload = {
        "schema": "conductor.background_model_goal_status.v1",
        "background": metadata,
        "liveness": background_model_goal_liveness(metadata),
    }
    goal_path = args.metadata_json.parent.parent / metadata["goal"]["artifact"]
    if goal_path.exists():
        payload["goal"] = load_goal_state(goal_path)
    print(json.dumps(redact_json_value(payload), indent=2, sort_keys=True))
    return 0


def _wait_background_model_goal(args) -> int:
    if not isinstance(args.poll_interval_ms, int) or not 50 <= args.poll_interval_ms <= 60000:
        raise ValidationError("poll-interval-ms must be from 50 to 60000")
    metadata = wait_for_background_model_goal(
        args.metadata_json,
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_ms / 1000.0,
    )
    print(json.dumps(redact_json_value(metadata), indent=2, sort_keys=True))
    return 0 if metadata["exit_code"] == 0 and metadata["goal_status"] == "completed" else 1


def _terminate_background_model_goal(args) -> int:
    result = terminate_background_model_goal(args.metadata_json, force=args.force)
    print("Background model goal signal: %s" % result["signal"])
    print("Worker pid: %s" % result["pid"])
    return 0


def _status(args) -> int:
    state_path = args.run_dir / "state.json"
    if state_path.is_symlink():
        raise ValidationError("state.json must not be a symlink: %s" % state_path)
    try:
        state = json.loads(read_regular_text_file_no_follow(state_path, "state.json", MAX_STATUS_JSON_BYTES))
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (state_path, exc))
    if not isinstance(state, dict):
        raise ValidationError("state.json must contain a JSON object")
    payload = {"state": state, "runner": summarize_runner(args.run_dir)} if args.with_runner else state
    text = json.dumps(
        payload,
        indent=2,
        sort_keys=True,
    ) + "\n"
    print(redact_text(text), end="")
    return 0


def _list(args) -> int:
    files = iter_workflow_files(args.paths)
    for path in files:
        try:
            workflow = load_workflow(path)
        except ConductorError as exc:
            print("INVALID: %s (%s)" % (redact_text(str(path)), redact_text(str(exc))))
            continue
        print(
            "%s\t%s\t%s"
            % (
                redact_text(str(path)),
                redact_text(workflow["name"]),
                redact_text(workflow.get("description", "")),
            )
        )
    return 0


def _list_runs(args) -> int:
    runs = collect_runs(
        args.runs_dir,
        max_runs=args.max_runs,
        max_text_bytes=args.max_text_bytes,
        status_filter=args.status or None,
    )
    records = [_run_list_record(run) for run in runs]
    if args.json:
        payload = {
            "schema": "conductor.run_list.v1",
            "runs_dir": str(args.runs_dir),
            "count": len(records),
            "runs": records,
        }
        print(json.dumps(redact_json_value(payload), indent=2, sort_keys=True))
        return 0
    for record in records:
        print(
            "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s"
            % (
                _list_runs_text_field(record["id"]),
                _list_runs_text_field(record["status"]),
                _list_runs_text_field(record["workflow"]),
                _list_runs_text_field(_progress_label(record["progress"])),
                _list_runs_text_field(_runner_list_label(record["runner"])),
                _list_runs_text_field(_background_list_label(record["background"])),
                _list_runs_text_field(_launch_list_label(record["launch"])),
                _list_runs_text_field(record.get("mtime_utc") or ""),
            )
        )
    return 0


def _diagnose_run(args) -> int:
    run_dir = args.run_dir
    if not run_dir.name or run_dir.name in {".", ".."}:
        raise ValidationError("run_dir must identify a concrete run directory")
    detail = collect_run_detail(run_dir.parent, run_dir.name, max_text_bytes=args.max_text_bytes)
    diagnostics = detail.get("diagnostics") if isinstance(detail.get("diagnostics"), dict) else {}
    if args.json:
        payload = {
            "schema": "conductor.run_diagnostics_report.v1",
            "run_id": detail.get("id") or run_dir.name,
            "status": detail.get("status") or "unknown",
            "diagnostics": diagnostics,
        }
        print(json.dumps(redact_json_value(payload), indent=2, sort_keys=True))
        return 0
    print("Run diagnostics: %s" % _list_runs_text_field(diagnostics.get("severity") or "unknown"))
    print("Summary: %s" % _list_runs_text_field(diagnostics.get("summary") or "No current diagnostic issues."))
    issues = diagnostics.get("issues") if isinstance(diagnostics.get("issues"), list) else []
    if not issues:
        return 0
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        print(
            "Issue: %s %s - %s"
            % (
                _list_runs_text_field(issue.get("severity") or "unknown"),
                _list_runs_text_field(issue.get("code") or "unknown"),
                _list_runs_text_field(issue.get("message") or ""),
            )
        )
        if issue.get("detail"):
            print("  Detail: %s" % _list_runs_text_field(issue.get("detail") or ""))
        if issue.get("recommendation"):
            print("  Recommendation: %s" % _list_runs_text_field(issue.get("recommendation") or ""))
        if issue.get("command"):
            print("  Command: %s" % _list_runs_text_field(issue.get("command") or ""))
    return 0


def _run_console(args) -> int:
    if (
        isinstance(args.refresh_ms, bool)
        or not isinstance(args.refresh_ms, int)
        or not RUN_CONSOLE_MIN_REFRESH_MS
        <= args.refresh_ms
        <= RUN_CONSOLE_MAX_REFRESH_MS
    ):
        raise ValidationError(
            "run-console refresh-ms must be from %d to %d"
            % (RUN_CONSOLE_MIN_REFRESH_MS, RUN_CONSOLE_MAX_REFRESH_MS)
        )
    if (
        isinstance(args.timeout_seconds, bool)
        or not isinstance(args.timeout_seconds, int)
        or not 1 <= args.timeout_seconds <= RUN_CONSOLE_MAX_TIMEOUT_SECONDS
    ):
        raise ValidationError(
            "run-console timeout-seconds must be from 1 to %d"
            % RUN_CONSOLE_MAX_TIMEOUT_SECONDS
        )
    if args.width is None:
        width = shutil.get_terminal_size((120, 40)).columns
        width = max(RUN_CONSOLE_MIN_WIDTH, min(RUN_CONSOLE_MAX_WIDTH, width))
    else:
        width = args.width
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or not RUN_CONSOLE_MIN_WIDTH <= width <= RUN_CONSOLE_MAX_WIDTH
        ):
            raise ValidationError(
                "run-console width must be from %d to %d"
                % (RUN_CONSOLE_MIN_WIDTH, RUN_CONSOLE_MAX_WIDTH)
            )
    run_dir = args.run_dir
    if not run_dir.name or run_dir.name in {".", ".."}:
        raise ValidationError("run_dir must identify a concrete run directory")

    def collect_snapshot():
        detail = collect_run_detail(
            run_dir.parent,
            run_dir.name,
            max_text_bytes=1,
            max_files=1,
        )
        return build_run_console_snapshot(detail)

    snapshot = collect_snapshot()
    if args.json:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
        return 0
    deadline = time.monotonic() + args.timeout_seconds
    last_fingerprint = None
    use_clear = bool(
        args.follow
        and not args.no_clear
        and hasattr(sys.stdout, "isatty")
        and sys.stdout.isatty()
    )
    try:
        while True:
            fingerprint = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
            if fingerprint != last_fingerprint:
                if use_clear:
                    sys.stdout.write("\x1b[2J\x1b[H")
                elif last_fingerprint is not None:
                    sys.stdout.write("\n")
                sys.stdout.write(render_run_console(snapshot, width=width))
                sys.stdout.flush()
                last_fingerprint = fingerprint
            if not args.follow or run_console_is_terminal(snapshot):
                return 0
            if time.monotonic() >= deadline:
                raise ValidationError("run-console follow timed out")
            time.sleep(args.refresh_ms / 1000.0)
            snapshot = collect_snapshot()
    except KeyboardInterrupt:
        if use_clear:
            sys.stdout.write("\n")
            sys.stdout.flush()
        return 130


def _run_list_record(run: Dict) -> Dict:
    runner = run.get("runner") if isinstance(run.get("runner"), dict) else {}
    return {
        "id": str(run.get("id") or "unknown"),
        "handle": run_handle(str(run.get("id") or "")),
        "workflow": str(run.get("workflow") or "unknown"),
        "status": str(run.get("status") or "unknown"),
        "started_at_utc": str(run.get("started_at_utc") or ""),
        "finished_at_utc": str(run.get("finished_at_utc") or ""),
        "mtime_utc": str(run.get("mtime_utc") or ""),
        "duration_ms": run.get("duration_ms") if isinstance(run.get("duration_ms"), int) else None,
        "progress": _run_progress(run),
        "runner": _compact_runner(runner),
        "background": _compact_background(
            run.get("background") if isinstance(run.get("background"), dict) else {}
        ),
        "launch": _compact_launch(run.get("launch") if isinstance(run.get("launch"), dict) else {}),
        "usage": _compact_usage(run.get("usage") if isinstance(run.get("usage"), dict) else {}),
        "evidence_complete": bool(run.get("evidence_complete")),
    }


def _run_progress(run: Dict) -> Dict:
    counts = {
        "total_steps": 0,
        "completed_steps": 0,
        "skipped_steps": 0,
        "pending_steps": 0,
        "planned_steps": 0,
        "running_steps": 0,
        "failed_steps": 0,
        "blocked_steps": 0,
        "other_steps": 0,
        "terminal_steps": 0,
        "percent_complete": None,
    }
    phases = run.get("phases") if isinstance(run.get("phases"), list) else []
    if phases:
        for phase in phases:
            if not isinstance(phase, dict):
                continue
            step_counts = phase.get("step_counts") if isinstance(phase.get("step_counts"), dict) else {}
            counted = 0
            for status, value in step_counts.items():
                if isinstance(value, int) and value > 0:
                    counted += value
                    _add_progress_count(counts, status, value)
            phase_total = phase.get("steps_total")
            if not isinstance(phase_total, int) or phase_total < counted:
                phase_total = counted
            if phase_total > counted:
                counts["pending_steps"] += phase_total - counted
            counts["total_steps"] += phase_total
    else:
        steps = run.get("steps") if isinstance(run.get("steps"), list) else []
        counts["total_steps"] = len(steps)
        for step in steps:
            if not isinstance(step, dict):
                continue
            _add_progress_count(counts, step.get("status"), 1)
    counts["terminal_steps"] = (
        counts["completed_steps"]
        + counts["skipped_steps"]
        + counts["failed_steps"]
        + counts["blocked_steps"]
    )
    total = counts["total_steps"]
    if total:
        done = counts["completed_steps"] + counts["skipped_steps"]
        counts["percent_complete"] = int((done * 100) / total)
    return counts


def _add_progress_count(counts: Dict, status, value: int) -> None:
    normalized = str(status or "unknown")
    if normalized == "completed":
        counts["completed_steps"] += value
    elif normalized == "skipped":
        counts["skipped_steps"] += value
    elif normalized == "pending":
        counts["pending_steps"] += value
    elif normalized == "planned":
        counts["planned_steps"] += value
    elif normalized == "running":
        counts["running_steps"] += value
    elif normalized == "failed":
        counts["failed_steps"] += value
    elif normalized == "blocked":
        counts["blocked_steps"] += value
    else:
        counts["other_steps"] += value


def _list_runs_text_field(value) -> str:
    text = redact_text(str(value))
    safe = []
    for char in text:
        codepoint = ord(char)
        if char == "\t":
            safe.append("\\t")
        elif char == "\n":
            safe.append("\\n")
        elif char == "\r":
            safe.append("\\r")
        elif codepoint < 32 or codepoint == 127 or 128 <= codepoint <= 159:
            safe.append("\\x%02x" % codepoint)
        else:
            safe.append(char)
    return "".join(safe)


def _compact_runner(runner: Dict) -> Dict:
    keys = [
        "status",
        "liveness",
        "event",
        "run_status",
        "mode",
        "pid",
        "heartbeat_at_utc",
        "heartbeat_age_seconds",
        "stale_after_seconds",
        "finished_at_utc",
        "error",
    ]
    return {key: runner.get(key) for key in keys if runner.get(key) is not None}


def _compact_usage(usage: Dict) -> Dict:
    return {
        "status": usage.get("status") or "unavailable",
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cost_usd": usage.get("cost_usd"),
    }


def _compact_background(background: Dict) -> Dict:
    keys = [
        "status",
        "run_status",
        "pid",
        "process_group_id",
        "session_id",
        "detached_session",
        "approval_count",
        "approval_values_persisted",
        "payload_persisted",
        "daemon_installed",
        "max_workers",
        "exit_code",
        "error_class",
        "updated_at_utc",
        "finished_at_utc",
        "error",
    ]
    return {key: background.get(key) for key in keys if background.get(key) is not None}


def _compact_launch(launch: Dict) -> Dict:
    policy = launch.get("policy") if isinstance(launch.get("policy"), dict) else {}
    external = launch.get("external_supervisor") if isinstance(launch.get("external_supervisor"), dict) else {}
    record = {
        "status": launch.get("status") or "unavailable",
        "kind": launch.get("kind"),
        "created_at_utc": launch.get("created_at_utc"),
        "workflow": launch.get("workflow"),
        "preflight_status": launch.get("preflight_status"),
        "dry_run": launch.get("dry_run"),
        "steps_planned": launch.get("steps_planned"),
        "no_process_started": launch.get("no_process_started"),
        "process_model": launch.get("process_model"),
        "resume_command": launch.get("resume_command"),
        "resume_command_truncated": launch.get("resume_command_truncated"),
        "resume_command_error": launch.get("resume_command_error"),
        "policy": {key: value for key, value in policy.items() if value is not None},
        "max_workers": launch.get("max_workers"),
        "external_supervisor": {key: value for key, value in external.items() if value is not None},
        "error": launch.get("error"),
    }
    return {key: value for key, value in record.items() if value is not None}


def _progress_label(progress: Dict) -> str:
    total = progress.get("total_steps") or 0
    done = (progress.get("completed_steps") or 0) + (progress.get("skipped_steps") or 0)
    percent = progress.get("percent_complete")
    if percent is None:
        return "%s/%s steps" % (done, total)
    return "%s/%s steps %s%%" % (done, total, percent)


def _runner_list_label(runner: Dict) -> str:
    status = str(runner.get("status") or "unavailable")
    liveness = str(runner.get("liveness") or "")
    if liveness and liveness not in {"finished", "unavailable"}:
        return "%s/%s" % (status, liveness)
    return status


def _background_list_label(background: Dict) -> str:
    if not isinstance(background, dict):
        return "unavailable"
    status = str(background.get("status") or "unavailable")
    if status == "unavailable":
        return status
    run_status = str(background.get("run_status") or "")
    return "%s/%s" % (status, run_status) if run_status else status


def _launch_list_label(launch: Dict) -> str:
    if not isinstance(launch, dict):
        return "unavailable"
    status = str(launch.get("status") or "unavailable")
    if status == "unavailable":
        return status
    parts = [status]
    preflight = str(launch.get("preflight_status") or "")
    if preflight:
        parts.append("preflight:%s" % preflight)
    if launch.get("no_process_started") is True:
        parts.append("no-process")
    policy = launch.get("policy") if isinstance(launch.get("policy"), dict) else {}
    approval_count = policy.get("approval_count")
    if isinstance(approval_count, int) and not isinstance(approval_count, bool) and approval_count > 0:
        parts.append("approvals:%s" % approval_count)
    if launch.get("error"):
        parts.append("error:%s" % launch.get("error"))
    return ",".join(parts)


def _list_saved_workflows(args) -> int:
    entries = iter_saved_workflow_entries(args.workspace, **_saved_workflow_scope_kwargs(args))
    for entry in entries:
        try:
            saved = load_saved_workflow(entry.path, scope=entry.scope, rank=entry.rank)
        except ConductorError as exc:
            print("INVALID: %s (%s)" % (redact_text(str(entry.path)), redact_text(str(exc))))
            continue
        print(
            "%s\t%s\t%s\t%s"
            % (
                redact_text(str(entry.path)),
                redact_text(saved.command_name),
                redact_text(saved.scope),
                redact_text(saved.description),
            )
        )
    return 0


def _inspect_saved_workflow(args) -> int:
    saved = _render_saved_workflow_for_cli(_resolve_saved_workflow_for_cli(args.saved_workflow, args), args)
    print("saved: %s" % redact_text(str(saved.path)))
    print("command: %s" % redact_text(saved.command_name))
    print("scope: %s" % redact_text(saved.scope))
    print("description: %s" % redact_text(saved.description))
    if saved.when_to_use:
        print("when to use: %s" % redact_text(saved.when_to_use))
    print(workflow_summary(saved.workflow))
    return 0


def _export_saved_workflow(args) -> int:
    saved = _render_saved_workflow_for_cli(_resolve_saved_workflow_for_cli(args.saved_workflow, args), args)
    destination = write_saved_workflow_json(saved, args.destination)
    print("Saved workflow JSON: %s" % redact_text(str(destination)))
    return 0


def _run_saved_workflow(args) -> int:
    if args.dry_run and args.desktop_notify:
        raise ValidationError("--desktop-notify requires real workflow execution, not --dry-run")
    saved = _render_saved_workflow_for_cli(_resolve_saved_workflow_for_cli(args.saved_workflow, args), args)
    _validate_print_result_request(saved.workflow, args)
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        allow_network=args.allow_network,
        allow_agent=args.allow_agent,
        allow_parallel=args.allow_parallel,
        approvals=approvals,
    )
    runner = WorkflowRunner(
        workflow=saved.workflow,
        workspace=args.workspace,
        base_run_dir=args.runs_dir,
        policy=policy,
        dry_run=args.dry_run,
        run_id=args.run_id,
        resume_dir=args.resume,
        max_workers=args.max_workers,
    )
    try:
        run = runner.execute()
    except Exception:
        _notify_run_fail_open(runner.run, args.desktop_notify)
        raise
    print("Saved workflow: %s" % redact_text(saved.command_name))
    print("Run directory: %s" % redact_text(str(run.run_dir)))
    status = str(run.read_state().get("status") or "failed")
    print("Status: %s" % status)
    _print_workflow_result(run, saved.workflow, status, args.print_result)
    _notify_run_fail_open(run, args.desktop_notify, status=status)
    return 0


def _notify_run_fail_open(run, requested: bool, *, status: str = ""):
    if not requested:
        return None
    try:
        current = status or str(run.read_state().get("status") or "failed")
        if current not in RUN_NOTIFICATION_STATUSES:
            current = desktop_notification_status_for_execution(current)
        receipt = send_run_desktop_notification(run, current)
        print(
            "Desktop notification: %s (%s)"
            % (receipt["status"], receipt["backend"])
        )
        return receipt
    except Exception as exc:
        print(
            "WARNING: desktop notification unavailable: %s" % exc.__class__.__name__,
            file=sys.stderr,
        )
        return None


def _notify_execution_fail_open(
    requested: bool,
    status: str,
    *,
    run_dir: Optional[Path] = None,
    artifact_path: Optional[Path] = None,
):
    if not requested:
        return None
    try:
        if artifact_path is not None:
            receipt = send_artifact_desktop_notification(artifact_path, status)
        elif run_dir is not None:
            receipt = send_run_desktop_notification(
                RunArtifacts.resume(Path(run_dir)),
                desktop_notification_status_for_execution(status),
            )
        else:
            raise ValidationError("desktop notification has no terminal artifact")
        print(
            "Desktop notification: %s (%s)"
            % (receipt["status"], receipt["backend"])
        )
        return receipt
    except Exception as exc:
        print(
            "WARNING: desktop notification unavailable: %s" % exc.__class__.__name__,
            file=sys.stderr,
        )
        return None


def _saved_workflow_scope_kwargs(args) -> Dict:
    return {
        "project_root": args.project_root,
        "include_personal": args.include_personal or args.personal_workflows_dir is not None,
        "home": args.home,
        "personal_workflows_dir": args.personal_workflows_dir,
    }


def _resolve_saved_workflow_for_cli(identifier: str, args):
    return resolve_saved_workflow(identifier, args.workspace, **_saved_workflow_scope_kwargs(args))


def _render_saved_workflow_for_cli(saved, args):
    return apply_saved_workflow_args(saved, parse_saved_workflow_args(args.arg))


def _init(args) -> int:
    if args.destination.exists():
        raise ValidationError("destination already exists: %s" % args.destination)
    workflow = load_workflow(args.template)
    template_text = read_regular_text_file_no_follow(args.template, "workflow template", MAX_WORKFLOW_JSON_BYTES)
    write_new_text_file_no_follow(args.destination, "workflow destination", template_text)
    print(
        "Copied %s workflow to %s"
        % (redact_text(workflow["name"]), redact_text(str(args.destination)))
    )
    return 0


def _doctor(args) -> int:
    approvals = validate_approval_tokens(args.approve)
    if args.repair_codex_host:
        codex = repair_codex_host_link(approvals=approvals)
    else:
        codex = inspect_codex_installation()
    payload = {
        "schema": "conductor.system_doctor.v1",
        "python": "%s.%s.%s" % sys.version_info[:3],
        "codex": codex,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("python: %s" % payload["python"])
        print("codex: %s" % codex.get("codex_launcher", "not found"))
        print("codex command host: %s" % codex["status"])
        if codex.get("expected_host"):
            print("codex command host path: %s" % codex["expected_host"])
        if codex["status"] != "ready":
            print(
                "repair: doctor --repair-codex-host --approve %s"
                % CODEX_HOST_REPAIR_APPROVAL
            )
        elif codex.get("repaired"):
            print("codex command host link: repaired")
    return 0 if codex["healthy"] else 1


def _schema(args) -> int:
    if args.list or not args.schema_type:
        if args.schema_type:
            raise ValidationError("schema type cannot be supplied with --list")
        records = schema_types()
        if args.json:
            print(json.dumps({"schema": SCHEMA_EXPORT_SCHEMA, "schemas": records}, indent=2, sort_keys=True))
            return 0
        for record in records:
            print(
                "%s\t%s\t%s"
                % (
                    redact_text(record["name"]),
                    redact_text(record["artifact_schema"]),
                    redact_text(record["title"]),
                )
            )
        return 0
    if args.json:
        raise ValidationError("--json is only supported with --list")
    print(schema_json(args.schema_type), end="")
    return 0


def _write_schema(args) -> int:
    destination = Path(args.destination)
    if destination.exists() or destination.is_symlink():
        raise ValidationError("schema output already exists: %s" % destination)
    try:
        write_new_text_file_no_follow(destination, "schema output", schema_json(args.schema_type))
    except FileExistsError:
        raise ValidationError("schema output already exists: %s" % destination)
    print("Schema written: %s" % redact_text(str(destination)))
    return 0


def _benchmark(args) -> int:
    import json

    suite = load_benchmark_suite(args.suite)
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        allow_network=args.allow_network,
        approvals=approvals,
    )
    report = run_benchmark_suite(
        suite=suite,
        workspace=args.workspace,
        policy=policy,
        system_name=args.system,
        output_limit_bytes=args.output_limit_bytes,
    )
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        write_text_file_no_follow(args.output, "benchmark report output", text)
        print("Benchmark report: %s" % redact_text(str(args.output)))
    else:
        print(text, end="")
    return 0 if report["failed_tasks"] == 0 else 1


def _score_benchmark_report(args) -> int:
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        raise ValidationError("scored benchmark report output already exists: %s" % output)
    report = load_benchmark_report(args.report)
    parity_tasks = load_parity_tasks(args.parity_tasks)
    score_input = load_benchmark_score_input(args.score_input)
    scored = score_benchmark_report(report, parity_tasks, score_input)
    validate_benchmark_report(scored)
    try:
        write_new_text_file_no_follow(
            output,
            "scored benchmark report output",
            json.dumps(scored, indent=2, sort_keys=True) + "\n",
        )
    except FileExistsError:
        raise ValidationError("scored benchmark report output already exists: %s" % output)
    print("Scored benchmark report: %s" % redact_text(str(output)))
    print("Quality-scored tasks: %d" % len(scored["results"]))
    return 0 if scored["failed_tasks"] == 0 else 1


def _validate_benchmark_quality_evidence(args) -> int:
    for path in args.paths:
        load_benchmark_quality_evidence(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_benchmark_report(args) -> int:
    for path in args.paths:
        load_benchmark_report(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_benchmark_score_input(args) -> int:
    for path in args.paths:
        load_benchmark_score_input(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _prepare_inline_verified_apply(
    args,
    approvals,
    *,
    evidence_path: Path,
    workspace: Path,
    stage_dir: Optional[Path] = None,
    patch_path: Optional[Path] = None,
):
    if not args.apply_verified_stage:
        if args.apply_receipt is not None:
            raise ValidationError("--apply-receipt requires --apply-verified-stage")
        if args.allow_destructive:
            raise ValidationError("--allow-destructive requires --apply-verified-stage")
        return None
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        approvals=approvals,
    )
    receipt_path = preflight_new_verified_stage_apply(
        evidence_path,
        workspace,
        stage_dir=stage_dir,
        patch_path=patch_path,
        receipt_path=args.apply_receipt,
        policy=policy,
    )
    return {"policy": policy, "receipt_path": receipt_path}


def _run_inline_verified_apply(
    context: Dict,
    *,
    evidence_path: Path,
    workspace: Path,
    stage_dir: Optional[Path] = None,
    patch_path: Optional[Path] = None,
) -> Dict:
    result = apply_verified_stage(
        evidence_path,
        workspace,
        stage_dir=stage_dir,
        patch_path=patch_path,
        receipt_path=context["receipt_path"],
        policy=context["policy"],
    )
    _print_staged_apply_summary(result, context["receipt_path"])
    return result


def _effective_staged_exit_contract(args) -> str:
    if args.exit_contract is not None:
        return args.exit_contract
    return "verified-stage" if args.apply_verified_stage else "strict"


def _run_claude_readonly_task(args) -> int:
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        raise ValidationError("Claude benchmark report output already exists: %s" % output)
    parity_tasks = load_parity_tasks(args.parity_tasks)
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(
        allow_agent=args.allow_agent,
        allow_network=args.allow_network,
        approvals=approvals,
    )
    report = run_claude_readonly_task(
        parity_tasks=parity_tasks,
        task_id=args.task_id,
        workspace=args.workspace,
        policy=policy,
        max_budget_usd=args.max_budget_usd,
        max_turns=args.max_turns,
        timeout_seconds=args.timeout_seconds,
        output_limit_bytes=args.output_limit_bytes,
    )
    try:
        write_new_text_file_no_follow(
            output,
            "Claude benchmark report output",
            json.dumps(report, indent=2, sort_keys=True) + "\n",
        )
    except FileExistsError:
        raise ValidationError("Claude benchmark report output already exists: %s" % output)
    evidence = report["results"][0]["provider_evidence"]
    print("Claude benchmark report: %s" % redact_text(str(output)))
    print("Provider status: %s" % evidence["status"])
    return 0 if report["failed_tasks"] == 0 else 1


def _validate_claude_provider_evidence(args) -> int:
    for path in args.paths:
        load_claude_provider_evidence(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _run_claude_staged_task(args) -> int:
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        raise ValidationError("Claude staged benchmark report output already exists: %s" % output)
    stage_dir = Path(args.stage_dir) if args.stage_dir is not None else output.parent / (output.stem + ".stage")
    patch_output = (
        Path(args.patch_output)
        if args.patch_output is not None
        else output.parent / (output.stem + ".patch")
    )
    workspace = Path(args.workspace)
    require_path_outside_workspace(workspace, output, "Claude staged benchmark report output")
    require_path_outside_workspace(stage_dir, output, "Claude staged benchmark report output")
    if output.resolve() == patch_output.resolve():
        raise ValidationError("Claude staged report and patch outputs must be different paths")
    parity_tasks = load_parity_tasks(args.parity_tasks)
    approvals = validate_approval_tokens(args.approve)
    inline_apply = _prepare_inline_verified_apply(
        args,
        approvals,
        evidence_path=output,
        workspace=workspace,
        stage_dir=stage_dir,
        patch_path=patch_output,
    )
    policy = RuntimePolicy(
        allow_agent=args.allow_agent,
        allow_network=args.allow_network,
        allow_writes=args.allow_writes,
        approvals=approvals,
    )
    report = run_claude_staged_task(
        parity_tasks=parity_tasks,
        task_id=args.task_id,
        workspace=workspace,
        stage_dir=stage_dir,
        patch_output=patch_output,
        check_command=parse_check_command_json(args.check_command_json),
        policy=policy,
        max_budget_usd=args.max_budget_usd,
        max_turns=args.max_turns,
        timeout_seconds=args.timeout_seconds,
        check_timeout_seconds=args.check_timeout_seconds,
        output_limit_bytes=args.output_limit_bytes,
        check_output_limit_bytes=args.check_output_limit_bytes,
    )
    try:
        write_new_text_file_no_follow(
            output,
            "Claude staged benchmark report output",
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            sync=inline_apply is not None,
        )
    except FileExistsError:
        raise ValidationError("Claude staged benchmark report output already exists: %s" % output)
    result = report["results"][0]
    print("Claude staged benchmark report: %s" % redact_text(str(output)))
    print("Provider status: %s" % result["provider_evidence"]["status"])
    print("Staged status: %s" % result["staged_evidence"]["status"])
    _print_staged_completion_summary(result)
    print("Stage directory: %s" % redact_text(str(stage_dir)))
    if result["staged_evidence"]["changes"]["patch_written"]:
        print("Patch output: %s" % redact_text(str(patch_output)))
    if inline_apply is not None:
        _run_inline_verified_apply(
            inline_apply,
            evidence_path=output,
            workspace=workspace,
            stage_dir=stage_dir,
            patch_path=patch_output,
        )
    return _staged_cli_exit_code(
        report,
        result,
        _effective_staged_exit_contract(args),
        patch_output,
        accepted_delivery=inline_apply is not None,
    )


def _validate_claude_staged_evidence(args) -> int:
    for path in args.paths:
        load_claude_staged_evidence(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _run_codex_readonly_task(args) -> int:
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        raise ValidationError("Codex benchmark report output already exists: %s" % output)
    stage_dir = Path(args.stage_dir) if args.stage_dir is not None else output.parent / (output.stem + ".stage")
    workspace = Path(args.workspace)
    require_path_outside_workspace(workspace, output, "Codex benchmark report output")
    require_path_outside_workspace(workspace, stage_dir, "Codex parity stage directory")
    require_path_outside_workspace(stage_dir, output, "Codex benchmark report output")
    parity_tasks = load_parity_tasks(args.parity_tasks)
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(
        allow_agent=args.allow_agent,
        allow_network=args.allow_network,
        approvals=approvals,
    )
    report = run_codex_readonly_task(
        parity_tasks=parity_tasks,
        task_id=args.task_id,
        workspace=workspace,
        stage_dir=stage_dir,
        policy=policy,
        model=args.model,
        effort=args.effort,
        service_tier=args.service_tier,
        max_tokens=args.max_tokens,
        timeout_seconds=args.timeout_seconds,
        output_limit_bytes=args.output_limit_bytes,
        tool_output_token_limit=args.tool_output_token_limit,
    )
    try:
        write_new_text_file_no_follow(
            output,
            "Codex benchmark report output",
            json.dumps(report, indent=2, sort_keys=True) + "\n",
        )
    except FileExistsError:
        raise ValidationError("Codex benchmark report output already exists: %s" % output)
    evidence = report["results"][0]["provider_evidence"]
    print("Codex benchmark report: %s" % redact_text(str(output)))
    print("Provider status: %s" % evidence["status"])
    print("Stage directory: %s" % redact_text(str(stage_dir)))
    return 0 if report["failed_tasks"] == 0 else 1


def _validate_codex_provider_evidence(args) -> int:
    for path in args.paths:
        load_codex_provider_evidence(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _run_codex_staged_task(args) -> int:
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        raise ValidationError("Codex staged benchmark report output already exists: %s" % output)
    stage_dir = Path(args.stage_dir) if args.stage_dir is not None else output.parent / (output.stem + ".stage")
    patch_output = (
        Path(args.patch_output)
        if args.patch_output is not None
        else output.parent / (output.stem + ".patch")
    )
    workspace = Path(args.workspace)
    require_path_outside_workspace(workspace, output, "Codex staged benchmark report output")
    require_path_outside_workspace(stage_dir, output, "Codex staged benchmark report output")
    if output.resolve() == patch_output.resolve():
        raise ValidationError("Codex staged report and patch outputs must be different paths")
    parity_tasks = load_parity_tasks(args.parity_tasks)
    approvals = validate_approval_tokens(args.approve)
    inline_apply = _prepare_inline_verified_apply(
        args,
        approvals,
        evidence_path=output,
        workspace=workspace,
        stage_dir=stage_dir,
        patch_path=patch_output,
    )
    policy = RuntimePolicy(
        allow_agent=args.allow_agent,
        allow_network=args.allow_network,
        allow_writes=args.allow_writes,
        approvals=approvals,
    )
    report = run_codex_staged_task(
        parity_tasks=parity_tasks,
        task_id=args.task_id,
        workspace=workspace,
        stage_dir=stage_dir,
        patch_output=patch_output,
        check_command=parse_check_command_json(args.check_command_json),
        policy=policy,
        model=args.model,
        effort=args.effort,
        service_tier=args.service_tier,
        max_tokens=args.max_tokens,
        timeout_seconds=args.timeout_seconds,
        check_timeout_seconds=args.check_timeout_seconds,
        output_limit_bytes=args.output_limit_bytes,
        check_output_limit_bytes=args.check_output_limit_bytes,
        tool_output_token_limit=args.tool_output_token_limit,
    )
    try:
        write_new_text_file_no_follow(
            output,
            "Codex staged benchmark report output",
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            sync=inline_apply is not None,
        )
    except FileExistsError:
        raise ValidationError("Codex staged benchmark report output already exists: %s" % output)
    result = report["results"][0]
    print("Codex staged benchmark report: %s" % redact_text(str(output)))
    print("Provider status: %s" % result["provider_evidence"]["status"])
    print("Staged status: %s" % result["staged_evidence"]["status"])
    _print_staged_completion_summary(result)
    print("Stage directory: %s" % redact_text(str(stage_dir)))
    if result["staged_evidence"]["changes"]["patch_written"]:
        print("Patch output: %s" % redact_text(str(patch_output)))
    if inline_apply is not None:
        _run_inline_verified_apply(
            inline_apply,
            evidence_path=output,
            workspace=workspace,
            stage_dir=stage_dir,
            patch_path=patch_output,
        )
    return _staged_cli_exit_code(
        report,
        result,
        _effective_staged_exit_contract(args),
        patch_output,
        accepted_delivery=inline_apply is not None,
    )


def _print_staged_completion_summary(result) -> None:
    summary = result.get("completion_summary")
    if not isinstance(summary, dict):
        return
    changed = summary.get("changed_files") or []
    print("Files changed: %s" % (", ".join(changed) if changed else "none"))
    print("Verification: %s" % summary.get("verification_status", "unknown"))


def _staged_cli_exit_code(
    report: Dict,
    result: Dict,
    exit_contract: str,
    patch_output: Path,
    *,
    accepted_delivery: bool = False,
) -> int:
    if exit_contract == "strict":
        return 0 if report.get("failed_tasks") == 0 else 1
    if exit_contract != "verified-stage":
        raise ValidationError("staged exit contract is invalid")

    accepted = accepted_delivery or _verified_stage_delivery(result, patch_output)
    print("Verified stage accepted: %s" % ("yes" if accepted else "no"))
    if accepted and report.get("failed_tasks") != 0:
        print("Provider/report outcome remains failed; verified-stage delivery does not relabel it.")
    return 0 if accepted else 1


def _validate_codex_staged_evidence(args) -> int:
    for path in args.paths:
        load_codex_staged_evidence(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _run_codex_staged_repair(args) -> int:
    workspace = Path(args.workspace)
    repair_dir = Path(args.repair_dir)
    require_path_outside_workspace(workspace, repair_dir, "Codex staged repair directory")
    parity_tasks = load_parity_tasks(args.parity_tasks)
    approvals = validate_approval_tokens(args.approve)
    repair_evidence_path = repair_dir / "repair.json"
    inline_apply = _prepare_inline_verified_apply(
        args,
        approvals,
        evidence_path=repair_evidence_path,
        workspace=workspace,
    )
    policy = RuntimePolicy(
        allow_agent=args.allow_agent,
        allow_network=args.allow_network,
        allow_writes=args.allow_writes,
        approvals=approvals,
    )
    evidence = run_codex_staged_repair(
        parity_tasks=parity_tasks,
        task_id=args.task_id,
        workspace=workspace,
        repair_dir=repair_dir,
        check_command=parse_check_command_json(args.check_command_json),
        policy=policy,
        model=args.model,
        effort=args.effort,
        max_attempts=args.max_attempts,
        max_tokens_per_attempt=args.max_tokens_per_attempt,
        provider_timeout_seconds=args.provider_timeout_seconds,
        check_timeout_seconds=args.check_timeout_seconds,
        output_limit_bytes=args.output_limit_bytes,
        check_output_limit_bytes=args.check_output_limit_bytes,
        tool_output_token_limit=args.tool_output_token_limit,
        resume=args.resume,
        attempts_this_run=args.attempts_this_run,
    )
    if evidence.get("schema") == CODEX_STAGED_REPAIR_STATE_SCHEMA:
        print("Codex staged repair checkpoint: %s" % redact_text(str(repair_dir / "repair-state.json")))
        print("Status: %s" % evidence["status"])
        print("Attempts: %d/%d" % (len(evidence["attempts"]), evidence["config"]["max_attempts"]))
        print("Resume with the same bound arguments plus --resume; --attempts-this-run may change.")
        return 1
    print("Codex staged repair: %s" % redact_text(str(repair_dir / "repair.json")))
    print("Status: %s" % evidence["status"])
    print("Attempts: %d/%d" % (len(evidence["attempts"]), evidence["limits"]["max_attempts"]))
    print("Budget charged: %d/%d" % (
        evidence["usage"]["budget_charge_tokens"],
        evidence["limits"]["total_token_cap"],
    ))
    print("Aggregate report: %s" % redact_text(str(repair_dir / "benchmark-report.json")))
    if evidence["final"]["patch_written"]:
        print("Cumulative patch: %s" % redact_text(str(repair_dir / evidence["final"]["patch_name"])))
    if inline_apply is not None:
        _run_inline_verified_apply(
            inline_apply,
            evidence_path=repair_evidence_path,
            workspace=workspace,
        )
    return _staged_repair_cli_exit_code(
        evidence,
        repair_dir,
        _effective_staged_exit_contract(args),
        accepted_delivery=inline_apply is not None,
    )


def _staged_repair_cli_exit_code(
    evidence: Dict,
    repair_dir: Path,
    exit_contract: str,
    *,
    accepted_delivery: bool = False,
) -> int:
    if exit_contract == "strict":
        return 0 if evidence.get("status") == "success" else 1
    if exit_contract != "verified-stage":
        raise ValidationError("staged repair exit contract is invalid")

    accepted = accepted_delivery or _verified_repair_stage_delivery(evidence, repair_dir)
    print("Verified repair stage accepted: %s" % ("yes" if accepted else "no"))
    if accepted and evidence.get("status") != "success":
        print("Provider/repair outcome remains failed; verified-stage delivery does not relabel it.")
    return 0 if accepted else 1


def _validate_codex_staged_repair(args) -> int:
    for path in args.paths:
        load_codex_staged_repair_evidence(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_codex_staged_repair_state(args) -> int:
    for path in args.paths:
        load_codex_staged_repair_state(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _apply_verified_stage(args) -> int:
    approvals = validate_approval_tokens(args.approve)
    result = apply_verified_stage(
        args.evidence,
        args.workspace,
        stage_dir=args.stage_dir,
        patch_path=args.patch,
        receipt_path=args.receipt,
        policy=RuntimePolicy(
            allow_writes=args.allow_writes,
            allow_destructive=args.allow_destructive,
            approvals=approvals,
        ),
    )
    receipt = Path(args.receipt) if args.receipt is not None else default_staged_apply_receipt_path(args.evidence)
    _print_staged_apply_summary(result, receipt)
    return 0


def _print_staged_apply_summary(result: Dict, receipt: Path) -> None:
    print("Verified stage apply receipt: %s" % redact_text(str(receipt)))
    print("Status: %s" % result["status"])
    print("Files applied: %d" % result["change_count"])
    print("Already applied: %s" % ("yes" if result["already_applied"] else "no"))
    print("Provider/verifier calls: 0/0")


def _validate_staged_apply(args) -> int:
    for path in args.paths:
        load_staged_apply_receipt(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _compare(args) -> int:
    import json

    manifest = load_comparison_manifest(args.manifest)
    report = compare_benchmark_reports(manifest, args.base_dir)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        write_text_file_no_follow(args.output, "comparison report output", text)
        print("Comparison report: %s" % redact_text(str(args.output)))
    else:
        print(text, end="")
    if args.require_claim_eligible and not report["provenance"]["claim_eligible"]:
        warnings = report["provenance"].get("warnings", [])
        detail = " ".join(warnings) if warnings else "comparison is not claim-eligible"
        print("Comparison is not claim-eligible: %s" % redact_text(detail), file=sys.stderr)
        return 1
    return 0


def _validate_parity_tasks(args) -> int:
    for path in args.paths:
        load_parity_tasks(path)
        print("OK: %s" % path)
    return 0


def _plan_parity_campaign(args) -> int:
    parity_tasks = load_parity_tasks(args.parity_tasks)
    campaign = build_parity_campaign(
        parity_tasks,
        run_id=args.run_id,
        seed=args.seed,
        repetitions=args.repetitions,
        artifacts_dir=args.artifacts_dir,
        codex_model=args.codex_model,
        codex_effort=args.codex_effort,
    )
    print(json.dumps(campaign, indent=2, sort_keys=True))
    return 0


def _write_parity_campaign(args) -> int:
    parity_tasks = load_parity_tasks(args.parity_tasks)
    campaign = build_parity_campaign(
        parity_tasks,
        run_id=args.run_id,
        seed=args.seed,
        repetitions=args.repetitions,
        artifacts_dir=args.artifacts_dir,
        codex_model=args.codex_model,
        codex_effort=args.codex_effort,
    )
    output = write_parity_campaign(campaign, args.output)
    print("Parity campaign: %s" % redact_text(str(output)))
    return 0


def _validate_parity_campaign(args) -> int:
    for path in args.paths:
        load_parity_campaign(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _status_parity_campaign(args) -> int:
    status = campaign_status(args.campaign)
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    lifecycle = status["lifecycle"]
    print("Campaign: %s" % status["campaign"]["name"])
    print(
        "Coverage: %d/%d (%0.1f%%)"
        % (
            lifecycle["completed"],
            status["campaign"]["planned_trials"],
            lifecycle["coverage"] * 100.0,
        )
    )
    print(
        "Lifecycle: %d completed, %d partial, %d pending, %d invalid"
        % (lifecycle["completed"], lifecycle["partial"], lifecycle["pending"], lifecycle["invalid"])
    )
    release_cohort = status["release_cohort"]
    print(
        "Release cohort: %s; claim eligible: %s%s"
        % (
            ", ".join(release_cohort["versions"]) or "none",
            "yes" if release_cohort["claim_eligible"] else "no",
            ""
            if release_cohort["claim_eligible"]
            else " (%s)" % ", ".join(release_cohort["reasons"]),
        )
    )
    for system in PARITY_CAMPAIGN_SYSTEMS:
        summary = status["systems"][system]
        percentage = "n/a" if summary["percentage"] is None else "%0.1f%%" % summary["percentage"]
        print(
            "%s: %d/%d quality points (%s)"
            % (system.capitalize(), summary["score"], summary["max_score"], percentage)
        )
    paired = status["paired"]
    print(
        "Quality outcomes: %d Codex wins, %d Claude wins, %d ties"
        % (paired["codex_wins"], paired["claude_wins"], paired["ties"])
    )
    for key, label in [
        ("execution", "Execution outcomes"),
        ("final_success", "Final-success outcomes"),
        ("verified_delivery", "Verified-delivery outcomes"),
        ("latency", "Latency outcomes"),
        ("safety", "Safety outcomes"),
    ]:
        dimension = status["dimensions"][key]
        print(
            "%s: %d Codex wins, %d Claude wins, %d ties"
            % (label, dimension["codex_wins"], dimension["claude_wins"], dimension["ties"])
        )
    cohorts = status["enforcement"]["cohorts"]
    print(
        "Native-cap cohorts: %s"
        % (", ".join("%s=%d" % (item["id"], item["completed_pairs"]) for item in cohorts) or "none")
    )
    resources = status["resources"]
    print(
        "Cost coverage: %d/%d comparable pairs; outcomes %d Codex wins, %d Claude wins, %d ties"
        % (
            resources["cost"]["comparable_pairs"],
            resources["completed_pairs"],
            resources["cost"]["codex_wins"],
            resources["cost"]["claude_wins"],
            resources["cost"]["ties"],
        )
    )
    print(
        "Token coverage: %d/%d observed, %d/%d comparable pairs; outcomes %d Codex wins, %d Claude wins, %d ties"
        % (
            resources["tokens"]["observed_pairs"],
            resources["completed_pairs"],
            resources["tokens"]["comparable_pairs"],
            resources["completed_pairs"],
            resources["tokens"]["codex_wins"],
            resources["tokens"]["claude_wins"],
            resources["tokens"]["ties"],
        )
    )
    successful = status["successful_outcome_resources"]
    for key, label in [
        ("time_to_success", "Time-to-success"),
        ("cost_to_success", "Cost-to-success"),
        ("tokens_to_success", "Tokens-to-success"),
    ]:
        summary = successful[key]
        print(
            "%s: %d/%d comparable pairs; outcomes %d Codex wins, %d Claude wins, %d ties"
            % (
                label,
                summary["comparable_pairs"],
                successful["completed_pairs"],
                summary["codex_wins"],
                summary["claude_wins"],
                summary["ties"],
            )
        )
    verified_delivery = status["verified_delivery_resources"]
    for key, label in [
        ("time_to_success", "Time-to-verified-delivery"),
        ("cost_to_success", "Cost-to-verified-delivery"),
        ("tokens_to_success", "Tokens-to-verified-delivery"),
    ]:
        summary = verified_delivery[key]
        print(
            "%s: %d/%d comparable pairs; outcomes %d Codex wins, %d Claude wins, %d ties"
            % (
                label,
                summary["comparable_pairs"],
                verified_delivery["completed_pairs"],
                summary["codex_wins"],
                summary["claude_wins"],
                summary["ties"],
            )
        )
    print(
        "Claim eligible: %s%s"
        % (
            "yes" if status["claim"]["eligible"] else "no",
            "" if status["claim"]["eligible"] else " (%s)" % ", ".join(status["claim"]["reasons"]),
        )
    )
    print(
        "Broad superiority supported: %s%s"
        % (
            "yes" if status["inference"]["broad_superiority_supported"] else "no",
            ""
            if status["inference"]["broad_superiority_supported"]
            else " (%s)" % ", ".join(status["inference"]["reasons"]),
        )
    )
    print(
        "Product-profile broad superiority supported: %s%s"
        % (
            "yes" if status["product_profile_inference"]["broad_superiority_supported"] else "no",
            ""
            if status["product_profile_inference"]["broad_superiority_supported"]
            else " (%s)" % ", ".join(status["product_profile_inference"]["reasons"]),
        )
    )
    print(
        "Universal superiority supported: %s%s"
        % (
            "yes" if status["universal"]["superiority_supported"] else "no",
            ""
            if status["universal"]["superiority_supported"]
            else " (%s)" % ", ".join(status["universal"]["reasons"]),
        )
    )
    print(
        "Product-profile universal superiority supported: %s%s"
        % (
            "yes" if status["product_profile_universal"]["superiority_supported"] else "no",
            ""
            if status["product_profile_universal"]["superiority_supported"]
            else " (%s)" % ", ".join(status["product_profile_universal"]["reasons"]),
        )
    )
    return 0 if lifecycle["invalid"] == 0 else 1


def _run_parity_campaign_trial(args) -> int:
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        allow_network=args.allow_network,
        allow_agent=args.allow_agent,
        allow_parallel=args.allow_parallel,
        approvals=approvals,
    )
    result = run_parity_campaign_trial(
        campaign_path=args.campaign,
        parity_tasks_path=args.parity_tasks,
        trial_id=args.trial_id,
        next_trial=args.next_trial,
        fixture_root=args.fixture_root,
        policy=policy,
        resume=args.resume,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print("Campaign: %s" % result["campaign"])
    print("Trial: %s (%s)" % (result["trial_id"], result["task_id"]))
    print("Provider order: %s" % " -> ".join(result["system_order"]))
    if result["schema"].endswith("trial_plan.v1"):
        claude = result["systems"]["claude"]
        print("Claude lane: %s + %s" % (claude["model"], claude["effort"]))
        if result["provider_launches"] == 0:
            print("Provider launches: 0")
        else:
            print("Provider launches: 2 after explicit capability and approval gates")
        print("Status: %s" % ("dry-run" if result["dry_run"] else result["current_status"]))
        return 0
    print("Status: %s" % result["status"])
    print("Launched: %s" % (", ".join(result["launched_systems"]) or "none"))
    print("Scores: Codex %s/10, Claude %s/10" % (result["scores"]["codex"], result["scores"]["claude"]))
    print("Winner: %s" % result["winner"])
    print("Claim eligible: %s" % ("yes" if result["claim_eligible"] else "no"))
    return 0


def _auto_topology_campaign_from_args(args) -> Dict:
    tasks = load_parity_tasks(args.parity_tasks)
    return build_auto_topology_campaign(
        tasks,
        run_id=args.run_id,
        seed=args.seed,
        repetitions=args.repetitions,
        artifacts_dir=args.artifacts_dir,
        model=args.model,
        effort=args.effort,
        max_iterations=args.max_iterations,
        parallel_workers=args.parallel_workers,
        fixture_root=args.fixture_root,
    )


def _plan_auto_topology_campaign(args) -> int:
    print(json.dumps(_auto_topology_campaign_from_args(args), indent=2, sort_keys=True))
    return 0


def _write_auto_topology_campaign(args) -> int:
    output = write_auto_topology_campaign(_auto_topology_campaign_from_args(args), args.output)
    print("Auto topology campaign: %s" % redact_text(str(output)))
    return 0


def _validate_auto_topology_campaign(args) -> int:
    for path in args.paths:
        load_auto_topology_campaign(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _status_auto_topology_campaign(args) -> int:
    status = auto_topology_campaign_status(args.campaign)
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    lifecycle = status["lifecycle"]
    arms = status["arm_lifecycle"]
    print("Campaign: %s" % status["campaign"]["name"])
    print(
        "Release binding: %s; completed evidence uniform: %s"
        % (
            "exact" if status["campaign"]["release_bound"] else "legacy-unbound",
            "yes" if status["inference"]["release_uniform"] else "no",
        )
    )
    print(
        "Cohorts: %d/%d complete (%0.1f%%), %d partial, %d invalid"
        % (
            lifecycle["completed"],
            status["campaign"]["planned_cohorts"],
            lifecycle["coverage"] * 100.0,
            lifecycle["partial"],
            lifecycle["invalid"],
        )
    )
    print(
        "Arms: %d completed, %d partial, %d pending, %d invalid"
        % (arms["completed"], arms["partial"], arms["pending"], arms["invalid"])
    )
    for topology in AUTO_TOPOLOGIES:
        summary = status["topologies"][topology]
        score = "n/a" if summary["score_mean"] is None else "%0.2f/10" % summary["score_mean"]
        tokens = "incomparable" if summary["total_tokens_mean"] is None else "%0.0f" % summary["total_tokens_mean"]
        print(
            "%s: %d arms, mean score %s, mean calls %s, mean tokens %s"
            % (
                topology,
                summary["completed_arms"],
                score,
                "n/a" if summary["provider_calls_mean"] is None else "%0.2f" % summary["provider_calls_mean"],
                tokens,
            )
        )
    resources = status["resources"]
    print(
        "Resource coverage: tokens %d/%d, cost %d/%d"
        % (
            resources["token_observed_arms"],
            resources["completed_arms"],
            resources["cost_observed_arms"],
            resources["completed_arms"],
        )
    )
    inference = status["inference"]
    print(
        "All-resource topology recommendation supported: %s%s"
        % (
            "yes" if inference["recommendation_supported"] else "no",
            "" if inference["recommendation_supported"] else " (%s)" % ", ".join(inference["reasons"]),
        )
    )
    print(
        "Quality/latency/token recommendation supported: %s%s"
        % (
            "yes" if inference["quality_latency_token_recommendation_supported"] else "no",
            ""
            if inference["quality_latency_token_recommendation_supported"]
            else " (%s)" % ", ".join(inference["quality_latency_token_reasons"]),
        )
    )
    return 0 if lifecycle["invalid"] == 0 and arms["invalid"] == 0 else 1


def _run_auto_topology_arm(args) -> int:
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        allow_network=args.allow_network,
        allow_agent=args.allow_agent,
        allow_parallel=args.allow_parallel,
        approvals=approvals,
    )
    result = run_auto_topology_arm(
        campaign_path=args.campaign,
        parity_tasks_path=args.parity_tasks,
        cohort_id=args.cohort_id,
        topology=args.topology,
        next_arm=args.next_arm,
        fixture_root=args.fixture_root,
        policy=policy,
        resume=args.resume,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print("Campaign: %s" % result["campaign"])
    print("Cohort: %s (%s)" % (result["cohort_id"], result["task_id"]))
    print("Topology: %s" % result["topology"])
    if result["schema"] == "conductor.auto_topology_arm_plan.v1":
        print("Model: %s + %s" % (result["model"]["name"], result["model"]["effort"]))
        print(
            "Runtime compatibility: release=%s build=%s"
            % (
                "yes" if result["runtime_release_compatible"] else "no",
                "yes" if result["runtime_build_compatible"] else "no",
            )
        )
        print("Provider launches: %s" % ("0" if result["provider_launches"] == 0 else "approval-gated"))
        print("Status: %s" % ("dry-run" if result["dry_run"] else result["current_status"]))
        return 0
    print("Status: %s" % result["status"])
    print("Score: %d/%d" % (result["outcome"]["score"], result["outcome"]["max_score"]))
    print("Goal satisfied: %s" % ("yes" if result["outcome"]["goal_satisfied"] else "no"))
    print("Provider calls: %d" % result["resources"]["total_provider_calls"])
    tokens = result["resources"]["total_tokens"] if result["resources"]["token_observed"] else "incomparable"
    print("Tokens: %s" % tokens)
    return 0


def _readonly_parity_campaign_from_args(args) -> Dict:
    run_id = args.run_id or (
        "product-readonly-parity-campaign"
        if args.budget_profile == "product-v1"
        else "matched-readonly-parity-campaign"
    )
    return build_readonly_parity_campaign(
        load_parity_tasks(args.parity_tasks),
        run_id=run_id,
        seed=args.seed,
        repetitions=args.repetitions,
        artifacts_dir=args.artifacts_dir,
        codex_model=args.codex_model,
        codex_effort=args.codex_effort,
        budget_profile=args.budget_profile,
        fixture_root=args.fixture_root,
    )


def _plan_readonly_parity_campaign(args) -> int:
    print(json.dumps(_readonly_parity_campaign_from_args(args), indent=2, sort_keys=True))
    return 0


def _write_readonly_parity_campaign(args) -> int:
    output = write_readonly_parity_campaign(_readonly_parity_campaign_from_args(args), args.output)
    print("Read-only parity campaign: %s" % redact_text(str(output)))
    return 0


def _validate_readonly_parity_campaign(args) -> int:
    for path in args.paths:
        load_readonly_parity_campaign(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _status_readonly_parity_campaign(args) -> int:
    status = readonly_parity_campaign_status(args.campaign)
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    lifecycle = status["lifecycle"]
    arms = status["arm_lifecycle"]
    print("Campaign: %s" % status["campaign"]["name"])
    print(
        "Cohorts: %d/%d complete (%0.1f%%), %d partial, %d invalid"
        % (
            lifecycle["completed"],
            status["campaign"]["planned_cohorts"],
            lifecycle["coverage"] * 100.0,
            lifecycle["partial"],
            lifecycle["invalid"],
        )
    )
    print(
        "Arms: %d completed, %d partial, %d pending, %d invalid"
        % (arms["completed"], arms["partial"], arms["pending"], arms["invalid"])
    )
    for system in READONLY_PARITY_SYSTEMS:
        summary = status["systems"][system]
        score = "n/a" if summary["score_mean"] is None else "%0.2f/10" % summary["score_mean"]
        gross = "n/a" if summary["gross_tokens_mean"] is None else "%0.0f" % summary["gross_tokens_mean"]
        print(
            "%s: %d arms, mean score %s, final success %d, mean gross tokens %s, cap breaches %d"
            % (
                system,
                summary["completed_arms"],
                score,
                summary["final_successes"],
                gross,
                summary["cap_overshoot_arms"],
            )
        )
    inference = status["inference"]
    print(
        "Routing recommendation: %s"
        % (inference["routing_recommendation"] if inference["routing_recommendation_supported"] else "not supported")
    )
    print("Universal superiority supported: no")
    return 0 if lifecycle["invalid"] == 0 and arms["invalid"] == 0 else 1


def _run_readonly_parity_arm(args) -> int:
    policy = RuntimePolicy(
        allow_agent=args.allow_agent,
        allow_network=args.allow_network,
        allow_parallel=args.allow_parallel,
        approvals=validate_approval_tokens(args.approve),
    )
    result = run_readonly_parity_arm(
        campaign_path=args.campaign,
        parity_tasks_path=args.parity_tasks,
        cohort_id=args.cohort_id,
        system=args.system,
        next_arm=args.next_arm,
        fixture_root=args.fixture_root,
        policy=policy,
        resume=args.resume,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print("Campaign: %s" % result["campaign"])
    print("Cohort: %s (%s)" % (result["cohort_id"], result["task_id"]))
    print("System: %s" % result["system"])
    if result["schema"] == "conductor.readonly_parity_arm_plan.v1":
        print("Model: %s + %s" % (result["model"], result["effort"]))
        print("Provider launches: %s" % ("0" if result["provider_launches"] == 0 else "approval-gated"))
        print("Status: %s" % ("dry-run" if result["dry_run"] else result["current_status"]))
        return 0
    print("Status: %s" % result["status"])
    print("Score: %d/10" % result["score"])
    print("Provider success: %s" % ("yes" if result["provider_success"] else "no"))
    print("Final success: %s" % ("yes" if result["final_success"] else "no"))
    print("Topology executed: %s" % ("yes" if result["topology_executed"] else "no"))
    print("Duration: %d ms" % result["duration_ms"])
    if result["cap_compliant"] is None:
        print("Cap compliance: unavailable")
    else:
        print("Cap compliance: %s" % ("yes" if result["cap_compliant"] else "no"))
        print("Cap overshoot: %s" % result["cap_overshoot"])
    if result["weighted_tokens"] is not None:
        print("Weighted tokens: %d" % result["weighted_tokens"])
    if result["cost_usd"] is not None:
        print("Cost: $%0.6f" % result["cost_usd"])
    return 0


def _plan_live_parity_runbook(args) -> int:
    import json

    parity_tasks = load_parity_tasks(args.parity_tasks)
    runbook = build_live_parity_runbook(
        parity_tasks,
        run_id=args.run_id,
        report_dir=args.report_dir,
        workflow_dir=args.workflow_dir,
    )
    print(json.dumps(runbook, indent=2, sort_keys=True))
    return 0


def _write_live_parity_runbook(args) -> int:
    parity_tasks = load_parity_tasks(args.parity_tasks)
    runbook = build_live_parity_runbook(
        parity_tasks,
        run_id=args.run_id,
        report_dir=args.report_dir,
        workflow_dir=args.workflow_dir,
    )
    output = write_live_parity_runbook(runbook, args.output)
    print("Live parity runbook: %s" % redact_text(str(output)))
    return 0


def _validate_live_parity_runbook(args) -> int:
    for path in args.paths:
        load_live_parity_runbook(path)
        print("OK: %s" % path)
    return 0


def _validate_comparison(args) -> int:
    for path in args.paths:
        load_comparison_manifest(path)
        print("OK: %s" % path)
    return 0


def _validate_run_manifest(args) -> int:
    for path in args.paths:
        load_run_manifest(path)
        print("OK: %s" % path)
    return 0


def _write_run_manifest(args) -> int:
    report = load_benchmark_report(args.report)
    budget = {
        "max_minutes": args.max_minutes,
        "max_agents": args.max_agents,
        "max_live_tool_cost": args.max_live_tool_cost,
    }
    if args.max_cost_usd is not None:
        budget["max_cost_usd"] = args.max_cost_usd
    if args.max_tokens is not None:
        budget["max_tokens"] = args.max_tokens
    if args.cost_cap_enforcement is not None:
        budget["cost_cap_enforcement"] = args.cost_cap_enforcement
    if args.token_cap_enforcement is not None:
        budget["token_cap_enforcement"] = args.token_cap_enforcement
    repository = {
        "state": args.repository_state,
        "commit": args.repository_commit,
        "branch": args.repository_branch,
        "dirty": args.repository_dirty,
    }
    model = {
        "provider": args.model_provider,
        "name": args.model_name,
        "effort": args.model_effort,
        "version": args.model_version,
    }
    approvals = sorted(validate_approval_tokens(args.approved_token))
    permissions = {
        "allow_writes": args.allow_writes,
        "allow_destructive": args.allow_destructive,
        "allow_network": args.allow_network,
        "allow_agent": args.allow_agent,
        "allow_parallel": args.allow_parallel,
        "approved_tokens": approvals,
    }
    manifest = build_run_manifest_from_report(
        report=report,
        run_id=args.run_id,
        live_run=args.live_run,
        repository=repository,
        model=model,
        budget=budget,
        permissions=permissions,
        system_name=args.system,
        task_max_score=args.task_max_score,
    )
    output = write_run_manifest(manifest, args.output)
    print("Run manifest: %s" % redact_text(str(output)))
    return 0


def _evidence_bundle(args) -> int:
    bundle = _build_evidence_bundle_from_args(args)
    print(json.dumps(redact_json_value(bundle), indent=2, sort_keys=True))
    if args.require_claim_eligible:
        try:
            require_claim_eligible(bundle, source_reports=bundle["reports"])
        except ValidationError as exc:
            print("Evidence bundle is not claim-eligible: %s" % redact_text(str(exc)), file=sys.stderr)
            return 1
    return 0


def _write_evidence_bundle(args) -> int:
    bundle = _build_evidence_bundle_from_args(args)
    output = write_evidence_bundle(bundle, args.output)
    print("Evidence bundle: %s" % redact_text(str(output)))
    if args.require_claim_eligible:
        try:
            require_claim_eligible(bundle, source_reports=bundle["reports"])
        except ValidationError as exc:
            print("Evidence bundle is not claim-eligible: %s" % redact_text(str(exc)), file=sys.stderr)
            return 1
    return 0


def _validate_evidence_bundle(args) -> int:
    failed_claim_gate = False
    source_reports = None
    if args.require_claim_eligible:
        source_reports = collect_evidence_report_records(args.benchmarks_dir, max_reports=args.max_reports)
    for path in args.paths:
        bundle = load_evidence_bundle(path)
        if args.require_claim_eligible:
            try:
                require_claim_eligible(bundle, source_reports=source_reports)
            except ValidationError as exc:
                failed_claim_gate = True
                print("CLAIM-BLOCKED: %s (%s)" % (redact_text(str(path)), redact_text(str(exc))))
                continue
        print("OK: %s" % redact_text(str(path)))
    return 1 if failed_claim_gate else 0


def _build_evidence_bundle_from_args(args) -> Dict:
    return build_evidence_bundle(
        runs_dir=args.runs_dir,
        benchmarks_dir=args.benchmarks_dir,
        goals_dir=args.goals_dir,
        routines_dir=args.routines_dir,
        max_runs=args.max_runs,
        max_reports=args.max_reports,
        max_goals=args.max_goals,
        max_routines=args.max_routines,
    )


def _validate_routine_manifest(args) -> int:
    for path in args.paths:
        load_routine_manifest(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_routine_controls(args) -> int:
    for path in args.paths:
        load_routine_controls_file(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_routine_state(args) -> int:
    for path in args.paths:
        load_supervisor_state_file(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_routine_service_grant(args) -> int:
    for path in args.paths:
        load_routine_service_grant(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _validate_routine_service_state(args) -> int:
    for path in args.paths:
        load_routine_service_state(path)
        print("OK: %s" % redact_text(str(path)))
    return 0


def _write_routine_manifest(args) -> int:
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        allow_network=args.allow_network,
        allow_agent=args.allow_agent,
        allow_parallel=args.allow_parallel,
        approvals=approvals,
    )
    schedule = build_schedule(
        manual=args.manual,
        interval_minutes=args.interval_minutes,
        daily_at=args.daily_at,
        weekdays_at=args.weekdays_at,
        weekly_at=args.weekly_at,
        monthly_at=args.monthly_at,
        once_at=args.once_at,
        cron=args.cron,
        timezone=args.timezone,
    )
    budget = {
        "max_minutes": args.max_minutes,
        "max_cost_usd": args.max_cost_usd,
        "max_tokens": args.max_tokens,
    }
    workspace = Path(args.workspace).resolve()
    runs_dir = Path(args.runs_dir).resolve()
    runtime_command = runtime_self_command()
    runtime_sha256 = None
    if len(runtime_command) == 2 and Path(runtime_command[1]).suffix == ".pyz":
        runtime_sha256 = hashlib.sha256(
            read_regular_file_bytes_no_follow(
                Path(runtime_command[1]),
                "routine packaged runtime",
                max_bytes=25 * 1024 * 1024,
            )
        ).hexdigest()
    if args.workflow:
        if args.arg:
            raise ValidationError("--arg is only supported with --saved-workflow")
        workflow_source = Path(args.workflow)
        workflow = load_workflow(workflow_source)
        workflow_path = workflow_source.absolute()
        target = {"kind": "workflow", "workflow_path": str(workflow_path)}
        command_argv = runtime_command + [
            "run",
            str(workflow_path),
            "--workspace",
            str(workspace),
            "--runs-dir",
            str(runs_dir),
        ]
    else:
        saved = _render_saved_workflow_for_cli(_resolve_saved_workflow_for_cli(args.saved_workflow, args), args)
        workflow = saved.workflow
        target = {
            "kind": "saved_workflow",
            "saved_command": saved.command_name,
            "saved_scope": saved.scope,
            "saved_path": str(saved.path),
        }
        command_argv = runtime_command + [
            "run-saved-workflow",
            saved.command_name,
            "--workspace",
            str(workspace),
            "--runs-dir",
            str(runs_dir),
        ]
        _append_saved_scope_args(command_argv, args)
        for raw_arg in args.arg:
            command_argv.extend(["--arg", raw_arg])
    if args.desktop_notify:
        command_argv.append("--desktop-notify")
    _append_runtime_policy_args(command_argv, policy, args.max_workers)
    manifest = build_routine_manifest(
        name=args.name,
        schedule=schedule,
        workflow=workflow,
        target=target,
        command_argv=command_argv,
        workspace=workspace,
        runs_dir=runs_dir,
        policy=policy,
        max_workers=args.max_workers,
        budget=budget,
        runtime_sha256=runtime_sha256,
    )
    output = write_routine_manifest(manifest, args.output)
    print("Routine manifest: %s" % redact_text(str(output)))
    print("No process started: true")
    print("Schedule: %s" % _list_runs_text_field(routine_list_record(output, manifest)["schedule"]))
    return 0


def _append_runtime_policy_args(command_argv, policy: RuntimePolicy, max_workers) -> None:
    if policy.allow_writes:
        command_argv.append("--allow-writes")
    if policy.allow_destructive:
        command_argv.append("--allow-destructive")
    if policy.allow_network:
        command_argv.append("--allow-network")
    if policy.allow_agent:
        command_argv.append("--allow-agent")
    if policy.allow_parallel:
        command_argv.append("--allow-parallel")
    if max_workers is not None:
        command_argv.extend(["--max-workers", str(max_workers)])


def _append_saved_scope_args(command_argv, args) -> None:
    if args.project_root:
        command_argv.extend(["--project-root", str(Path(args.project_root).resolve())])
    if args.include_personal:
        command_argv.append("--include-personal")
    if args.personal_workflows_dir:
        command_argv.extend(["--personal-workflows-dir", str(Path(args.personal_workflows_dir).resolve())])


def _list_routines(args) -> int:
    records = []
    controls = load_routine_controls(args.routines_dir)
    state = load_supervisor_state(args.routines_dir)
    for path in iter_routine_manifest_paths(args.routines_dir, max_routines=args.max_routines):
        try:
            manifest = load_routine_manifest(path)
            record = routine_list_record(path, manifest)
            control = routine_control_record(manifest["name"], controls)
            execution = state.get("routines", {}).get(manifest["name"], {})
            execution = execution if isinstance(execution, dict) else {}
            record["control"] = {
                "paused": control["paused"],
                "updated_at_utc": control["updated_at_utc"],
                "last_action": control["last_action"],
            }
            record["execution"] = {
                "last_status": execution.get("last_status") or "never",
                "last_trigger": execution.get("last_trigger") or "",
                "last_scheduled_for_utc": execution.get("last_scheduled_for_utc"),
                "last_finished_at_utc": execution.get("last_finished_at_utc"),
                "executions": execution.get("executions") if isinstance(execution.get("executions"), int) else 0,
                "history_count": len(execution.get("history", [])) if isinstance(execution.get("history"), list) else 0,
            }
            records.append(record)
        except ConductorError as exc:
            records.append(
                {
                    "path": str(path),
                    "name": path.stem,
                    "schedule": "invalid",
                    "target_kind": "invalid",
                    "workflow": "unknown",
                    "evidence_status": "invalid",
                    "no_process_started": True,
                    "policy": {},
                    "created_at_utc": "",
                    "error": str(exc),
                }
            )
    background_supervisor = latest_background_routine_supervisor(args.routines_dir)
    routine_service = status_routine_service(args.routines_dir)
    if args.json:
        payload = {
            "schema": "conductor.routine_list.v1",
            "routines_dir": str(args.routines_dir),
            "count": len(records),
            "routines": records,
            "background_supervisor": background_supervisor,
            "routine_service": routine_service,
        }
        print(json.dumps(redact_json_value(payload), indent=2, sort_keys=True))
        return 0
    for record in records:
        print(
            "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s"
            % (
                _list_runs_text_field(record["name"]),
                _list_runs_text_field(record["schedule"]),
                _list_runs_text_field("paused" if record.get("control", {}).get("paused") else "active"),
                _list_runs_text_field(record["target_kind"]),
                _list_runs_text_field(record["workflow"]),
                _list_runs_text_field(record["evidence_status"]),
                _list_runs_text_field(
                    _background_routine_supervisor_list_label(
                        background_supervisor
                    )
                ),
                _list_runs_text_field(record["created_at_utc"]),
            )
        )
    return 0


def _background_routine_supervisor_list_label(supervisor: Dict) -> str:
    if not isinstance(supervisor, dict) or not supervisor:
        return "unavailable"
    return "%s/%s/%s-cycles" % (
        supervisor.get("status") or "unknown",
        supervisor.get("liveness") or "unknown",
        supervisor.get("cycles_completed") if isinstance(supervisor.get("cycles_completed"), int) else 0,
    )


def _status_routine(args) -> int:
    print(json.dumps(redact_json_value(routine_status(args.routine_manifest)), indent=2, sort_keys=True))
    return 0


def _run_routine(args) -> int:
    approvals = validate_approval_tokens(args.approve)
    result = execute_routine_manifest(
        args.routine_manifest,
        approvals=approvals,
        output_limit_bytes=args.output_limit_bytes,
    )
    print("Routine: %s" % _list_runs_text_field(result["name"]))
    print("Status: %s" % _list_runs_text_field(result["status"]))
    print("Return code: %s" % result["returncode"])
    return 0 if result["status"] == "completed" else 1


def _pause_routine(args) -> int:
    result = set_routine_paused(args.routine_manifest, paused=True, reason=args.reason)
    print("Routine: %s" % _list_runs_text_field(result["routine"]))
    print("Status: paused")
    print("Changed: %s" % str(result["changed"]).lower())
    return 0


def _resume_routine(args) -> int:
    result = set_routine_paused(
        args.routine_manifest,
        paused=False,
        approvals=validate_approval_tokens(args.approve),
        reason=args.reason,
    )
    print("Routine: %s" % _list_runs_text_field(result["routine"]))
    print("Status: active")
    print("Changed: %s" % str(result["changed"]).lower())
    return 0


def _update_routine_schedule(args) -> int:
    schedule = build_schedule(
        manual=args.manual,
        interval_minutes=args.interval_minutes,
        daily_at=args.daily_at,
        weekdays_at=args.weekdays_at,
        weekly_at=args.weekly_at,
        monthly_at=args.monthly_at,
        once_at=args.once_at,
        cron=args.cron,
        timezone=args.timezone,
    )
    result = update_routine_schedule(
        args.routine_manifest,
        schedule=schedule,
        approvals=validate_approval_tokens(args.approve),
        reason=args.reason,
    )
    print("Routine: %s" % _list_runs_text_field(result["routine"]))
    print("Schedule: %s" % _list_runs_text_field(result["schedule_after"]))
    print("Changed: %s" % str(result["changed"]).lower())
    return 0


def _supervise_routines(args) -> int:
    approvals = validate_approval_tokens(args.approve)
    max_cycles = args.max_cycles if args.max_cycles is not None else (0 if args.background else 1)
    if args.background:
        result = start_background_routine_supervisor(
            routines_dir=args.routines_dir,
            approvals=approvals,
            poll_seconds=args.poll_seconds,
            max_cycles=max_cycles,
            max_routines=args.max_routines,
            output_limit_bytes=args.output_limit_bytes,
        )
        print("Status: %s" % result.metadata["status"])
        print("Worker pid: %s" % result.pid)
        print("Background metadata: %s" % redact_text(str(result.metadata_path)))
        return 0
    results = supervise_routines(
        args.routines_dir,
        approvals=approvals,
        poll_seconds=args.poll_seconds,
        max_cycles=max_cycles,
        max_routines=args.max_routines,
        output_limit_bytes=args.output_limit_bytes,
    )
    failed = 0
    for result in results:
        status = result.get("status", "unknown")
        print(
            "%s\t%s\t%s"
            % (
                _list_runs_text_field(result.get("name") or "unknown"),
                _list_runs_text_field(status),
                _list_runs_text_field(result.get("manifest") or ""),
            )
        )
        if status != "completed":
            failed += 1
    return 1 if failed else 0


def _status_background_routine_supervisor(args) -> int:
    metadata = load_background_routine_supervisor(args.metadata_json)
    routines_dir = background_routine_supervisor_routines_dir(args.metadata_json)
    payload = {
        "schema": "conductor.background_routine_supervisor_status.v1",
        "background": metadata,
        "liveness": background_routine_supervisor_liveness(metadata),
        "routine_state": load_supervisor_state(routines_dir),
        "routine_controls": load_routine_controls(routines_dir),
    }
    print(json.dumps(redact_json_value(payload), indent=2, sort_keys=True))
    return 0


def _wait_background_routine_supervisor(args) -> int:
    if not isinstance(args.poll_interval_ms, int) or not 50 <= args.poll_interval_ms <= 60000:
        raise ValidationError("poll-interval-ms must be from 50 to 60000")
    metadata = wait_for_background_routine_supervisor(
        args.metadata_json,
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_ms / 1000.0,
    )
    print(json.dumps(redact_json_value(metadata), indent=2, sort_keys=True))
    return 0 if metadata["exit_code"] == 0 and metadata["supervisor_status"] == "completed" else 1


def _terminate_background_routine_supervisor(args) -> int:
    result = terminate_background_routine_supervisor(args.metadata_json, force=args.force)
    print("Background routine supervisor signal: %s" % result["signal"])
    print("Worker pid: %s" % result["pid"])
    return 0


def _install_routine_service(args) -> int:
    approvals = validate_approval_tokens(args.approve)
    lifecycle_approval = (
        ROUTINE_SERVICE_UPDATE_APPROVAL
        if args.command == "update-routine-service"
        else ROUTINE_SERVICE_INSTALL_APPROVAL
    )
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        allow_network=args.allow_network,
        allow_agent=args.allow_agent,
        allow_parallel=args.allow_parallel,
        approvals=approvals,
    )
    grant = build_routine_service_grant(
        routines_dir=args.routines_dir,
        routine_manifests=args.routine_manifest,
        runtime_path=args.runtime,
        policy=policy,
        allow_service_install=args.allow_service_install,
        platform=args.platform,
        poll_seconds=args.poll_seconds,
        max_routines=args.max_routines,
        output_limit_bytes=args.output_limit_bytes,
        grant_days=args.grant_days,
        approval=lifecycle_approval,
    )
    result = install_routine_service(
        grant,
        home=args.home,
        start=not args.no_start,
        replace=args.command == "update-routine-service",
    )
    print("Routine service: %s" % _list_runs_text_field(result["service_id"]))
    print("Platform: %s" % result["platform"])
    print("Grant: %s" % redact_text(result["grant_path"]))
    print("Descriptor: %s" % redact_text(result["descriptor_path"]))
    print("Started: %s" % str(result["started"]).lower())
    print("Expires: %s" % result["expires_at_utc"])
    return 0


def _status_routine_service(args) -> int:
    result = status_routine_service(args.routines_dir, home=args.home)
    if args.json:
        print(json.dumps(redact_json_value(result), indent=2, sort_keys=True))
    else:
        print("Routine service: %s" % _list_runs_text_field(result["service_id"]))
        print("Status: %s" % _list_runs_text_field(result["effective_status"]))
        print("Installed: %s" % str(result["installed"]).lower())
        print("Liveness: %s" % _list_runs_text_field(result["liveness"]))
        print("Bindings valid: %s" % str(result["bindings_valid"]).lower())
        if result["error"]:
            print("Attention: %s" % _list_runs_text_field(result["error"]))
    return 1 if result["needs_attention"] else 0


def _uninstall_routine_service(args) -> int:
    result = uninstall_routine_service(
        args.routines_dir,
        approvals=validate_approval_tokens(args.approve),
        allow_service_install=args.allow_service_install,
        home=args.home,
    )
    print("Routine service: %s" % _list_runs_text_field(result["service_id"]))
    print("Removed grant: %s" % str(result["removed_grant"]).lower())
    print("Removed descriptor: %s" % str(result["removed_descriptor"]).lower())
    return 0


def _cloud_policy(args, allow_writes: bool = False) -> RuntimePolicy:
    return RuntimePolicy(
        allow_writes=allow_writes,
        allow_network=args.allow_network,
        approvals=validate_approval_tokens(args.approve),
    )


def _cloud_submit(args) -> int:
    task = args.task
    if args.task_file:
        task = read_regular_text_file_no_follow(args.task_file, "cloud task file", 1024 * 1024)
    result = submit_cloud_task(
        task=task,
        environment_id=args.environment_id,
        workspace=args.workspace,
        cloud_dir=args.cloud_dir,
        policy=_cloud_policy(args),
        branch=args.branch,
        attempts=args.attempts,
        timeout_seconds=args.timeout_seconds,
        output_limit_bytes=args.output_limit_bytes,
    )
    print("Cloud task: %s" % _list_runs_text_field(result["task_id"]))
    print("Record: %s" % _list_runs_text_field(result["record_path"]))
    return 0


def _cloud_status(args) -> int:
    result = cloud_task_status(
        args.task_id,
        workspace=args.workspace,
        policy=_cloud_policy(args),
        timeout_seconds=args.timeout_seconds,
        output_limit_bytes=args.output_limit_bytes,
    )
    print(json.dumps(redact_json_value(result), indent=2, sort_keys=True))
    return 0


def _cloud_review(args) -> int:
    result = review_cloud_task(
        task_id=args.task_id,
        workspace=args.workspace,
        cloud_dir=args.cloud_dir,
        policy=_cloud_policy(args),
        attempt=args.attempt,
        timeout_seconds=args.timeout_seconds,
        output_limit_bytes=args.output_limit_bytes,
    )
    print("Cloud review receipt: %s" % _list_runs_text_field(result["receipt_path"]))
    print("Diff SHA-256: %s" % result["diff_sha256"])
    return 0


def _cloud_apply(args) -> int:
    result = apply_cloud_task(
        task_id=args.task_id,
        review_receipt=args.review_receipt,
        workspace=args.workspace,
        policy=_cloud_policy(args, allow_writes=args.allow_writes),
        attempt=args.attempt,
        timeout_seconds=args.timeout_seconds,
        output_limit_bytes=args.output_limit_bytes,
    )
    print("Cloud task applied: %s" % _list_runs_text_field(result["task_id"]))
    print("Verified diff SHA-256: %s" % result["verified_diff_sha256"])
    return 0


def _dashboard(args) -> int:
    output = write_dashboard(
        runs_dir=args.runs_dir,
        benchmarks_dir=args.benchmarks_dir,
        goals_dir=args.goals_dir,
        routines_dir=args.routines_dir,
        generated_dir=args.generated_dir,
        auto_dir=args.auto_dir,
        output=args.output,
        title=args.title,
        max_runs=args.max_runs,
        max_reports=args.max_reports,
        max_goals=args.max_goals,
        max_auto=args.max_auto,
        max_text_bytes=args.max_text_bytes,
    )
    print("Dashboard: %s" % redact_text(str(output)))
    return 0


def _serve(args) -> int:
    serve_live_dashboard(
        runs_dir=args.runs_dir,
        benchmarks_dir=args.benchmarks_dir,
        goals_dir=args.goals_dir,
        routines_dir=args.routines_dir,
        generated_dir=args.generated_dir,
        auto_dir=args.auto_dir,
        host=args.host,
        port=args.port,
        max_runs=args.max_runs,
        max_reports=args.max_reports,
        max_goals=args.max_goals,
        max_auto=args.max_auto,
        max_text_bytes=args.max_text_bytes,
        title=args.title,
        control_token=args.control_token,
        allow_remote=args.allow_remote,
    )
    return 0


def _retry_step(args) -> int:
    return _print_control_result(retry_step(args.run_dir, args.step_id, reason=args.reason, cascade=args.cascade))


def _reset_step(args) -> int:
    return _print_control_result(reset_step(args.run_dir, args.step_id, reason=args.reason, cascade=args.cascade))


def _retry_packet(args) -> int:
    return _print_control_result(
        retry_packet(
            args.run_dir,
            args.step_id,
            args.packet_index,
            reason=args.reason,
            cascade=args.cascade,
        )
    )


def _recover_run(args) -> int:
    return _print_control_result(
        recover_run(
            args.run_dir,
            reason=args.reason,
            retry_running=args.retry_running,
            resume_codex=args.resume_codex,
        )
    )


def _skip_step(args) -> int:
    return _print_control_result(skip_step(args.run_dir, args.step_id, reason=args.reason, cascade=args.cascade))


def _pause_run(args) -> int:
    return _print_control_result(pause_run(args.run_dir, reason=args.reason))


def _resume_run(args) -> int:
    return _print_control_result(resume_run(args.run_dir, reason=args.reason))


def _stop_run(args) -> int:
    return _print_control_result(stop_run(args.run_dir, reason=args.reason))


def _restart_run(args) -> int:
    return _print_control_result(restart_run(args.run_dir, reason=args.reason))


def _terminate_run(args) -> int:
    return _print_control_result(terminate_run(args.run_dir, reason=args.reason, force=args.force))


def _save_run_workflow(args) -> int:
    return _print_control_result(save_run_workflow(args.run_dir, args.destination))


def _print_control_result(result) -> int:
    print("Run control: %s" % result["action"])
    print("Run directory: %s" % result["run_dir"])
    affected = result.get("affected_steps") or []
    print("Affected steps: %s" % (", ".join(affected) if affected else "none"))
    if result.get("packet_index") is not None:
        print("Packet: %s#%s" % (result.get("requested_step"), result["packet_index"]))
        print("Packet generation: %s" % result.get("packet_generation"))
    if result.get("resolution"):
        print("Recovery resolution: %s" % result["resolution"])
    print("Status: %s" % result["status"])
    if result.get("destination"):
        print("Destination: %s" % result["destination"])
    if result.get("signal"):
        print("Signal: %s" % result["signal"])
    if result.get("pid"):
        print("PID: %s" % result["pid"])
    if result.get("process_group_id"):
        print("Process group: %s" % result["process_group_id"])
    return 0


def _record_usage(args) -> int:
    result = record_usage(
        run_dir=args.run_dir,
        step_id=args.step_id or "",
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        total_tokens=args.total_tokens,
        cost_usd=args.cost_usd,
        source=args.source,
        reason=args.reason,
    )
    return _print_usage_result(result)


def _import_usage(args) -> int:
    imported = load_usage_import(args.usage_json, provider=args.provider)
    source = args.source or "usage-import:%s" % imported.provider
    reason = args.reason or "imported measured usage from local usage artifact"
    result = record_usage(
        run_dir=args.run_dir,
        step_id=args.step_id or "",
        source=source,
        reason=reason,
        **imported.as_record_kwargs(),
    )
    return _print_usage_result(result)


def _import_session_trace(args) -> int:
    imported = load_session_trace_import(args.trace_json, provider=args.provider, step_id=args.step_id or "")
    source = args.source or "session-trace:%s" % imported.provider
    reason = args.reason or "imported redacted session trace from local artifact"
    usage = aggregate_session_trace_usage(imported.events, provider=imported.provider) if args.record_usage else None
    if usage is not None:
        result = record_session_trace_with_usage(
            run_dir=args.run_dir,
            events=imported.events,
            step_id=args.step_id or "",
            source=source,
            reason=reason,
            **usage.as_record_kwargs(),
        )
    else:
        result = record_session_trace(
            run_dir=args.run_dir,
            events=imported.events,
            source=source,
            reason=reason,
            step_id=args.step_id or "",
        )
    print("Session trace import: %s event(s)" % result["imported_events"])
    print("Total trace events: %s" % result["total_events"])
    if usage is not None:
        usage_parts = []
        for field in ["input_tokens", "output_tokens", "total_tokens", "cost_usd"]:
            value = result.get("usage", {}).get(field)
            if value is not None:
                usage_parts.append("%s=%s" % (field, value))
        print("Trace usage import: %s" % (", ".join(usage_parts) if usage_parts else "recorded"))
    print("Run directory: %s" % redact_text(str(result["run_dir"])))
    print("Status: %s" % redact_text(str(result["status"])))
    return 0


def _print_usage_result(result) -> int:
    usage = result.get("usage") or {}
    parts = []
    for field in ["input_tokens", "output_tokens", "total_tokens", "cost_usd"]:
        if field in usage:
            parts.append("%s=%s" % (field, usage[field]))
    print("Run usage: %s" % result["action"])
    print("Run directory: %s" % result["run_dir"])
    print("Target: %s" % result["target"])
    print("Usage: %s" % (", ".join(parts) if parts else "recorded"))
    print("Status: %s" % result["status"])
    return 0


def _plan_workflow(args) -> int:
    if args.saved_output and not args.saved_command:
        raise ValidationError("--saved-output requires --saved-command")
    saved_command = None
    saved_output = None
    if args.saved_command:
        saved_command = validate_saved_workflow_command_name(args.saved_command)
        saved_output = args.saved_output or Path(".claude") / "workflows" / ("%s.js" % saved_command)
        validate_saved_workflow_script_destination(saved_output)
    if args.task_file:
        task = _read_task_file(args.task_file)
    else:
        task = args.task
    workflow = build_workflow_plan(
        task=task,
        name=args.name,
        kind=args.kind,
        profile=args.profile,
        failing_commands=args.failing_command,
        check_commands=args.check_command,
        include_agent=not args.no_agent,
        write_capable=args.write_capable,
        max_workers=args.max_workers,
        packet_items=args.packet_item,
        packet_items_file=args.packet_items_file.as_posix() if args.packet_items_file else None,
        packet_globs=args.packet_glob,
        packet_max_items=args.packet_max_items,
        packet_max_packets=args.packet_max_packets,
        workspace=Path("."),
    )
    text = json.dumps(workflow, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = write_workflow_plan(workflow, args.output)
        print("Workflow draft: %s" % redact_text(str(output)))
    if saved_command:
        saved_path = write_saved_workflow_script(workflow, saved_output, saved_command)
        print("Saved workflow command: %s" % redact_text(str(saved_path)))
    if not args.output and not args.saved_command:
        print(text, end="")
    return 0


def _plan_model_workflow(args) -> int:
    task = _read_task_file(args.task_file) if args.task_file else args.task
    output = args.output or default_model_workflow_output(args.workspace, args.name)
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(allow_agent=args.allow_agent, approvals=approvals)
    result = plan_model_workflow(
        task=task,
        workspace=args.workspace,
        output_path=output,
        receipt_path=args.receipt,
        policy=policy,
        name=args.name,
        model=args.model,
        reasoning_effort=args.planner_effort,
        write_capable=args.write_capable,
        max_workers=args.max_workers,
        max_items=args.max_items,
        max_steps=args.max_steps,
        timeout_seconds=args.timeout_seconds,
        output_limit_bytes=args.output_limit_bytes,
        bind_workspace=not args.no_workspace_binding,
    )
    print("Model workflow draft: %s" % redact_text(str(result.output_path)))
    print("Receipt: %s" % redact_text(str(result.receipt_path)))
    print("Status: validated-draft")
    print("Workspace binding: %s" % result.receipt["workspace"]["mode"])
    print("Execution: not started; inspect the draft before running it")
    return 0


def _run_model_workflow(args) -> int:
    if args.dry_run and args.desktop_notify:
        raise ValidationError("--desktop-notify requires real model workflow execution")
    task = _read_task_file(args.task_file) if args.task_file else args.task
    output = args.output or default_model_workflow_output(args.workspace, args.name)
    run_receipt = args.run_receipt or default_model_workflow_run_receipt(output)
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        allow_network=args.allow_network,
        allow_agent=args.allow_agent,
        allow_parallel=args.allow_parallel,
        approvals=approvals,
    )
    result = run_model_workflow(
        task=task,
        workspace=args.workspace,
        output_path=output,
        planning_receipt_path=args.planning_receipt,
        run_receipt_path=run_receipt,
        runs_dir=args.runs_dir,
        policy=policy,
        name=args.name,
        model=args.model,
        planner_effort=args.planner_effort,
        write_capable=args.write_capable,
        max_workers=args.max_workers,
        max_items=args.max_items,
        max_steps=args.max_steps,
        planning_timeout_seconds=args.planning_timeout_seconds,
        planning_output_limit_bytes=args.planning_output_limit_bytes,
        run_id=args.run_id,
        dry_run=args.dry_run,
        background=args.background,
        desktop_notify=args.desktop_notify,
    )
    if result.draft is None:
        print("Model workflow draft: not created")
        print("Planning receipt: not created")
    else:
        print("Model workflow draft: %s" % redact_text(str(result.draft.output_path)))
        print("Planning receipt: %s" % redact_text(str(result.draft.receipt_path)))
    print("Run receipt: %s" % redact_text(str(result.receipt_path)))
    if result.run_dir is not None:
        print("Run directory: %s" % redact_text(str(result.run_dir)))
    if result.background_pid is not None:
        print("Background worker pid: %d" % result.background_pid)
    print("Status: %s" % result.status)
    if result.error_class:
        print("Error class: %s" % redact_text(result.error_class))
    expected_status = "planned" if args.dry_run else ("running" if args.background else "completed")
    successful = result.status == expected_status
    if not args.dry_run and not (args.background and result.status == "running"):
        _notify_execution_fail_open(
            args.desktop_notify,
            result.status,
            run_dir=result.run_dir,
            artifact_path=None if result.run_dir is not None else result.receipt_path,
        )
    return 0 if successful else 1


def _run_reviewed_model_workflow(args) -> int:
    if args.dry_run and args.desktop_notify:
        raise ValidationError("--desktop-notify requires real reviewed workflow execution")
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        allow_network=args.allow_network,
        allow_agent=args.allow_agent,
        allow_parallel=args.allow_parallel,
        approvals=approvals,
    )
    result = run_reviewed_model_workflow(
        workflow_path=args.workflow,
        planning_receipt_path=args.planning_receipt,
        workspace=args.workspace,
        runs_dir=args.runs_dir,
        policy=policy,
        run_receipt_path=args.run_receipt,
        run_id=args.run_id,
        dry_run=args.dry_run,
        background=args.background,
        allow_unbound_workspace=args.allow_unbound_workspace,
        desktop_notify=args.desktop_notify,
    )
    print("Reviewed workflow draft: %s" % redact_text(str(result.draft.output_path)))
    print("Planning receipt: %s" % redact_text(str(result.draft.receipt_path)))
    print("Planner calls: 0")
    print(
        "Workspace binding: %s"
        % (
            result.draft.receipt.get("workspace", {}).get("mode", "legacy-unbound")
        )
    )
    print("Run receipt: %s" % redact_text(str(result.receipt_path)))
    if result.run_dir is not None:
        print("Run directory: %s" % redact_text(str(result.run_dir)))
    if result.background_pid is not None:
        print("Background worker pid: %d" % result.background_pid)
    print("Status: %s" % result.status)
    if result.error_class:
        print("Error class: %s" % redact_text(result.error_class))
    expected_status = "planned" if args.dry_run else ("running" if args.background else "completed")
    if not args.dry_run and not (args.background and result.status == "running"):
        _notify_execution_fail_open(
            args.desktop_notify,
            result.status,
            run_dir=result.run_dir,
            artifact_path=None if result.run_dir is not None else result.receipt_path,
        )
    return 0 if result.status == expected_status else 1


def _run_model_goal(args) -> int:
    task = _read_task_file(args.task_file) if args.task_file else args.task
    check_command = parse_check_command_json(args.check_command_json) if args.check_command_json is not None else None
    monitor_command = (
        parse_check_command_json(args.monitor_command_json)
        if args.monitor_command_json is not None
        else None
    )
    check_prompt = args.check_prompt
    if args.check_prompt_file is not None:
        check_prompt = load_check_prompt_file(args.check_prompt_file, args.workspace)
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        allow_network=args.allow_network,
        allow_agent=args.allow_agent,
        allow_parallel=args.allow_parallel,
        approvals=approvals,
    )
    if args.background:
        result = start_background_model_goal(
            task=task,
            workspace=args.workspace,
            runs_dir=args.runs_dir,
            goals_dir=args.goals_dir,
            policy=policy,
            check_command=check_command,
            check_prompt=check_prompt,
            check_model=args.check_model,
            planner_model=args.planner_model,
            planner_effort=args.planner_effort,
            name=args.name,
            write_capable=args.write_capable,
            max_iterations=args.max_iterations,
            dynamic_interval=args.dynamic_interval,
            min_delay_minutes=args.min_delay_minutes,
            max_delay_minutes=args.max_delay_minutes,
            monitor_command=monitor_command,
            max_workers=args.max_workers,
            max_items=args.max_items,
            max_steps=args.max_steps,
            planning_timeout_seconds=args.planning_timeout_seconds,
            planning_output_limit_bytes=args.planning_output_limit_bytes,
            check_timeout_seconds=args.check_timeout_seconds,
            check_output_limit_bytes=args.output_limit_bytes,
            goal_id=args.goal_id,
            resume_goal=args.resume_goal,
            reuse_planner_session=not args.fresh_planner_session_per_iteration,
            desktop_notify=args.desktop_notify,
        )
        print("Goal: %s" % redact_text(result.goal_id))
        print("Status: %s" % result.metadata["status"])
        print("Worker pid: %s" % result.pid)
        print("Goal artifact: %s" % redact_text(str(result.goal_path)))
        print("Background metadata: %s" % redact_text(str(result.metadata_path)))
        return 0
    result = run_model_goal_loop(
        task=task,
        workspace=args.workspace,
        runs_dir=args.runs_dir,
        goals_dir=args.goals_dir,
        policy=policy,
        check_command=check_command,
        check_prompt=check_prompt,
        check_model=args.check_model,
        planner_model=args.planner_model,
        planner_effort=args.planner_effort,
        name=args.name,
        write_capable=args.write_capable,
        max_iterations=args.max_iterations,
        dynamic_interval=args.dynamic_interval,
        min_delay_minutes=args.min_delay_minutes,
        max_delay_minutes=args.max_delay_minutes,
        monitor_command=monitor_command,
        max_workers=args.max_workers,
        max_items=args.max_items,
        max_steps=args.max_steps,
        planning_timeout_seconds=args.planning_timeout_seconds,
        planning_output_limit_bytes=args.planning_output_limit_bytes,
        check_timeout_seconds=args.check_timeout_seconds,
        check_output_limit_bytes=args.output_limit_bytes,
        goal_id=args.goal_id,
        resume_goal=args.resume_goal,
        reuse_planner_session=not args.fresh_planner_session_per_iteration,
    )
    print("Goal: %s" % redact_text(result.goal_id))
    print("Status: %s" % result.status)
    print("Iterations: %s" % result.iterations)
    print("Goal artifact: %s" % redact_text(str(result.goal_path)))
    for run_dir in result.run_dirs:
        print("Run directory: %s" % redact_text(str(run_dir)))
    _notify_execution_fail_open(
        args.desktop_notify,
        result.status,
        artifact_path=result.goal_path,
    )
    return 0 if result.status == "completed" else 1


def _auto(args) -> int:
    task = _read_task_file(args.task_file) if args.task_file else args.task
    check_command = parse_check_command_json(args.check_command_json) if args.check_command_json is not None else None
    check_prompt = args.check_prompt
    if args.check_prompt_file is not None:
        check_prompt = load_check_prompt_file(args.check_prompt_file, args.workspace)
    approvals = validate_approval_tokens(args.approve)
    policy = RuntimePolicy(
        allow_writes=args.allow_writes,
        allow_destructive=args.allow_destructive,
        allow_network=args.allow_network,
        allow_agent=args.allow_agent,
        allow_parallel=args.allow_parallel,
        approvals=approvals,
    )
    result = run_auto_orchestration(
        task=task,
        workspace=args.workspace,
        runs_dir=args.runs_dir,
        goals_dir=args.goals_dir,
        policy=policy,
        strategy=args.strategy,
        plan_only=args.plan_only,
        background=args.background,
        check_command=check_command,
        check_prompt=check_prompt,
        output_path=args.output,
        receipt_path=args.receipt,
        name=args.name,
        planner_model=args.planner_model,
        planner_effort=args.planner_effort,
        check_model=args.check_model,
        max_iterations=args.max_iterations,
        max_workers=args.max_workers,
        max_items=args.max_items,
        max_steps=args.max_steps,
        planning_timeout_seconds=args.planning_timeout_seconds,
        planning_output_limit_bytes=args.planning_output_limit_bytes,
        check_timeout_seconds=args.check_timeout_seconds,
        check_output_limit_bytes=args.output_limit_bytes,
        run_id=args.run_id,
        goal_id=args.goal_id,
        resume_goal=args.resume_goal,
        reuse_planner_session=not args.fresh_planner_session_per_iteration,
        native_agent_threads=args.native_agent_threads,
        desktop_notify=args.desktop_notify,
    )
    print("Auto route: %s" % result.route)
    print("Execution mode: %s" % result.execution_mode)
    print("Auto receipt: %s" % redact_text(str(result.receipt_path)))
    for artifact_path in result.artifact_paths:
        print("Artifact: %s" % redact_text(str(artifact_path)))
    if result.run_dir is not None:
        print("Run directory: %s" % redact_text(str(result.run_dir)))
    if result.goal_path is not None:
        print("Goal artifact: %s" % redact_text(str(result.goal_path)))
    if result.background_pid is not None:
        print("Background worker pid: %d" % result.background_pid)
    print("Status: %s" % result.status)
    if result.error_class:
        print("Error class: %s" % redact_text(result.error_class))
    if result.error_message:
        print("Error: %s" % redact_text(result.error_message))
    if not (result.execution_mode == "background" and result.status == "running"):
        notification_artifact = result.goal_path or (
            None if result.run_dir is not None else result.receipt_path
        )
        _notify_execution_fail_open(
            args.desktop_notify,
            result.status,
            run_dir=result.run_dir,
            artifact_path=notification_artifact,
        )
    return 0 if result.status in {"planned", "running", "completed"} else 1


def _read_task_file(path: Path) -> str:
    reject_symlink_path(path, "task-file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValidationError("failed to open task-file %s: %s" % (path, exc.__class__.__name__))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError("task-file must be a regular file: %s" % path)
        if info.st_size > MAX_TASK_CHARS:
            raise ValidationError("task-file must be at most %d bytes" % MAX_TASK_CHARS)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = None
            task = handle.read(MAX_TASK_CHARS + 1)
        if len(task) > MAX_TASK_CHARS:
            raise ValidationError("task-file must be at most %d characters" % MAX_TASK_CHARS)
        return task
    finally:
        if fd is not None:
            os.close(fd)
