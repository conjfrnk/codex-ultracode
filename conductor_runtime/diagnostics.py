import shlex
from typing import Dict, List, Optional

from .redaction import redact_text


RUN_DIAGNOSTICS_SCHEMA = "conductor.run_diagnostics.v1"
_SEVERITY_ORDER = {"ok": 0, "info": 1, "warning": 2, "action_required": 3}
_TERMINAL_OK_STEP_STATUSES = {"completed", "skipped"}
_RESUMABLE_STATUSES = {"needs_resume", "paused"}
_CONTROL_PENDING_STATUSES = {"pause_requested", "stop_requested", "restart_requested"}
_MAX_AGENT_MAP_DIAGNOSTIC_PACKETS = 5
_AGENT_PACKET_ACTION_STATUSES = {"cached-output-missing", "ran-without-output"}
_AGENT_TRACE_ACTION_STATUSES = {"failed", "timed_out", "no_output"}


def diagnose_run_detail(detail: Dict) -> Dict:
    """Build redacted, read-only diagnostics from a collected run detail payload."""
    if not isinstance(detail, dict):
        return {
            "schema": RUN_DIAGNOSTICS_SCHEMA,
            "severity": "action_required",
            "summary": "Run detail is unavailable.",
            "issues": [
                _issue(
                    "detail_unavailable",
                    "action_required",
                    "Run detail could not be collected.",
                    recommendation="Re-run diagnostics after the run directory is readable.",
                )
            ],
        }

    issues: List[Dict] = []
    status = _text(detail.get("status") or "unknown")
    steps = detail.get("steps") if isinstance(detail.get("steps"), list) else []
    runner = detail.get("runner") if isinstance(detail.get("runner"), dict) else {}
    launch = detail.get("launch") if isinstance(detail.get("launch"), dict) else {}
    standard_files = detail.get("standard_files") if isinstance(detail.get("standard_files"), list) else []

    if detail.get("error"):
        issues.append(
            _issue(
                "detail_collection_error",
                "action_required",
                "Run detail could not be fully collected.",
                detail=_text(detail.get("error")),
                recommendation="Inspect the run directory structure before attempting recovery.",
            )
        )
    if detail.get("state_error"):
        issues.append(
            _issue(
                "state_error",
                "action_required",
                "state.json could not be read as valid run state.",
                detail=_text(detail.get("state_error")),
                recommendation="Repair or restore state.json before resuming this run.",
            )
        )
    if detail.get("workflow_error"):
        issues.append(
            _issue(
                "workflow_error",
                "action_required",
                "workflow.json could not be read as valid workflow metadata.",
                detail=_text(detail.get("workflow_error")),
                recommendation="Repair or restore workflow.json before resuming this run.",
            )
        )

    if status == "failed":
        issues.append(
            _issue(
                "run_failed",
                "action_required",
                "Run status is failed.",
                recommendation="Inspect failed steps and retry or reset only after the cause is understood.",
            )
        )
    elif status == "blocked":
        approvals = detail.get("approvals") if isinstance(detail.get("approvals"), dict) else {}
        pending_approvals = approvals.get("pending_count")
        approval_blocked = isinstance(pending_approvals, int) and not isinstance(pending_approvals, bool) and pending_approvals > 0
        issues.append(
            _issue(
                "run_blocked",
                "action_required",
                "Run status is blocked.",
                recommendation=(
                    "Review the manual-gate approval guidance and resume from the CLI with --approve."
                    if approval_blocked
                    else "Resolve the blocker, then retry or reset the affected step."
                ),
            )
        )
    elif status in _RESUMABLE_STATUSES:
        launch_command = _launch_resume_command(launch)
        launch_status = _text(launch.get("status") or "")
        launch_error = _text(launch.get("error") or "")
        detail_text = ""
        if launch_status and launch_status != "unavailable":
            detail_text = "Launch manifest status: %s." % launch_status
        if launch_error and launch_error != "missing":
            detail_text = ("%s " % detail_text if detail_text else "") + "Launch manifest error: %s." % launch_error
        command_error = _text(launch.get("resume_command_error") or "")
        if command_error:
            detail_text = ("%s " % detail_text if detail_text else "") + "Resume command unavailable: %s." % command_error
        issues.append(
            _issue(
                "run_waiting_for_resume",
                "info",
                "Run is waiting for an explicit resume.",
                detail=detail_text,
                recommendation=(
                    "Resume with the prepared launch command after reviewing current artifacts."
                    if launch_command
                    else "Resume from the saved run directory after reviewing current artifacts."
                ),
                command=launch_command or "python3 -m conductor_runtime run RUN_WORKFLOW --resume RUN_DIR",
            )
        )
    elif status in _CONTROL_PENDING_STATUSES:
        issues.append(
            _issue(
                "run_control_pending",
                "info",
                "Run has a pending control request.",
                recommendation="Wait for the next step boundary or inspect runner liveness if progress has stopped.",
            )
        )

    _diagnose_runner(status, runner, steps, issues)
    _diagnose_steps(status, steps, issues)
    _diagnose_runtime(detail.get("runtime") if isinstance(detail.get("runtime"), dict) else {}, status, issues)
    _diagnose_agent_native_tool_hooks(
        detail.get("agent_native_tool_hooks")
        if isinstance(detail.get("agent_native_tool_hooks"), dict)
        else {},
        status,
        issues,
    )
    _diagnose_agent_maps(detail.get("agent_maps") if isinstance(detail.get("agent_maps"), list) else [], issues)
    _diagnose_completed_evidence(status, steps, standard_files, issues)

    severity = _top_severity(issues)
    return {
        "schema": RUN_DIAGNOSTICS_SCHEMA,
        "severity": severity,
        "summary": _summary(severity, issues),
        "issues": issues,
    }


