import hashlib
import json
import math
import os
import signal
import uuid
try:
    import fcntl
except ImportError:  # pragma: no cover - Unix-only locking when available.
    fcntl = None
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from .agent_map_packet_terminal import load_agent_map_packet_terminal
from .agent_packets import MAX_AGENT_PACKETS, packetize_agent_items
from .clock import utc_now
from .agent_team import (
    AGENT_TEAM_STATE_SCHEMA,
    AGENT_TEAM_STATE_SCHEMA_V3,
    AGENT_TEAM_STATE_SCHEMA_V4,
    AGENT_TEAM_STATE_SCHEMA_V5,
    MAX_AGENT_TEAM_GENERATION,
    MAX_AGENT_TEAM_MESSAGE_CHARS,
    MAX_AGENT_TEAM_PLAN_CRITERIA_CHARS,
    load_agent_team_state,
    prepare_agent_team_operator_tasks,
)
from .agent_team_plan_approval import (
    AGENT_TEAM_PLAN_APPROVAL_SCHEMA,
    AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V2,
    AGENT_TEAM_PLAN_REVIEW_SCHEMA,
    agent_team_plan_approval_lock,
    agent_team_plan_approval_path,
    agent_team_plan_approval_summary,
    agent_team_plan_review_output_relative,
    complete_agent_team_operator_plan_review,
    load_agent_team_plan,
    load_agent_team_plan_approval,
    load_agent_team_plan_review,
    verify_agent_team_plan_approval_outputs,
    write_agent_team_plan_approval,
    write_agent_team_plan_review,
)
from .agent_team_chat import (
    agent_team_operator_chat_lock,
    agent_team_operator_chat_summary,
    answer_agent_team_operator_question,
    initial_agent_team_operator_chat,
    load_agent_team_operator_chat,
    reconcile_agent_team_operator_chat,
    verify_agent_team_operator_chat,
    write_agent_team_operator_chat,
)
from .agent_team_transcript import (
    list_agent_team_transcript_summaries,
    load_bound_agent_team_transcript,
)
from .agent_team_operator import (
    AGENT_TEAM_OPERATOR_INBOX_SCHEMA,
    AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1,
    agent_team_operator_inbox_lock,
    agent_team_operator_inbox_summary,
    append_agent_team_operator_entry,
    initial_agent_team_operator_inbox,
    load_agent_team_operator_inbox,
    reconcile_agent_team_operator_interruptions,
    supersede_stale_agent_team_operator_entries,
    verify_agent_team_operator_inbox,
    write_agent_team_operator_inbox,
)
from .artifacts import MAX_RUN_WORKFLOW_JSON_BYTES, RunArtifacts
from .codex_checkpoint import (
    MAX_CODEX_STEP_RESUMES,
    load_codex_step_checkpoint_with_sha256,
)
from .codex_step_terminal import codex_step_terminal_path, load_codex_step_terminal
from .errors import ValidationError
from .packet_items import (
    clean_packet_items,
    read_packet_items_file,
    read_packet_items_json_file,
)
from .redaction import redact_public_workflow_value, contains_secret_like, redact_text
from .run_ownership import run_execution_lock
from .security import (
    ensure_dir_no_follow,
    open_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    require_no_path_escape,
)
from .workflow import MAX_AGENT_ITEMS, validate_workflow, workflow_fingerprint


CONTROL_EVENT_LIMIT = 100
USAGE_EVENT_LIMIT = 100
SESSION_TRACE_EVENT_LIMIT = 500
TELEMETRY_RECEIPT_LIMIT = 8192
RUNNER_METADATA_BYTES = 64 * 1024
MAX_TERMINATE_HEARTBEAT_AGE_SECONDS = 300
MAX_TERMINATE_HEARTBEAT_FUTURE_SKEW_SECONDS = 30
MAX_REASON_CHARS = 512
MAX_USAGE_TOKENS = 10**12
MAX_TRACE_DURATION_MS = 30 * 24 * 60 * 60 * 1000
MAX_AGENT_PACKET_CONTROL_TRACE_BYTES = 16 * 1024 * 1024
RETRYABLE_STATUSES = {"failed", "blocked"}
RUN_ACTIVE_STATUSES = {"planning", "running", "pause_requested", "stop_requested", "restart_requested"}
RUN_FINAL_STATUSES = {"completed", "failed", "blocked", "stopped"}
RUNNER_FORCE_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
RUNNER_TERMINATION_SIGNAL_BY_FORCE = {
    False: ("TERM", signal.SIGTERM),
    True: ("KILL" if RUNNER_FORCE_SIGNAL != signal.SIGTERM else "TERM", RUNNER_FORCE_SIGNAL),
}
TEAM_CONSOLE_SNAPSHOT_SCHEMA = "conductor.team_console_snapshot.v1"


def retry_step(run_dir: Path, step_id: str, reason: str = "", cascade: bool = False) -> Dict:
    _validate_reason(reason)
    with _run_control_lock(run_dir):
        with run_execution_lock(run_dir):
            context = _load_context(run_dir)
            _require_inactive_direct_control(context["state"])
            _require_step(context, step_id)
            if context["step_map"][step_id]["kind"] == "manual_gate":
                raise ValidationError("manual_gate steps cannot be retried; resume with the required approval instead")
            current_status = _step_status(context["state"], step_id)
            if current_status not in RETRYABLE_STATUSES:
                raise ValidationError("step %s can only be retried from failed or blocked status" % step_id)
            return _reset_step(context, step_id, action="retry-step", reason=reason, cascade=cascade)


def reset_step(run_dir: Path, step_id: str, reason: str = "", cascade: bool = False) -> Dict:
    _validate_reason(reason)
    with _run_control_lock(run_dir):
        with run_execution_lock(run_dir):
            context = _load_context(run_dir)
            _require_inactive_direct_control(context["state"])
            _require_step(context, step_id)
            if context["step_map"][step_id]["kind"] == "manual_gate":
                raise ValidationError("manual_gate steps cannot be reset; resume with the required approval instead")
            return _reset_step(context, step_id, action="reset-step", reason=reason, cascade=cascade)


def retry_packet(
    run_dir: Path,
    step_id: str,
    packet_index: int,
    reason: str = "",
    cascade: bool = False,
) -> Dict:
    _validate_reason(reason)
    index = _validate_packet_index(packet_index)
    with _run_control_lock(run_dir):
        with run_execution_lock(run_dir):
            context = _load_context(run_dir)
            _require_inactive_direct_control(context["state"])
            _require_step(context, step_id)
            step = context["step_map"][step_id]
            if step["kind"] != "agent_map":
                raise ValidationError("retry-packet requires an agent_map step")
            state = context["state"]
            max_items = step.get("max_items", context["workflow"].get("max_items", MAX_AGENT_ITEMS))
            if index > max_items:
                raise ValidationError("packet_index exceeds the agent_map item limit")
            _require_no_pending_agent_map_terminals(
                context,
                [step_id],
                packet_indexes={step_id: {index}},
            )
            item_sha256 = _packet_trace_item_sha256(context, step, index)
            generation = _increment_agent_map_packet_generation(state, step_id, index, item_sha256)
            return _reset_step(
                context,
                step_id,
                action="retry-packet",
                reason=reason,
                cascade=cascade,
                packet_index=index,
                packet_generation=generation,
            )


def recover_run(
    run_dir: Path,
    reason: str,
    retry_running: bool = False,
    resume_codex: bool = False,
) -> Dict:
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("recover-run requires a non-empty --reason")
    if retry_running and resume_codex:
        raise ValidationError("recover-run accepts only one running-step resolution")
    _validate_reason(reason)
    with _run_control_lock(run_dir):
        with run_execution_lock(run_dir, require_cross_process=True):
            context = _load_context(run_dir)
            state = context["state"]
            steps = state.setdefault("steps", {})
            if not isinstance(steps, dict):
                raise ValidationError("run state steps must be an object")
            running_steps = [
                step["id"]
                for step in context["workflow"]["steps"]
                if isinstance(steps.get(step["id"]), dict) and steps[step["id"]].get("status") == "running"
            ]
            if len(running_steps) > 1:
                raise ValidationError("interrupted run has multiple running steps; refusing ambiguous recovery")
            if state.get("status") not in RUN_ACTIVE_STATUSES and not running_steps:
                raise ValidationError("recover-run requires an active run state or an interrupted running step")
            if resume_codex and len(running_steps) != 1:
                raise ValidationError("--resume-codex requires exactly one interrupted running step")

            codex_resume_binding = None
            if resume_codex:
                step_id = running_steps[0]
                step = context["step_map"][step_id]
                if step["kind"] != "codex_exec":
                    raise ValidationError("--resume-codex requires an interrupted codex_exec step")
                try:
                    checkpoint, checkpoint_sha256 = load_codex_step_checkpoint_with_sha256(
                        context["run"],
                        step_id,
                    )
                except FileNotFoundError:
                    raise ValidationError("Codex step checkpoint is unavailable")
                if checkpoint["status"] != "active":
                    raise ValidationError("Codex step checkpoint is not active")
                if checkpoint["workflow_fingerprint"] != state.get("workflow_fingerprint"):
                    raise ValidationError("Codex step checkpoint workflow binding does not match the run")
                if checkpoint["sandbox"] != step.get("sandbox", "read-only"):
                    raise ValidationError("Codex step checkpoint sandbox binding does not match the step")
                if checkpoint["model"] != step.get("model"):
                    raise ValidationError("Codex step checkpoint model binding does not match the step")
                if checkpoint["output"] != step.get("capture", "%s.md" % step_id):
                    raise ValidationError("Codex step checkpoint output binding does not match the step")
                if checkpoint["resume_count"] >= MAX_CODEX_STEP_RESUMES:
                    raise ValidationError("Codex step checkpoint reached the resume limit")
                codex_resume_binding = {
                    "session_id": checkpoint["session_id"],
                    "checkpoint_sha256": checkpoint_sha256,
                }

            runner = _load_runner_metadata(context["run"].run_dir)
            runner_evidence = _recoverable_runner_evidence(runner)
            timestamp = _utc_now()
            if resume_codex:
                resolution = "resume-codex"
            else:
                resolution = ("retry" if retry_running else "failed") if running_steps else "resume"
            _clear_codex_resume_bindings(state, running_steps)
            if codex_resume_binding is not None:
                state["codex_resume_bindings"] = {
                    running_steps[0]: codex_resume_binding,
                }
            for step_id in running_steps:
                previous = steps[step_id]
                if resume_codex:
                    detail = (
                        "interrupted Codex execution recovered for same-thread continuation; "
                        "the prior in-flight turn outcome remains uncertain: %s" % _clean_reason(reason)
                    )
                else:
                    detail = "interrupted execution recovered as %s; prior outcome is uncertain: %s" % (
                        "pending retry" if retry_running else "failed",
                        _clean_reason(reason),
                    )
                entry = _step_entry(
                    "pending" if retry_running or resume_codex else "failed",
                    detail,
                    timestamp,
                    previous,
                    kind=context["step_map"][step_id]["kind"],
                )
                if not retry_running and not resume_codex:
                    started_at = previous.get("started_at_utc")
                    entry["started_at_utc"] = started_at if isinstance(started_at, str) and started_at else timestamp
                    entry["finished_at_utc"] = timestamp
                    duration = _duration_ms(entry["started_at_utc"], timestamp)
                    if duration is not None:
                        entry["duration_ms"] = duration
                steps[step_id] = entry
            state.pop("control_request", None)
            result = _save_control_event(
                context,
                action="recover-run",
                step_id="",
                affected=running_steps,
                reason=reason,
                cascade=False,
                timestamp=timestamp,
                event_fields={
                    "resolution": resolution,
                    "runner_status": runner_evidence["runner_status"],
                    "runner_pid": runner_evidence["runner_pid"],
                    "tracked_processes": runner_evidence["tracked_processes"],
                    "provider_continuation": bool(resume_codex),
                },
                result_fields={
                    "resolution": resolution,
                    "recovered_steps": running_steps,
                },
            )
            _finalize_recovered_runner_metadata(
                context,
                runner,
                timestamp=timestamp,
                resolution=resolution,
                run_status=result["status"],
            )
            return result


