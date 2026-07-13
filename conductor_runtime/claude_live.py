import json
import math
import platform
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from .benchmark import (
    BENCHMARK_REPORT_SCHEMA,
    parity_prompt_environment,
    render_parity_task_contract,
    validate_benchmark_report,
    validate_parity_tasks,
)
from .clock import utc_from_timestamp
from .errors import PolicyError, StepExecutionError, ValidationError
from .redaction import redact_text
from .runner import DEFAULT_OUTPUT_LIMIT_BYTES, ProcessResult, run_process
from .security import RuntimePolicy, read_regular_text_file_no_follow
from .workflow import MAX_TIMEOUT_SECONDS, SAFE_ID


CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V1 = "conductor.claude_provider_evidence.v1"
CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V2 = "conductor.claude_provider_evidence.v2"
CLAUDE_PROVIDER_EVIDENCE_SCHEMA = "conductor.claude_provider_evidence.v3"
CLAUDE_PAID_RUN_APPROVAL = "claude-paid-run"
CLAUDE_MODEL = "sonnet"
CLAUDE_EFFORT = "ultracode"
CLAUDE_LEGACY_PERMISSION_MODE = "plan"
CLAUDE_PERMISSION_MODE = "dontAsk"
CLAUDE_READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]
CLAUDE_STAGED_PERMISSION_MODE = "acceptEdits"
CLAUDE_STAGED_WRITE_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write"]
CLAUDE_MINIMUM_VERSION = (2, 1, 203)
CLAUDE_PROVIDER_STATUSES = {
    "success",
    "provider-error",
    "budget-exceeded",
    "model-rejected",
    "timed-out",
    "malformed-output",
    "output-truncated",
}
CLAUDE_PROVIDER_SEVERITIES = {"info", "low", "medium", "high", "critical"}
CLAUDE_PROVIDER_EVIDENCE_FIELDS = {"schema", "status", "requested", "observed", "policy", "incidents"}
CLAUDE_REQUESTED_FIELDS = {
    "model",
    "effort",
    "permission_mode",
    "tools",
    "max_budget_usd",
    "max_turns",
    "timeout_seconds",
    "output_limit_bytes",
}
CLAUDE_OBSERVED_FIELDS_V1 = {
    "cli_version",
    "main_models",
    "helper_models",
    "sonnet_main_only",
    "opus_observed",
    "terminal_event_present",
    "terminal_subtype",
    "is_error",
    "cost_usd",
    "budget_overshoot_usd",
    "turns",
    "assistant_messages",
    "stream_events",
    "output_source",
    "partial_output_preserved",
    "returncode",
    "timed_out",
    "stdout_truncated",
    "stderr_truncated",
    "parse_error",
}
CLAUDE_OBSERVED_FIELDS = CLAUDE_OBSERVED_FIELDS_V1 | {
    "token_usage_source",
    "token_accounting",
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "total_tokens",
}
CLAUDE_POLICY_FIELDS = {
    "paid_approval_present",
    "approval_values_persisted",
    "no_fallback_model",
    "no_session_persistence",
    "prompt_in_argv",
    "read_only_tools",
}
CLAUDE_INCIDENT_FIELDS = {"id", "severity", "description"}
CLAUDE_OUTPUT_SOURCES = {"final", "assistant-messages", "none"}
MAX_CLAUDE_STREAM_EVENTS = 20000
MAX_CLAUDE_STREAM_LINE_BYTES = 1024 * 1024
MAX_CLAUDE_EVIDENCE_BYTES = 512 * 1024
MAX_CLAUDE_TURNS = 100
MAX_CLAUDE_INCIDENTS = 20
MAX_CLAUDE_TOKENS = 10**12
CLAUDE_TOKEN_USAGE_SOURCES = {"modelUsage", "unavailable"}
CLAUDE_TOKEN_ACCOUNTING = "provider-native-gross-v1"
_VERSION_PATTERN = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")