def _diagnose_runner(status: str, runner: Dict, steps: List[Dict], issues: List[Dict]) -> None:
    runner_status = _text(runner.get("status") or "unavailable")
    liveness = _text(runner.get("liveness") or "unavailable")
    if runner_status == "active" and liveness == "stale":
        heartbeat = _text(runner.get("heartbeat_at_utc"))
        reason = _text(runner.get("liveness_reason"))
        detail = "Heartbeat %s. %s" % (heartbeat or "unavailable", reason or "Runner heartbeat is stale.")
        issues.append(
            _issue(
                "runner_stale",
                "action_required",
                "Runner metadata is active but the heartbeat is stale.",
                detail=detail,
                recommendation=(
                    "Inspect the runner terminal. After the owner and every tracked child have exited, "
                    "use audited interrupted-run recovery before resuming."
                ),
                command=_interrupted_recovery_command(
                    steps,
                    "confirmed interrupted runner is inactive",
                ),
            )
        )
    elif status in {
        "planning",
        "running",
        "pause_requested",
        "stop_requested",
        "restart_requested",
    } and runner_status == "finished":
        issues.append(
            _issue(
                "runner_finished_with_active_state",
                "action_required",
                "Runner finished while the run state still appears active.",
                recommendation="Reconcile the interrupted state with recover-run before resuming.",
                command=_interrupted_recovery_command(
                    steps,
                    "runner finished before state finalization",
                ),
            )
        )
    elif status in {
        "running",
        "pause_requested",
        "stop_requested",
        "restart_requested",
    } and runner_status == "unavailable":
        issues.append(
            _issue(
                "runner_unavailable_for_active_run",
                "warning",
                "Run appears active but runner metadata is unavailable.",
                detail=_text(runner.get("error")),
                recommendation="Inspect the run directory and terminal history before assuming the run is still live.",
            )
        )