def skip_step(run_dir: Path, step_id: str, reason: str, cascade: bool = False) -> Dict:
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("skip-step requires a non-empty --reason")
    _validate_reason(reason)
    with _run_control_lock(run_dir):
        with run_execution_lock(run_dir):
            context = _load_context(run_dir)
            _require_inactive_direct_control(context["state"])
            _require_step(context, step_id)
            step = context["step_map"][step_id]
            if step["kind"] == "manual_gate":
                raise ValidationError("manual_gate steps cannot be skipped; resume with the required approval instead")
            affected_for_guard = [step_id]
            if cascade:
                affected_for_guard.extend(
                    _downstream_steps(context["workflow"], step_id)
                )
            _require_no_pending_agent_map_terminals(
                context,
                affected_for_guard,
            )
            _require_no_pending_codex_step_terminals(
                context,
                affected_for_guard,
            )
            state = context["state"]
            steps = state.setdefault("steps", {})
            if not isinstance(steps, dict):
                raise ValidationError("run state steps must be an object")

            timestamp = _utc_now()
            previous = steps.get(step_id, {})
            steps[step_id] = _step_entry(
                "skipped",
                "skipped by control action: %s" % _clean_reason(reason),
                timestamp,
                previous,
                kind=step["kind"],
            )
            affected = [step_id]
            if cascade:
                for downstream in _downstream_steps(context["workflow"], step_id):
                    previous = steps.get(downstream, {})
                    steps[downstream] = _step_entry(
                        "pending",
                        "reset by skip-step cascade from %s: %s" % (step_id, _clean_reason(reason)),
                        timestamp,
                        previous,
                        kind=context["step_map"][downstream]["kind"],
                    )
                    if context["step_map"][downstream]["kind"] == "agent_map":
                        _increment_agent_map_cache_generation(state, downstream)
                    elif context["step_map"][downstream]["kind"] == "agent_team":
                        _increment_agent_team_generation(state, downstream)
                    affected.append(downstream)
            _clear_codex_resume_bindings(state, affected)
            return _save_control_event(context, action="skip-step", step_id=step_id, affected=affected, reason=reason, cascade=cascade, timestamp=timestamp)


def queue_team_task(
    run_dir: Path,
    step_id: str,
    member_id: str,
    instruction: str,
    interrupt_current: bool = False,
) -> Dict:
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValidationError("queue-team-task requires a non-empty instruction")
    if len(instruction) > MAX_AGENT_TEAM_MESSAGE_CHARS:
        raise ValidationError(
            "queue-team-task instruction exceeds %d characters"
            % MAX_AGENT_TEAM_MESSAGE_CHARS
        )
    if contains_secret_like(instruction):
        raise ValidationError("queue-team-task instruction contains a secret-like value")
    instruction = redact_text(instruction).strip()
    with _run_control_lock(run_dir):
        context = _load_context(run_dir)
        _require_step(context, step_id)
        step = context["step_map"][step_id]
        if step["kind"] != "agent_team":
            raise ValidationError("queue-team-task requires an agent_team step")
        if step.get("max_operator_tasks", 0) == 0:
            raise ValidationError(
                "agent_team step %s does not allow operator tasks" % step_id
            )
        state_path, inbox_path = _agent_team_control_paths(context["run"], step)
        fingerprint = workflow_fingerprint(context["workflow"])
        with agent_team_operator_inbox_lock(inbox_path):
            team_state = load_agent_team_state(
                state_path,
                step=step,
                workflow_fingerprint=fingerprint,
            )
            if team_state["schema"] not in {
                AGENT_TEAM_STATE_SCHEMA_V3,
                AGENT_TEAM_STATE_SCHEMA_V4,
                AGENT_TEAM_STATE_SCHEMA_V5,
                AGENT_TEAM_STATE_SCHEMA,
            }:
                raise ValidationError("legacy agent team state cannot accept operator tasks")
            if not isinstance(interrupt_current, bool):
                raise ValidationError("queue-team-task interrupt_current must be boolean")
            if interrupt_current and team_state["schema"] not in {
                AGENT_TEAM_STATE_SCHEMA_V4,
                AGENT_TEAM_STATE_SCHEMA_V5,
                AGENT_TEAM_STATE_SCHEMA,
            }:
                raise ValidationError(
                    "legacy agent team state cannot interrupt a teammate turn"
                )
            if team_state["status"] != "running":
                raise ValidationError("completed agent team cannot accept operator tasks")
            member_ids = {member["id"] for member in team_state["members"]}
            if member_id not in member_ids:
                raise ValidationError("queue-team-task member is not part of the team")
            member = next(
                value for value in team_state["members"] if value["id"] == member_id
            )
            interrupt_round = None
            interrupt_task_id = None
            if interrupt_current:
                if member["status"] != "working" or member["current_task_id"] is None:
                    raise ValidationError(
                        "queue-team-task can interrupt only a currently working member"
                    )
                interrupt_round = team_state["round"]
                interrupt_task_id = member["current_task_id"]
            if inbox_path.exists() or inbox_path.is_symlink():
                inbox = load_agent_team_operator_inbox(inbox_path)
                if reconcile_agent_team_operator_interruptions(inbox, team_state):
                    write_agent_team_operator_inbox(inbox_path, inbox)
                verify_agent_team_operator_inbox(inbox, team_state)
            else:
                inbox = initial_agent_team_operator_inbox(
                    step_id,
                    team_state["workflow_fingerprint"],
                    schema=(
                        AGENT_TEAM_OPERATOR_INBOX_SCHEMA
                        if team_state["schema"] in {
                            AGENT_TEAM_STATE_SCHEMA_V4,
                            AGENT_TEAM_STATE_SCHEMA_V5,
                            AGENT_TEAM_STATE_SCHEMA,
                        }
                        else AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1
                    ),
                )
            supersede_stale_agent_team_operator_entries(
                inbox,
                team_state["generation"],
            )
            if interrupt_current and any(
                value["generation"] == team_state["generation"]
                and value["status"] == "pending"
                and value.get("delivery") == "interrupt-current"
                and value.get("member_id") == member_id
                and value.get("interrupt_round") == interrupt_round
                and value.get("interrupt_task_id") == interrupt_task_id
                for value in inbox["entries"]
            ):
                raise ValidationError(
                    "queue-team-task already has a pending interrupt for this teammate turn"
                )
            entry = append_agent_team_operator_entry(
                inbox,
                generation=team_state["generation"],
                member_id=member_id,
                instruction=instruction,
                delivery=("interrupt-current" if interrupt_current else "next-turn"),
                interrupt_round=interrupt_round,
                interrupt_task_id=interrupt_task_id,
            )
            pending = [
                value
                for value in inbox["entries"]
                if value["generation"] == team_state["generation"]
                and value["status"] == "pending"
            ]
            prepare_agent_team_operator_tasks(
                step,
                team_state,
                pending,
                max_workers=_agent_team_control_max_workers(context["run"], step),
                assume_claimed_complete=True,
            )
            write_agent_team_operator_inbox(inbox_path, inbox)
            summary = agent_team_operator_inbox_summary(inbox)
            return next(value for value in summary["entries"] if value["id"] == entry["id"])


def list_team_inbox(run_dir: Path, step_id: str) -> Dict:
    context = _load_context(run_dir, ensure_run_dirs=False)
    _require_step(context, step_id)
    step = context["step_map"][step_id]
    if step["kind"] != "agent_team":
        raise ValidationError("list-team-inbox requires an agent_team step")
    state_path, inbox_path = _agent_team_control_paths(context["run"], step)
    team_state = load_agent_team_state(
        state_path,
        step=step,
        workflow_fingerprint=workflow_fingerprint(context["workflow"]),
    )
    if inbox_path.exists() or inbox_path.is_symlink():
        inbox = load_agent_team_operator_inbox(inbox_path)
        if reconcile_agent_team_operator_interruptions(inbox, team_state):
            write_agent_team_operator_inbox(inbox_path, inbox)
        verify_agent_team_operator_inbox(inbox, team_state)
    else:
        inbox = initial_agent_team_operator_inbox(
            step_id,
            team_state["workflow_fingerprint"],
            schema=(
                AGENT_TEAM_OPERATOR_INBOX_SCHEMA
                if team_state["schema"] in {
                    AGENT_TEAM_STATE_SCHEMA_V4,
                    AGENT_TEAM_STATE_SCHEMA_V5,
                    AGENT_TEAM_STATE_SCHEMA,
                }
                else AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1
            ),
        )
    summary = agent_team_operator_inbox_summary(inbox)
    summary.update(
        {
            "generation": team_state["generation"],
            "team_status": team_state["status"],
            "max_operator_tasks": team_state.get("max_operator_tasks", 0),
            "operator_tasks_added": team_state.get("operator_tasks_added", 0),
        }
    )
    return summary


def reply_team_question(
    run_dir: Path,
    step_id: str,
    question_id: str,
    reply: str,
) -> Dict:
    if not isinstance(reply, str) or not reply.strip():
        raise ValidationError("reply-team-question requires a non-empty reply")
    if len(reply) > MAX_AGENT_TEAM_MESSAGE_CHARS:
        raise ValidationError(
            "reply-team-question reply exceeds %d characters"
            % MAX_AGENT_TEAM_MESSAGE_CHARS
        )
    if contains_secret_like(reply):
        raise ValidationError("reply-team-question reply contains a secret-like value")
    reply = redact_text(reply).strip()
    with _run_control_lock(run_dir):
        context = _load_context(run_dir)
        _require_step(context, step_id)
        step = context["step_map"][step_id]
        if step["kind"] != "agent_team":
            raise ValidationError("reply-team-question requires an agent_team step")
        if step.get("operator_chat") is None:
            raise ValidationError("agent_team step %s does not allow operator chat" % step_id)
        state_path, _ = _agent_team_control_paths(context["run"], step)
        chat_path = _agent_team_chat_control_path(context["run"], step)
        fingerprint = workflow_fingerprint(context["workflow"])
        with agent_team_operator_chat_lock(chat_path):
            state = load_agent_team_state(
                state_path,
                step=step,
                workflow_fingerprint=fingerprint,
            )
            if state["schema"] != AGENT_TEAM_STATE_SCHEMA:
                raise ValidationError("legacy agent team state cannot accept operator replies")
            if state["status"] != "running":
                raise ValidationError("completed agent team cannot accept operator replies")
            chat = _load_or_initialize_agent_team_chat(chat_path, state)
            changed = reconcile_agent_team_operator_chat(chat, state)
            if changed:
                write_agent_team_operator_chat(chat_path, chat)
            entry = answer_agent_team_operator_question(chat, question_id, reply)
            if entry["generation"] != state["generation"]:
                raise ValidationError("operator question belongs to a stale team generation")
            write_agent_team_operator_chat(chat_path, chat)
            return next(
                value
                for value in agent_team_operator_chat_summary(chat)["entries"]
                if value["id"] == entry["id"]
            )