def run_claude_readonly_task(
    *,
    parity_tasks: Dict,
    task_id: str,
    workspace: Path,
    policy: RuntimePolicy,
    max_budget_usd: float,
    max_turns: int,
    timeout_seconds: int,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
) -> Dict:
    public_parity_tasks = {
        key: value for key, value in parity_tasks.items() if not str(key).startswith("_")
    } if isinstance(parity_tasks, dict) else parity_tasks
    validate_parity_tasks(public_parity_tasks)
    task = _parity_task(public_parity_tasks, task_id)
    task_contract = render_parity_task_contract(task)
    prompt_environment = parity_prompt_environment(task_contract, task_contract)
    workspace_path = Path(workspace).resolve()
    if not workspace_path.is_dir():
        raise ValidationError("Claude parity workspace must be a directory: %s" % workspace)
    _require_paid_readonly_policy(policy)
    budget = _positive_number(max_budget_usd, "max_budget_usd")
    fixture_budget = task["budget"].get("max_cost_usd")
    if not isinstance(fixture_budget, (int, float)) or isinstance(fixture_budget, bool) or fixture_budget <= 0:
        raise ValidationError("parity task %s does not authorize paid live-tool cost" % task_id)
    if budget > float(fixture_budget):
        raise ValidationError(
            "max_budget_usd %.6f exceeds parity task %s cap %.6f"
            % (budget, task_id, float(fixture_budget))
        )
    _bounded_int(max_turns, "max_turns", 1, MAX_CLAUDE_TURNS)
    fixture_timeout = int(task["budget"]["max_minutes"]) * 60
    _bounded_int(timeout_seconds, "timeout_seconds", 1, min(MAX_TIMEOUT_SECONDS, fixture_timeout))
    _bounded_int(output_limit_bytes, "output_limit_bytes", 1, 16 * DEFAULT_OUTPUT_LIMIT_BYTES)

    claude_path = shutil.which("claude")
    if not claude_path:
        raise ValidationError("Claude Code CLI is not available on PATH")
    cli_version = _claude_cli_version(claude_path, workspace_path)
    command = build_claude_readonly_command(
        claude_path=claude_path,
        max_budget_usd=budget,
        max_turns=max_turns,
    )
    started = time.time()
    started_at_utc = utc_from_timestamp(started).isoformat(timespec="seconds") + "Z"
    try:
        process = run_process(
            command,
            cwd=workspace_path,
            timeout=timeout_seconds,
            input_text=task_contract,
            output_limit_bytes=output_limit_bytes,
        )
    except OSError as exc:
        raise StepExecutionError("Claude Code launch failed: %s" % exc.__class__.__name__)
    duration_ms = max(0, int((time.time() - started) * 1000))
    analysis = analyze_claude_output(
        process,
        cli_version=cli_version,
        max_budget_usd=budget,
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
        permission_mode=CLAUDE_PERMISSION_MODE,
        tools=CLAUDE_READ_ONLY_TOOLS,
        read_only_tools=True,
    )
    evidence = analysis["provider_evidence"]
    result = {
        "id": task["id"],
        "description": "Pinned Claude Sonnet Ultracode read-only parity run.",
        "passed": evidence["status"] == "success",
        "returncode": process.returncode,
        "timed_out": process.timed_out,
        "duration_ms": duration_ms,
        "stdout_truncated": process.stdout_truncated,
        "stderr_truncated": process.stderr_truncated,
        "stdout": analysis["output_text"],
        "stderr": redact_text(process.stderr),
        "provider_evidence": evidence,
    }
    report = {
        "schema": BENCHMARK_REPORT_SCHEMA,
        "suite": public_parity_tasks["name"],
        "suite_source": "configured-parity-tasks",
        "system": "claude-sonnet-ultracode",
        "started_at_utc": started_at_utc,
        "environment": {
            "python": "%s.%s.%s" % sys.version_info[:3],
            "platform": platform.platform(),
            "claude": "available",
            "claude_version": cli_version,
            "external_tool_version_probes": "required",
            **prompt_environment,
        },
        "total_tasks": 1,
        "passed_tasks": 1 if result["passed"] else 0,
        "failed_tasks": 0 if result["passed"] else 1,
        "duration_ms": duration_ms,
        "results": [result],
    }
    validate_benchmark_report(report)
    return report


def build_claude_readonly_command(*, claude_path: str, max_budget_usd: float, max_turns: int) -> List[str]:
    return build_claude_command(
        claude_path=claude_path,
        max_budget_usd=max_budget_usd,
        max_turns=max_turns,
        permission_mode=CLAUDE_PERMISSION_MODE,
        tools=CLAUDE_READ_ONLY_TOOLS,
    )


def build_claude_command(
    *,
    claude_path: str,
    max_budget_usd: float,
    max_turns: int,
    permission_mode: str,
    tools: List[str],
) -> List[str]:
    _validate_provider_tool_contract(permission_mode, tools)
    command = [
        claude_path,
        "--print",
        "--safe-mode",
        "--no-session-persistence",
        "--prompt-suggestions",
        "false",
        "--permission-mode",
        permission_mode,
        "--tools",
        ",".join(tools),
    ]
    if permission_mode == CLAUDE_PERMISSION_MODE:
        command.extend(["--allowedTools", ",".join(tools)])
    command.extend([
        "--model",
        CLAUDE_MODEL,
        "--effort",
        CLAUDE_EFFORT,
        "--max-budget-usd",
        _format_budget(max_budget_usd),
        "--max-turns",
        str(max_turns),
        "--output-format",
        "stream-json",
        "--verbose",
    ])
    return command