def _diagnose_agent_native_tool_hooks(
    summary: Dict,
    run_status: str,
    issues: List[Dict],
) -> None:
    status = _text(summary.get("status") or "not-configured")
    if status == "not-configured":
        return
    if status == "invalid":
        issues.append(
            _issue(
                "agent_native_tool_evidence_invalid",
                "action_required",
                "Native pre-tool gate summary is invalid.",
                recommendation="Inspect the private gate evidence before trusting or resuming this run.",
            )
        )
        return
    if status == "failed":
        issues.append(
            _issue(
                "agent_native_tool_gate_failed",
                "action_required",
                "At least one native pre-tool gate failed integrity settlement.",
                recommendation="Treat the provider result as rejected and inspect the failed gate evidence.",
            )
        )
    elif status == "prepared" and run_status not in {
        "planning",
        "running",
        "pause_requested",
        "stop_requested",
        "restart_requested",
    }:
        issues.append(
            _issue(
                "agent_native_tool_gate_unsettled",
                "action_required",
                "A terminal run retains an unsettled native pre-tool gate.",
                recommendation="Use audited terminal recovery; do not replay the provider call.",
            )
        )
    warning_count = summary.get("warning_count")
    if isinstance(warning_count, int) and warning_count > 0:
        issues.append(
            _issue(
                "agent_native_tool_verifier_warning",
                "warning",
                "%d advisory native pre-tool verifier failure(s) were allowed."
                % warning_count,
                recommendation="Review the verifier policy before relying on the affected tool calls.",
            )
        )
    denied_count = summary.get("denied_count")
    if isinstance(denied_count, int) and denied_count > 0:
        issues.append(
            _issue(
                "agent_native_tool_calls_denied",
                "info",
                "%d proposed native tool call(s) were denied before execution."
                % denied_count,
            )
        )