def list_team_questions(run_dir: Path, step_id: str) -> Dict:
    context = _load_context(run_dir, ensure_run_dirs=False)
    _require_step(context, step_id)
    step = context["step_map"][step_id]
    if step["kind"] != "agent_team":
        raise ValidationError("list-team-questions requires an agent_team step")
    if step.get("operator_chat") is None:
        raise ValidationError("agent_team step %s does not allow operator chat" % step_id)
    state_path, _ = _agent_team_control_paths(context["run"], step)
    chat_path = _agent_team_chat_control_path(context["run"], step)
    state = load_agent_team_state(
        state_path,
        step=step,
        workflow_fingerprint=workflow_fingerprint(context["workflow"]),
    )
    with agent_team_operator_chat_lock(chat_path):
        chat = _load_or_initialize_agent_team_chat(chat_path, state)
        if reconcile_agent_team_operator_chat(chat, state):
            write_agent_team_operator_chat(chat_path, chat)
        verify_agent_team_operator_chat(chat, state)
        summary = agent_team_operator_chat_summary(chat)
    summary.update(
        {
            "generation": state["generation"],
            "team_status": state["status"],
            "max_operator_questions": state["max_operator_questions"],
            "operator_questions_added": state["operator_questions_added"],
        }
    )
    return summary


def read_team_question(run_dir: Path, step_id: str, question_id: str) -> Dict:
    context = _load_context(run_dir, ensure_run_dirs=False)
    _require_step(context, step_id)
    step = context["step_map"][step_id]
    if step["kind"] != "agent_team" or step.get("operator_chat") is None:
        raise ValidationError("read-team-question requires an operator-chat agent_team step")
    state_path, _ = _agent_team_control_paths(context["run"], step)
    chat_path = _agent_team_chat_control_path(context["run"], step)
    state = load_agent_team_state(
        state_path,
        step=step,
        workflow_fingerprint=workflow_fingerprint(context["workflow"]),
    )
    with agent_team_operator_chat_lock(chat_path):
        chat = _load_or_initialize_agent_team_chat(chat_path, state)
        if reconcile_agent_team_operator_chat(chat, state):
            write_agent_team_operator_chat(chat_path, chat)
        verify_agent_team_operator_chat(chat, state)
        matches = [entry for entry in chat["entries"] if entry["id"] == question_id]
        if len(matches) != 1:
            raise ValidationError("agent team operator question is unknown")
        entry = matches[0]
        return {
            "id": entry["id"],
            "generation": entry["generation"],
            "message_id": entry["message_id"],
            "member_id": entry["member_id"],
            "source_task_id": entry["source_task_id"],
            "status": entry["status"],
            "question": entry["question"],
            "question_sha256": entry["question_sha256"],
            "reply": entry["reply"],
            "reply_sha256": entry["reply_sha256"],
        }


def list_team_plans(run_dir: Path, step_id: str) -> Dict:
    context = _load_context(run_dir, ensure_run_dirs=False)
    _require_step(context, step_id)
    step = context["step_map"][step_id]
    policy = step.get("plan_approval") if step["kind"] == "agent_team" else None
    if not isinstance(policy, dict):
        raise ValidationError("list-team-plans requires a plan-approved agent_team step")
    team_state = _load_current_team_state(context, step)
    values = []
    for task_id in policy["task_ids"]:
        path = agent_team_plan_approval_path(
            context["run"],
            step,
            team_state["generation"],
            task_id,
        )
        if not (path.exists() or path.is_symlink()):
            continue
        approval = load_agent_team_plan_approval(
            path,
            step=step,
            workflow_fingerprint=team_state["workflow_fingerprint"],
            generation=team_state["generation"],
        )
        verify_agent_team_plan_approval_outputs(approval, context["run"].artifacts_dir)
        values.append(agent_team_plan_approval_summary(approval))
    return {
        "step_id": step_id,
        "generation": team_state["generation"],
        "team_status": team_state["status"],
        "reviewer": policy.get("reviewer", "lead"),
        "plans": values,
    }


def read_team_plan(run_dir: Path, step_id: str, task_id: str) -> Dict:
    context, step, team_state, approval = _load_team_plan_context(
        run_dir,
        step_id,
        task_id,
    )
    if not approval["attempts"]:
        raise ValidationError("agent team plan is not ready")
    attempt = approval["attempts"][-1]
    plan = load_agent_team_plan(context["run"].artifacts_dir / attempt["plan_output"])
    review = None
    if attempt["review_output"] is not None:
        review = load_agent_team_plan_review(
            context["run"].artifacts_dir / attempt["review_output"]
        )
    return {
        "step_id": step_id,
        "generation": team_state["generation"],
        "task_id": task_id,
        "member_id": approval["member_id"],
        "reviewer": approval.get("reviewer", "lead"),
        "status": approval["status"],
        "criteria": step["plan_approval"]["criteria"],
        "plan": plan,
        "review": review,
    }


def list_team_transcripts(run_dir: Path, step_id: str) -> Dict:
    context, step, team_state = _load_team_transcript_context(
        run_dir,
        step_id,
        action="list-team-transcripts",
    )
    values = list_agent_team_transcript_summaries(
        context["run"].artifacts_dir,
        step,
        team_state["workflow_fingerprint"],
        team_state["generation"],
    )
    return {
        "step_id": step_id,
        "generation": team_state["generation"],
        "team_status": team_state["status"],
        "max_events": step["operator_console"]["max_events"],
        "max_bytes": step["operator_console"]["max_bytes"],
        "transcripts": values,
    }


def read_team_transcript(run_dir: Path, step_id: str, transcript_id: str) -> Dict:
    context, step, team_state = _load_team_transcript_context(run_dir, step_id)
    return load_bound_agent_team_transcript(
        context["run"].artifacts_dir,
        step,
        team_state["workflow_fingerprint"],
        team_state["generation"],
        transcript_id,
    )


def read_team_transcript_view(
    run_dir: Path,
    step_id: str,
    transcript_id: str,
) -> Dict:
    context, step, team_state = _load_team_transcript_context(run_dir, step_id)
    transcript = load_bound_agent_team_transcript(
        context["run"].artifacts_dir,
        step,
        team_state["workflow_fingerprint"],
        team_state["generation"],
        transcript_id,
    )
    member = next(
        (value for value in team_state["members"] if value["id"] == transcript["member_id"]),
        None,
    )
    if member is None:
        raise ValidationError("agent team transcript member binding changed")
    _state_path, inbox_path = _agent_team_control_paths(context["run"], step)
    pending_operator_tasks = 0
    if inbox_path.exists() or inbox_path.is_symlink():
        inbox = load_agent_team_operator_inbox(inbox_path)
        verify_agent_team_operator_inbox(inbox, team_state)
        pending_operator_tasks = sum(
            value["generation"] == team_state["generation"]
            and value["status"] == "pending"
            for value in inbox["entries"]
        )
    return {
        "transcript": transcript,
        "team_status": team_state["status"],
        "member_status": member["status"],
        "current_task_id": member["current_task_id"],
        "max_operator_tasks": team_state.get("max_operator_tasks", 0),
        "operator_tasks_added": team_state.get("operator_tasks_added", 0),
        "pending_operator_tasks": pending_operator_tasks,
    }


def read_team_console_snapshot(
    run_dir: Path,
    step_id: str,
    member_ids: Optional[List[str]] = None,
) -> Dict:
    context, step, team_state = _load_team_transcript_context(
        run_dir,
        step_id,
        action="team-console",
    )
    available_members = [member["id"] for member in team_state["members"]]
    if member_ids is None:
        selected_members = available_members
    else:
        if not isinstance(member_ids, list) or not member_ids:
            raise ValidationError("team-console member filter must be a non-empty list")
        if not all(isinstance(value, str) and value for value in member_ids):
            raise ValidationError("team-console member filter is invalid")
        if len(set(member_ids)) != len(member_ids):
            raise ValidationError("team-console member filter contains duplicates")
        unknown = sorted(set(member_ids) - set(available_members))
        if unknown:
            raise ValidationError(
                "team-console member is not part of the team: %s" % unknown[0]
            )
        requested = set(member_ids)
        selected_members = [
            member_id for member_id in available_members if member_id in requested
        ]
    selected = set(selected_members)
    summaries = list_agent_team_transcript_summaries(
        context["run"].artifacts_dir,
        step,
        team_state["workflow_fingerprint"],
        team_state["generation"],
    )
    transcripts = [
        load_bound_agent_team_transcript(
            context["run"].artifacts_dir,
            step,
            team_state["workflow_fingerprint"],
            team_state["generation"],
            summary["transcript_id"],
        )
        for summary in summaries
        if summary["member_id"] in selected
    ]
    return {
        "schema": TEAM_CONSOLE_SNAPSHOT_SCHEMA,
        "step_id": step_id,
        "generation": team_state["generation"],
        "team_status": team_state["status"],
        "round": team_state["round"],
        "max_rounds": team_state["max_rounds"],
        "max_events": step["operator_console"]["max_events"],
        "max_bytes": step["operator_console"]["max_bytes"],
        "transcript_count": len(transcripts),
        "members": [
            {
                "id": member["id"],
                "role": member["role"],
                "lead": member["lead"],
                "status": member["status"],
                "turns": member["turns"],
                "current_task_id": member["current_task_id"],
            }
            for member in team_state["members"]
            if member["id"] in selected
        ],
        "transcripts": transcripts,
    }


def _load_team_transcript_context(
    run_dir: Path,
    step_id: str,
    *,
    action: str = "read-team-transcript",
):
    context = _load_context(run_dir, ensure_run_dirs=False)
    _require_step(context, step_id)
    step = context["step_map"][step_id]
    if step["kind"] != "agent_team" or step.get("operator_console") is None:
        raise ValidationError(
            "%s requires an operator-console agent_team step" % action
        )
    team_state = _load_current_team_state(context, step)
    return context, step, team_state


def review_team_plan(
    run_dir: Path,
    step_id: str,
    task_id: str,
    decision: str,
    feedback: str,
) -> Dict:
    if decision not in {"approve", "reject"}:
        raise ValidationError("review-team-plan decision must be approve or reject")
    if not isinstance(feedback, str) or not feedback.strip():
        raise ValidationError("review-team-plan requires non-empty feedback")
    if len(feedback) > MAX_AGENT_TEAM_PLAN_CRITERIA_CHARS:
        raise ValidationError(
            "review-team-plan feedback exceeds %d characters"
            % MAX_AGENT_TEAM_PLAN_CRITERIA_CHARS
        )
    if contains_secret_like(feedback):
        raise ValidationError("review-team-plan feedback contains a secret-like value")
    feedback = redact_text(feedback).strip()
    with _run_control_lock(run_dir):
        context, step, team_state, approval = _load_team_plan_context(
            run_dir,
            step_id,
            task_id,
        )
        if step["plan_approval"].get("reviewer", "lead") != "operator":
            raise ValidationError("agent team plan is not configured for operator review")
        path = agent_team_plan_approval_path(
            context["run"],
            step,
            team_state["generation"],
            task_id,
        )
        with agent_team_plan_approval_lock(path):
            approval = load_agent_team_plan_approval(
                path,
                step=step,
                workflow_fingerprint=team_state["workflow_fingerprint"],
                generation=team_state["generation"],
            )
            verify_agent_team_plan_approval_outputs(
                approval,
                context["run"].artifacts_dir,
            )
            if approval["schema"] not in {
                AGENT_TEAM_PLAN_APPROVAL_SCHEMA,
                AGENT_TEAM_PLAN_APPROVAL_SCHEMA_V2,
            }:
                raise ValidationError("legacy plan approval cannot accept operator review")
            if approval["status"] != "plan-ready":
                raise ValidationError("agent team plan is not waiting for operator review")
            review = {
                "schema": AGENT_TEAM_PLAN_REVIEW_SCHEMA,
                "task_id": task_id,
                "revision": approval["revision"],
                "decision": decision,
                "feedback": feedback,
            }
            relative = agent_team_plan_review_output_relative(
                step,
                team_state["generation"],
                task_id,
                approval["revision"],
            )
            review_path = context["run"].resolve_artifact_path(relative)
            digest = write_agent_team_plan_review(review_path, review)
            timestamp = _utc_now()
            approval = complete_agent_team_operator_plan_review(
                approval,
                review_output=relative,
                review_output_sha256=digest,
                operator_decision_id="operator-plan-review-%s" % uuid.uuid4().hex,
                decision=decision,
                feedback=feedback,
                timestamp=timestamp,
            )
            write_agent_team_plan_approval(context["run"], step, approval)
            return agent_team_plan_approval_summary(approval)