def analyze_claude_output(
    process: ProcessResult,
    *,
    cli_version: str,
    max_budget_usd: float,
    max_turns: int,
    timeout_seconds: int,
    output_limit_bytes: int,
    permission_mode: str = CLAUDE_PERMISSION_MODE,
    tools: Optional[List[str]] = None,
    read_only_tools: bool = True,
) -> Dict:
    requested_tools = list(CLAUDE_READ_ONLY_TOOLS if tools is None else tools)
    _validate_provider_tool_contract(permission_mode, requested_tools)
    if read_only_tools != (requested_tools == CLAUDE_READ_ONLY_TOOLS):
        raise ValidationError("Claude provider read_only_tools flag does not match the requested tool contract")
    parse_error = None
    try:
        parsed = parse_claude_stream(process.stdout)
    except ValidationError as exc:
        parse_error = redact_text(str(exc))
        parsed = _empty_parsed_stream()
    evidence = build_claude_provider_evidence(
        parsed,
        process=process,
        cli_version=cli_version,
        max_budget_usd=max_budget_usd,
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
        parse_error=parse_error,
        permission_mode=permission_mode,
        tools=requested_tools,
        read_only_tools=read_only_tools,
    )
    output_text = redact_text(parsed["output_text"])
    return {"provider_evidence": evidence, "output_text": output_text}


def parse_claude_stream(text: str) -> Dict:
    events = 0
    stream_events = 0
    assistant_messages = 0
    main_models = []
    usage_models = []
    assistant_message_texts = []
    terminal = None
    token_usage = _empty_claude_token_usage()
    for line_number, raw_line in enumerate(str(text or "").splitlines(), start=1):
        if not raw_line.strip():
            continue
        if len(raw_line.encode("utf-8")) > MAX_CLAUDE_STREAM_LINE_BYTES:
            raise ValidationError("Claude stream line %d exceeds the supported size" % line_number)
        events += 1
        if events > MAX_CLAUDE_STREAM_EVENTS:
            raise ValidationError("Claude stream exceeds the supported event count")
        try:
            event = json.loads(
                raw_line,
                object_pairs_hook=_reject_claude_stream_pairs,
                parse_constant=_reject_claude_stream_constant,
            )
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValidationError("Claude stream line %d is not valid JSON: %s" % (line_number, exc.__class__.__name__))
        if not isinstance(event, dict):
            raise ValidationError("Claude stream line %d must contain an object" % line_number)
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise ValidationError("Claude stream line %d is missing an event type" % line_number)
        if event_type == "stream_event":
            stream_events += 1
        if event_type == "system" and event.get("subtype") == "init":
            _append_model(main_models, event.get("model"))
        if event_type == "assistant":
            assistant_messages += 1
            message = event.get("message")
            if not isinstance(message, dict):
                raise ValidationError("Claude assistant event must contain a message object")
            _append_model(main_models, message.get("model"))
            message_text = "\n\n".join(
                text for text in _assistant_text_blocks(message.get("content")) if text
            ).strip()
            if message_text:
                assistant_message_texts.append(message_text)
        if event_type == "result":
            if terminal is not None:
                raise ValidationError("Claude stream contains multiple terminal result events")
            if _optional_short_text(event.get("subtype")) is None:
                raise ValidationError("Claude result event must contain a terminal subtype")
            if not isinstance(event.get("is_error"), bool):
                raise ValidationError("Claude result is_error must be boolean")
            if _optional_non_negative_number(event.get("total_cost_usd"), "Claude result total_cost_usd") is None:
                raise ValidationError("Claude result total_cost_usd must be present")
            if _optional_non_negative_int(event.get("num_turns"), "Claude result num_turns") is None:
                raise ValidationError("Claude result num_turns must be present")
            terminal = event
            model_usage = event.get("modelUsage")
            if model_usage is not None:
                if not isinstance(model_usage, dict):
                    raise ValidationError("Claude result modelUsage must be an object")
                for model_name, model_record in model_usage.items():
                    if not isinstance(model_record, dict):
                        raise ValidationError("Claude result modelUsage records must be objects")
                    _append_model(usage_models, model_name)
            token_usage = _parse_claude_model_usage(model_usage)

    if events == 0:
        raise ValidationError("Claude stream contains no JSON events")
    final_text = ""
    if terminal is not None:
        raw_result = terminal.get("result")
        if raw_result is not None and not isinstance(raw_result, str):
            raise ValidationError("Claude result text must be a string when present")
        final_text = raw_result or ""
    assistant_text = assistant_message_texts[-1] if assistant_message_texts else ""
    output_text = final_text.strip() or assistant_text
    output_source = "final" if final_text.strip() else "assistant-messages" if assistant_text else "none"
    main_models = sorted(set(main_models))
    usage_models = sorted(set(usage_models))
    helper_models = sorted(model for model in usage_models if model not in main_models)
    sonnet_main_only = bool(main_models) and all(_model_family(model) == "sonnet" for model in main_models)
    opus_observed = any(_model_family(model) == "opus" for model in main_models + usage_models)
    return {
        "terminal": terminal,
        "main_models": main_models,
        "helper_models": helper_models,
        "sonnet_main_only": sonnet_main_only,
        "opus_observed": opus_observed,
        "assistant_messages": assistant_messages,
        "stream_events": stream_events,
        "output_text": output_text,
        "output_source": output_source,
        **token_usage,
    }