def _diagnose_steps(run_status: str, steps: List[Dict], issues: List[Dict]) -> None:
    emitted = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        status = _text(step.get("status") or "unknown")
        checkpoint = step.get("codex_checkpoint") if isinstance(step.get("codex_checkpoint"), dict) else {}
        terminal = step.get("codex_terminal") if isinstance(step.get("codex_terminal"), dict) else {}
        progress = step.get("codex_progress") if isinstance(step.get("codex_progress"), dict) else {}
        packet_progress = (
            step.get("codex_packet_progress")
            if isinstance(step.get("codex_packet_progress"), list)
            else []
        )
        if progress.get("status") == "invalid" or any(
            isinstance(value, dict) and value.get("status") == "invalid"
            for value in packet_progress
        ):
            issues.append(
                _issue(
                    "codex_progress_invalid",
                    "warning",
                    "Informational Codex progress for step %s is invalid."
                    % _text(step.get("id") or "unknown"),
                    recommendation=(
                        "Treat progress as unavailable; execution and recovery evidence remain authoritative."
                    ),
                )
            )
        elif status in _TERMINAL_OK_STEP_STATUSES | {"failed", "blocked"} and (
            progress.get("status") == "active"
            or any(
                isinstance(value, dict) and value.get("status") == "active"
                for value in packet_progress
            )
        ):
            issues.append(
                _issue(
                    "codex_progress_stale",
                    "warning",
                    "Informational Codex progress for step %s remained active after the step ended."
                    % _text(step.get("id") or "unknown"),
                    recommendation=(
                        "Use step state, checkpoints, terminals, and packet traces as authoritative."
                    ),
                )
            )
        completion_verdict_status = _text(
            step.get("completion_verdict_status") or "none"
        )
        if completion_verdict_status == "unsatisfied":
            issues.append(
                _issue(
                    "completion_verdict_unsatisfied",
                    "action_required",
                    "Completion review rejected step %s."
                    % _text(step.get("id") or "unknown"),
                    recommendation=(
                        "Read the private review artifact, address its bounded feedback, and retry only after evidence changes."
                    ),
                )
            )
        elif completion_verdict_status == "invalid":
            issues.append(
                _issue(
                    "completion_verdict_invalid",
                    "action_required",
                    "Completion review evidence for step %s is invalid."
                    % _text(step.get("id") or "unknown"),
                    recommendation=(
                        "Treat completion as unproven; inspect the private output and rerun the review with a strict verdict."
                    ),
                )
            )
        progress_values = [progress] + [
            value for value in packet_progress if isinstance(value, dict)
        ]
        if (
            status == "failed"
            and step.get("native_agents_enabled") is True
            and step.get("runtime_budget_exhausted") is True
            and step.get("partial_output_preserved") is not True
        ):
            checkpoint_tokens = step.get(
                "native_agent_parent_completion_checkpoint_tokens"
            )
            integration_tokens = step.get(
                "native_agent_parent_integration_checkpoint_tokens"
            )
            checkpoint_text = (
                "%d weighted tokens remaining" % checkpoint_tokens
                if isinstance(checkpoint_tokens, int)
                and not isinstance(checkpoint_tokens, bool)
                and checkpoint_tokens > 0
                else "the configured parent-synthesis checkpoint"
            )
            issues.append(
                _issue(
                    "native_parent_answer_missing",
                    "action_required",
                    "Native Codex exhausted the shared threshold without a recoverable parent answer for step %s."
                    % _text(step.get("id") or "unknown"),
                    detail=(
                        "Parent integration was scheduled at %d weighted tokens remaining; synthesis was scheduled at %s."
                        % (integration_tokens, checkpoint_text)
                        if isinstance(integration_tokens, int)
                        and not isinstance(integration_tokens, bool)
                        and integration_tokens > 0
                        else "Parent synthesis was scheduled at %s." % checkpoint_text
                    ),
                    recommendation=(
                        "Prefer the serial route for this task shape; retry native only after narrowing delegated scope."
                    ),
                )
            )
        native_count = sum(
            value.get("native_agent_count", 0)
            for value in progress_values
            if value.get("status") != "invalid"
            and isinstance(value.get("native_agent_count", 0), int)
            and not isinstance(value.get("native_agent_count", 0), bool)
            and value.get("native_agent_count", 0) > 0
        )
        unattributed = any(
            value.get("status") != "invalid"
            and (
                value.get("native_agents_enabled_by_runner") is True
                or (
                    isinstance(value.get("native_agent_count", 0), int)
                    and not isinstance(value.get("native_agent_count", 0), bool)
                    and value.get("native_agent_count", 0) > 0
                )
            )
            and value.get("native_agent_usage_attributed") is not True
            for value in progress_values
        )
        if unattributed:
            issues.append(
                _issue(
                    "native_agent_usage_unattributed",
                    "info",
                    "Native Codex usage reconciliation is unavailable for step %s; %d child agent(s) were observed in the provider stream."
                    % (_text(step.get("id") or "unknown"), native_count),
                    recommendation=(
                        "Treat top-level provider tokens as incomplete; the hard shared rollout cap remains authoritative."
                    ),
                )
            )
        if any(
            value.get("status") != "invalid"
            and value.get("native_agent_observation_truncated") is True
            for value in progress_values
        ):
            issues.append(
                _issue(
                    "native_agent_observation_truncated",
                    "warning",
                    "Native-agent observation for step %s reached its bounded identity limit."
                    % _text(step.get("id") or "unknown"),
                    recommendation=(
                        "Treat native-agent counts as lower bounds; execution and recovery evidence remain authoritative."
                    ),
                )
            )
        if checkpoint.get("status") == "invalid":
            issues.append(
                _issue(
                    "codex_checkpoint_invalid",
                    "action_required",
                    "Codex continuation checkpoint for step %s is invalid." % _text(step.get("id") or "unknown"),
                    detail=_text(checkpoint.get("error")),
                    recommendation="Use fresh-step recovery only after inspecting the checkpoint path and run history.",
                )
            )
        if terminal.get("status") == "invalid":
            issues.append(
                _issue(
                    "codex_terminal_invalid",
                    "action_required",
                    "Local Codex terminal evidence for step %s is invalid."
                    % _text(step.get("id") or "unknown"),
                    detail=_text(terminal.get("error")),
                    recommendation=(
                        "Inspect the private terminal, checkpoint, output, and workspace bindings; "
                        "do not launch another provider turn while the evidence is unresolved."
                    ),
                )
            )
        elif terminal.get("status") == "provider-terminal-recovery-pending":
            step_id = _text(step.get("id") or "unknown")
            command = "python3 -m conductor_runtime retry-step RUN_DIR %s --reason %s" % (
                _shell_arg(step_id),
                _shell_arg("recover locally completed Codex turn"),
            )
            if status == "running":
                command = "python3 -m conductor_runtime recover-run RUN_DIR --retry-running --reason %s" % _shell_arg(
                    "runner is inactive; recover locally completed Codex turn"
                )
            issues.append(
                _issue(
                    "codex_terminal_recovery_pending",
                    "action_required",
                    "Step %s has a locally completed Codex turn pending recovery." % step_id,
                    recommendation=(
                        "Use ordinary retry recovery; exact local evidence will be promoted "
                        "before any provider continuation or fresh launch."
                    ),
                    command=command,
                )
            )
        if step.get("partial_output_status") == "invalid":
            issues.append(
                _issue(
                    "codex_partial_output_invalid",
                    "action_required",
                    "Failed-turn output evidence for Codex step %s is invalid."
                    % _text(step.get("id") or "unknown"),
                    recommendation=(
                        "Treat the partial artifact as untrusted and inspect artifact/state drift "
                        "before retrying or resetting the step."
                    ),
                )
            )
        elif step.get("partial_output_status") == "valid":
            size = step.get("partial_output_bytes")
            size = size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else "unknown"
            source = _text(step.get("partial_output_source") or "unknown")
            digest = _text(step.get("partial_output_sha256") or "")[:12] or "unknown"
            issues.append(
                _issue(
                    "codex_partial_output_preserved",
                    "info",
                    "Failed Codex step %s retained bounded partial output." % _text(step.get("id") or "unknown"),
                    detail="%s bytes from %s; hash %s." % (size, source, digest),
                    recommendation=(
                        "Inspect the bound artifact before deciding whether a retry is necessary; "
                        "provider and step failure remain authoritative."
                    ),
                )
            )
        if status == "running" and run_status not in {
            "planning",
            "running",
            "pause_requested",
            "stop_requested",
            "restart_requested",
        }:
            step_id = _text(step.get("id") or "unknown")
            issues.append(
                _issue(
                    "interrupted_running_step",
                    "action_required",
                    "Step %s is still marked running after its run became inactive." % step_id,
                    recommendation="Reconcile the interrupted step with recover-run before resuming.",
                    command=_interrupted_recovery_command(
                        [step],
                        "confirmed interrupted runner is inactive",
                    ),
                )
            )
            emitted += 1
            if emitted >= 10:
                break
            continue
        if status not in {"failed", "blocked"}:
            continue
        step_id = _text(step.get("id") or "unknown")
        detail = _text(step.get("detail"))
        kind = _text(step.get("kind") or "")
        if status == "blocked" and kind == "manual_gate":
            approval_id = _text(step.get("approval_id") or "<approval-id>")
            command = _text(step.get("approval_command") or "")
            if not command:
                command = "python3 -m conductor_runtime run RUN_WORKFLOW --resume RUN_DIR --approve <approval-id>"
            issues.append(
                _issue(
                    "manual_gate_approval_required",
                    "action_required",
                    "Manual gate %s requires approval." % step_id,
                    detail="Approval id: %s. %s" % (approval_id or "<approval-id>", detail),
                    recommendation=(
                        "Resume from the CLI with --approve after chat-visible/operator approval. "
                        "The live control token does not grant manual-gate approval."
                    ),
                    command=command,
                )
            )
            emitted += 1
            if emitted >= 5:
                remaining = sum(
                    1
                    for other in steps
                    if isinstance(other, dict) and _text(other.get("status") or "unknown") in {"failed", "blocked"}
                ) - emitted
                if remaining > 0:
                    issues.append(
                        _issue(
                            "additional_failed_or_blocked_steps",
                            "warning",
                            "%d additional failed or blocked step(s) are not expanded." % remaining,
                            recommendation="Open the full run detail to inspect every non-terminal step.",
                        )
                    )
                return
            continue
        code = "step_%s" % status
        command = "python3 -m conductor_runtime retry-step RUN_DIR %s --reason %s" % (
            _shell_arg(step_id),
            _shell_arg("diagnostic follow-up"),
        )
        if status == "blocked":
            command = "python3 -m conductor_runtime reset-step RUN_DIR %s --reason %s" % (
                _shell_arg(step_id),
                _shell_arg("resolved blocker"),
            )
        issues.append(
            _issue(
                code,
                "action_required",
                "Step %s is %s." % (step_id, status),
                detail=detail,
                recommendation="Review the step output, then use the suggested recovery command if appropriate.",
                command=command,
            )
        )
        emitted += 1
        if emitted >= 5:
            remaining = sum(
                1
                for other in steps
                if isinstance(other, dict) and _text(other.get("status") or "unknown") in {"failed", "blocked"}
            ) - emitted
            if remaining > 0:
                issues.append(
                    _issue(
                        "additional_failed_or_blocked_steps",
                        "warning",
                        "%d additional failed or blocked step(s) are not expanded." % remaining,
                        recommendation="Open the full run detail to inspect every non-terminal step.",
                    )
                )
                return