def _load_current_team_state(context: Dict, step: Dict) -> Dict:
    state_path, _ = _agent_team_control_paths(context["run"], step)
    return load_agent_team_state(
        state_path,
        step=step,
        workflow_fingerprint=workflow_fingerprint(context["workflow"]),
    )


def _load_team_plan_context(run_dir: Path, step_id: str, task_id: str):
    context = _load_context(run_dir, ensure_run_dirs=False)
    _require_step(context, step_id)
    step = context["step_map"][step_id]
    policy = step.get("plan_approval") if step["kind"] == "agent_team" else None
    if not isinstance(policy, dict) or task_id not in policy["task_ids"]:
        raise ValidationError("unknown plan-approved agent team task")
    team_state = _load_current_team_state(context, step)
    path = agent_team_plan_approval_path(
        context["run"],
        step,
        team_state["generation"],
        task_id,
    )
    if not (path.exists() or path.is_symlink()):
        raise ValidationError("agent team plan approval is not available")
    approval = load_agent_team_plan_approval(
        path,
        step=step,
        workflow_fingerprint=team_state["workflow_fingerprint"],
        generation=team_state["generation"],
    )
    verify_agent_team_plan_approval_outputs(approval, context["run"].artifacts_dir)
    return context, step, team_state, approval


def pause_run(run_dir: Path, reason: str = "") -> Dict:
    _validate_reason(reason)
    with _run_control_lock(run_dir):
        context = _load_context(run_dir)
        state = context["state"]
        status = str(state.get("status", ""))
        if status in RUN_FINAL_STATUSES:
            raise ValidationError("run cannot be paused from %s status" % (status or "unknown"))
        if status == "stop_requested":
            raise ValidationError("run already has a stop requested")
        timestamp = _utc_now()
        immediate = status not in RUN_ACTIVE_STATUSES
        if immediate:
            state.pop("control_request", None)
        else:
            state["control_request"] = _control_request("pause", reason, timestamp)
        return _save_control_event(
            context,
            action="pause-run",
            step_id="",
            affected=[],
            reason=reason,
            cascade=False,
            timestamp=timestamp,
            run_status="paused" if immediate else "pause_requested",
        )


def resume_run(run_dir: Path, reason: str = "") -> Dict:
    _validate_reason(reason)
    with _run_control_lock(run_dir):
        context = _load_context(run_dir)
        state = context["state"]
        status = str(state.get("status", ""))
        request = state.get("control_request") if isinstance(state.get("control_request"), dict) else {}
        if status in RUN_FINAL_STATUSES:
            raise ValidationError("run can only resume from paused or pause_requested status")
        if status not in {"paused", "pause_requested"} and request.get("action") != "pause":
            raise ValidationError("run can only resume from paused or pause_requested status")
        timestamp = _utc_now()
        state.pop("control_request", None)
        return _save_control_event(
            context,
            action="resume-run",
            step_id="",
            affected=[],
            reason=reason,
            cascade=False,
            timestamp=timestamp,
        )


def stop_run(run_dir: Path, reason: str = "") -> Dict:
    _validate_reason(reason)
    with _run_control_lock(run_dir):
        context = _load_context(run_dir)
        state = context["state"]
        status = str(state.get("status", ""))
        if status in RUN_FINAL_STATUSES:
            raise ValidationError("run cannot be stopped from %s status" % (status or "unknown"))
        timestamp = _utc_now()
        immediate = status not in RUN_ACTIVE_STATUSES
        if immediate:
            state.pop("control_request", None)
        else:
            state["control_request"] = _control_request("stop", reason, timestamp)
        return _save_control_event(
            context,
            action="stop-run",
            step_id="",
            affected=[],
            reason=reason,
            cascade=False,
            timestamp=timestamp,
            run_status="stopped" if immediate else "stop_requested",
        )


def restart_run(run_dir: Path, reason: str = "") -> Dict:
    _validate_reason(reason)
    with _run_control_lock(run_dir):
        context = _load_context(run_dir)
        state = context["state"]
        status = str(state.get("status", ""))
        timestamp = _utc_now()
        affected = [step["id"] for step in context["workflow"]["steps"]]
        _require_no_pending_agent_map_terminals(context, affected)
        _require_no_pending_codex_step_terminals(context, affected)
        immediate = status not in RUN_ACTIVE_STATUSES
        if immediate:
            _restart_all_steps(context, reason, timestamp)
            return _save_control_event(
                context,
                action="restart-run",
                step_id="",
                affected=affected,
                reason=reason,
                cascade=False,
                timestamp=timestamp,
                run_status="needs_resume",
            )
        state["control_request"] = _control_request("restart", reason, timestamp)
        return _save_control_event(
            context,
            action="restart-run",
            step_id="",
            affected=affected,
            reason=reason,
            cascade=False,
            timestamp=timestamp,
            run_status="restart_requested",
        )


def terminate_run(run_dir: Path, reason: str = "", force: bool = False) -> Dict:
    _validate_reason(reason)
    signal_name, signal_number = RUNNER_TERMINATION_SIGNAL_BY_FORCE[bool(force)]
    with _run_control_lock(run_dir):
        context = _load_context(run_dir)
        state = context["state"]
        status = str(state.get("status", ""))
        if status in RUN_FINAL_STATUSES:
            raise ValidationError("run cannot be terminated from %s status" % (status or "unknown"))
        if status not in RUN_ACTIVE_STATUSES:
            raise ValidationError("run can only be terminated while active")
        runner = _load_runner_metadata(context["run"].run_dir)
        target = _terminable_runner_target(runner, status)
        timestamp = _utc_now()
        events = state.setdefault("termination_events", [])
        if not isinstance(events, list):
            raise ValidationError("run state termination_events must be an array")
        original_status = status
        original_control_request = state.get("control_request") if isinstance(state.get("control_request"), dict) else None
        event = {
            "action": "terminate-run",
            "force": bool(force),
            "signal": signal_name,
            "status": "pending",
            "pid": target["pid"],
            "process_group_id": target["process_group_id"],
            "session_id": target["session_id"],
            "hostname": target["hostname"],
            "reason": _clean_reason(reason),
            "requested_at_utc": timestamp,
        }
        events.append(event)
        state["termination_events"] = events[-CONTROL_EVENT_LIMIT:]
        state["control_request"] = _control_request("stop", reason or "terminate-run signal pending", timestamp)
        state["status"] = "stop_requested"
        state["updated_at_utc"] = timestamp
        state.pop("finished_at_utc", None)
        state.pop("duration_ms", None)
        control_events = state.setdefault("control_events", [])
        if not isinstance(control_events, list):
            raise ValidationError("run state control_events must be an array")
        control_event = {
            "action": "terminate-run",
            "target": "run",
            "affected_steps": [],
            "cascade": False,
            "reason": _clean_reason(reason),
            "signal": signal_name,
            "status": "pending",
            "pid": target["pid"],
            "process_group_id": target["process_group_id"],
            "updated_at_utc": timestamp,
        }
        control_events.append(control_event)
        state["control_events"] = control_events[-CONTROL_EVENT_LIMIT:]
        context["run"].save_state_with_standard_append(
            state,
            "05-decision-log.md",
            "| %s | Runtime control `terminate-run` requested on `run` | %s | %s |\n"
            % (
                timestamp,
                _table_cell("signal=%s, pid=%s, process_group_id=%s" % (signal_name, target["pid"], target["process_group_id"])),
                _table_cell(reason or "session-isolated runner termination requested"),
            ),
        )
        try:
            _send_runner_signal(target["pid"], signal_number)
        except ValidationError as exc:
            failed_at = _utc_now()
            event["status"] = "failed"
            event["error"] = _clean_reason(str(exc))
            event["failed_at_utc"] = failed_at
            control_event["status"] = "failed"
            control_event["error"] = _clean_reason(str(exc))
            control_event["updated_at_utc"] = failed_at
            state["status"] = original_status
            if original_control_request is not None:
                state["control_request"] = original_control_request
            else:
                state.pop("control_request", None)
            state["updated_at_utc"] = failed_at
            context["run"].save_state_with_standard_append(
                state,
                "05-decision-log.md",
                "| %s | Runtime control `terminate-run` failed on `run` | %s | %s |\n"
                % (
                    failed_at,
                    _table_cell("signal=%s, pid=%s, process_group_id=%s" % (signal_name, target["pid"], target["process_group_id"])),
                    _table_cell(str(exc)),
                ),
            )
            raise
        sent_at = _utc_now()
        event["status"] = "sent"
        event["sent_at_utc"] = sent_at
        control_event["status"] = "sent"
        control_event["updated_at_utc"] = sent_at
        state["control_request"] = _control_request("stop", reason or "terminate-run signal sent", sent_at)
        state["status"] = "stop_requested"
        state["updated_at_utc"] = sent_at
        state.pop("finished_at_utc", None)
        state.pop("duration_ms", None)
        context["run"].save_state_with_standard_append(
            state,
            "05-decision-log.md",
            "| %s | Runtime control `terminate-run` signal sent on `run` | %s | %s |\n"
            % (
                sent_at,
                _table_cell("signal=%s, pid=%s, process_group_id=%s" % (signal_name, target["pid"], target["process_group_id"])),
                _table_cell(reason or "session-isolated runner termination signal sent"),
            ),
        )
        return {
            "action": "terminate-run",
            "requested_step": "",
            "affected_steps": [],
            "status": state["status"],
            "run_dir": str(context["run"].run_dir),
            "signal": signal_name,
            "pid": target["pid"],
            "process_group_id": target["process_group_id"],
        }


def save_run_workflow(run_dir: Path, destination: Path) -> Dict:
    with _run_control_lock(run_dir):
        context = _load_context(run_dir)
        destination = Path(destination)
        _write_exported_workflow(context["workflow"], destination)
        return {
            "action": "save-run-workflow",
            "requested_step": "",
            "affected_steps": [],
            "status": context["state"]["status"],
            "run_dir": str(context["run"].run_dir),
            "destination": str(destination),
        }


def export_run_workflow_text(run_dir: Path) -> str:
    context = _load_context(run_dir, ensure_run_dirs=False)
    return _serialized_exported_workflow(context["workflow"], str(context["run"].run_dir / "workflow.json"))


def record_usage(
    run_dir: Path,
    step_id: str = "",
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
    source: str = "manual",
    reason: str = "",
) -> Dict:
    _validate_reason(reason)
    clean_source = _clean_usage_source(source)
    with _run_control_lock(run_dir):
        context = _load_context(run_dir)
        timestamp = _utc_now()
        usage = _usage_record(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            source=clean_source,
            timestamp=timestamp,
        )
        state = context["state"]
        target, result_usage = _apply_usage_record(context, state, step_id, usage, reason, timestamp)
        context["run"].save_state_with_standard_append(
            state,
            "05-decision-log.md",
            _usage_decision_log_row(timestamp, target, usage, reason),
        )
        return {
            "action": "record-usage",
            "target": target,
            "status": state["status"],
            "run_dir": str(context["run"].run_dir),
            "usage": result_usage,
        }