def build_claude_provider_evidence(
    parsed: Dict,
    *,
    process: ProcessResult,
    cli_version: str,
    max_budget_usd: float,
    max_turns: int,
    timeout_seconds: int,
    output_limit_bytes: int,
    parse_error: Optional[str] = None,
    permission_mode: str = CLAUDE_PERMISSION_MODE,
    tools: Optional[List[str]] = None,
    read_only_tools: bool = True,
) -> Dict:
    requested_tools = list(CLAUDE_READ_ONLY_TOOLS if tools is None else tools)
    _validate_provider_tool_contract(permission_mode, requested_tools)
    if read_only_tools != (requested_tools == CLAUDE_READ_ONLY_TOOLS):
        raise ValidationError("Claude provider read_only_tools flag does not match the requested tool contract")
    terminal = parsed.get("terminal") if isinstance(parsed.get("terminal"), dict) else None
    terminal_subtype = _optional_short_text(terminal.get("subtype")) if terminal else None
    is_error = terminal.get("is_error") if terminal and isinstance(terminal.get("is_error"), bool) else None
    cost = _optional_non_negative_number(terminal.get("total_cost_usd"), "Claude result total_cost_usd") if terminal else None
    turns = _optional_non_negative_int(terminal.get("num_turns"), "Claude result num_turns") if terminal else None
    overshoot = max(0.0, (cost or 0.0) - max_budget_usd)
    overshoot = round(overshoot, 12)
    status = _claude_status(
        process,
        parsed,
        terminal_subtype=terminal_subtype,
        is_error=is_error,
        cost=cost,
        max_budget_usd=max_budget_usd,
        parse_error=parse_error,
    )
    incidents = _claude_incidents(
        status,
        parsed,
        terminal_subtype=terminal_subtype,
        cost=cost,
        max_budget_usd=max_budget_usd,
        parse_error=parse_error,
    )
    evidence = {
        "schema": CLAUDE_PROVIDER_EVIDENCE_SCHEMA,
        "status": status,
        "requested": {
            "model": CLAUDE_MODEL,
            "effort": CLAUDE_EFFORT,
            "permission_mode": permission_mode,
            "tools": requested_tools,
            "max_budget_usd": float(max_budget_usd),
            "max_turns": max_turns,
            "timeout_seconds": timeout_seconds,
            "output_limit_bytes": output_limit_bytes,
        },
        "observed": {
            "cli_version": cli_version,
            "main_models": list(parsed["main_models"]),
            "helper_models": list(parsed["helper_models"]),
            "sonnet_main_only": bool(parsed["sonnet_main_only"]),
            "opus_observed": bool(parsed["opus_observed"]),
            "terminal_event_present": terminal is not None,
            "terminal_subtype": terminal_subtype,
            "is_error": is_error,
            "cost_usd": cost,
            "budget_overshoot_usd": overshoot,
            "turns": turns,
            "assistant_messages": int(parsed["assistant_messages"]),
            "stream_events": int(parsed["stream_events"]),
            "output_source": parsed["output_source"],
            "partial_output_preserved": parsed["output_source"] == "assistant-messages",
            "returncode": process.returncode,
            "timed_out": process.timed_out,
            "stdout_truncated": process.stdout_truncated,
            "stderr_truncated": process.stderr_truncated,
            "parse_error": parse_error,
            "token_usage_source": parsed["token_usage_source"],
            "token_accounting": CLAUDE_TOKEN_ACCOUNTING,
            "input_tokens": parsed["input_tokens"],
            "cache_creation_input_tokens": parsed["cache_creation_input_tokens"],
            "cache_read_input_tokens": parsed["cache_read_input_tokens"],
            "output_tokens": parsed["output_tokens"],
            "total_tokens": parsed["total_tokens"],
        },
        "policy": {
            "paid_approval_present": True,
            "approval_values_persisted": False,
            "no_fallback_model": True,
            "no_session_persistence": True,
            "prompt_in_argv": False,
            "read_only_tools": read_only_tools,
        },
        "incidents": incidents,
    }
    validate_claude_provider_evidence(evidence)
    return evidence