def _diagnose_runtime(runtime: Dict, status: str, issues: List[Dict]) -> None:
    if not isinstance(runtime, dict) or not runtime.get("available"):
        return
    counts = runtime.get("counts") if isinstance(runtime.get("counts"), dict) else {}
    ready = counts.get("ready") if isinstance(counts.get("ready"), int) and not isinstance(counts.get("ready"), bool) else 0
    waiting = counts.get("waiting") if isinstance(counts.get("waiting"), int) and not isinstance(counts.get("waiting"), bool) else 0
    active = counts.get("active") if isinstance(counts.get("active"), int) and not isinstance(counts.get("active"), bool) else 0
    operator_action = (
        counts.get("operator_action")
        if isinstance(counts.get("operator_action"), int) and not isinstance(counts.get("operator_action"), bool)
        else 0
    )
    has_manual_gate_issue = any(isinstance(issue, dict) and issue.get("code") == "manual_gate_approval_required" for issue in issues)
    if operator_action > 0 and not has_manual_gate_issue:
        command = ""
        actions = runtime.get("operator_actions") if isinstance(runtime.get("operator_actions"), list) else []
        for action in actions:
            if isinstance(action, dict) and isinstance(action.get("command"), str) and action.get("command"):
                command = action["command"]
                break
        issues.append(
            _issue(
                "runtime_operator_action_required",
                "action_required",
                "%d workflow step(s) require operator action." % operator_action,
                recommendation=(
                    "Review runtime action guidance and resume from the CLI after the required approval. "
                    "The live control token does not grant manual-gate approval."
                ),
                command=command or "python3 -m conductor_runtime run RUN_WORKFLOW --resume RUN_DIR --approve <approval-id>",
            )
        )
    if ready > 0 and status in {"needs_resume", "paused", "stopped"}:
        issues.append(
            _issue(
                "runtime_ready_steps",
                "info",
                "%d workflow step(s) are ready to run on resume." % ready,
                recommendation="Resume only after reviewing current artifacts and diagnostics.",
                command="python3 -m conductor_runtime run RUN_WORKFLOW --resume RUN_DIR",
            )
        )
    if waiting > 0 and status not in {"completed", "planned"}:
        issues.append(
            _issue(
                "runtime_waiting_on_dependencies",
                "info",
                "%d workflow step(s) are waiting on upstream dependencies." % waiting,
                recommendation="Resolve or recover upstream steps before expecting these steps to run.",
            )
        )
    if active > 0 and status in {"running", "pause_requested", "stop_requested", "restart_requested"}:
        issues.append(
            _issue(
                "runtime_active_steps",
                "info",
                "%d workflow step(s) appear active." % active,
                recommendation="Inspect runner liveness before issuing recovery controls.",
            )
        )