def record_session_trace(
    run_dir: Path,
    events: List[Dict],
    source: str = "session-trace-import",
    reason: str = "",
    step_id: str = "",
    idempotency_key: Optional[str] = None,
) -> Dict:
    _validate_reason(reason)
    clean_source = _clean_trace_source(source)
    _validate_session_trace_events(events)
    _validate_telemetry_idempotency_key(idempotency_key)
    with _run_control_lock(run_dir):
        context = _load_context(run_dir)
        state = context["state"]
        if _telemetry_receipt_recorded(state, idempotency_key):
            return {
                "action": "import-session-trace",
                "imported_events": 0,
                "total_events": len(state.get("session_trace_events", [])),
                "status": state["status"],
                "run_dir": str(context["run"].run_dir),
                "idempotent": True,
            }
        timestamp = _utc_now()
        normalized = _prepared_session_trace_events(context, events, clean_source, timestamp, step_id)
        _apply_session_trace_events(state, normalized, timestamp)
        _append_telemetry_receipt(state, idempotency_key)
        context["run"].save_state_with_standard_append(
            state,
            "05-decision-log.md",
            _session_trace_decision_log_row(timestamp, clean_source, len(normalized), reason),
        )
        return {
            "action": "import-session-trace",
            "imported_events": len(normalized),
            "total_events": len(state["session_trace_events"]),
            "status": state["status"],
            "run_dir": str(context["run"].run_dir),
        }


def record_session_trace_with_usage(
    run_dir: Path,
    events: List[Dict],
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
    source: str = "session-trace-import",
    reason: str = "",
    step_id: str = "",
    idempotency_key: Optional[str] = None,
) -> Dict:
    _validate_reason(reason)
    clean_trace_source = _clean_trace_source(source)
    clean_usage_source = _clean_usage_source(source)
    _validate_session_trace_events(events)
    _validate_telemetry_idempotency_key(idempotency_key)
    usage = _usage_record(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        source=clean_usage_source,
        timestamp=_utc_now(),
    )
    with _run_control_lock(run_dir):
        context = _load_context(run_dir)
        state = context["state"]
        if _telemetry_receipt_recorded(state, idempotency_key):
            target = step_id or "run"
            result_usage = (
                state.get("steps", {}).get(step_id, {})
                if step_id
                else state.get("usage", {})
            )
            if not _usage_record_matches(result_usage, usage):
                timestamp = _utc_now()
                usage["recorded_at_utc"] = timestamp
                target, result_usage = _apply_usage_record(
                    context,
                    state,
                    step_id,
                    usage,
                    reason,
                    timestamp,
                    append_event=False,
                )
                context["run"].save_state_with_standard_append(
                    state,
                    "05-decision-log.md",
                    _usage_decision_log_row(
                        timestamp,
                        target,
                        usage,
                        reason or "idempotent provider telemetry usage reconciliation",
                    ),
                )
            return {
                "action": "import-session-trace",
                "imported_events": 0,
                "total_events": len(state.get("session_trace_events", [])),
                "status": state["status"],
                "run_dir": str(context["run"].run_dir),
                "usage_target": target,
                "usage": result_usage,
                "idempotent": True,
            }
        timestamp = _utc_now()
        usage["recorded_at_utc"] = timestamp
        normalized = _prepared_session_trace_events(context, events, clean_trace_source, timestamp, step_id)
        target, result_usage = _apply_usage_record(context, state, step_id, usage, reason, timestamp)
        _apply_session_trace_events(state, normalized, timestamp)
        _append_telemetry_receipt(state, idempotency_key)
        context["run"].save_state_with_standard_append(
            state,
            "05-decision-log.md",
            _session_trace_decision_log_row(timestamp, clean_trace_source, len(normalized), reason)
            + _usage_decision_log_row(timestamp, target, usage, reason),
        )
        return {
            "action": "import-session-trace",
            "imported_events": len(normalized),
            "total_events": len(state["session_trace_events"]),
            "status": state["status"],
            "run_dir": str(context["run"].run_dir),
            "usage_target": target,
            "usage": result_usage,
        }


def _reset_step(
    context: Dict,
    step_id: str,
    action: str,
    reason: str,
    cascade: bool,
    packet_index: Optional[int] = None,
    packet_generation: Optional[int] = None,
) -> Dict:
    state = context["state"]
    steps = state.setdefault("steps", {})
    if not isinstance(steps, dict):
        raise ValidationError("run state steps must be an object")
    timestamp = _utc_now()
    affected = [step_id]
    if cascade:
        affected.extend(_downstream_steps(context["workflow"], step_id))
    invalidated = affected if action == "reset-step" else affected[1:]
    _require_no_pending_agent_map_terminals(context, invalidated)
    _require_no_pending_codex_step_terminals(context, invalidated)
    for target in affected:
        previous = steps.get(target, {})
        detail = "%s reset by control action" % target
        if reason:
            detail += ": %s" % _clean_reason(reason)
        steps[target] = _step_entry("pending", detail, timestamp, previous, kind=context["step_map"][target]["kind"])
        if context["step_map"][target]["kind"] == "agent_map" and (action == "reset-step" or target != step_id):
            _increment_agent_map_cache_generation(state, target)
        elif context["step_map"][target]["kind"] == "agent_team" and (
            action == "reset-step" or target != step_id
        ):
            _increment_agent_team_generation(state, target)
    _clear_codex_resume_bindings(state, affected)
    return _save_control_event(
        context,
        action=action,
        step_id=step_id,
        affected=affected,
        reason=reason,
        cascade=cascade,
        timestamp=timestamp,
        packet_index=packet_index,
        packet_generation=packet_generation,
    )


def _restart_all_steps(context: Dict, reason: str, timestamp: str) -> None:
    state = context["state"]
    steps = state.setdefault("steps", {})
    if not isinstance(steps, dict):
        raise ValidationError("run state steps must be an object")
    detail = "reset by restart-run control action"
    if reason:
        detail += ": %s" % _clean_reason(reason)
    for step in context["workflow"]["steps"]:
        step_id = step["id"]
        previous = steps.get(step_id, {})
        steps[step_id] = _step_entry("pending", detail, timestamp, previous, kind=step["kind"])
        if step["kind"] == "agent_map":
            _increment_agent_map_cache_generation(state, step_id)
        elif step["kind"] == "agent_team":
            _increment_agent_team_generation(state, step_id)
    _clear_codex_resume_bindings(state)
    state.pop("control_request", None)
    state["status"] = "needs_resume"
    state["updated_at_utc"] = timestamp
    state.pop("finished_at_utc", None)
    state.pop("duration_ms", None)


def _clear_codex_resume_bindings(state: Dict, step_ids: Optional[List[str]] = None) -> None:
    bindings = state.get("codex_resume_bindings")
    if bindings is None:
        return
    if not isinstance(bindings, dict):
        raise ValidationError("run state codex_resume_bindings must be an object")
    if step_ids is None:
        state.pop("codex_resume_bindings", None)
        return
    for step_id in step_ids:
        bindings.pop(step_id, None)
    if not bindings:
        state.pop("codex_resume_bindings", None)