def validate_claude_provider_evidence(evidence: Dict, source: str = "<memory>") -> None:
    if not isinstance(evidence, dict):
        raise ValidationError("%s must contain an object" % source)
    _exact_fields(evidence, CLAUDE_PROVIDER_EVIDENCE_FIELDS, "%s provider evidence" % source)
    schema = evidence.get("schema")
    if schema == CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V1:
        observed_fields = CLAUDE_OBSERVED_FIELDS_V1
    elif schema in {CLAUDE_PROVIDER_EVIDENCE_SCHEMA_V2, CLAUDE_PROVIDER_EVIDENCE_SCHEMA}:
        observed_fields = CLAUDE_OBSERVED_FIELDS
    else:
        raise ValidationError("%s uses an unsupported Claude provider evidence schema" % source)
    status = evidence.get("status")
    if status not in CLAUDE_PROVIDER_STATUSES:
        raise ValidationError("%s has unsupported Claude provider status" % source)
    requested = evidence.get("requested")
    observed = evidence.get("observed")
    policy = evidence.get("policy")
    if not isinstance(requested, dict) or not isinstance(observed, dict) or not isinstance(policy, dict):
        raise ValidationError("%s requested, observed, and policy fields must be objects" % source)
    _exact_fields(requested, CLAUDE_REQUESTED_FIELDS, "%s requested" % source)
    _exact_fields(observed, observed_fields, "%s observed" % source)
    _exact_fields(policy, CLAUDE_POLICY_FIELDS, "%s policy" % source)
    if requested.get("model") != CLAUDE_MODEL or requested.get("effort") != CLAUDE_EFFORT:
        raise ValidationError("%s must request pinned Claude Sonnet Ultracode" % source)
    readonly_permission_mode = (
        CLAUDE_PERMISSION_MODE
        if schema == CLAUDE_PROVIDER_EVIDENCE_SCHEMA
        else CLAUDE_LEGACY_PERMISSION_MODE
    )
    _validate_provider_tool_contract(
        requested.get("permission_mode"),
        requested.get("tools"),
        source=source,
        readonly_permission_mode=readonly_permission_mode,
    )
    expected_read_only = requested.get("tools") == CLAUDE_READ_ONLY_TOOLS
    budget = _positive_number(requested.get("max_budget_usd"), "%s max_budget_usd" % source)
    _bounded_int(requested.get("max_turns"), "%s max_turns" % source, 1, MAX_CLAUDE_TURNS)
    _bounded_int(requested.get("timeout_seconds"), "%s timeout_seconds" % source, 1, MAX_TIMEOUT_SECONDS)
    _bounded_int(
        requested.get("output_limit_bytes"),
        "%s output_limit_bytes" % source,
        1,
        16 * DEFAULT_OUTPUT_LIMIT_BYTES,
    )
    if not isinstance(observed.get("cli_version"), str) or not observed["cli_version"]:
        raise ValidationError("%s observed cli_version must be non-empty" % source)
    main_models = _model_list(observed.get("main_models"), "%s main_models" % source)
    helper_models = _model_list(observed.get("helper_models"), "%s helper_models" % source)
    if set(main_models) & set(helper_models):
        raise ValidationError("%s main_models and helper_models must be disjoint" % source)
    for field in [
        "sonnet_main_only",
        "opus_observed",
        "terminal_event_present",
        "partial_output_preserved",
        "timed_out",
        "stdout_truncated",
        "stderr_truncated",
    ]:
        if not isinstance(observed.get(field), bool):
            raise ValidationError("%s observed %s must be boolean" % (source, field))
    expected_sonnet = bool(main_models) and all(_model_family(model) == "sonnet" for model in main_models)
    expected_opus = any(_model_family(model) == "opus" for model in main_models + helper_models)
    if observed["sonnet_main_only"] != expected_sonnet or observed["opus_observed"] != expected_opus:
        raise ValidationError("%s observed model summary is inconsistent" % source)
    if observed.get("terminal_subtype") is not None and not isinstance(observed["terminal_subtype"], str):
        raise ValidationError("%s terminal_subtype must be a string or null" % source)
    if observed.get("is_error") is not None and not isinstance(observed["is_error"], bool):
        raise ValidationError("%s is_error must be boolean or null" % source)
    cost = _optional_non_negative_number(observed.get("cost_usd"), "%s cost_usd" % source)
    overshoot = _optional_non_negative_number(observed.get("budget_overshoot_usd"), "%s budget_overshoot_usd" % source)
    expected_overshoot = round(max(0.0, (cost or 0.0) - budget), 12)
    if overshoot is None or abs(overshoot - expected_overshoot) > 1e-9:
        raise ValidationError("%s budget overshoot summary is inconsistent" % source)
    if observed.get("turns") is not None:
        _bounded_int(observed["turns"], "%s turns" % source, 0, 10**9)
    if schema == CLAUDE_PROVIDER_EVIDENCE_SCHEMA:
        _validate_claude_token_usage(observed, source)
    _bounded_int(observed.get("assistant_messages"), "%s assistant_messages" % source, 0, MAX_CLAUDE_STREAM_EVENTS)
    _bounded_int(observed.get("stream_events"), "%s stream_events" % source, 0, MAX_CLAUDE_STREAM_EVENTS)
    if observed.get("output_source") not in CLAUDE_OUTPUT_SOURCES:
        raise ValidationError("%s output_source is invalid" % source)
    if observed["partial_output_preserved"] != (observed["output_source"] == "assistant-messages"):
        raise ValidationError("%s partial output summary is inconsistent" % source)
    if isinstance(observed.get("returncode"), bool) or not isinstance(observed.get("returncode"), int):
        raise ValidationError("%s returncode must be an integer" % source)
    parse_error = observed.get("parse_error")
    if parse_error is not None and (not isinstance(parse_error, str) or not parse_error):
        raise ValidationError("%s parse_error must be a non-empty string or null" % source)
    expected_policy = {
        "paid_approval_present": True,
        "approval_values_persisted": False,
        "no_fallback_model": True,
        "no_session_persistence": True,
        "prompt_in_argv": False,
        "read_only_tools": expected_read_only,
    }
    if policy != expected_policy:
        raise ValidationError("%s policy metadata violates the Claude live-run contract" % source)
    incidents = evidence.get("incidents")
    if not isinstance(incidents, list) or len(incidents) > MAX_CLAUDE_INCIDENTS:
        raise ValidationError("%s incidents must be a bounded list" % source)
    incident_ids = set()
    for incident in incidents:
        if not isinstance(incident, dict):
            raise ValidationError("%s incidents must contain objects" % source)
        _exact_fields(incident, CLAUDE_INCIDENT_FIELDS, "%s incident" % source)
        incident_id = incident.get("id")
        if not isinstance(incident_id, str) or not SAFE_ID.match(incident_id) or incident_id in incident_ids:
            raise ValidationError("%s incident ids must be unique safe identifiers" % source)
        incident_ids.add(incident_id)
        if incident.get("severity") not in CLAUDE_PROVIDER_SEVERITIES:
            raise ValidationError("%s incident severity is invalid" % source)
        if not isinstance(incident.get("description"), str) or not incident["description"]:
            raise ValidationError("%s incident description must be non-empty" % source)
    expected_status = _status_from_observed(observed, budget)
    if status != expected_status:
        raise ValidationError("%s provider status is inconsistent with observed evidence" % source)
    expected_incidents = _claude_incidents(
        status,
        observed,
        terminal_subtype=observed.get("terminal_subtype"),
        cost=cost,
        max_budget_usd=budget,
        parse_error=parse_error,
    )
    if incidents != expected_incidents:
        raise ValidationError("%s provider incidents are inconsistent with observed evidence" % source)
    if status == "success":
        non_informational = [
            incident for incident in incidents if incident.get("id") != "non-sonnet-helper-observed"
        ]
        if non_informational or not observed["terminal_event_present"] or observed["terminal_subtype"] != "success":
            raise ValidationError("%s successful provider evidence is inconsistent" % source)
        if observed["is_error"] is not False or not observed["sonnet_main_only"] or observed["opus_observed"]:
            raise ValidationError("%s successful provider model evidence is inconsistent" % source)
        if observed["output_source"] == "none" or expected_overshoot > 0:
            raise ValidationError("%s successful provider output or budget evidence is inconsistent" % source)