def _diagnose_agent_maps(agent_maps: List[Dict], issues: List[Dict]) -> None:
    emitted_packets = 0
    suppressed_packets = 0
    for group in agent_maps:
        if not isinstance(group, dict):
            continue
        step_id = _text(group.get("step_id") or "unknown")
        cache_error = _text(group.get("cache_error"))
        if cache_error:
            issues.append(
                _issue(
                    "agent_map_cache_error",
                    "warning",
                    "Agent packet cache for step %s could not be read safely." % step_id,
                    detail=cache_error,
                    recommendation=(
                        "Inspect packet artifacts, then reset this step if cache reuse should be bypassed."
                    ),
                    command="python3 -m conductor_runtime reset-step RUN_DIR %s --reason %s"
                    % (_shell_arg(step_id), _shell_arg("discard invalid packet cache")),
                )
            )
        trace_error = _text(group.get("trace_error"))
        if trace_error:
            issues.append(
                _issue(
                    "agent_map_trace_error",
                    "warning",
                    "Agent packet trace for step %s could not be read safely." % step_id,
                    detail=trace_error,
                    recommendation="Inspect packet logs before relying on packet-level recovery evidence.",
                )
            )
        if group.get("trace_truncated") is True:
            issues.append(
                _issue(
                    "agent_map_trace_truncated",
                    "warning",
                    "Agent packet trace for step %s is truncated." % step_id,
                    recommendation="Open the run detail or packet logs before relying on packet-level recovery evidence.",
                )
            )
        group_error = _text(group.get("error"))
        if group_error:
            issues.append(
                _issue(
                    "agent_map_detail_error",
                    "warning",
                    "Agent packet detail for step %s is incomplete." % step_id,
                    detail=group_error,
                    recommendation="Inspect the workflow packet source and run artifacts before resuming.",
                )
            )
        packets = group.get("packets") if isinstance(group.get("packets"), list) else []
        for packet in packets:
            if not isinstance(packet, dict):
                continue
            issue = _agent_packet_issue(group, packet)
            if issue is None:
                continue
            if emitted_packets < _MAX_AGENT_MAP_DIAGNOSTIC_PACKETS:
                issues.append(issue)
                emitted_packets += 1
            else:
                suppressed_packets += 1
    if suppressed_packets:
        issues.append(
            _issue(
                "additional_agent_map_packet_issues",
                "warning",
                "%d additional agent packet issue(s) are not expanded." % suppressed_packets,
                recommendation="Open the run detail to inspect every packet before resuming.",
            )
        )