def _write_exported_workflow(workflow: Dict, destination: Path) -> None:
    reject_symlink_path(destination, "workflow destination")
    if destination.exists():
        raise ValidationError("workflow destination already exists: %s" % destination)
    parent = destination.parent
    reject_symlink_path(destination, "workflow destination")
    serialized = _serialized_exported_workflow(workflow, str(destination))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_fd = ensure_dir_no_follow(parent, "workflow destination parent")
    fd = None
    try:
        fd = os.open(destination.name, flags, 0o600, dir_fd=parent_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise ValidationError("workflow destination already exists: %s" % destination)
    except OSError as exc:
        try:
            os.unlink(destination.name, dir_fd=parent_fd)
        except OSError:
            pass
        raise ValidationError("failed to write workflow destination %s: %s" % (destination, exc.__class__.__name__))
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def _serialized_exported_workflow(workflow: Dict, source: str) -> str:
    workflow = _exportable_workflow(workflow)
    validate_workflow(workflow, source=source)
    public_workflow = redact_public_workflow_value(workflow)
    return json.dumps(public_workflow, indent=2, sort_keys=True) + "\n"


def _exportable_workflow(workflow: Dict) -> Dict:
    return {key: value for key, value in workflow.items() if not str(key).startswith("_")}


def _load_runner_metadata(run_dir: Path) -> Dict:
    path = Path(run_dir) / "runner.json"
    if path.exists() and path.is_symlink():
        raise ValidationError("runner.json must not be a symlink: %s" % path)
    try:
        text = read_regular_text_file_no_follow(path, "runner.json", RUNNER_METADATA_BYTES)
    except FileNotFoundError:
        raise ValidationError("missing runner metadata: %s" % path)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError("runner.json is not valid JSON: %s" % exc)
    if not isinstance(data, dict):
        raise ValidationError("runner.json must contain an object")
    return data


def _terminable_runner_target(runner: Dict, run_status: str) -> Dict:
    if runner.get("schema") != "conductor.runner.v1":
        raise ValidationError("runner metadata has an unsupported schema")
    if runner.get("status") != "active":
        raise ValidationError("runner metadata is not active")
    if runner.get("run_status") != run_status:
        raise ValidationError("runner metadata run_status does not match run state")
    hostname = runner.get("hostname")
    current_hostname = _current_hostname()
    if not isinstance(hostname, str) or not hostname:
        raise ValidationError("runner metadata hostname is missing")
    if hostname != current_hostname:
        raise ValidationError("runner metadata hostname does not match this host")
    pid = _positive_runner_int(runner.get("pid"), "pid")
    process_group_id = _positive_runner_int(runner.get("process_group_id"), "process_group_id")
    session_id = _positive_runner_int(runner.get("session_id"), "session_id")
    heartbeat_at = runner.get("heartbeat_at_utc")
    heartbeat = _parse_utc(heartbeat_at) if isinstance(heartbeat_at, str) else None
    if heartbeat is None:
        raise ValidationError("runner metadata heartbeat is missing or invalid")
    heartbeat_delta = (utc_now() - heartbeat).total_seconds()
    if heartbeat_delta < -MAX_TERMINATE_HEARTBEAT_FUTURE_SKEW_SECONDS:
        raise ValidationError("runner metadata heartbeat is from the future")
    heartbeat_age = max(0, int(heartbeat_delta))
    if heartbeat_age > MAX_TERMINATE_HEARTBEAT_AGE_SECONDS:
        raise ValidationError(
            "runner metadata heartbeat is too old for terminate-run; use stop-run or inspect the process manually"
        )
    if pid == os.getpid():
        raise ValidationError("refusing to terminate the current process")
    if process_group_id != pid or session_id != pid:
        raise ValidationError("runner is not session-isolated; use stop-run for foreground runs")
    current_process_group = _current_process_group_id()
    if current_process_group is not None and process_group_id == current_process_group:
        raise ValidationError("refusing to terminate the current process group")
    current_session = _current_session_id()
    if current_session is not None and session_id == current_session:
        raise ValidationError("refusing to terminate the current session")
    if not _pid_is_running(pid):
        raise ValidationError("runner pid is not running")
    return {
        "pid": pid,
        "process_group_id": process_group_id,
        "session_id": session_id,
        "hostname": hostname,
        "heartbeat_at_utc": heartbeat_at,
    }


def _recoverable_runner_evidence(runner: Dict) -> Dict:
    if runner.get("schema") != "conductor.runner.v1":
        raise ValidationError("runner metadata has an unsupported schema")
    runner_status = runner.get("status")
    if runner_status not in {"active", "finished"}:
        raise ValidationError("runner metadata is not recoverable")
    hostname = runner.get("hostname")
    if not isinstance(hostname, str) or not hostname:
        raise ValidationError("runner metadata hostname is missing")
    if hostname != _current_hostname():
        raise ValidationError("runner metadata hostname does not match this host")
    runner_pid = _positive_runner_int(runner.get("pid"), "pid")
    if runner_status == "active" and _pid_is_running(runner_pid):
        raise ValidationError("runner owner is still running; stop or terminate it before recovery")

    processes = runner.get("active_processes")
    if not isinstance(processes, list):
        raise ValidationError(
            "runner metadata lacks tracked child-process evidence; inspect legacy processes before recovery"
        )
    seen = set()
    for record in processes:
        if not isinstance(record, dict):
            raise ValidationError("runner child-process metadata must contain objects")
        pid = _positive_runner_int(record.get("pid"), "child pid")
        process_group_id = _positive_runner_int(record.get("process_group_id"), "child process_group_id")
        session_id = _positive_runner_int(record.get("session_id"), "child session_id")
        if pid in seen:
            raise ValidationError("runner child-process metadata contains duplicate pids")
        seen.add(pid)
        if process_group_id != pid or session_id != pid:
            raise ValidationError("runner child process is not session-isolated")
        if _recorded_process_identity_is_live(pid, process_group_id, session_id):
            raise ValidationError(
                "recorded child process %d is still running; terminate it before recovery" % pid
            )
    return {
        "runner_status": runner_status,
        "runner_pid": runner_pid,
        "tracked_processes": len(processes),
    }


def _finalize_recovered_runner_metadata(
    context: Dict,
    runner: Dict,
    timestamp: str,
    resolution: str,
    run_status: str,
) -> None:
    finalized = dict(runner)
    finalized["status"] = "finished"
    finalized["event"] = "interruption-recovered"
    finalized["run_status"] = run_status
    finalized["finished_at_utc"] = timestamp
    finalized["recovered_at_utc"] = timestamp
    finalized["active_processes"] = []
    finalized["recovery"] = {
        "previous_status": runner.get("status"),
        "resolution": resolution,
        "tracked_processes": len(runner.get("active_processes", [])),
    }
    context["run"].write_json("runner.json", finalized)


def _recorded_process_identity_is_live(pid: int, process_group_id: int, session_id: int) -> bool:
    if not _pid_is_running(pid):
        return False
    try:
        current_group = os.getpgid(pid)
        current_session = os.getsid(pid)
    except ProcessLookupError:
        return False
    except (AttributeError, PermissionError):
        return True
    except OSError:
        return True
    return current_group == process_group_id and current_session == session_id


def _send_runner_signal(pid: int, signal_number: int) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(pid, signal_number)
        else:  # pragma: no cover - platform fallback.
            os.kill(pid, signal_number)
    except ProcessLookupError:
        raise ValidationError("runner pid is not running")
    except PermissionError:
        raise ValidationError("permission denied signaling runner process")
    except OSError as exc:
        raise ValidationError("failed to signal runner process: %s" % exc.__class__.__name__)


def _positive_runner_int(value, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError("runner metadata %s must be a positive integer" % label)
    return value


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _current_hostname() -> str:
    try:
        return os.uname().nodename
    except AttributeError:  # pragma: no cover - platform fallback.
        return "unknown"


def _current_process_group_id() -> Optional[int]:
    try:
        return os.getpgrp()
    except AttributeError:  # pragma: no cover - platform fallback.
        return None


def _current_session_id() -> Optional[int]:
    try:
        return os.getsid(0)
    except (AttributeError, OSError):  # pragma: no cover - platform fallback.
        return None


def _agent_team_control_paths(run: RunArtifacts, step: Dict):
    state_relative = "%s/team-state.json" % step["capture_dir"]
    inbox_relative = "%s/operator-inbox.json" % step["capture_dir"]
    require_no_path_escape(state_relative)
    require_no_path_escape(inbox_relative)
    state_path = run.resolve_artifact_path(state_relative)
    inbox_path = run.resolve_artifact_path(inbox_relative)
    reject_symlink_path(state_path, "agent team state")
    reject_symlink_path(inbox_path, "agent team operator inbox")
    return state_path, inbox_path


def _agent_team_chat_control_path(run: RunArtifacts, step: Dict) -> Path:
    relative = "%s/operator-chat.json" % step["capture_dir"]
    require_no_path_escape(relative)
    path = run.resolve_artifact_path(relative)
    reject_symlink_path(path, "agent team operator chat")
    return path


def _load_or_initialize_agent_team_chat(path: Path, state: Dict) -> Dict:
    if path.exists() or path.is_symlink():
        return load_agent_team_operator_chat(path)
    return initial_agent_team_operator_chat(
        state["step_id"],
        state["workflow_fingerprint"],
    )


def _agent_team_control_max_workers(run: RunArtifacts, step: Dict) -> int:
    runner = _load_runner_metadata(run.run_dir)
    if runner.get("schema") != "conductor.runner.v1":
        raise ValidationError("runner metadata has an unsupported schema")
    max_workers = runner.get("max_workers")
    if (
        not isinstance(max_workers, int)
        or isinstance(max_workers, bool)
        or max_workers < 1
    ):
        raise ValidationError("runner metadata max_workers must be a positive integer")
    return min(step["max_workers"], max_workers)


def _load_context(run_dir: Path, ensure_run_dirs: bool = True) -> Dict:
    run = RunArtifacts.resume(run_dir) if ensure_run_dirs else _resume_run_read_only(run_dir)
    workflow_path = run.run_dir / "workflow.json"
    state = run.read_state()
    if not isinstance(state, dict) or state.get("schema") != "conductor.run_state.v1":
        raise ValidationError("run state has an unsupported schema")
    if "steps" in state and not isinstance(state["steps"], dict):
        raise ValidationError("run state steps must be an object")
    try:
        workflow_text = read_regular_text_file_no_follow(
            workflow_path,
            "workflow file",
            MAX_RUN_WORKFLOW_JSON_BYTES,
        )
        workflow = json.loads(workflow_text)
    except FileNotFoundError:
        raise FileNotFoundError("missing workflow file: %s" % workflow_path)
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (workflow_path, exc))
    if not isinstance(workflow, dict):
        raise ValidationError("workflow.json must contain a JSON object")
    validate_workflow(workflow, source=str(workflow_path))
    if state.get("workflow") != workflow.get("name"):
        raise ValidationError("run state workflow does not match workflow.json")
    saved_fingerprint = state.get("workflow_fingerprint")
    redacted_fingerprint = workflow_fingerprint(workflow)
    saved_redacted_fingerprint = state.get("redacted_workflow_fingerprint")
    if saved_redacted_fingerprint:
        if saved_redacted_fingerprint != redacted_fingerprint:
            raise ValidationError("run state redacted workflow fingerprint does not match workflow.json")
    elif saved_fingerprint and saved_fingerprint != redacted_fingerprint and "<redacted" not in workflow_text:
        raise ValidationError("run state workflow fingerprint does not match workflow.json")
    step_map = {step["id"]: step for step in workflow["steps"]}
    return {
        "run": run,
        "state": state,
        "workflow": workflow,
        "step_map": step_map,
    }


def _resume_run_read_only(run_dir: Path) -> RunArtifacts:
    reject_symlink_path(run_dir, "run_dir")
    run_fd = open_dir_no_follow(run_dir, "run directory")
    os.close(run_fd)
    run = RunArtifacts(run_dir)
    if run.state_path.exists() and run.state_path.is_symlink():
        raise ValidationError("state.json must not be a symlink: %s" % run.state_path)
    if not run.state_path.is_file():
        raise FileNotFoundError("missing state file: %s" % run.state_path)
    workflow_path = run.run_dir / "workflow.json"
    if workflow_path.exists() and workflow_path.is_symlink():
        raise ValidationError("workflow.json must not be a symlink: %s" % workflow_path)
    return run


@contextmanager
def _run_control_lock(run_dir: Path):
    reject_symlink_path(run_dir, "run_dir")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    run_fd = None
    fd = None
    try:
        run_fd = open_dir_no_follow(run_dir, "run_dir")
        try:
            fd = os.open(".control.lock", flags, 0o600, dir_fd=run_fd)
        except OSError as exc:
            raise ValidationError("failed to open control lock %s: %s" % (Path(run_dir) / ".control.lock", exc.__class__.__name__))
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fd is not None:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        if run_fd is not None:
            os.close(run_fd)


def _save_control_event(
    context: Dict,
    action: str,
    step_id: str,
    affected: List[str],
    reason: str,
    cascade: bool,
    timestamp: str,
    run_status: str = "",
    packet_index: Optional[int] = None,
    packet_generation: Optional[int] = None,
    event_fields: Optional[Dict] = None,
    result_fields: Optional[Dict] = None,
) -> Dict:
    state = context["state"]
    event = {
        "action": action,
        "cascade": cascade,
        "reason": _clean_reason(reason),
        "updated_at_utc": timestamp,
    }
    if step_id:
        event["requested_step"] = step_id
        event["affected_steps"] = affected
    else:
        event["target"] = "run"
        event["affected_steps"] = affected
    if packet_index is not None:
        event["packet_index"] = packet_index
        event["packet_generation"] = packet_generation
    if event_fields:
        event.update(event_fields)
    events = state.setdefault("control_events", [])
    if not isinstance(events, list):
        raise ValidationError("run state control_events must be an array")
    events.append(event)
    state["control_events"] = events[-CONTROL_EVENT_LIMIT:]
    if run_status:
        _apply_run_status(state, run_status, timestamp)
    else:
        _refresh_status(state, context["workflow"], timestamp)
    context["run"].save_state_with_standard_append(
        state,
        "05-decision-log.md",
        "| %s | Runtime control `%s` on `%s` | %s | Affected: %s |\n"
        % (
            timestamp,
            action,
            "%s#%s" % (step_id, packet_index) if packet_index is not None else (step_id or "run"),
            _table_cell(reason or "no reason supplied"),
            _table_cell(", ".join(affected) if affected else "run"),
        ),
    )
    result = {
        "action": action,
        "requested_step": step_id,
        "affected_steps": affected,
        "status": state["status"],
        "run_dir": str(context["run"].run_dir),
    }
    if packet_index is not None:
        result["packet_index"] = packet_index
        result["packet_generation"] = packet_generation
    if result_fields:
        result.update(result_fields)
    return result


def _refresh_status(state: Dict, workflow: Dict, timestamp: str) -> None:
    steps = state.get("steps", {})
    statuses = [steps.get(step["id"], {}).get("status") for step in workflow["steps"]]
    if statuses and all(status in {"completed", "skipped"} for status in statuses):
        status = "completed"
    elif any(status == "failed" for status in statuses):
        status = "failed"
    elif any(status == "blocked" for status in statuses):
        status = "blocked"
    elif statuses and all(status in {"completed", "skipped", "planned"} for status in statuses):
        status = "planned"
    else:
        status = "needs_resume"
    state["status"] = status
    state["updated_at_utc"] = timestamp
    if status in {"completed", "failed", "blocked", "planned"}:
        state["finished_at_utc"] = timestamp
        duration = _duration_ms(state.get("started_at_utc"), timestamp)
        if duration is not None:
            state["duration_ms"] = duration
    else:
        state.pop("finished_at_utc", None)
        state.pop("duration_ms", None)


def _apply_run_status(state: Dict, status: str, timestamp: str) -> None:
    state["status"] = status
    state["updated_at_utc"] = timestamp
    if status == "stopped":
        state["finished_at_utc"] = timestamp
        duration = _duration_ms(state.get("started_at_utc"), timestamp)
        if duration is not None:
            state["duration_ms"] = duration
    else:
        state.pop("finished_at_utc", None)
        state.pop("duration_ms", None)


def _control_request(action: str, reason: str, timestamp: str) -> Dict:
    return {
        "action": action,
        "reason": _clean_reason(reason),
        "requested_at_utc": timestamp,
    }


def _downstream_steps(workflow: Dict, step_id: str) -> List[str]:
    dependents: Dict[str, List[str]] = {step["id"]: [] for step in workflow["steps"]}
    for step in workflow["steps"]:
        for dependency in step.get("depends_on", []):
            dependents[dependency].append(step["id"])
    selected: Set[str] = set()
    stack = list(dependents.get(step_id, []))
    while stack:
        current = stack.pop()
        if current in selected:
            continue
        selected.add(current)
        stack.extend(dependents.get(current, []))
    return [step["id"] for step in workflow["steps"] if step["id"] in selected]


def _step_status(state: Dict, step_id: str) -> str:
    steps = state.get("steps", {})
    if step_id not in steps:
        return "missing"
    entry = steps.get(step_id)
    if not isinstance(entry, dict):
        return "invalid"
    return entry.get("status", "missing")


def _require_inactive_direct_control(state: Dict) -> None:
    steps = state.get("steps", {})
    if not isinstance(steps, dict):
        raise ValidationError("run state steps must be an object")
    has_running_step = any(
        isinstance(entry, dict) and entry.get("status") == "running" for entry in steps.values()
    )
    if state.get("status") in RUN_ACTIVE_STATUSES or has_running_step:
        raise ValidationError(
            "step controls cannot mutate an active run or an interrupted running step; "
            "use pause, stop, terminate, or recover-run first"
        )


def _require_step(context: Dict, step_id: str) -> None:
    if step_id not in context["step_map"]:
        raise ValidationError("unknown workflow step: %s" % step_id)


def _step_entry(status: str, detail: str, timestamp: str, previous: Dict, kind: str = "") -> Dict:
    previous_status = previous.get("status") if isinstance(previous, dict) else None
    entry = {
        "status": status,
        "detail": redact_text(detail),
        "updated_at_utc": timestamp,
    }
    step_kind = kind or (previous.get("kind") if isinstance(previous, dict) else "")
    if step_kind:
        entry["kind"] = redact_text(str(step_kind))
    if previous_status:
        entry["previous_status"] = previous_status
    attempt = previous.get("attempt") if isinstance(previous, dict) else None
    if isinstance(attempt, int) and attempt > 0:
        entry["attempt"] = attempt
    if status == "skipped":
        started_at = previous.get("started_at_utc") if isinstance(previous, dict) else None
        entry["started_at_utc"] = started_at if isinstance(started_at, str) and started_at else timestamp
        entry["finished_at_utc"] = timestamp
        duration = _duration_ms(entry["started_at_utc"], timestamp)
        if duration is not None:
            entry["duration_ms"] = duration
    return entry


def _apply_usage_record(
    context: Dict,
    state: Dict,
    step_id: str,
    usage: Dict,
    reason: str,
    timestamp: str,
    *,
    append_event: bool = True,
):
    target = "run"
    result_usage = None
    if step_id:
        _require_step(context, step_id)
        steps = state.setdefault("steps", {})
        if not isinstance(steps, dict):
            raise ValidationError("run state steps must be an object")
        previous = steps.get(step_id, {})
        if previous is not None and not isinstance(previous, dict):
            raise ValidationError("run state step %s must be an object" % step_id)
        updated = dict(previous or {})
        for field in ["input_tokens", "output_tokens", "total_tokens", "cost_usd", "usage_source", "usage_recorded_at_utc"]:
            updated.pop(field, None)
        for field in ["input_tokens", "output_tokens", "total_tokens", "cost_usd"]:
            if field in usage:
                updated[field] = usage[field]
        updated["usage_source"] = usage["source"]
        updated["usage_recorded_at_utc"] = timestamp
        steps[step_id] = updated
        state["usage"] = _rollup_step_usage(steps, timestamp)
        target = step_id
        result_usage = updated
    else:
        run_usage = dict(usage)
        run_usage["status"] = "recorded"
        state["usage"] = run_usage
        result_usage = run_usage
    state["updated_at_utc"] = timestamp
    if append_event:
        _append_usage_event(state, target, usage, reason)
    return target, result_usage


def _usage_record_matches(current, expected: Dict) -> bool:
    if not isinstance(current, dict):
        return False
    current_source = current.get("usage_source", current.get("source"))
    if current_source != expected.get("source"):
        return False
    return all(
        current.get(field) == expected.get(field)
        for field in ("input_tokens", "output_tokens", "total_tokens", "cost_usd")
        if field in expected
    )


def _validate_session_trace_events(events: List[Dict]) -> None:
    if not isinstance(events, list) or not events:
        raise ValidationError("record-session-trace requires at least one event")
    if len(events) > SESSION_TRACE_EVENT_LIMIT:
        raise ValidationError("record-session-trace accepts at most %d events" % SESSION_TRACE_EVENT_LIMIT)


def _prepared_session_trace_events(
    context: Dict,
    events: List[Dict],
    source: str,
    timestamp: str,
    step_id: str = "",
) -> List[Dict]:
    if step_id:
        _require_step(context, step_id)
    normalized = [_session_trace_event(event, source, timestamp, step_id) for event in events]
    for event in normalized:
        event_step_id = event.get("step_id")
        if event_step_id:
            _require_step(context, event_step_id)
    return normalized


def _apply_session_trace_events(state: Dict, events: List[Dict], timestamp: str) -> None:
    existing = state.setdefault("session_trace_events", [])
    if not isinstance(existing, list):
        raise ValidationError("run state session_trace_events must be an array")
    existing.extend(events)
    state["session_trace_events"] = existing[-SESSION_TRACE_EVENT_LIMIT:]
    state["session_trace_summary"] = _session_trace_summary(state["session_trace_events"])
    state["updated_at_utc"] = timestamp


def _validate_telemetry_idempotency_key(value: Optional[str]) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValidationError("telemetry idempotency key must be a SHA-256 value")


def _telemetry_receipt_recorded(state: Dict, value: Optional[str]) -> bool:
    if value is None:
        return False
    receipts = state.get("telemetry_receipts", [])
    if not isinstance(receipts, list) or any(
        not isinstance(item, str)
        or len(item) != 64
        or any(character not in "0123456789abcdef" for character in item)
        for item in receipts
    ):
        raise ValidationError("run state telemetry_receipts must be SHA-256 values")
    return value in receipts


def _append_telemetry_receipt(state: Dict, value: Optional[str]) -> None:
    if value is None:
        return
    receipts = state.setdefault("telemetry_receipts", [])
    if not isinstance(receipts, list):
        raise ValidationError("run state telemetry_receipts must be an array")
    if value not in receipts:
        receipts.append(value)
    state["telemetry_receipts"] = receipts[-TELEMETRY_RECEIPT_LIMIT:]


def _session_trace_decision_log_row(timestamp: str, source: str, event_count: int, reason: str) -> str:
    return "| %s | Runtime session trace `import-session-trace` | %s | %s |\n" % (
        timestamp,
        _table_cell("source=%s, events=%s" % (source, event_count)),
        _table_cell(reason or "local session trace artifact imported"),
    )


def _usage_decision_log_row(timestamp: str, target: str, usage: Dict, reason: str) -> str:
    return "| %s | Runtime usage `record-usage` on `%s` | %s | %s |\n" % (
        timestamp,
        _table_cell(target),
        _table_cell(_usage_summary_text(usage)),
        _table_cell(reason or "measured usage supplied by operator"),
    )


def _usage_record(
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    total_tokens: Optional[int],
    cost_usd: Optional[float],
    source: str,
    timestamp: str,
) -> Dict:
    input_value = _optional_nonnegative_int(input_tokens, "input_tokens")
    output_value = _optional_nonnegative_int(output_tokens, "output_tokens")
    total_value = _optional_nonnegative_int(total_tokens, "total_tokens")
    cost_value = _optional_nonnegative_number(cost_usd, "cost_usd")
    if input_value is None and output_value is None and total_value is None and cost_value is None:
        raise ValidationError("record-usage requires at least one token or cost value")
    if input_value is not None and output_value is not None:
        derived_total = input_value + output_value
        if total_value is None:
            total_value = derived_total
            _require_usage_token_bound(total_value, "total_tokens")
        elif total_value != derived_total:
            raise ValidationError("total_tokens must equal input_tokens plus output_tokens")
    usage = {
        "source": source,
        "recorded_at_utc": timestamp,
    }
    if input_value is not None:
        usage["input_tokens"] = input_value
    if output_value is not None:
        usage["output_tokens"] = output_value
    if total_value is not None:
        usage["total_tokens"] = total_value
    if cost_value is not None:
        usage["cost_usd"] = cost_value
    return usage


def _rollup_step_usage(steps: Dict, timestamp: str) -> Dict:
    input_total = 0
    output_total = 0
    total_total = 0
    cost_total = 0.0
    has_input = False
    has_output = False
    has_total = False
    has_cost = False
    for entry in steps.values():
        if not isinstance(entry, dict):
            continue
        input_tokens = entry.get("input_tokens")
        output_tokens = entry.get("output_tokens")
        total_tokens = entry.get("total_tokens")
        cost_usd = entry.get("cost_usd")
        if _is_nonnegative_int(input_tokens):
            input_total += input_tokens
            _require_usage_token_bound(input_total, "input_tokens")
            has_input = True
        if _is_nonnegative_int(output_tokens):
            output_total += output_tokens
            _require_usage_token_bound(output_total, "output_tokens")
            has_output = True
        if _is_nonnegative_int(total_tokens):
            total_total += total_tokens
            _require_usage_token_bound(total_total, "total_tokens")
            has_total = True
        if _is_nonnegative_number(cost_usd):
            cost_total += float(cost_usd)
            has_cost = True
    usage = {
        "status": "recorded",
        "source": "step-rollup",
        "recorded_at_utc": timestamp,
    }
    if has_input:
        usage["input_tokens"] = input_total
    if has_output:
        usage["output_tokens"] = output_total
    if has_total:
        usage["total_tokens"] = total_total
    elif has_input and has_output:
        derived_total = input_total + output_total
        _require_usage_token_bound(derived_total, "total_tokens")
        usage["total_tokens"] = derived_total
    if has_cost:
        usage["cost_usd"] = round(cost_total, 12)
    return usage


def _append_usage_event(state: Dict, target: str, usage: Dict, reason: str) -> None:
    events = state.setdefault("usage_events", [])
    if not isinstance(events, list):
        raise ValidationError("run state usage_events must be an array")
    event = {
        "action": "record-usage",
        "target": target,
        "source": usage["source"],
        "recorded_at_utc": usage["recorded_at_utc"],
        "reason": _clean_reason(reason),
    }
    for field in ["input_tokens", "output_tokens", "total_tokens", "cost_usd"]:
        if field in usage:
            event[field] = usage[field]
    events.append(event)
    state["usage_events"] = events[-USAGE_EVENT_LIMIT:]


def _session_trace_event(raw: Dict, source: str, imported_at_utc: str, step_id: str = "") -> Dict:
    if not isinstance(raw, dict):
        raise ValidationError("session trace events must be objects")
    event = {
        "source": source,
        "imported_at_utc": imported_at_utc,
        "provider": _trace_text(raw.get("provider") or "generic", "provider"),
        "event": _trace_text(raw.get("event") or "event", "event"),
    }
    for field in ["status", "session_id", "agent_id", "model", "role", "tool", "detail"]:
        value = raw.get(field)
        if value is not None:
            event[field] = _trace_text(value, field)
    effective_step_id = step_id or raw.get("step_id")
    if effective_step_id:
        event["step_id"] = _trace_text(effective_step_id, "step_id")
    for field in ["started_at_utc", "finished_at_utc", "recorded_at_utc"]:
        value = raw.get(field)
        if value is not None:
            event[field] = _trace_text(value, field)
    duration_ms = _optional_trace_int(raw.get("duration_ms"), "duration_ms", MAX_TRACE_DURATION_MS)
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    for field in ["input_tokens", "output_tokens", "total_tokens"]:
        value = _optional_trace_int(raw.get(field), field, MAX_USAGE_TOKENS)
        if value is not None:
            event[field] = value
    cost = raw.get("cost_usd")
    if cost is not None:
        event["cost_usd"] = _optional_nonnegative_number(cost, "cost_usd")
    return event


def _session_trace_summary(events: List[Dict]) -> Dict:
    by_provider: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        provider = str(event.get("provider") or "generic")
        status = str(event.get("status") or "unknown")
        by_provider[provider] = by_provider.get(provider, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "count": sum(by_provider.values()),
        "by_provider": by_provider,
        "by_status": by_status,
    }


def _clean_usage_source(source: str) -> str:
    value = " ".join(str(source or "manual").split())
    if not value:
        value = "manual"
    if len(value) > 80:
        raise ValidationError("usage source must be at most 80 characters")
    if contains_secret_like(value):
        raise ValidationError("usage source must not contain secret-like values")
    return value


def _clean_trace_source(source: str) -> str:
    value = " ".join(str(source or "session-trace-import").split())
    if not value:
        value = "session-trace-import"
    if len(value) > 80:
        raise ValidationError("session trace source must be at most 80 characters")
    if contains_secret_like(value):
        raise ValidationError("session trace source must not contain secret-like values")
    return value


def _trace_text(value, label: str) -> str:
    text = redact_text(" ".join(str(value).split()))[:300]
    if not text:
        raise ValidationError("session trace %s must not be empty" % label)
    return text


def _optional_trace_int(value, label: str, maximum: int) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ValidationError("session trace %s must be a non-negative integer no greater than %s" % (label, maximum))
    return value


def _usage_summary_text(usage: Dict) -> str:
    parts = ["source=%s" % usage["source"]]
    for field in ["input_tokens", "output_tokens", "total_tokens", "cost_usd"]:
        if field in usage:
            parts.append("%s=%s" % (field, usage[field]))
    return ", ".join(parts)


def _optional_nonnegative_int(value, label: str) -> Optional[int]:
    if value is None:
        return None
    if not _is_nonnegative_int(value):
        raise ValidationError("%s must be a non-negative integer no greater than %s" % (label, MAX_USAGE_TOKENS))
    return value


def _optional_nonnegative_number(value, label: str):
    if value is None:
        return None
    if not _is_nonnegative_number(value):
        raise ValidationError("%s must be a non-negative finite number" % label)
    return float(value)


def _is_nonnegative_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_USAGE_TOKENS


def _require_usage_token_bound(value: int, label: str) -> None:
    if not _is_nonnegative_int(value):
        raise ValidationError("%s must be a non-negative integer no greater than %s" % (label, MAX_USAGE_TOKENS))


def _is_nonnegative_number(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _increment_agent_map_cache_generation(state: Dict, step_id: str) -> None:
    generations = state.setdefault("agent_map_cache_generations", {})
    if not isinstance(generations, dict):
        raise ValidationError("run state agent_map_cache_generations must be an object")
    current = generations.get(step_id, 0)
    generations[step_id] = (current if isinstance(current, int) and current >= 0 else 0) + 1
    packet_generations = state.get("agent_map_packet_generations")
    if isinstance(packet_generations, dict):
        packet_generations.pop(step_id, None)


def _require_no_pending_agent_map_terminals(
    context: Dict,
    step_ids: List[str],
    packet_indexes: Optional[Dict[str, Set[int]]] = None,
) -> None:
    workflow_sha256 = workflow_fingerprint(context["workflow"])
    for step_id in step_ids:
        step = context["step_map"].get(step_id)
        if not isinstance(step, dict) or step.get("kind") != "agent_map":
            continue
        capture_dir = step.get("capture_dir", step_id)
        require_no_path_escape(capture_dir)
        directory = context["run"].resolve_artifact_path(
            "%s/.agent-map-turn-terminals" % capture_dir
        )
        reject_symlink_path(directory, "agent_map packet terminal directory")
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise ValidationError("agent_map packet terminal directory is invalid")
        selected = packet_indexes.get(step_id) if packet_indexes else None
        pending = []
        try:
            entries = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise ValidationError(
                "failed to inspect agent_map packet terminals: %s"
                % exc.__class__.__name__
            )
        for path in entries:
            reject_symlink_path(path, "agent_map packet terminal")
            stem = path.stem
            if (
                path.suffix != ".json"
                or len(stem) != 6
                or not stem.isdigit()
                or not 1 <= int(stem) <= MAX_AGENT_PACKETS
                or path.name != "%06d.json" % int(stem)
                or not path.is_file()
            ):
                raise ValidationError(
                    "agent_map packet terminal directory has an unknown entry"
                )
            terminal = load_agent_map_packet_terminal(
                path,
                step=step,
                expected={"workflow_fingerprint": workflow_sha256},
            )
            index = int(stem)
            if terminal["index"] != index:
                raise ValidationError(
                    "agent_map packet terminal path binding changed"
                )
            if selected is None or index in selected:
                pending.append(index)
        if pending:
            raise ValidationError(
                "agent_map step %s has locally completed packet(s) %s pending "
                "recovery; use retry-step and resume before resetting, retrying, "
                "skipping, or restarting"
                % (step_id, ", ".join(str(index) for index in pending))
            )


def _require_no_pending_codex_step_terminals(
    context: Dict,
    step_ids: List[str],
) -> None:
    workflow_sha256 = workflow_fingerprint(context["workflow"])
    for step_id in step_ids:
        step = context["step_map"].get(step_id)
        if not isinstance(step, dict) or step.get("kind") != "codex_exec":
            continue
        path = codex_step_terminal_path(context["run"], step_id)
        if not path.exists() and not path.is_symlink():
            continue
        terminal = load_codex_step_terminal(
            path,
            workflow_fingerprint=workflow_sha256,
        )
        if terminal["step_id"] != step_id:
            raise ValidationError("Codex step terminal path binding changed")
        raise ValidationError(
            "codex_exec step %s has a locally completed turn pending recovery; "
            "use retry-step and resume before resetting, skipping, or restarting"
            % step_id
        )


def _increment_agent_team_generation(state: Dict, step_id: str) -> None:
    generations = state.setdefault("agent_team_generations", {})
    if not isinstance(generations, dict):
        raise ValidationError("run state agent_team_generations must be an object")
    current = generations.get(step_id, 0)
    if (
        not isinstance(current, int)
        or isinstance(current, bool)
        or not 0 <= current < MAX_AGENT_TEAM_GENERATION
    ):
        raise ValidationError("run state agent team generation is invalid for %s" % step_id)
    generations[step_id] = current + 1


def _increment_agent_map_packet_generation(
    state: Dict,
    step_id: str,
    packet_index: int,
    item_sha256: str,
) -> int:
    all_steps = state.setdefault("agent_map_packet_generations", {})
    if not isinstance(all_steps, dict):
        raise ValidationError("run state agent_map_packet_generations must be an object")
    records = all_steps.setdefault(step_id, {})
    if not isinstance(records, dict):
        raise ValidationError("run state packet generations for %s must be an object" % step_id)
    key = str(packet_index)
    previous = records.get(key)
    if previous is None:
        generation = 1
    else:
        if not isinstance(previous, dict) or set(previous) != {"generation", "item_sha256"}:
            raise ValidationError("run state packet generation record is invalid for %s" % step_id)
        if previous.get("item_sha256") != item_sha256:
            raise ValidationError("packet item changed since its prior targeted retry; reset the whole step")
        current = previous.get("generation")
        if not isinstance(current, int) or isinstance(current, bool) or current < 1:
            raise ValidationError("run state packet generation must be a positive integer")
        generation = current + 1
    records[key] = {"generation": generation, "item_sha256": item_sha256}
    return generation


def _packet_trace_item_sha256(context: Dict, step: Dict, packet_index: int) -> str:
    capture_dir = step.get("capture_dir", step["id"])
    require_no_path_escape(capture_dir)
    path = context["run"].artifacts_dir / capture_dir / ".agent-map-trace.jsonl"
    text = read_regular_text_file_no_follow(
        path,
        "agent_map packet trace",
        MAX_AGENT_PACKET_CONTROL_TRACE_BYTES,
    )
    latest_hash = None
    saw_target = False
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_json_pairs,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise ValidationError(
                "agent_map packet trace line %d is invalid: %s" % (line_number, exc.__class__.__name__)
            )
        if not isinstance(row, dict) or row.get("schema") != "conductor.agent_map_trace.v1":
            raise ValidationError("agent_map packet trace line %d has an unsupported schema" % line_number)
        if row.get("step_id") != step["id"]:
            raise ValidationError("agent_map packet trace step binding is inconsistent")
        row_index = row.get("index")
        if not isinstance(row_index, int) or isinstance(row_index, bool) or row_index < 1:
            raise ValidationError("agent_map packet trace index is invalid")
        item_hash = row.get("item_sha256")
        if item_hash is not None and not _is_sha256(item_hash):
            raise ValidationError("agent_map packet trace item hash is invalid")
        if row_index == packet_index:
            saw_target = True
            latest_hash = item_hash
    if saw_target and latest_hash is None:
        latest_hash = _packet_item_sha256_from_available_source(context, step, packet_index)
    if latest_hash is None:
        raise ValidationError(
            "packet %d has no current item hash in its trace; use retry-step or reset-step" % packet_index
        )
    return latest_hash


def _packet_item_sha256_from_available_source(context: Dict, step: Dict, packet_index: int) -> Optional[str]:
    max_items = step.get("max_items", context["workflow"].get("max_items", MAX_AGENT_ITEMS))
    preserve_duplicates = step.get("preserve_duplicate_items", False)
    item_semantics = step.get("item_semantics", "workspace_path")
    if isinstance(step.get("items"), list):
        items = clean_packet_items(
            step["items"],
            "retry-packet items",
            max_items,
            preserve_duplicates=preserve_duplicates,
            item_semantics=item_semantics,
        )
    elif isinstance(step.get("items_artifact"), str):
        require_no_path_escape(step["items_artifact"])
        path = context["run"].resolve_artifact_path(step["items_artifact"])
        label = "retry-packet items artifact"
        if isinstance(step.get("items_pointer"), str):
            items = read_packet_items_json_file(
                path,
                label,
                max_items,
                step["items_pointer"],
                preserve_duplicates=preserve_duplicates,
                item_semantics=item_semantics,
            )
        else:
            items = read_packet_items_file(
                path,
                label,
                max_items,
                preserve_duplicates=preserve_duplicates,
                item_semantics=item_semantics,
            )
    else:
        return None
    packets = packetize_agent_items(items, step.get("max_packets"))
    if packet_index > len(packets):
        return None
    return _sha256_text(packets[packet_index - 1].value)


def _validate_packet_index(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_AGENT_ITEMS:
        raise ValidationError("packet_index must be an integer from 1 to %d" % MAX_AGENT_ITEMS)
    return value


def _is_sha256(value) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject_duplicate_json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key %s" % key)
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant %s" % value)


def _clean_reason(reason: str) -> str:
    return redact_text(" ".join(str(reason).split()))


def _validate_reason(reason: str) -> None:
    if len(str(reason)) > MAX_REASON_CHARS:
        raise ValidationError("reason must be at most %d characters" % MAX_REASON_CHARS)


def _table_cell(value: str) -> str:
    cleaned = _clean_reason(value)
    return cleaned.replace("|", "\\|")


def _utc_now() -> str:
    return utc_now().isoformat(timespec="seconds") + "Z"


def _duration_ms(started_at, finished_at):
    if not isinstance(started_at, str) or not isinstance(finished_at, str):
        return None
    started = _parse_utc(started_at)
    finished = _parse_utc(finished_at)
    if started is None or finished is None:
        return None
    return max(0, int((finished - started).total_seconds() * 1000))


def _parse_utc(value: str):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return None
    return parsed