def load_claude_provider_evidence(path: Path) -> Dict:
    text = read_regular_text_file_no_follow(
        Path(path),
        "Claude provider evidence",
        max_bytes=MAX_CLAUDE_EVIDENCE_BYTES,
    )
    try:
        evidence = json.loads(text, object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError("%s is not valid Claude provider evidence JSON: %s" % (path, exc))
    validate_claude_provider_evidence(evidence, source=str(path))
    return evidence


def _claude_status(
    process: ProcessResult,
    parsed: Dict,
    *,
    terminal_subtype: Optional[str],
    is_error,
    cost: Optional[float],
    max_budget_usd: float,
    parse_error: Optional[str],
) -> str:
    if process.timed_out:
        return "timed-out"
    if process.stdout_truncated or process.stderr_truncated:
        return "output-truncated"
    if parse_error:
        return "malformed-output"
    if parsed["opus_observed"] or not parsed["sonnet_main_only"]:
        return "model-rejected"
    if terminal_subtype == "error_max_budget_usd" or (cost is not None and cost > max_budget_usd + 1e-9):
        return "budget-exceeded"
    if parsed.get("terminal") is None:
        return "provider-error"
    if process.returncode != 0 or is_error is not False or terminal_subtype != "success":
        return "provider-error"
    if parsed["output_source"] == "none":
        return "provider-error"
    return "success"


def _status_from_observed(observed: Dict, max_budget_usd: float) -> str:
    if observed["timed_out"]:
        return "timed-out"
    if observed["stdout_truncated"] or observed["stderr_truncated"]:
        return "output-truncated"
    if observed.get("parse_error"):
        return "malformed-output"
    if observed["opus_observed"] or not observed["sonnet_main_only"]:
        return "model-rejected"
    cost = observed.get("cost_usd")
    if observed.get("terminal_subtype") == "error_max_budget_usd" or (
        cost is not None and cost > max_budget_usd + 1e-9
    ):
        return "budget-exceeded"
    if not observed["terminal_event_present"]:
        return "provider-error"
    if observed["returncode"] != 0 or observed.get("is_error") is not False:
        return "provider-error"
    if observed.get("terminal_subtype") != "success" or observed.get("output_source") == "none":
        return "provider-error"
    return "success"


def _claude_incidents(
    status: str,
    parsed: Dict,
    *,
    terminal_subtype: Optional[str],
    cost: Optional[float],
    max_budget_usd: float,
    parse_error: Optional[str],
) -> List[Dict]:
    incidents = []
    if parsed["opus_observed"]:
        incidents.append(_incident("opus-observed", "high", "Claude output reported Opus usage despite the Sonnet pin."))
    elif not parsed["sonnet_main_only"] and status != "malformed-output":
        incidents.append(_incident("main-model-unverified", "high", "Claude did not report an exclusively Sonnet main model."))
    non_sonnet_helpers = [model for model in parsed["helper_models"] if _model_family(model) != "sonnet"]
    if non_sonnet_helpers and not parsed["opus_observed"]:
        incidents.append(
            _incident(
                "non-sonnet-helper-observed",
                "info",
                "Claude reported a non-Sonnet internal helper model; the main response remained separately gated.",
            )
        )
    if cost is not None and cost > max_budget_usd + 1e-9:
        incidents.append(
            _incident(
                "budget-cap-overshoot",
                "medium",
                "Claude reported cost above the requested max-budget-usd cap.",
            )
        )
    elif terminal_subtype == "error_max_budget_usd":
        incidents.append(_incident("budget-cap-reached", "low", "Claude stopped at its max-budget-usd gate."))
    if status == "timed-out":
        incidents.append(_incident("provider-timeout", "medium", "Claude exceeded the configured wall-clock timeout."))
    elif status == "output-truncated":
        incidents.append(_incident("provider-output-truncated", "medium", "Claude output exceeded the configured capture limit."))
    elif status == "malformed-output":
        incidents.append(
            _incident(
                "provider-output-malformed",
                "medium",
                "Claude stream validation failed: %s" % (parse_error or "unknown parse error"),
            )
        )
    elif status == "provider-error" and terminal_subtype not in {"error_max_budget_usd"}:
        incidents.append(_incident("provider-run-failed", "low", "Claude did not produce a successful terminal result."))
    return incidents[:MAX_CLAUDE_INCIDENTS]


def _require_paid_readonly_policy(policy: RuntimePolicy) -> None:
    if not policy.allow_agent:
        raise PolicyError("Claude live parity requires --allow-agent")
    if not policy.allow_network:
        raise PolicyError("Claude live parity requires --allow-network")
    if not policy.has_approval(CLAUDE_PAID_RUN_APPROVAL):
        raise PolicyError("Claude live parity requires --approve %s" % CLAUDE_PAID_RUN_APPROVAL)


def _validate_provider_tool_contract(
    permission_mode,
    tools,
    source: str = "Claude provider",
    *,
    readonly_permission_mode: str = CLAUDE_PERMISSION_MODE,
) -> None:
    contracts = [
        (readonly_permission_mode, CLAUDE_READ_ONLY_TOOLS),
        (CLAUDE_STAGED_PERMISSION_MODE, CLAUDE_STAGED_WRITE_TOOLS),
    ]
    if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
        raise ValidationError("%s tools must be a string list" % source)
    if not any(permission_mode == expected_mode and tools == expected_tools for expected_mode, expected_tools in contracts):
        raise ValidationError("%s must preserve an approved Claude tool and permission contract" % source)


def _claude_cli_version(claude_path: str, workspace: Path) -> str:
    try:
        result = run_process([claude_path, "--version"], cwd=workspace, timeout=5, output_limit_bytes=4096)
    except OSError as exc:
        raise StepExecutionError("Claude Code version probe failed: %s" % exc.__class__.__name__)
    if result.timed_out or result.returncode != 0 or result.stdout_truncated or result.stderr_truncated:
        raise ValidationError("Claude Code version probe failed")
    version_text = (result.stdout.strip() or result.stderr.strip())[:300]
    match = _VERSION_PATTERN.search(version_text)
    if not match:
        raise ValidationError("Claude Code version could not be parsed")
    version = tuple(int(part) for part in match.groups())
    if version < CLAUDE_MINIMUM_VERSION:
        raise ValidationError("Claude Code %s or later is required for Ultracode effort" % ".".join(map(str, CLAUDE_MINIMUM_VERSION)))
    return redact_text(version_text)


def _parity_task(parity_tasks: Dict, task_id: str) -> Dict:
    if not isinstance(task_id, str) or not SAFE_ID.match(task_id):
        raise ValidationError("Claude parity task id must be a safe identifier")
    matches = [task for task in parity_tasks["tasks"] if task["id"] == task_id]
    if len(matches) != 1:
        raise ValidationError("Claude parity task does not exist: %s" % task_id)
    return matches[0]


def _assistant_text_blocks(content) -> List[str]:
    if content is None:
        return []
    if not isinstance(content, list):
        raise ValidationError("Claude assistant message content must be a list")
    texts = []
    for block in content:
        if not isinstance(block, dict):
            raise ValidationError("Claude assistant content blocks must be objects")
        if block.get("type") == "text":
            value = block.get("text")
            if not isinstance(value, str):
                raise ValidationError("Claude assistant text blocks must contain strings")
            texts.append(value)
    return texts


def _append_model(target: List[str], value) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValidationError("Claude model identifiers must be bounded non-empty strings")
    target.append(" ".join(value.split()))


def _model_family(model: str) -> str:
    normalized = str(model or "").lower()
    if "opus" in normalized:
        return "opus"
    if "sonnet" in normalized:
        return "sonnet"
    if "haiku" in normalized:
        return "haiku"
    return "other"


def _model_list(value, label: str) -> List[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValidationError("%s must be a bounded list" % label)
    cleaned = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 300:
            raise ValidationError("%s must contain bounded non-empty strings" % label)
        cleaned.append(item)
    if cleaned != sorted(set(cleaned)):
        raise ValidationError("%s must be sorted and unique" % label)
    return cleaned


def _empty_parsed_stream() -> Dict:
    return {
        "terminal": None,
        "main_models": [],
        "helper_models": [],
        "sonnet_main_only": False,
        "opus_observed": False,
        "assistant_messages": 0,
        "stream_events": 0,
        "output_text": "",
        "output_source": "none",
        **_empty_claude_token_usage(),
    }


def _empty_claude_token_usage() -> Dict:
    return {
        "token_usage_source": "unavailable",
        "input_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }


def _parse_claude_model_usage(model_usage) -> Dict:
    if not model_usage:
        return _empty_claude_token_usage()
    token_fields = {
        "inputTokens": "input_tokens",
        "cacheCreationInputTokens": "cache_creation_input_tokens",
        "cacheReadInputTokens": "cache_read_input_tokens",
        "outputTokens": "output_tokens",
    }
    records = list(model_usage.values())
    field_presence = [field in record for record in records for field in token_fields]
    if not any(field_presence):
        return _empty_claude_token_usage()
    if not all(field_presence):
        raise ValidationError("Claude result modelUsage token counters must be complete or unavailable")
    totals = {target: 0 for target in token_fields.values()}
    for record in records:
        for source_field, target_field in token_fields.items():
            value = _optional_non_negative_int(
                record[source_field],
                "Claude result modelUsage %s" % source_field,
            )
            if value is None or value > MAX_CLAUDE_TOKENS:
                raise ValidationError("Claude result modelUsage token counters exceed the supported range")
            totals[target_field] += value
            if totals[target_field] > MAX_CLAUDE_TOKENS:
                raise ValidationError("Claude result modelUsage token totals exceed the supported range")
    gross_total = sum(totals.values())
    if gross_total > MAX_CLAUDE_TOKENS:
        raise ValidationError("Claude result gross token total exceeds the supported range")
    return {
        "token_usage_source": "modelUsage",
        **totals,
        "total_tokens": gross_total,
    }


def _validate_claude_token_usage(observed: Dict, source: str) -> None:
    if observed.get("token_accounting") != CLAUDE_TOKEN_ACCOUNTING:
        raise ValidationError("%s Claude token accounting is invalid" % source)
    usage_source = observed.get("token_usage_source")
    if usage_source not in CLAUDE_TOKEN_USAGE_SOURCES:
        raise ValidationError("%s Claude token usage source is invalid" % source)
    fields = [
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
        "total_tokens",
    ]
    values = [observed.get(field) for field in fields]
    if usage_source == "unavailable":
        if any(value is not None for value in values):
            raise ValidationError("%s unavailable Claude token usage must be null" % source)
        return
    if any(value is None for value in values):
        raise ValidationError("%s provider-observed Claude token usage must be complete" % source)
    for field, value in zip(fields, values):
        _bounded_int(value, "%s observed %s" % (source, field), 0, MAX_CLAUDE_TOKENS)
    if observed["total_tokens"] != sum(observed[field] for field in fields[:-1]):
        raise ValidationError("%s Claude total tokens must equal all input categories plus output" % source)


def _incident(incident_id: str, severity: str, description: str) -> Dict:
    return {"id": incident_id, "severity": severity, "description": redact_text(description)}


def _format_budget(value: float) -> str:
    return ("%.6f" % float(value)).rstrip("0").rstrip(".")


def _positive_number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValidationError("%s must be a finite positive number" % label)
    return float(value)


def _optional_non_negative_number(value, label: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValidationError("%s must be a finite non-negative number or null" % label)
    return float(value)


def _optional_non_negative_int(value, label: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError("%s must be a non-negative integer or null" % label)
    return value


def _bounded_int(value, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise ValidationError("%s must be an integer between %d and %d" % (label, minimum, maximum))
    return value


def _optional_short_text(value) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValidationError("Claude terminal subtype must be a bounded non-empty string or null")
    return " ".join(value.split())


def _exact_fields(value: Dict, fields: set, label: str) -> None:
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown or missing:
        detail = []
        if unknown:
            detail.append("unknown: %s" % ", ".join(unknown))
        if missing:
            detail.append("missing: %s" % ", ".join(missing))
        raise ValidationError("%s fields are invalid (%s)" % (label, "; ".join(detail)))


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("Claude provider evidence JSON contains duplicate key: %s" % key)
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValidationError("Claude provider evidence JSON contains invalid numeric constant: %s" % value)


def _reject_claude_stream_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("Claude stream JSON contains duplicate key: %s" % key)
        result[key] = value
    return result


def _reject_claude_stream_constant(value):
    raise ValidationError("Claude stream JSON contains invalid numeric constant: %s" % value)