def _agent_packet_issue(group: Dict, packet: Dict) -> Optional[Dict]:
    step_id = _text(group.get("step_id") or "unknown")
    packet_index = _packet_index(packet.get("index"))
    item = _text(packet.get("item") or "unknown")
    status = _text(packet.get("status") or "unknown")
    trace = packet.get("trace") if isinstance(packet.get("trace"), dict) else {}
    trace_status = _text(trace.get("status") or "")
    if status == "cached-output-missing":
        return _issue(
            "agent_map_cached_output_missing",
            "action_required",
            "Agent packet %s for step %s has a cache entry but no output artifact." % (packet_index, step_id),
            detail="Item: %s." % item,
            recommendation="Retry only this packet while retaining compatible sibling results.",
            command="python3 -m conductor_runtime retry-packet RUN_DIR %s %s --reason %s"
            % (_shell_arg(step_id), _shell_arg(packet_index), _shell_arg("cached packet output missing")),
        )
    if trace_status in _AGENT_TRACE_ACTION_STATUSES:
        return _issue(
            "agent_map_packet_%s" % trace_status,
            "action_required",
            "Agent packet %s for step %s recorded trace status %s." % (packet_index, step_id, trace_status),
            detail=_agent_packet_trace_detail(item, trace),
            recommendation="Review packet stdout/stderr, then retry only this packet.",
            command="python3 -m conductor_runtime retry-packet RUN_DIR %s %s --reason %s"
            % (_shell_arg(step_id), _shell_arg(packet_index), _shell_arg("retry failed packet")),
        )
    if status in _AGENT_PACKET_ACTION_STATUSES:
        return _issue(
            "agent_map_packet_%s" % status.replace("-", "_"),
            "warning",
            "Agent packet %s for step %s has status %s." % (packet_index, step_id, status),
            detail="Item: %s." % item,
            recommendation="Review packet logs before relying on this packet result.",
        )
    return None


def _agent_packet_trace_detail(item: str, trace: Dict) -> str:
    parts = ["Item: %s." % item]
    detail = _text(trace.get("detail"))
    if detail:
        parts.append("Detail: %s." % detail)
    returncode = trace.get("returncode")
    if isinstance(returncode, int) and not isinstance(returncode, bool):
        parts.append("Return code: %d." % returncode)
    if trace.get("timed_out") is True:
        parts.append("Timed out.")
    return " ".join(parts)


def _packet_index(value) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    return "unknown"


def _diagnose_completed_evidence(status: str, steps: List[Dict], standard_files: List[Dict], issues: List[Dict]) -> None:
    if status != "completed":
        return
    missing = [str(item.get("name") or "unknown") for item in standard_files if isinstance(item, dict) and not item.get("present")]
    if missing:
        issues.append(
            _issue(
                "missing_standard_artifacts",
                "warning",
                "Completed run is missing standard conductor artifacts.",
                detail=", ".join(redact_text(name) for name in missing[:8]),
                recommendation="Create or restore the missing evidence files before using this run as proof of completion.",
            )
        )
    if not _has_completion_evidence(steps, standard_files):
        issues.append(
            _issue(
                "completion_evidence_incomplete",
                "warning",
                "Run is marked completed but diagnostic evidence is incomplete.",
                recommendation="Review step statuses and final-report substance before relying on this run.",
            )
        )


def _launch_resume_command(launch: Dict) -> str:
    if not isinstance(launch, dict):
        return ""
    if _text(launch.get("status") or "") != "prepared":
        return ""
    command = _text(launch.get("resume_command") or "")
    if "--resume" not in command:
        return ""
    return command


def _interrupted_recovery_command(steps: List[Dict], reason: str) -> str:
    running = [
        step
        for step in steps
        if isinstance(step, dict) and _text(step.get("status") or "unknown") == "running"
    ]
    resume_codex = False
    if len(running) == 1 and _text(running[0].get("kind") or "") == "codex_exec":
        checkpoint = running[0].get("codex_checkpoint")
        terminal = running[0].get("codex_terminal")
        terminal_pending = (
            isinstance(terminal, dict)
            and terminal.get("status") == "provider-terminal-recovery-pending"
        )
        resume_codex = (
            not terminal_pending
            and isinstance(checkpoint, dict)
            and checkpoint.get("status") == "active"
        )
    resolution = "--resume-codex" if resume_codex else "--retry-running"
    return "python3 -m conductor_runtime recover-run RUN_DIR %s --reason %s" % (
        resolution,
        _shell_arg(reason),
    )


def _has_completion_evidence(steps: List[Dict], standard_files: List[Dict]) -> bool:
    if not steps:
        return False
    for step in steps:
        if not isinstance(step, dict) or _text(step.get("status") or "unknown") not in _TERMINAL_OK_STEP_STATUSES:
            return False
    if not standard_files or any(isinstance(item, dict) and not item.get("present") for item in standard_files):
        return False
    final_report = _standard_file(standard_files, "09-final-report.md")
    return _final_report_has_substance(final_report)


def _standard_file(standard_files: List[Dict], name: str) -> Dict:
    for item in standard_files:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return {}


def _final_report_has_substance(final_report: Dict) -> bool:
    if not isinstance(final_report, dict) or not final_report.get("present"):
        return False
    excerpt = final_report.get("excerpt") if isinstance(final_report.get("excerpt"), dict) else {}
    text = str(excerpt.get("text") or "").strip()
    if not text:
        return False
    lower = text.lower()
    pending_markers = [
        "summary:\n\npending",
        "changes:\n\npending",
        "verification performed:\n\npending",
        "risks and caveats:\n\npending",
    ]
    placeholder_lines = {line.strip().lower() for line in text.splitlines()}
    if any(marker in lower for marker in pending_markers):
        return False
    return not {"none yet", "open"}.intersection(placeholder_lines)


def _issue(
    code: str,
    severity: str,
    message: str,
    detail: str = "",
    recommendation: str = "",
    command: Optional[str] = None,
) -> Dict:
    issue = {
        "code": redact_text(code)[:120],
        "severity": severity if severity in _SEVERITY_ORDER else "warning",
        "message": _text(message),
        "detail": _text(detail),
        "recommendation": _text(recommendation),
    }
    if command:
        issue["command"] = _text(command)
    return issue


def _top_severity(issues: List[Dict]) -> str:
    if not issues:
        return "ok"
    return max((_text(issue.get("severity")) for issue in issues), key=lambda item: _SEVERITY_ORDER.get(item, 1))


def _summary(severity: str, issues: List[Dict]) -> str:
    if not issues:
        return "No current diagnostic issues."
    counts = {}
    for issue in issues:
        issue_severity = _text(issue.get("severity") or "warning")
        counts[issue_severity] = counts.get(issue_severity, 0) + 1
    rendered = ", ".join("%s %s" % (count, label) for label, count in sorted(counts.items()))
    return "%d diagnostic issue(s): %s. Highest severity: %s." % (len(issues), rendered, severity)


def _text(value) -> str:
    if value is None:
        return ""
    return redact_text(str(value))[:500]


def _shell_arg(value) -> str:
    return shlex.quote(_text(value) or "unknown")
