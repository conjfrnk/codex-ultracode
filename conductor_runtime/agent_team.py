import hashlib
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .agent_profiles import agent_profile_map, effective_agent_step
from .agent_team_transcript import validate_agent_team_transcript_policy
from .codex_config import (
    MIN_CODEX_RUNTIME_TOKEN_CAP,
    validate_codex_effort,
    validate_codex_token_cap,
)
from .errors import ValidationError
from .redaction import redact_text
from .security import (
    ensure_dir_no_follow,
    read_regular_file_bytes_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    require_no_path_escape,
)


AGENT_TEAM_STATE_SCHEMA_V1 = "conductor.agent_team_state.v1"
AGENT_TEAM_STATE_SCHEMA_V2 = "conductor.agent_team_state.v2"
AGENT_TEAM_STATE_SCHEMA_V3 = "conductor.agent_team_state.v3"
AGENT_TEAM_STATE_SCHEMA_V4 = "conductor.agent_team_state.v4"
AGENT_TEAM_STATE_SCHEMA_V5 = "conductor.agent_team_state.v5"
AGENT_TEAM_STATE_SCHEMA = "conductor.agent_team_state.v6"
AGENT_TEAM_TURN_SCHEMA_V1 = "conductor.agent_team_turn.v1"
AGENT_TEAM_TURN_SCHEMA = "conductor.agent_team_turn.v2"
AGENT_TEAM_INTERRUPTION_SCHEMA = "conductor.agent_team_interruption.v1"
AGENT_TEAM_STATUSES = {"running", "completed"}
AGENT_TEAM_TASK_STATUSES_V5 = {"pending", "claimed", "completed"}
AGENT_TEAM_TASK_STATUSES = AGENT_TEAM_TASK_STATUSES_V5 | {"waiting"}
AGENT_TEAM_PROVIDER_TURN_STATUSES = {"completed", "continue"}
AGENT_TEAM_STATE_TURN_STATUSES = AGENT_TEAM_PROVIDER_TURN_STATUSES | {"interrupted"}
SAFE_TEAM_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

MIN_AGENT_TEAM_MEMBERS = 2
MAX_AGENT_TEAM_MEMBERS = 16
MAX_AGENT_TEAM_TASKS = 128
MAX_AGENT_TEAM_DYNAMIC_TASKS = 32
MAX_AGENT_TEAM_OPERATOR_TASKS = 32
MAX_AGENT_TEAM_MESSAGE_TASKS = 32
MAX_AGENT_TEAM_MESSAGE_DEPTH = 4
MAX_AGENT_TEAM_OPERATOR_QUESTIONS = 32
MAX_AGENT_TEAM_OPERATOR_REPLY_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_AGENT_TEAM_PLAN_APPROVAL_TASKS = 16
MAX_AGENT_TEAM_PLAN_REVISIONS = 3
MAX_AGENT_TEAM_PLAN_CRITERIA_CHARS = 8000
MAX_AGENT_TEAM_PLAN_REVIEW_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_AGENT_TEAM_TASK_PROPOSALS_PER_TURN = 8
MAX_AGENT_TEAM_ROUNDS = 16
MAX_AGENT_TEAM_TURNS = MAX_AGENT_TEAM_MEMBERS * MAX_AGENT_TEAM_ROUNDS
MAX_AGENT_TEAM_GENERATION = 10**9
MAX_AGENT_TEAM_MESSAGES = 1024
MAX_AGENT_TEAM_MESSAGES_PER_TURN = 8
MAX_AGENT_TEAM_CLAIMS_PER_TURN = 16
MAX_AGENT_TEAM_ROLE_CHARS = 300
MAX_AGENT_TEAM_INSTRUCTIONS_CHARS = 8000
MAX_AGENT_TEAM_TASK_DESCRIPTION_CHARS = 12000
MAX_AGENT_TEAM_SUMMARY_CHARS = 16000
MAX_AGENT_TEAM_MESSAGE_CHARS = 4000
MAX_AGENT_TEAM_STATE_BYTES = 8 * 1024 * 1024
MAX_AGENT_TEAM_TURN_BYTES = 256 * 1024
MAX_AGENT_TEAM_PROMPT_BYTES = 512 * 1024

AGENT_TEAM_STEP_FIELDS = {
    "id",
    "kind",
    "description",
    "risk",
    "depends_on",
    "phase",
    "output_limit_bytes",
    "timeout_seconds",
    "model",
    "effort",
    "sandbox",
    "max_tokens",
    "max_total_tokens",
    "max_workers",
    "max_rounds",
    "max_dynamic_tasks",
    "max_operator_tasks",
    "active_messaging",
    "operator_chat",
    "operator_console",
    "plan_approval",
    "capture_dir",
    "report",
    "members",
    "tasks",
}
AGENT_TEAM_MEMBER_FIELDS = {"id", "role", "instructions", "agent_profile", "lead"}
AGENT_TEAM_TASK_FIELDS = {"id", "description", "assignee", "depends_on"}
AGENT_TEAM_STATE_FIELDS_V1 = {
    "schema",
    "step_id",
    "workflow_fingerprint",
    "generation",
    "status",
    "round",
    "max_rounds",
    "max_turns",
    "max_total_tokens",
    "authorized_tokens",
    "created_at_utc",
    "updated_at_utc",
    "members",
    "tasks",
    "messages",
    "turns",
}
AGENT_TEAM_STATE_FIELDS_V2 = AGENT_TEAM_STATE_FIELDS_V1 | {
    "max_dynamic_tasks",
    "dynamic_tasks_added",
}
AGENT_TEAM_STATE_FIELDS_V3 = AGENT_TEAM_STATE_FIELDS_V2 | {
    "max_operator_tasks",
    "operator_tasks_added",
}
AGENT_TEAM_STATE_FIELDS_V4 = AGENT_TEAM_STATE_FIELDS_V3
AGENT_TEAM_STATE_FIELDS_V5 = AGENT_TEAM_STATE_FIELDS_V4 | {
    "max_message_tasks",
    "message_tasks_added",
}
AGENT_TEAM_STATE_FIELDS = AGENT_TEAM_STATE_FIELDS_V5 | {
    "max_operator_questions",
    "operator_questions_added",
}
AGENT_TEAM_STATE_MEMBER_FIELDS = {
    "id",
    "role",
    "agent_profile",
    "lead",
    "status",
    "session_id",
    "turns",
    "current_task_id",
    "claim_task_ids",
    "last_error",
}
AGENT_TEAM_STATE_TASK_FIELDS_V1 = {
    "id",
    "status",
    "assignee",
    "claimed_by",
    "depends_on",
    "attempts",
    "summary",
    "output_sha256",
    "completed_round",
}
AGENT_TEAM_STATE_TASK_FIELDS_V2 = AGENT_TEAM_STATE_TASK_FIELDS_V1 | {
    "description",
    "origin",
    "proposed_by",
    "proposed_round",
}
AGENT_TEAM_STATE_TASK_FIELDS_V3 = AGENT_TEAM_STATE_TASK_FIELDS_V2 | {
    "operator_entry_id",
}
AGENT_TEAM_STATE_TASK_FIELDS_V4 = AGENT_TEAM_STATE_TASK_FIELDS_V3
AGENT_TEAM_STATE_TASK_FIELDS_V5 = AGENT_TEAM_STATE_TASK_FIELDS_V4 | {
    "message_id",
    "message_depth",
}
AGENT_TEAM_STATE_TASK_FIELDS = AGENT_TEAM_STATE_TASK_FIELDS_V5 | {
    "operator_reply_sha256",
}
AGENT_TEAM_STATE_MESSAGE_FIELDS = {"id", "round", "from", "to", "body", "body_sha256"}
AGENT_TEAM_STATE_TURN_FIELDS = {
    "round",
    "member_id",
    "task_id",
    "status",
    "output",
    "output_sha256",
    "session_id_sha256",
    "started_at_utc",
    "finished_at_utc",
    "max_tokens",
    "input_tokens",
    "output_tokens",
    "total_tokens",
}
AGENT_TEAM_STATE_TURN_FIELDS_V4 = AGENT_TEAM_STATE_TURN_FIELDS | {
    "interruption_entry_id",
    "interruption_instruction_sha256",
}
AGENT_TEAM_TURN_FIELDS_V1 = {"schema", "task_id", "status", "summary", "messages", "claim_task_ids"}
AGENT_TEAM_TURN_FIELDS = AGENT_TEAM_TURN_FIELDS_V1 | {"task_proposals"}
AGENT_TEAM_TURN_MESSAGE_FIELDS = {"to", "body"}
AGENT_TEAM_TASK_PROPOSAL_FIELDS = {"id", "description", "assignee", "depends_on"}
AGENT_TEAM_PLAN_APPROVAL_POLICY_BASE_FIELDS = {
    "task_ids",
    "criteria",
    "max_revisions",
    "plan_max_tokens",
}
AGENT_TEAM_PLAN_APPROVAL_POLICY_LEAD_FIELDS = AGENT_TEAM_PLAN_APPROVAL_POLICY_BASE_FIELDS | {
    "review_max_tokens",
}
AGENT_TEAM_PLAN_APPROVAL_POLICY_OPERATOR_FIELDS = AGENT_TEAM_PLAN_APPROVAL_POLICY_BASE_FIELDS | {
    "reviewer",
    "reply_timeout_seconds",
}
AGENT_TEAM_ACTIVE_MESSAGING_POLICY_FIELDS = {
    "max_tasks",
    "max_depth",
    "max_tokens",
    "allow_broadcast",
}
AGENT_TEAM_OPERATOR_CHAT_POLICY_FIELDS = {
    "max_questions",
    "max_tokens",
    "reply_timeout_seconds",
}
AGENT_TEAM_MESSAGE_TASK_PREFIX = "team-message-task-"
AGENT_TEAM_OPERATOR_REPLY_TASK_PREFIX = "team-operator-reply-"
AGENT_TEAM_INTERRUPTION_FIELDS = {
    "schema",
    "task_id",
    "status",
    "operator_entry_id",
    "instruction_sha256",
    "session_id_sha256",
}
AGENT_TEAM_INTERRUPTION_SUMMARY = (
    "Operator interrupted this turn; no provider output or workspace changes were accepted."
)


def validate_agent_team_step(step: Dict, workflow: Dict) -> None:
    if not isinstance(step, dict) or step.get("kind") != "agent_team":
        raise ValidationError("agent_team step must be an object with kind agent_team")
    unknown = sorted(set(step) - AGENT_TEAM_STEP_FIELDS)
    if unknown:
        raise ValidationError(
            "agent_team step %s contains unsupported field(s): %s"
            % (step.get("id", "<unknown>"), ", ".join(unknown))
        )
    step_id = step.get("id")
    _validate_id(step_id, "agent_team step id")
    sandbox = step.get("sandbox", "read-only")
    if sandbox not in {"read-only", "workspace-write"}:
        raise ValidationError(
            "agent_team step %s sandbox must be read-only or workspace-write" % step_id
        )
    if sandbox == "workspace-write" and step.get("risk", "medium") == "low":
        raise ValidationError("write-capable agent_team step %s must be medium or high risk" % step_id)
    _validate_required_int(step, "max_workers", 1, MAX_AGENT_TEAM_MEMBERS, step_id)
    _validate_required_int(step, "max_rounds", 1, MAX_AGENT_TEAM_ROUNDS, step_id)
    max_dynamic_tasks = step.get("max_dynamic_tasks", 0)
    if (
        not _strict_int(max_dynamic_tasks)
        or not 0 <= max_dynamic_tasks <= MAX_AGENT_TEAM_DYNAMIC_TASKS
    ):
        raise ValidationError(
            "agent_team step %s max_dynamic_tasks must be an integer from 0 to %d"
            % (step_id, MAX_AGENT_TEAM_DYNAMIC_TASKS)
        )
    max_operator_tasks = step.get("max_operator_tasks", 0)
    if (
        not _strict_int(max_operator_tasks)
        or not 0 <= max_operator_tasks <= MAX_AGENT_TEAM_OPERATOR_TASKS
    ):
        raise ValidationError(
            "agent_team step %s max_operator_tasks must be an integer from 0 to %d"
            % (step_id, MAX_AGENT_TEAM_OPERATOR_TASKS)
        )
    validate_codex_token_cap(step.get("max_tokens"), "agent_team step %s max_tokens" % step_id)
    validate_codex_token_cap(
        step.get("max_total_tokens"),
        "agent_team step %s max_total_tokens" % step_id,
    )
    if "effort" in step:
        validate_codex_effort(step["effort"], "agent_team step %s effort" % step_id)
    members = step.get("members")
    if (
        not isinstance(members, list)
        or not MIN_AGENT_TEAM_MEMBERS <= len(members) <= MAX_AGENT_TEAM_MEMBERS
    ):
        raise ValidationError(
            "agent_team step %s members must contain %d to %d members"
            % (step_id, MIN_AGENT_TEAM_MEMBERS, MAX_AGENT_TEAM_MEMBERS)
        )
    if step["max_workers"] > len(members):
        raise ValidationError("agent_team step %s max_workers cannot exceed its member count" % step_id)
    profiles = agent_profile_map(workflow)
    member_ids = []
    lead_count = 0
    for index, member in enumerate(members):
        source = "agent_team step %s members[%d]" % (step_id, index)
        if not isinstance(member, dict) or set(member) != AGENT_TEAM_MEMBER_FIELDS:
            raise ValidationError("%s has invalid fields" % source)
        member_id = member.get("id")
        _validate_id(member_id, "%s id" % source)
        if member_id == "all":
            raise ValidationError("%s id is reserved for broadcast routing" % source)
        if member_id == "operator":
            raise ValidationError("%s id is reserved for operator routing" % source)
        if member_id in member_ids:
            raise ValidationError("agent_team step %s contains duplicate member %s" % (step_id, member_id))
        member_ids.append(member_id)
        _validate_text(member.get("role"), "%s role" % source, MAX_AGENT_TEAM_ROLE_CHARS)
        _validate_text(
            member.get("instructions"),
            "%s instructions" % source,
            MAX_AGENT_TEAM_INSTRUCTIONS_CHARS,
        )
        profile_name = member.get("agent_profile")
        _validate_id(profile_name, "%s agent_profile" % source)
        if profile_name not in profiles:
            raise ValidationError("%s references unknown agent profile %s" % (source, profile_name))
        if not isinstance(member.get("lead"), bool):
            raise ValidationError("%s lead must be a boolean" % source)
        lead_count += int(member["lead"])
        effective = team_member_effective_step(workflow, step, member_id)
        if effective.get("sandbox") != sandbox:
            raise ValidationError("%s profile cannot provide the team sandbox %s" % (source, sandbox))
    if lead_count != 1:
        raise ValidationError("agent_team step %s must define exactly one lead member" % step_id)

    tasks = step.get("tasks")
    if not isinstance(tasks, list) or not tasks or len(tasks) > MAX_AGENT_TEAM_TASKS:
        raise ValidationError(
            "agent_team step %s tasks must contain 1 to %d tasks" % (step_id, MAX_AGENT_TEAM_TASKS)
        )
    task_ids = []
    for index, task in enumerate(tasks):
        source = "agent_team step %s tasks[%d]" % (step_id, index)
        if not isinstance(task, dict) or set(task) != AGENT_TEAM_TASK_FIELDS:
            raise ValidationError("%s has invalid fields" % source)
        task_id = task.get("id")
        _validate_id(task_id, "%s id" % source)
        if task_id in task_ids:
            raise ValidationError("agent_team step %s contains duplicate task %s" % (step_id, task_id))
        task_ids.append(task_id)
        _validate_text(
            task.get("description"),
            "%s description" % source,
            MAX_AGENT_TEAM_TASK_DESCRIPTION_CHARS,
        )
        assignee = task.get("assignee")
        if assignee is not None and assignee not in member_ids:
            raise ValidationError("%s assignee is not a team member" % source)
        dependencies = task.get("depends_on")
        if not isinstance(dependencies, list) or not all(isinstance(value, str) for value in dependencies):
            raise ValidationError("%s depends_on must be an array of task ids" % source)
        if len(dependencies) != len(set(dependencies)):
            raise ValidationError("%s depends_on contains duplicates" % source)
    _validate_task_dependencies(tasks, task_ids, step_id)
    max_message_tasks = _validate_agent_team_active_messaging(step, tasks)
    max_operator_questions = _validate_agent_team_operator_chat(step, tasks)
    if step.get("operator_console") is not None:
        validate_agent_team_transcript_policy(step["operator_console"], step_id)
    _validate_agent_team_plan_approval(step, tasks, members)
    if (
        len(tasks)
        + max_dynamic_tasks
        + max_operator_tasks
        + max_message_tasks
        + max_operator_questions
        > MAX_AGENT_TEAM_TASKS
    ):
        raise ValidationError(
            "agent_team step %s static, dynamic, operator, message, and operator-reply task limits exceed %d"
            % (step_id, MAX_AGENT_TEAM_TASKS)
        )
    if len(tasks) > step["max_workers"] * step["max_rounds"]:
        raise ValidationError("agent_team step %s cannot schedule all tasks within its round limit" % step_id)
    assigned_counts = {
        member_id: sum(task["assignee"] == member_id for task in tasks)
        for member_id in member_ids
    }
    if any(count > step["max_rounds"] for count in assigned_counts.values()):
        raise ValidationError("agent_team step %s assigns more tasks than one member can run" % step_id)
    task_depths = {}
    for task in tasks:
        task_depths[task["id"]] = 1 + max(
            (task_depths[dependency] for dependency in task["depends_on"]),
            default=0,
        )
    if max(task_depths.values()) > step["max_rounds"]:
        raise ValidationError("agent_team step %s dependency depth exceeds its round limit" % step_id)

    for field in ("capture_dir", "report"):
        value = step.get(field)
        if not isinstance(value, str) or not value:
            raise ValidationError("agent_team step %s must set %s" % (step_id, field))
        require_no_path_escape(value)
    if step["report"] == step["capture_dir"] or step["report"].startswith(step["capture_dir"] + "/"):
        raise ValidationError("agent_team step %s report must be outside its internal capture directory" % step_id)


def _validate_agent_team_active_messaging(step: Dict, tasks: List[Dict]) -> int:
    policy = step.get("active_messaging")
    if policy is None:
        return 0
    step_id = step["id"]
    if not isinstance(policy, dict) or set(policy) != AGENT_TEAM_ACTIVE_MESSAGING_POLICY_FIELDS:
        raise ValidationError("agent_team step %s active_messaging has invalid fields" % step_id)
    maximum = policy.get("max_tasks")
    if not _strict_int(maximum) or not 1 <= maximum <= MAX_AGENT_TEAM_MESSAGE_TASKS:
        raise ValidationError(
            "agent_team step %s active_messaging max_tasks must be an integer from 1 to %d"
            % (step_id, MAX_AGENT_TEAM_MESSAGE_TASKS)
        )
    depth = policy.get("max_depth")
    if not _strict_int(depth) or not 1 <= depth <= MAX_AGENT_TEAM_MESSAGE_DEPTH:
        raise ValidationError(
            "agent_team step %s active_messaging max_depth must be an integer from 1 to %d"
            % (step_id, MAX_AGENT_TEAM_MESSAGE_DEPTH)
        )
    max_tokens = policy.get("max_tokens")
    validate_codex_token_cap(
        max_tokens,
        "agent_team step %s active_messaging max_tokens" % step_id,
    )
    if not _strict_int(max_tokens) or max_tokens > step["max_tokens"]:
        raise ValidationError(
            "agent_team step %s active_messaging max_tokens cannot exceed max_tokens"
            % step_id
        )
    if not isinstance(policy.get("allow_broadcast"), bool):
        raise ValidationError(
            "agent_team step %s active_messaging allow_broadcast must be a boolean"
            % step_id
        )
    if step["max_rounds"] < 2:
        raise ValidationError(
            "agent_team step %s active_messaging requires at least two rounds" % step_id
        )
    if any(task["id"].startswith(AGENT_TEAM_MESSAGE_TASK_PREFIX) for task in tasks):
        raise ValidationError(
            "agent_team step %s reserves task ids beginning with %s"
            % (step_id, AGENT_TEAM_MESSAGE_TASK_PREFIX)
        )
    return maximum


def _validate_agent_team_operator_chat(step: Dict, tasks: List[Dict]) -> int:
    policy = step.get("operator_chat")
    if policy is None:
        return 0
    step_id = step["id"]
    if not isinstance(policy, dict) or set(policy) != AGENT_TEAM_OPERATOR_CHAT_POLICY_FIELDS:
        raise ValidationError("agent_team step %s operator_chat has invalid fields" % step_id)
    maximum = policy.get("max_questions")
    if not _strict_int(maximum) or not 1 <= maximum <= MAX_AGENT_TEAM_OPERATOR_QUESTIONS:
        raise ValidationError(
            "agent_team step %s operator_chat max_questions must be an integer from 1 to %d"
            % (step_id, MAX_AGENT_TEAM_OPERATOR_QUESTIONS)
        )
    max_tokens = policy.get("max_tokens")
    validate_codex_token_cap(
        max_tokens,
        "agent_team step %s operator_chat max_tokens" % step_id,
    )
    if not _strict_int(max_tokens) or max_tokens > step["max_tokens"]:
        raise ValidationError(
            "agent_team step %s operator_chat max_tokens cannot exceed max_tokens"
            % step_id
        )
    reply_timeout = policy.get("reply_timeout_seconds")
    if (
        not _strict_int(reply_timeout)
        or not 1 <= reply_timeout <= MAX_AGENT_TEAM_OPERATOR_REPLY_TIMEOUT_SECONDS
    ):
        raise ValidationError(
            "agent_team step %s operator_chat reply_timeout_seconds must be an integer from 1 to %d"
            % (step_id, MAX_AGENT_TEAM_OPERATOR_REPLY_TIMEOUT_SECONDS)
        )
    if step["max_rounds"] < 2:
        raise ValidationError(
            "agent_team step %s operator_chat requires at least two rounds" % step_id
        )
    if any(task["id"].startswith(AGENT_TEAM_OPERATOR_REPLY_TASK_PREFIX) for task in tasks):
        raise ValidationError(
            "agent_team step %s reserves task ids beginning with %s"
            % (step_id, AGENT_TEAM_OPERATOR_REPLY_TASK_PREFIX)
        )
    return maximum


def _validate_agent_team_plan_approval(step: Dict, tasks: List[Dict], members: List[Dict]) -> None:
    policy = step.get("plan_approval")
    if policy is None:
        return
    step_id = step["id"]
    if not isinstance(policy, dict):
        raise ValidationError("agent_team step %s plan_approval has invalid fields" % step_id)
    reviewer = policy.get("reviewer", "lead")
    fields = set(policy)
    if reviewer == "lead":
        expected = AGENT_TEAM_PLAN_APPROVAL_POLICY_LEAD_FIELDS | (
            {"reviewer"} if "reviewer" in fields else set()
        )
    elif reviewer == "operator":
        expected = AGENT_TEAM_PLAN_APPROVAL_POLICY_OPERATOR_FIELDS
    else:
        raise ValidationError(
            "agent_team step %s plan_approval reviewer must be lead or operator" % step_id
        )
    if fields != expected:
        raise ValidationError("agent_team step %s plan_approval has invalid fields" % step_id)
    if step.get("sandbox", "read-only") != "workspace-write":
        raise ValidationError(
            "agent_team step %s plan_approval requires workspace-write execution" % step_id
        )
    selected = policy.get("task_ids")
    if (
        not isinstance(selected, list)
        or not 1 <= len(selected) <= MAX_AGENT_TEAM_PLAN_APPROVAL_TASKS
        or len(selected) != len(set(selected))
        or not all(isinstance(value, str) for value in selected)
    ):
        raise ValidationError(
            "agent_team step %s plan_approval task_ids must contain 1 to %d unique ids"
            % (step_id, MAX_AGENT_TEAM_PLAN_APPROVAL_TASKS)
        )
    task_map = {task["id"]: task for task in tasks}
    member_map = {member["id"]: member for member in members}
    for task_id in selected:
        task = task_map.get(task_id)
        if task is None:
            raise ValidationError(
                "agent_team step %s plan_approval references unknown task %s"
                % (step_id, task_id)
            )
        assignee = task.get("assignee")
        if assignee is None or member_map[assignee]["lead"]:
            raise ValidationError(
                "agent_team step %s plan_approval task %s requires a fixed non-lead assignee"
                % (step_id, task_id)
            )
    _validate_text(
        policy.get("criteria"),
        "agent_team step %s plan_approval criteria" % step_id,
        MAX_AGENT_TEAM_PLAN_CRITERIA_CHARS,
    )
    revisions = policy.get("max_revisions")
    if not _strict_int(revisions) or not 0 <= revisions <= MAX_AGENT_TEAM_PLAN_REVISIONS:
        raise ValidationError(
            "agent_team step %s plan_approval max_revisions must be an integer from 0 to %d"
            % (step_id, MAX_AGENT_TEAM_PLAN_REVISIONS)
        )
    step_cap = step.get("max_tokens")
    token_fields = ["plan_max_tokens"]
    if reviewer == "lead":
        token_fields.append("review_max_tokens")
    for field in token_fields:
        value = policy.get(field)
        validate_codex_token_cap(value, "agent_team step %s plan_approval %s" % (step_id, field))
        if not _strict_int(value) or (_strict_int(step_cap) and value > step_cap):
            raise ValidationError(
                "agent_team step %s plan_approval %s cannot exceed max_tokens"
                % (step_id, field)
            )
    if reviewer == "operator":
        timeout = policy.get("reply_timeout_seconds")
        if (
            not _strict_int(timeout)
            or not 1 <= timeout <= MAX_AGENT_TEAM_PLAN_REVIEW_TIMEOUT_SECONDS
        ):
            raise ValidationError(
                "agent_team step %s plan_approval reply_timeout_seconds must be an integer from 1 to %d"
                % (step_id, MAX_AGENT_TEAM_PLAN_REVIEW_TIMEOUT_SECONDS)
            )
    review_reserve = policy["review_max_tokens"] if reviewer == "lead" else 0
    approval_reserve = len(selected) * (revisions + 1) * (
        policy["plan_max_tokens"] + review_reserve
    )
    implementation_reserve = len(tasks) * MIN_CODEX_RUNTIME_TOKEN_CAP
    if step.get("max_total_tokens", 0) < approval_reserve + implementation_reserve:
        raise ValidationError(
            "agent_team step %s max_total_tokens cannot fund worst-case plan approval and implementation"
            % step_id
        )


def agent_team_profile_names(step: Dict) -> List[str]:
    if not isinstance(step, dict) or step.get("kind") != "agent_team":
        return []
    names = []
    for member in step.get("members", []):
        name = member.get("agent_profile") if isinstance(member, dict) else None
        if isinstance(name, str) and name not in names:
            names.append(name)
    return names


def team_member_effective_step(
    workflow: Dict,
    team_step: Dict,
    member_id: str,
    *,
    task_id: Optional[str] = None,
) -> Dict:
    member = next(
        (
            value
            for value in team_step.get("members", [])
            if isinstance(value, dict) and value.get("id") == member_id
        ),
        None,
    )
    if member is None:
        raise ValidationError("agent_team references unknown member %s" % member_id)
    synthetic = {
        "id": "%s.%s%s"
        % (
            team_step["id"],
            member_id,
            ".%s" % task_id if task_id else "",
        ),
        "kind": "codex_exec",
        "risk": team_step.get("risk", "medium"),
        "agent_profile": member["agent_profile"],
        "prompt": "agent team turn",
        "capture": "agent-team-turn.json",
        "sandbox": team_step.get("sandbox", "read-only"),
        "max_tokens": team_step["max_tokens"],
    }
    for field in ("model", "effort", "timeout_seconds", "output_limit_bytes"):
        if field in team_step:
            synthetic[field] = team_step[field]
    return effective_agent_step(workflow, synthetic)


def initial_agent_team_state(step: Dict, workflow_fingerprint: str, generation: int = 0) -> Dict:
    _validate_sha256(workflow_fingerprint, "agent team workflow fingerprint")
    if not _strict_int(generation) or not 0 <= generation <= MAX_AGENT_TEAM_GENERATION:
        raise ValidationError("agent team generation is invalid")
    now = _utc_now()
    state = {
        "schema": AGENT_TEAM_STATE_SCHEMA,
        "step_id": step["id"],
        "workflow_fingerprint": workflow_fingerprint,
        "generation": generation,
        "status": "running",
        "round": 0,
        "max_rounds": step["max_rounds"],
        "max_turns": min(MAX_AGENT_TEAM_TURNS, step["max_rounds"] * len(step["members"])),
        "max_total_tokens": step["max_total_tokens"],
        "max_dynamic_tasks": step.get("max_dynamic_tasks", 0),
        "dynamic_tasks_added": 0,
        "max_operator_tasks": step.get("max_operator_tasks", 0),
        "operator_tasks_added": 0,
        "max_message_tasks": (step.get("active_messaging") or {}).get("max_tasks", 0),
        "message_tasks_added": 0,
        "max_operator_questions": (step.get("operator_chat") or {}).get("max_questions", 0),
        "operator_questions_added": 0,
        "authorized_tokens": 0,
        "created_at_utc": now,
        "updated_at_utc": now,
        "members": [
            {
                "id": member["id"],
                "role": member["role"],
                "agent_profile": member["agent_profile"],
                "lead": member["lead"],
                "status": "idle",
                "session_id": None,
                "turns": 0,
                "current_task_id": None,
                "claim_task_ids": [],
                "last_error": None,
            }
            for member in step["members"]
        ],
        "tasks": [
            {
                "id": task["id"],
                "description": task["description"],
                "origin": "static",
                "proposed_by": None,
                "proposed_round": None,
                "operator_entry_id": None,
                "message_id": None,
                "message_depth": None,
                "operator_reply_sha256": None,
                "status": "pending",
                "assignee": task["assignee"],
                "claimed_by": None,
                "depends_on": list(task["depends_on"]),
                "attempts": 0,
                "summary": None,
                "output_sha256": None,
                "completed_round": None,
            }
            for task in step["tasks"]
        ],
        "messages": [],
        "turns": [],
    }
    validate_agent_team_state(
        state,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=generation,
    )
    return state


def assign_agent_team_tasks(step: Dict, state: Dict, max_workers: Optional[int] = None) -> List[Dict]:
    if state["status"] == "completed":
        return []
    member_order = [member["id"] for member in state["members"]]
    member_map = {member["id"]: member for member in state["members"]}
    task_map = {task["id"]: task for task in state["tasks"]}
    assignments = []
    assigned_members = set()
    assignment_limit = step["max_workers"]
    if max_workers is not None:
        if not _strict_int(max_workers) or max_workers < 1:
            raise ValidationError("agent team runtime worker limit is invalid")
        assignment_limit = min(assignment_limit, max_workers)

    for member_id in member_order:
        member = member_map[member_id]
        current = member["current_task_id"]
        if current is None:
            continue
        task = task_map[current]
        if task["status"] != "claimed" or task["claimed_by"] != member_id:
            raise ValidationError("agent team current task binding is inconsistent")
        assignments.append({"member_id": member_id, "task_id": current})
        assigned_members.add(member_id)
    if len(assignments) > assignment_limit:
        raise ValidationError("agent team has more continuing claims than its worker limit")

    ready = [
        task
        for task in state["tasks"]
        if task["status"] == "pending"
        and all(task_map[dependency]["status"] == "completed" for dependency in task["depends_on"])
    ]
    for member_id in member_order:
        if len(assignments) >= assignment_limit:
            break
        if member_id in assigned_members:
            continue
        member = member_map[member_id]
        operator_task = next(
            (
                task
                for task in ready
                if task.get("origin") == "operator" and task["assignee"] == member_id
            ),
            None,
        )
        if operator_task is None:
            continue
        _claim_task(member, operator_task)
        assignments.append({"member_id": member_id, "task_id": operator_task["id"]})
        assigned_members.add(member_id)
        ready.remove(operator_task)

    for member_id in member_order:
        if len(assignments) >= assignment_limit:
            break
        if member_id in assigned_members:
            continue
        member = member_map[member_id]
        message_task = next(
            (
                task
                for task in ready
                if task.get("origin") == "message" and task["assignee"] == member_id
            ),
            None,
        )
        if message_task is None:
            continue
        _claim_task(member, message_task)
        assignments.append({"member_id": member_id, "task_id": message_task["id"]})
        assigned_members.add(member_id)
        ready.remove(message_task)

    for member_id in member_order:
        if len(assignments) >= assignment_limit:
            break
        if member_id in assigned_members:
            continue
        member = member_map[member_id]
        for task_id in list(member["claim_task_ids"]):
            task = task_map.get(task_id)
            if task not in ready or task["assignee"] not in {None, member_id}:
                continue
            _claim_task(member, task)
            assignments.append({"member_id": member_id, "task_id": task_id})
            assigned_members.add(member_id)
            ready.remove(task)
            break

    for task in list(ready):
        if len(assignments) >= assignment_limit:
            break
        assignee = task["assignee"]
        if assignee is None or assignee in assigned_members:
            continue
        member = member_map[assignee]
        _claim_task(member, task)
        assignments.append({"member_id": assignee, "task_id": task["id"]})
        assigned_members.add(assignee)
        ready.remove(task)

    free_members = [member_id for member_id in member_order if member_id not in assigned_members]
    unassigned_ready = [task for task in ready if task["assignee"] is None]
    for task, member_id in zip(unassigned_ready, free_members):
        if len(assignments) >= assignment_limit:
            break
        member = member_map[member_id]
        _claim_task(member, task)
        assignments.append({"member_id": member_id, "task_id": task["id"]})
        assigned_members.add(member_id)

    assignments.sort(key=lambda value: member_order.index(value["member_id"]))
    return assignments


def agent_team_turn_schema_for_state(state: Dict) -> str:
    schema = state.get("schema") if isinstance(state, dict) else None
    if schema == AGENT_TEAM_STATE_SCHEMA_V1:
        return AGENT_TEAM_TURN_SCHEMA_V1
    if schema in {
        AGENT_TEAM_STATE_SCHEMA_V2,
        AGENT_TEAM_STATE_SCHEMA_V3,
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    }:
        return AGENT_TEAM_TURN_SCHEMA
    raise ValidationError("agent team state schema is invalid")


def parse_agent_team_turn(
    text: str,
    task_id: str,
    member_ids: List[str],
    task_ids: List[str],
    *,
    expected_schema: Optional[str] = None,
    allow_operator: bool = False,
) -> Dict:
    if not isinstance(text, str):
        raise ValidationError("agent team turn must be text")
    if len(text.encode("utf-8")) > MAX_AGENT_TEAM_TURN_BYTES:
        raise ValidationError("agent team turn exceeds %d bytes" % MAX_AGENT_TEAM_TURN_BYTES)
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3 and lines[0].strip() in {"```", "```json"} and lines[-1].strip() == "```":
            value = "\n".join(lines[1:-1]).strip()
    try:
        turn = json.loads(
            value,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("agent team turn is invalid JSON: %s" % exc.__class__.__name__)
    validate_agent_team_turn(
        turn,
        task_id,
        member_ids,
        task_ids,
        expected_schema=expected_schema,
        allow_operator=allow_operator,
    )
    clean = dict(turn)
    clean["summary"] = redact_text(turn["summary"])
    clean["messages"] = [
        {"to": message["to"], "body": redact_text(message["body"])}
        for message in turn["messages"]
    ]
    if turn["schema"] == AGENT_TEAM_TURN_SCHEMA:
        clean["task_proposals"] = [
            {
                "id": proposal["id"],
                "description": redact_text(proposal["description"]),
                "assignee": proposal["assignee"],
                "depends_on": list(proposal["depends_on"]),
            }
            for proposal in turn["task_proposals"]
        ]
    return clean


def validate_agent_team_turn(
    turn: Dict,
    task_id: str,
    member_ids: List[str],
    task_ids: List[str],
    *,
    expected_schema: Optional[str] = None,
    allow_operator: bool = False,
) -> None:
    schema = turn.get("schema") if isinstance(turn, dict) else None
    if schema not in {AGENT_TEAM_TURN_SCHEMA_V1, AGENT_TEAM_TURN_SCHEMA}:
        raise ValidationError("agent team turn schema is invalid")
    if expected_schema is not None and schema != expected_schema:
        raise ValidationError("agent team turn schema does not match team state")
    expected_fields = AGENT_TEAM_TURN_FIELDS_V1 if schema == AGENT_TEAM_TURN_SCHEMA_V1 else AGENT_TEAM_TURN_FIELDS
    if not isinstance(turn, dict) or set(turn) != expected_fields:
        raise ValidationError("agent team turn has invalid fields")
    if turn.get("task_id") != task_id:
        raise ValidationError("agent team turn task_id does not match the assignment")
    if turn.get("status") not in AGENT_TEAM_PROVIDER_TURN_STATUSES:
        raise ValidationError("agent team turn status must be completed or continue")
    _validate_text(turn.get("summary"), "agent team turn summary", MAX_AGENT_TEAM_SUMMARY_CHARS)
    messages = turn.get("messages")
    if not isinstance(messages, list) or len(messages) > MAX_AGENT_TEAM_MESSAGES_PER_TURN:
        raise ValidationError("agent team turn messages exceed their limit")
    for message in messages:
        if not isinstance(message, dict) or set(message) != AGENT_TEAM_TURN_MESSAGE_FIELDS:
            raise ValidationError("agent team turn message has invalid fields")
        allowed_recipients = set(member_ids) | {"all"}
        if allow_operator:
            allowed_recipients.add("operator")
        if message.get("to") not in allowed_recipients:
            raise ValidationError("agent team turn message recipient is invalid")
        _validate_text(message.get("body"), "agent team turn message body", MAX_AGENT_TEAM_MESSAGE_CHARS)
    claims = turn.get("claim_task_ids")
    if (
        not isinstance(claims, list)
        or len(claims) > MAX_AGENT_TEAM_CLAIMS_PER_TURN
        or not all(isinstance(value, str) for value in claims)
        or len(claims) != len(set(claims))
        or any(value not in task_ids for value in claims)
    ):
        raise ValidationError("agent team turn claim_task_ids are invalid")
    if turn["status"] == "continue" and claims:
        raise ValidationError("continuing agent team turns cannot claim another task")
    if schema == AGENT_TEAM_TURN_SCHEMA:
        proposals = turn.get("task_proposals")
        if not isinstance(proposals, list) or len(proposals) > MAX_AGENT_TEAM_TASK_PROPOSALS_PER_TURN:
            raise ValidationError("agent team turn task proposals exceed their limit")
        known_task_ids = list(task_ids)
        for proposal in proposals:
            if not isinstance(proposal, dict) or set(proposal) != AGENT_TEAM_TASK_PROPOSAL_FIELDS:
                raise ValidationError("agent team task proposal has invalid fields")
            proposal_id = proposal.get("id")
            _validate_id(proposal_id, "agent team task proposal id")
            if proposal_id in known_task_ids:
                raise ValidationError("agent team task proposal id already exists")
            _validate_text(
                proposal.get("description"),
                "agent team task proposal description",
                MAX_AGENT_TEAM_TASK_DESCRIPTION_CHARS,
            )
            if proposal.get("assignee") is not None and proposal["assignee"] not in member_ids:
                raise ValidationError("agent team task proposal assignee is invalid")
            dependencies = proposal.get("depends_on")
            if (
                not isinstance(dependencies, list)
                or len(dependencies) != len(set(dependencies))
                or not all(isinstance(value, str) and value in known_task_ids for value in dependencies)
            ):
                raise ValidationError("agent team task proposal dependencies are invalid or out of order")
            known_task_ids.append(proposal_id)
        if turn["status"] == "continue" and proposals:
            raise ValidationError("continuing agent team turns cannot propose tasks")
        if claims and proposals:
            raise ValidationError("agent team turns cannot claim and propose tasks together")


def prepare_agent_team_task_proposals(
    step: Dict,
    state: Dict,
    member_id: str,
    current_task_id: str,
    turn: Dict,
    *,
    max_workers: Optional[int] = None,
) -> List[Dict]:
    proposals = turn.get("task_proposals", [])
    if not proposals:
        return []
    if state.get("schema") not in {
        AGENT_TEAM_STATE_SCHEMA_V2,
        AGENT_TEAM_STATE_SCHEMA_V3,
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    }:
        raise ValidationError("legacy agent team state cannot accept dynamic tasks")
    member = next((value for value in state["members"] if value["id"] == member_id), None)
    if member is None or not member["lead"]:
        raise ValidationError("only the configured agent team lead may propose tasks")
    if turn["status"] != "completed":
        raise ValidationError("agent team task proposals require a completed lead turn")
    if any(
        proposal["id"].startswith(prefix)
        for proposal in proposals
        for prefix in (AGENT_TEAM_MESSAGE_TASK_PREFIX, AGENT_TEAM_OPERATOR_REPLY_TASK_PREFIX)
    ):
        raise ValidationError("agent team task proposal uses a reserved task id")
    maximum = state["max_dynamic_tasks"]
    if maximum <= 0 or state["dynamic_tasks_added"] + len(proposals) > maximum:
        raise ValidationError("agent team dynamic task limit exceeded")
    worker_limit = step["max_workers"]
    if max_workers is not None:
        if not _strict_int(max_workers) or max_workers < 1:
            raise ValidationError("agent team runtime worker limit is invalid")
        worker_limit = min(worker_limit, max_workers)

    candidate = []
    for task in state["tasks"]:
        candidate.append(
            {
                "id": task["id"],
                "status": "completed" if task["id"] == current_task_id else task["status"],
                "assignee": task["assignee"],
                "depends_on": list(task["depends_on"]),
            }
        )
    prepared = []
    for proposal in proposals:
        record = {
            "id": proposal["id"],
            "description": proposal["description"],
            "origin": "proposed",
            "proposed_by": member_id,
            "proposed_round": state["round"],
            "status": "pending",
            "assignee": proposal["assignee"],
            "claimed_by": None,
            "depends_on": list(proposal["depends_on"]),
            "attempts": 0,
            "summary": None,
            "output_sha256": None,
            "completed_round": None,
        }
        if state["schema"] in {
            AGENT_TEAM_STATE_SCHEMA_V3,
            AGENT_TEAM_STATE_SCHEMA_V4,
            AGENT_TEAM_STATE_SCHEMA_V5,
            AGENT_TEAM_STATE_SCHEMA,
        }:
            record["operator_entry_id"] = None
        if state["schema"] in {AGENT_TEAM_STATE_SCHEMA_V5, AGENT_TEAM_STATE_SCHEMA}:
            record["message_id"] = None
            record["message_depth"] = None
        if state["schema"] == AGENT_TEAM_STATE_SCHEMA:
            record["operator_reply_sha256"] = None
        prepared.append(record)
        candidate.append(
            {
                "id": record["id"],
                "status": record["status"],
                "assignee": record["assignee"],
                "depends_on": list(record["depends_on"]),
            }
        )

    _validate_agent_team_candidate_capacity(
        state,
        candidate,
        worker_limit=worker_limit,
        reserved_turns=1,
        label="task proposals",
    )
    return prepared


def prepare_agent_team_message_tasks(
    step: Dict,
    state: Dict,
    member_id: str,
    current_task_id: str,
    turn: Dict,
    *,
    max_workers: Optional[int] = None,
) -> List[Dict]:
    policy = step.get("active_messaging")
    indexed_messages = [
        (offset, message)
        for offset, message in enumerate(turn.get("messages", []))
        if message.get("to") != "operator"
    ]
    if policy is None or not indexed_messages:
        return []
    if state.get("schema") not in {AGENT_TEAM_STATE_SCHEMA_V5, AGENT_TEAM_STATE_SCHEMA}:
        raise ValidationError("legacy agent team state cannot accept active messages")
    if turn["status"] != "completed":
        raise ValidationError("active agent team messages require a completed sender turn")
    if turn.get("claim_task_ids") or turn.get("task_proposals"):
        raise ValidationError(
            "active agent team messages cannot be combined with claims or task proposals"
        )
    if state["message_tasks_added"] >= state["max_message_tasks"]:
        raise ValidationError("agent team active message task limit exceeded")
    if len(state["tasks"]) >= MAX_AGENT_TEAM_TASKS:
        raise ValidationError("agent team active messages exceed the state task limit")

    member_order = [member["id"] for member in state["members"]]
    task_map = {task["id"]: task for task in state["tasks"]}
    current_task = task_map.get(current_task_id)
    if current_task is None:
        raise ValidationError("agent team active message sender task is unknown")
    parent_depth = current_task.get("message_depth") or 0
    depth = parent_depth + 1
    if depth > policy["max_depth"]:
        raise ValidationError("agent team active message depth limit exceeded")

    prepared = []
    observed_recipients = set()
    first_message_index = len(state["messages"]) + 1
    for offset, message in indexed_messages:
        recipient = message["to"]
        if recipient == "all":
            if not policy["allow_broadcast"]:
                raise ValidationError("agent team active message broadcast is disabled")
            recipients = [value for value in member_order if value != member_id]
        else:
            if recipient == member_id:
                raise ValidationError("agent team active messages cannot target their sender")
            recipients = [recipient]
        message_index = first_message_index + offset
        message_id = "message-%04d" % message_index
        for target in recipients:
            if target in observed_recipients:
                raise ValidationError(
                    "agent team active messages may target each recipient at most once per turn"
                )
            observed_recipients.add(target)
            task_id = _agent_team_message_task_id(
                message_index,
                member_order.index(target) + 1,
            )
            if task_id in task_map or any(task["id"] == task_id for task in prepared):
                raise ValidationError("agent team active message task identity is duplicated")
            prepared.append(
                {
                    "id": task_id,
                    "description": message["body"],
                    "origin": "message",
                    "proposed_by": member_id,
                    "proposed_round": state["round"],
                    "operator_entry_id": None,
                    "message_id": message_id,
                    "message_depth": depth,
                    **(
                        {"operator_reply_sha256": None}
                        if state["schema"] == AGENT_TEAM_STATE_SCHEMA
                        else {}
                    ),
                    "status": "pending",
                    "assignee": target,
                    "claimed_by": None,
                    "depends_on": [],
                    "attempts": 0,
                    "summary": None,
                    "output_sha256": None,
                    "completed_round": None,
                }
            )

    if state["message_tasks_added"] + len(prepared) > state["max_message_tasks"]:
        raise ValidationError("agent team active message task limit exceeded")
    if len(state["tasks"]) + len(prepared) > MAX_AGENT_TEAM_TASKS:
        raise ValidationError("agent team active messages exceed the state task limit")
    candidate = [dict(task, depends_on=list(task["depends_on"])) for task in state["tasks"]]
    candidate_task = next(task for task in candidate if task["id"] == current_task_id)
    candidate_task["status"] = "completed"
    candidate_task["claimed_by"] = None
    candidate.extend(prepared)
    worker_limit = step["max_workers"]
    if max_workers is not None:
        if not _strict_int(max_workers) or max_workers < 1:
            raise ValidationError("agent team runtime worker limit is invalid")
        worker_limit = min(worker_limit, max_workers)
    _validate_agent_team_candidate_capacity(
        state,
        candidate,
        worker_limit=worker_limit,
        reserved_turns=1,
        label="active message tasks",
    )
    return prepared


def prepare_agent_team_operator_question_tasks(
    step: Dict,
    state: Dict,
    member_id: str,
    current_task_id: str,
    turn: Dict,
    *,
    max_workers: Optional[int] = None,
) -> List[Dict]:
    policy = step.get("operator_chat")
    indexed_questions = [
        (offset, message)
        for offset, message in enumerate(turn.get("messages", []))
        if message.get("to") == "operator"
    ]
    if policy is None or not indexed_questions:
        return []
    if state.get("schema") != AGENT_TEAM_STATE_SCHEMA:
        raise ValidationError("legacy agent team state cannot accept operator questions")
    if turn["status"] != "completed":
        raise ValidationError("agent team operator questions require a completed sender turn")
    if turn.get("claim_task_ids") or turn.get("task_proposals"):
        raise ValidationError(
            "agent team operator questions cannot be combined with claims or task proposals"
        )
    if state["operator_questions_added"] + len(indexed_questions) > state["max_operator_questions"]:
        raise ValidationError("agent team operator question limit exceeded")
    if len(state["tasks"]) + len(indexed_questions) > MAX_AGENT_TEAM_TASKS:
        raise ValidationError("agent team operator questions exceed the state task limit")

    task_map = {task["id"]: task for task in state["tasks"]}
    if current_task_id not in task_map:
        raise ValidationError("agent team operator question sender task is unknown")
    first_message_index = len(state["messages"]) + 1
    prepared = []
    for offset, message in indexed_questions:
        message_index = first_message_index + offset
        message_id = "message-%04d" % message_index
        task_id = _agent_team_operator_reply_task_id(message_index)
        if task_id in task_map or any(task["id"] == task_id for task in prepared):
            raise ValidationError("agent team operator reply task identity is duplicated")
        prepared.append(
            {
                "id": task_id,
                "description": message["body"],
                "origin": "operator-reply",
                "proposed_by": member_id,
                "proposed_round": state["round"],
                "operator_entry_id": None,
                "message_id": message_id,
                "message_depth": None,
                "operator_reply_sha256": None,
                "status": "waiting",
                "assignee": member_id,
                "claimed_by": None,
                "depends_on": [],
                "attempts": 0,
                "summary": None,
                "output_sha256": None,
                "completed_round": None,
            }
        )

    candidate = [dict(task, depends_on=list(task["depends_on"])) for task in state["tasks"]]
    candidate_task = next(task for task in candidate if task["id"] == current_task_id)
    candidate_task["status"] = "completed"
    candidate_task["claimed_by"] = None
    candidate.extend(prepared)
    worker_limit = step["max_workers"]
    if max_workers is not None:
        if not _strict_int(max_workers) or max_workers < 1:
            raise ValidationError("agent team runtime worker limit is invalid")
        worker_limit = min(worker_limit, max_workers)
    _validate_agent_team_candidate_capacity(
        state,
        candidate,
        worker_limit=worker_limit,
        reserved_turns=1,
        label="operator reply tasks",
    )
    return prepared


def prepare_agent_team_operator_tasks(
    step: Dict,
    state: Dict,
    entries: List[Dict],
    *,
    max_workers: Optional[int] = None,
    assume_claimed_complete: bool = False,
) -> List[Dict]:
    if not entries:
        return []
    if state.get("schema") not in {
        AGENT_TEAM_STATE_SCHEMA_V3,
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    }:
        raise ValidationError("legacy agent team state cannot accept operator tasks")
    if state["operator_tasks_added"] + len(entries) > state["max_operator_tasks"]:
        raise ValidationError("agent team operator task limit exceeded")
    if len(state["tasks"]) + len(entries) > MAX_AGENT_TEAM_TASKS:
        raise ValidationError("agent team operator tasks exceed the state task limit")
    member_ids = {member["id"] for member in state["members"]}
    known_task_ids = {task["id"] for task in state["tasks"]}
    known_entry_ids = {
        task["operator_entry_id"]
        for task in state["tasks"]
        if task["origin"] == "operator"
    }
    candidate = [dict(task, depends_on=list(task["depends_on"])) for task in state["tasks"]]
    reserved_turns = 0
    if assume_claimed_complete:
        interrupt_targets = {
            (
                entry.get("member_id"),
                entry.get("interrupt_task_id"),
                entry.get("interrupt_round"),
            )
            for entry in entries
            if entry.get("delivery", "next-turn") == "interrupt-current"
        }
        if len(interrupt_targets) != sum(
            entry.get("delivery", "next-turn") == "interrupt-current"
            for entry in entries
        ):
            raise ValidationError("agent team operator interrupt target is duplicated")
        member_claims = {
            (member["id"], member["current_task_id"], state["round"])
            for member in state["members"]
            if member["current_task_id"] is not None
        }
        if not interrupt_targets.issubset(member_claims):
            raise ValidationError(
                "agent team operator interrupt target is not currently working"
            )
        for task in candidate:
            if task["status"] == "claimed":
                if interrupt_targets:
                    task["status"] = "pending"
                    task["claimed_by"] = None
                else:
                    task["status"] = "completed"
                reserved_turns += 1
    prepared = []
    for entry in entries:
        entry_id = entry.get("id")
        task_id = entry.get("task_id")
        member_id = entry.get("member_id")
        instruction = entry.get("instruction")
        delivery = entry.get("delivery", "next-turn")
        _validate_id(entry_id, "agent team operator entry id")
        _validate_id(task_id, "agent team operator task id")
        if entry_id in known_entry_ids or task_id in known_task_ids:
            raise ValidationError("agent team operator task identity is duplicated")
        if member_id not in member_ids:
            raise ValidationError("agent team operator task member is invalid")
        _validate_text(
            instruction,
            "agent team operator task instruction",
            MAX_AGENT_TEAM_MESSAGE_CHARS,
        )
        if delivery not in {"next-turn", "interrupt-current"}:
            raise ValidationError("agent team operator task delivery is invalid")
        if delivery == "interrupt-current" and state.get("schema") not in {
            AGENT_TEAM_STATE_SCHEMA_V4,
            AGENT_TEAM_STATE_SCHEMA_V5,
            AGENT_TEAM_STATE_SCHEMA,
        }:
            raise ValidationError("legacy agent team state cannot interrupt a turn")
        record = {
            "id": task_id,
            "description": instruction,
            "origin": "operator",
            "proposed_by": "operator",
            "proposed_round": state["round"],
            "operator_entry_id": entry_id,
            "status": "pending",
            "assignee": member_id,
            "claimed_by": None,
            "depends_on": [],
            "attempts": 0,
            "summary": None,
            "output_sha256": None,
            "completed_round": None,
        }
        if state["schema"] in {AGENT_TEAM_STATE_SCHEMA_V5, AGENT_TEAM_STATE_SCHEMA}:
            record["message_id"] = None
            record["message_depth"] = None
        if state["schema"] == AGENT_TEAM_STATE_SCHEMA:
            record["operator_reply_sha256"] = None
        prepared.append(record)
        candidate.append(record)
        known_entry_ids.add(entry_id)
        known_task_ids.add(task_id)
    worker_limit = step["max_workers"]
    if max_workers is not None:
        if not _strict_int(max_workers) or max_workers < 1:
            raise ValidationError("agent team runtime worker limit is invalid")
        worker_limit = min(worker_limit, max_workers)
    _validate_agent_team_candidate_capacity(
        state,
        candidate,
        worker_limit=worker_limit,
        reserved_turns=reserved_turns,
        label="operator tasks",
    )
    return prepared


def activate_agent_team_operator_reply(
    state: Dict,
    *,
    message_id: str,
    reply: str,
    reply_sha256: str,
) -> Dict:
    if state.get("schema") != AGENT_TEAM_STATE_SCHEMA:
        raise ValidationError("legacy agent team state cannot activate an operator reply")
    _validate_id(message_id, "agent team operator question message_id")
    _validate_text(reply, "agent team operator reply", MAX_AGENT_TEAM_MESSAGE_CHARS)
    if reply_sha256 != _sha256_text(reply):
        raise ValidationError("agent team operator reply hash is invalid")
    matches = [
        task
        for task in state["tasks"]
        if task.get("origin") == "operator-reply" and task.get("message_id") == message_id
    ]
    if len(matches) != 1:
        raise ValidationError("agent team operator question task binding is invalid")
    task = matches[0]
    if task["status"] != "waiting" or task.get("operator_reply_sha256") is not None:
        raise ValidationError("agent team operator question is not waiting for a reply")
    task["description"] = reply
    task["operator_reply_sha256"] = reply_sha256
    task["status"] = "pending"
    state["updated_at_utc"] = _utc_now()
    return task


def pending_agent_team_operator_question_ids(state: Dict) -> List[str]:
    if state.get("schema") != AGENT_TEAM_STATE_SCHEMA:
        return []
    return [
        task["message_id"]
        for task in state["tasks"]
        if task.get("origin") == "operator-reply" and task.get("status") == "waiting"
    ]


def _validate_agent_team_candidate_capacity(
    state: Dict,
    candidate: List[Dict],
    *,
    worker_limit: int,
    reserved_turns: int,
    label: str,
) -> None:
    remaining_rounds = state["max_rounds"] - state["round"]
    unfinished = [task for task in candidate if task["status"] != "completed"]
    if remaining_rounds < 1 or len(unfinished) > remaining_rounds * worker_limit:
        raise ValidationError("agent team %s exceed remaining round capacity" % label)
    for assignee in [member["id"] for member in state["members"]]:
        if sum(task["assignee"] == assignee for task in unfinished) > remaining_rounds:
            raise ValidationError("agent team %s overload one member's remaining rounds" % label)
    depths = {}
    for task in candidate:
        if task["status"] == "completed":
            depths[task["id"]] = 0
        else:
            depths[task["id"]] = 1 + max(
                (depths[dependency] for dependency in task["depends_on"]),
                default=0,
            )
    if max((depths[task["id"]] for task in unfinished), default=0) > remaining_rounds:
        raise ValidationError("agent team %s exceed remaining dependency depth" % label)
    remaining_turn_slots = state["max_turns"] - len(state["turns"]) - reserved_turns
    if len(unfinished) > remaining_turn_slots:
        raise ValidationError("agent team %s exceed remaining turn capacity" % label)
    remaining_tokens = state["max_total_tokens"] - state["authorized_tokens"]
    if len(unfinished) * MIN_CODEX_RUNTIME_TOKEN_CAP > remaining_tokens:
        raise ValidationError("agent team %s exceed remaining minimum token funding" % label)


def apply_agent_team_turn(
    state: Dict,
    *,
    member_id: str,
    task_id: str,
    turn: Dict,
    output: str,
    output_sha256: str,
    session_id: str,
    started_at_utc: str,
    finished_at_utc: str,
    max_tokens: int,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    total_tokens: Optional[int],
    step: Optional[Dict] = None,
    max_workers: Optional[int] = None,
) -> None:
    validate_agent_team_turn(
        turn,
        task_id,
        [member["id"] for member in state["members"]],
        [task["id"] for task in state["tasks"]],
        expected_schema=agent_team_turn_schema_for_state(state),
        allow_operator=bool(step is not None and step.get("operator_chat") is not None),
    )
    member_map = {member["id"]: member for member in state["members"]}
    task_map = {task["id"]: task for task in state["tasks"]}
    member = member_map.get(member_id)
    task = task_map.get(task_id)
    if member is None or task is None:
        raise ValidationError("agent team turn references unknown state")
    if member["current_task_id"] != task_id or task["claimed_by"] != member_id:
        raise ValidationError("agent team turn does not match the active claim")
    prepared_proposals = []
    if turn.get("task_proposals"):
        if step is None:
            raise ValidationError("agent team task proposals require workflow step context")
        prepared_proposals = prepare_agent_team_task_proposals(
            step,
            state,
            member_id,
            task_id,
            turn,
            max_workers=max_workers,
        )
    prepared_message_tasks = []
    if turn.get("messages") and step is not None and step.get("active_messaging") is not None:
        prepared_message_tasks = prepare_agent_team_message_tasks(
            step,
            state,
            member_id,
            task_id,
            turn,
            max_workers=max_workers,
        )
    prepared_operator_questions = []
    if turn.get("messages") and step is not None and step.get("operator_chat") is not None:
        prepared_operator_questions = prepare_agent_team_operator_question_tasks(
            step,
            state,
            member_id,
            task_id,
            turn,
            max_workers=max_workers,
        )
    if prepared_message_tasks and prepared_operator_questions:
        candidate = [dict(value, depends_on=list(value["depends_on"])) for value in state["tasks"]]
        candidate_task = next(value for value in candidate if value["id"] == task_id)
        candidate_task["status"] = "completed"
        candidate_task["claimed_by"] = None
        candidate.extend(prepared_message_tasks)
        candidate.extend(prepared_operator_questions)
        worker_limit = step["max_workers"]
        if max_workers is not None:
            if not _strict_int(max_workers) or max_workers < 1:
                raise ValidationError("agent team runtime worker limit is invalid")
            worker_limit = min(worker_limit, max_workers)
        _validate_agent_team_candidate_capacity(
            state,
            candidate,
            worker_limit=worker_limit,
            reserved_turns=1,
            label="message and operator reply tasks",
        )
    if not isinstance(output, str) or not output:
        raise ValidationError("agent team turn output is invalid")
    require_no_path_escape(output)
    _validate_sha256(output_sha256, "agent team turn output hash")
    _validate_uuid(session_id, "agent team member session_id")
    if member["session_id"] is not None and member["session_id"] != session_id:
        raise ValidationError("agent team member session changed")
    _validate_timestamp(started_at_utc, "agent team turn started_at_utc")
    _validate_timestamp(finished_at_utc, "agent team turn finished_at_utc")
    if not _strict_int(max_tokens) or not MIN_CODEX_RUNTIME_TOKEN_CAP <= max_tokens <= 10**12:
        raise ValidationError("agent team turn max_tokens is invalid")
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    for field, value in usage.items():
        if value is not None and (not _strict_int(value) or not 0 <= value <= 10**12):
            raise ValidationError("agent team turn %s is invalid" % field)
    if input_tokens is not None and output_tokens is not None:
        if total_tokens != input_tokens + output_tokens:
            raise ValidationError("agent team turn token totals are inconsistent")
    if len(state["messages"]) + len(turn["messages"]) > MAX_AGENT_TEAM_MESSAGES:
        raise ValidationError("agent team message limit exceeded")
    if len(state["turns"]) >= state["max_turns"]:
        raise ValidationError("agent team turn limit exceeded")
    turn_key = (state["round"], member_id, task_id)
    if any(
        (record["round"], record["member_id"], record["task_id"]) == turn_key
        or record["output"] == output
        for record in state["turns"]
    ):
        raise ValidationError("agent team turn identity is duplicated")
    member["session_id"] = session_id
    member["turns"] += 1
    member["status"] = "idle"
    member["last_error"] = None
    member["claim_task_ids"] = list(turn["claim_task_ids"])
    task["attempts"] += 1
    task["summary"] = turn["summary"]
    task["output_sha256"] = output_sha256
    if turn["status"] == "completed":
        task["status"] = "completed"
        task["completed_round"] = state["round"]
        task["claimed_by"] = None
        member["current_task_id"] = None
    else:
        task["status"] = "claimed"
        task["completed_round"] = None
        member["status"] = "working"
    for message in turn["messages"]:
        body = message["body"]
        state["messages"].append(
            {
                "id": "message-%04d" % (len(state["messages"]) + 1),
                "round": state["round"],
                "from": member_id,
                "to": message["to"],
                "body": body,
                "body_sha256": _sha256_text(body),
            }
        )
    turn_record = {
        "round": state["round"],
        "member_id": member_id,
        "task_id": task_id,
        "status": turn["status"],
        "output": output,
        "output_sha256": output_sha256,
        "session_id_sha256": _sha256_text(session_id),
        "started_at_utc": started_at_utc,
        "finished_at_utc": finished_at_utc,
        "max_tokens": max_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    if state["schema"] in {
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    }:
        turn_record["interruption_entry_id"] = None
        turn_record["interruption_instruction_sha256"] = None
    state["turns"].append(turn_record)
    if prepared_proposals:
        state["tasks"].extend(prepared_proposals)
        state["dynamic_tasks_added"] += len(prepared_proposals)
    if prepared_message_tasks:
        state["tasks"].extend(prepared_message_tasks)
        state["message_tasks_added"] += len(prepared_message_tasks)
    if prepared_operator_questions:
        state["tasks"].extend(prepared_operator_questions)
        state["operator_questions_added"] += len(prepared_operator_questions)
    state["updated_at_utc"] = _utc_now()


def build_agent_team_interruption(
    *,
    task_id: str,
    operator_entry_id: str,
    instruction_sha256: str,
    session_id: str,
) -> Dict:
    value = {
        "schema": AGENT_TEAM_INTERRUPTION_SCHEMA,
        "task_id": task_id,
        "status": "interrupted",
        "operator_entry_id": operator_entry_id,
        "instruction_sha256": instruction_sha256,
        "session_id_sha256": _sha256_text(session_id),
    }
    validate_agent_team_interruption(value, task_id=task_id)
    return value


def parse_agent_team_interruption(text: str, *, task_id: str) -> Dict:
    if not isinstance(text, str):
        raise ValidationError("agent team interruption must be text")
    if len(text.encode("utf-8")) > MAX_AGENT_TEAM_TURN_BYTES:
        raise ValidationError(
            "agent team interruption exceeds %d bytes" % MAX_AGENT_TEAM_TURN_BYTES
        )
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError(
            "agent team interruption is invalid JSON: %s" % exc.__class__.__name__
        )
    validate_agent_team_interruption(value, task_id=task_id)
    return dict(value)


def validate_agent_team_interruption(
    value: Dict,
    *,
    task_id: Optional[str] = None,
) -> None:
    if not isinstance(value, dict) or set(value) != AGENT_TEAM_INTERRUPTION_FIELDS:
        raise ValidationError("agent team interruption has invalid fields")
    if value.get("schema") != AGENT_TEAM_INTERRUPTION_SCHEMA:
        raise ValidationError("agent team interruption schema is invalid")
    _validate_id(value.get("task_id"), "agent team interruption task_id")
    if task_id is not None and value.get("task_id") != task_id:
        raise ValidationError("agent team interruption task binding changed")
    if value.get("status") != "interrupted":
        raise ValidationError("agent team interruption status is invalid")
    _validate_id(value.get("operator_entry_id"), "agent team interruption operator entry")
    _validate_sha256(
        value.get("instruction_sha256"),
        "agent team interruption instruction hash",
    )
    _validate_sha256(
        value.get("session_id_sha256"),
        "agent team interruption session hash",
    )


def load_agent_team_interruption(path: Path) -> Dict:
    reject_symlink_path(path, "agent team interruption")
    text = read_regular_text_file_no_follow(
        path,
        "agent team interruption",
        MAX_AGENT_TEAM_TURN_BYTES,
    )
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError(
            "agent team interruption is invalid JSON: %s" % exc.__class__.__name__
        )
    validate_agent_team_interruption(value)
    return value


def agent_team_interruption_summary(value: Dict) -> Dict:
    validate_agent_team_interruption(value)
    return dict(value)


def apply_agent_team_interruption(
    state: Dict,
    *,
    member_id: str,
    task_id: str,
    interruption: Dict,
    output: str,
    output_sha256: str,
    session_id: str,
    started_at_utc: str,
    finished_at_utc: str,
    max_tokens: int,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    total_tokens: Optional[int],
) -> None:
    if state.get("schema") not in {
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    }:
        raise ValidationError("legacy agent team state cannot record an interruption")
    validate_agent_team_interruption(interruption, task_id=task_id)
    if interruption["session_id_sha256"] != _sha256_text(session_id):
        raise ValidationError("agent team interruption session binding changed")
    member_map = {member["id"]: member for member in state["members"]}
    task_map = {task["id"]: task for task in state["tasks"]}
    member = member_map.get(member_id)
    task = task_map.get(task_id)
    if member is None or task is None:
        raise ValidationError("agent team interruption references unknown state")
    if member["current_task_id"] != task_id or task["claimed_by"] != member_id:
        raise ValidationError("agent team interruption does not match the active claim")
    if not isinstance(output, str) or not output:
        raise ValidationError("agent team interruption output is invalid")
    require_no_path_escape(output)
    _validate_sha256(output_sha256, "agent team interruption output hash")
    _validate_uuid(session_id, "agent team interrupted session_id")
    if member["session_id"] is not None and member["session_id"] != session_id:
        raise ValidationError("agent team interrupted member session changed")
    _validate_timestamp(started_at_utc, "agent team interruption started_at_utc")
    _validate_timestamp(finished_at_utc, "agent team interruption finished_at_utc")
    if not _strict_int(max_tokens) or not MIN_CODEX_RUNTIME_TOKEN_CAP <= max_tokens <= 10**12:
        raise ValidationError("agent team interruption max_tokens is invalid")
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    for field, value in usage.items():
        if value is not None and (not _strict_int(value) or not 0 <= value <= 10**12):
            raise ValidationError("agent team interruption %s is invalid" % field)
    if input_tokens is not None and output_tokens is not None:
        if total_tokens != input_tokens + output_tokens:
            raise ValidationError("agent team interruption token totals are inconsistent")
    if len(state["turns"]) >= state["max_turns"]:
        raise ValidationError("agent team turn limit exceeded")
    turn_key = (state["round"], member_id, task_id)
    if any(
        (record["round"], record["member_id"], record["task_id"]) == turn_key
        or record["output"] == output
        or record.get("interruption_entry_id") == interruption["operator_entry_id"]
        for record in state["turns"]
    ):
        raise ValidationError("agent team interruption identity is duplicated")

    member["session_id"] = session_id
    member["turns"] += 1
    member["status"] = "idle"
    member["current_task_id"] = None
    member["claim_task_ids"] = []
    member["last_error"] = "OperatorInterrupted"
    task["attempts"] += 1
    task["status"] = "pending"
    task["claimed_by"] = None
    task["summary"] = AGENT_TEAM_INTERRUPTION_SUMMARY
    task["output_sha256"] = output_sha256
    task["completed_round"] = None
    state["turns"].append(
        {
            "round": state["round"],
            "member_id": member_id,
            "task_id": task_id,
            "status": "interrupted",
            "output": output,
            "output_sha256": output_sha256,
            "session_id_sha256": _sha256_text(session_id),
            "started_at_utc": started_at_utc,
            "finished_at_utc": finished_at_utc,
            "max_tokens": max_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "interruption_entry_id": interruption["operator_entry_id"],
            "interruption_instruction_sha256": interruption["instruction_sha256"],
        }
    )
    state["updated_at_utc"] = _utc_now()


def validate_agent_team_state(
    state: Dict,
    source: str = "agent team state",
    *,
    step: Optional[Dict] = None,
    workflow_fingerprint: Optional[str] = None,
    generation: Optional[int] = None,
) -> None:
    if not isinstance(state, dict):
        raise ValidationError("%s has invalid fields" % source)
    state_schema = state.get("schema")
    if state_schema not in {
        AGENT_TEAM_STATE_SCHEMA_V1,
        AGENT_TEAM_STATE_SCHEMA_V2,
        AGENT_TEAM_STATE_SCHEMA_V3,
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    }:
        raise ValidationError("%s schema is invalid" % source)
    state_fields = (
        AGENT_TEAM_STATE_FIELDS_V1
        if state_schema == AGENT_TEAM_STATE_SCHEMA_V1
        else AGENT_TEAM_STATE_FIELDS_V2
        if state_schema == AGENT_TEAM_STATE_SCHEMA_V2
        else AGENT_TEAM_STATE_FIELDS_V3
        if state_schema == AGENT_TEAM_STATE_SCHEMA_V3
        else AGENT_TEAM_STATE_FIELDS_V4
        if state_schema == AGENT_TEAM_STATE_SCHEMA_V4
        else AGENT_TEAM_STATE_FIELDS_V5
        if state_schema == AGENT_TEAM_STATE_SCHEMA_V5
        else AGENT_TEAM_STATE_FIELDS
    )
    if set(state) != state_fields:
        raise ValidationError("%s has invalid fields" % source)
    has_dynamic_tasks = state_schema in {
        AGENT_TEAM_STATE_SCHEMA_V2,
        AGENT_TEAM_STATE_SCHEMA_V3,
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    }
    has_operator_tasks = state_schema in {
        AGENT_TEAM_STATE_SCHEMA_V3,
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    }
    has_interruptions = state_schema in {
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    }
    has_message_tasks = state_schema in {AGENT_TEAM_STATE_SCHEMA_V5, AGENT_TEAM_STATE_SCHEMA}
    has_operator_chat = state_schema == AGENT_TEAM_STATE_SCHEMA
    _validate_id(state.get("step_id"), "%s step_id" % source)
    _validate_sha256(state.get("workflow_fingerprint"), "%s workflow_fingerprint" % source)
    if workflow_fingerprint is not None and state["workflow_fingerprint"] != workflow_fingerprint:
        raise ValidationError("%s workflow fingerprint changed" % source)
    _validate_state_int(state, "generation", 0, MAX_AGENT_TEAM_GENERATION, source)
    if generation is not None:
        if not _strict_int(generation) or not 0 <= generation <= MAX_AGENT_TEAM_GENERATION:
            raise ValidationError("%s expected generation is invalid" % source)
        if state["generation"] != generation:
            raise ValidationError("%s generation changed" % source)
    if state.get("status") not in AGENT_TEAM_STATUSES:
        raise ValidationError("%s status is invalid" % source)
    _validate_state_int(state, "round", 0, MAX_AGENT_TEAM_ROUNDS, source)
    _validate_state_int(state, "max_rounds", 1, MAX_AGENT_TEAM_ROUNDS, source)
    _validate_state_int(state, "max_turns", 1, MAX_AGENT_TEAM_TURNS, source)
    _validate_state_int(state, "max_total_tokens", MIN_CODEX_RUNTIME_TOKEN_CAP, 10**12, source)
    _validate_state_int(state, "authorized_tokens", 0, state["max_total_tokens"], source)
    if has_dynamic_tasks:
        _validate_state_int(state, "max_dynamic_tasks", 0, MAX_AGENT_TEAM_DYNAMIC_TASKS, source)
        _validate_state_int(state, "dynamic_tasks_added", 0, state["max_dynamic_tasks"], source)
    if has_operator_tasks:
        _validate_state_int(state, "max_operator_tasks", 0, MAX_AGENT_TEAM_OPERATOR_TASKS, source)
        _validate_state_int(state, "operator_tasks_added", 0, state["max_operator_tasks"], source)
    if has_message_tasks:
        _validate_state_int(state, "max_message_tasks", 0, MAX_AGENT_TEAM_MESSAGE_TASKS, source)
        _validate_state_int(state, "message_tasks_added", 0, state["max_message_tasks"], source)
    if has_operator_chat:
        _validate_state_int(
            state,
            "max_operator_questions",
            0,
            MAX_AGENT_TEAM_OPERATOR_QUESTIONS,
            source,
        )
        _validate_state_int(
            state,
            "operator_questions_added",
            0,
            state["max_operator_questions"],
            source,
        )
    if state["round"] > state["max_rounds"]:
        raise ValidationError("%s round exceeds max_rounds" % source)
    for field in ("created_at_utc", "updated_at_utc"):
        _validate_timestamp(state.get(field), "%s %s" % (source, field))
    members = state.get("members")
    if (
        not isinstance(members, list)
        or not MIN_AGENT_TEAM_MEMBERS <= len(members) <= MAX_AGENT_TEAM_MEMBERS
    ):
        raise ValidationError("%s members are invalid" % source)
    member_ids = []
    lead_count = 0
    for member in members:
        if not isinstance(member, dict) or set(member) != AGENT_TEAM_STATE_MEMBER_FIELDS:
            raise ValidationError("%s member has invalid fields" % source)
        member_id = member.get("id")
        _validate_id(member_id, "%s member id" % source)
        if member_id == "all":
            raise ValidationError("%s member id is reserved for broadcast routing" % source)
        if member_id == "operator":
            raise ValidationError("%s member id is reserved for operator routing" % source)
        if member_id in member_ids:
            raise ValidationError("%s contains duplicate members" % source)
        member_ids.append(member_id)
        _validate_text(member.get("role"), "%s member role" % source, MAX_AGENT_TEAM_ROLE_CHARS)
        _validate_id(member.get("agent_profile"), "%s member agent_profile" % source)
        if not isinstance(member.get("lead"), bool):
            raise ValidationError("%s member lead is invalid" % source)
        lead_count += int(member["lead"])
        if member.get("status") not in {"idle", "working"}:
            raise ValidationError("%s member status is invalid" % source)
        if member.get("session_id") is not None:
            _validate_uuid(member["session_id"], "%s member session_id" % source)
        _validate_state_int(member, "turns", 0, state["max_turns"], source)
        current = member.get("current_task_id")
        if current is not None:
            _validate_id(current, "%s member current_task_id" % source)
        if (current is None and member["status"] != "idle") or (
            current is not None and member["status"] != "working"
        ):
            raise ValidationError("%s member work status is inconsistent" % source)
        claims = member.get("claim_task_ids")
        if (
            not isinstance(claims, list)
            or len(claims) > MAX_AGENT_TEAM_CLAIMS_PER_TURN
            or len(claims) != len(set(claims))
            or not all(isinstance(value, str) for value in claims)
        ):
            raise ValidationError("%s member claims are invalid" % source)
        if member.get("last_error") is not None:
            _validate_text(member["last_error"], "%s member last_error" % source, 200)
        if (member["turns"] == 0) != (member["session_id"] is None):
            raise ValidationError("%s member session and turn state are inconsistent" % source)
    if lead_count != 1:
        raise ValidationError("%s must contain exactly one lead" % source)

    tasks = state.get("tasks")
    if not isinstance(tasks, list) or not tasks or len(tasks) > MAX_AGENT_TEAM_TASKS:
        raise ValidationError("%s tasks are invalid" % source)
    task_ids = []
    for task in tasks:
        task_fields = (
            AGENT_TEAM_STATE_TASK_FIELDS_V1
            if not has_dynamic_tasks
            else AGENT_TEAM_STATE_TASK_FIELDS_V2
            if not has_operator_tasks
            else AGENT_TEAM_STATE_TASK_FIELDS_V3
            if not has_message_tasks
            else AGENT_TEAM_STATE_TASK_FIELDS_V5
            if not has_operator_chat
            else AGENT_TEAM_STATE_TASK_FIELDS
        )
        if not isinstance(task, dict) or set(task) != task_fields:
            raise ValidationError("%s task has invalid fields" % source)
        task_id = task.get("id")
        _validate_id(task_id, "%s task id" % source)
        if task_id in task_ids:
            raise ValidationError("%s contains duplicate tasks" % source)
        task_ids.append(task_id)
        if has_dynamic_tasks:
            _validate_text(
                task.get("description"),
                "%s task description" % source,
                MAX_AGENT_TEAM_TASK_DESCRIPTION_CHARS,
            )
            origin = task.get("origin")
            allowed_origins = (
                {"static", "proposed", "operator", "message", "operator-reply"}
                if has_operator_chat
                else {"static", "proposed", "operator", "message"}
                if has_message_tasks
                else {"static", "proposed", "operator"}
                if has_operator_tasks
                else {"static", "proposed"}
            )
            if origin not in allowed_origins:
                raise ValidationError("%s task origin is invalid" % source)
            if origin == "static":
                if task.get("proposed_by") is not None or task.get("proposed_round") is not None:
                    raise ValidationError("%s static task has proposal metadata" % source)
            elif origin == "proposed":
                lead_ids = {member["id"] for member in members if member["lead"]}
                if task.get("proposed_by") not in lead_ids:
                    raise ValidationError("%s proposed task author is invalid" % source)
                proposed_round = task.get("proposed_round")
                if not _strict_int(proposed_round) or not 1 <= proposed_round <= state["round"]:
                    raise ValidationError("%s proposed task round is invalid" % source)
            elif origin == "operator":
                if task.get("proposed_by") != "operator":
                    raise ValidationError("%s operator task author is invalid" % source)
                proposed_round = task.get("proposed_round")
                if not _strict_int(proposed_round) or not 0 <= proposed_round <= state["round"]:
                    raise ValidationError("%s operator task round is invalid" % source)
            elif origin in {"message", "operator-reply"}:
                if task.get("proposed_by") not in member_ids:
                    raise ValidationError("%s routed task author is invalid" % source)
                proposed_round = task.get("proposed_round")
                if not _strict_int(proposed_round) or not 1 <= proposed_round <= state["round"]:
                    raise ValidationError("%s routed task round is invalid" % source)
            if has_operator_tasks:
                entry_id = task.get("operator_entry_id")
                if origin == "operator":
                    _validate_id(entry_id, "%s operator_entry_id" % source)
                elif entry_id is not None:
                    raise ValidationError("%s non-operator task has operator metadata" % source)
            if has_message_tasks:
                message_id = task.get("message_id")
                message_depth = task.get("message_depth")
                if origin in {"message", "operator-reply"}:
                    _validate_id(message_id, "%s message_id" % source)
                    if origin == "message":
                        if not _strict_int(message_depth) or not 1 <= message_depth <= MAX_AGENT_TEAM_MESSAGE_DEPTH:
                            raise ValidationError("%s message_depth is invalid" % source)
                        if task.get("assignee") is None or task.get("assignee") == task.get("proposed_by"):
                            raise ValidationError("%s message task recipient is invalid" % source)
                    elif message_depth is not None or task.get("assignee") != task.get("proposed_by"):
                        raise ValidationError("%s operator reply routing is invalid" % source)
                    if task.get("depends_on") != []:
                        raise ValidationError("%s routed task cannot have dependencies" % source)
                elif message_id is not None or message_depth is not None:
                    raise ValidationError("%s non-message task has message metadata" % source)
            if has_operator_chat:
                reply_sha256 = task.get("operator_reply_sha256")
                if origin == "operator-reply":
                    if task.get("status") == "waiting":
                        if reply_sha256 is not None:
                            raise ValidationError("%s waiting operator reply has answer metadata" % source)
                    else:
                        _validate_sha256(reply_sha256, "%s operator_reply_sha256" % source)
                        if reply_sha256 != _sha256_text(task["description"]):
                            raise ValidationError("%s operator reply text binding changed" % source)
                elif reply_sha256 is not None:
                    raise ValidationError("%s non-operator-reply task has reply metadata" % source)
        allowed_statuses = AGENT_TEAM_TASK_STATUSES if has_operator_chat else AGENT_TEAM_TASK_STATUSES_V5
        if task.get("status") not in allowed_statuses:
            raise ValidationError("%s task status is invalid" % source)
        if task.get("assignee") is not None and task["assignee"] not in member_ids:
            raise ValidationError("%s task assignee is invalid" % source)
        if task.get("claimed_by") is not None and task["claimed_by"] not in member_ids:
            raise ValidationError("%s task claimed_by is invalid" % source)
        dependencies = task.get("depends_on")
        if not isinstance(dependencies, list) or len(dependencies) != len(set(dependencies)):
            raise ValidationError("%s task dependencies are invalid" % source)
        _validate_state_int(task, "attempts", 0, state["max_rounds"], source)
        for field in ("summary", "output_sha256", "completed_round"):
            value = task.get(field)
            if field == "summary" and value is not None:
                _validate_text(value, "%s task summary" % source, MAX_AGENT_TEAM_SUMMARY_CHARS)
            elif field == "output_sha256" and value is not None:
                _validate_sha256(value, "%s task output_sha256" % source)
            elif field == "completed_round" and value is not None:
                if not _strict_int(value) or not 1 <= value <= state["max_rounds"]:
                    raise ValidationError("%s task completed_round is invalid" % source)
        if task["status"] == "claimed" and task["claimed_by"] is None:
            raise ValidationError("%s claimed task has no member" % source)
        if task["status"] in {"pending", "waiting"} and task["claimed_by"] is not None:
            raise ValidationError("%s unclaimed task cannot have a member claim" % source)
        if task["status"] == "completed" and (
            task["claimed_by"] is not None
            or task["summary"] is None
            or task["output_sha256"] is None
            or task["completed_round"] is None
        ):
            raise ValidationError("%s completed task evidence is incomplete" % source)
        if task["status"] != "completed" and task["completed_round"] is not None:
            raise ValidationError("%s unfinished task has a completion round" % source)
        if task["completed_round"] is not None and task["completed_round"] > state["round"]:
            raise ValidationError("%s task completion round exceeds the current round" % source)
        if task["attempts"] == 0 and (task["summary"] is not None or task["output_sha256"] is not None):
            raise ValidationError("%s unattempted task has output evidence" % source)
        if task["attempts"] > 0 and (task["summary"] is None or task["output_sha256"] is None):
            raise ValidationError("%s attempted task is missing output evidence" % source)
        if task["status"] == "waiting" and (
            task.get("origin") != "operator-reply"
            or task["attempts"] != 0
            or task["summary"] is not None
            or task["output_sha256"] is not None
        ):
            raise ValidationError("%s waiting task evidence is invalid" % source)
    if any(dependency not in task_ids for task in tasks for dependency in task["depends_on"]):
        raise ValidationError("%s task dependency is unknown" % source)
    if has_dynamic_tasks:
        origins = [task["origin"] for task in tasks]
        static_count = origins.count("static")
        if static_count < 1 or origins[:static_count] != ["static"] * static_count:
            raise ValidationError("%s static tasks must precede added tasks" % source)
        proposed_count = sum(task["origin"] == "proposed" for task in tasks)
        if proposed_count != state["dynamic_tasks_added"]:
            raise ValidationError("%s dynamic task total is inconsistent" % source)
        if has_operator_tasks:
            operator_count = sum(task["origin"] == "operator" for task in tasks)
            if operator_count != state["operator_tasks_added"]:
                raise ValidationError("%s operator task total is inconsistent" % source)
            operator_entry_ids = [
                task["operator_entry_id"] for task in tasks if task["origin"] == "operator"
            ]
            if len(operator_entry_ids) != len(set(operator_entry_ids)):
                raise ValidationError("%s operator entry identity is duplicated" % source)
        if has_message_tasks:
            message_count = sum(task["origin"] == "message" for task in tasks)
            if message_count != state["message_tasks_added"]:
                raise ValidationError("%s message task total is inconsistent" % source)
        if has_operator_chat:
            question_count = sum(task["origin"] == "operator-reply" for task in tasks)
            if question_count != state["operator_questions_added"]:
                raise ValidationError("%s operator question total is inconsistent" % source)
    for member in members:
        if member["current_task_id"] is not None and member["current_task_id"] not in task_ids:
            raise ValidationError("%s member current task is unknown" % source)
        if any(task_id not in task_ids for task_id in member["claim_task_ids"]):
            raise ValidationError("%s member claim is unknown" % source)
    member_map = {member["id"]: member for member in members}
    task_map = {task["id"]: task for task in tasks}
    positions = {task_id: index for index, task_id in enumerate(task_ids)}
    for task in tasks:
        if any(positions[dependency] >= positions[task["id"]] for dependency in task["depends_on"]):
            raise ValidationError("%s task dependencies are not ordered" % source)
        if task["status"] in {"claimed", "completed"} and any(
            task_map[dependency]["status"] != "completed" for dependency in task["depends_on"]
        ):
            raise ValidationError("%s active or completed task has unfinished dependencies" % source)
        if task["status"] == "claimed":
            owner = member_map[task["claimed_by"]]
            if owner["current_task_id"] != task["id"]:
                raise ValidationError("%s task claim is not bound to its member" % source)
    for member in members:
        current = member["current_task_id"]
        if current is not None and (
            task_map[current]["status"] != "claimed" or task_map[current]["claimed_by"] != member["id"]
        ):
            raise ValidationError("%s member current task is not bound to its claim" % source)

    messages = state.get("messages")
    if not isinstance(messages, list) or len(messages) > MAX_AGENT_TEAM_MESSAGES:
        raise ValidationError("%s messages are invalid" % source)
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict) or set(message) != AGENT_TEAM_STATE_MESSAGE_FIELDS:
            raise ValidationError("%s message has invalid fields" % source)
        if message.get("id") != "message-%04d" % index:
            raise ValidationError("%s message id sequence is invalid" % source)
        _validate_state_int(message, "round", 1, state["max_rounds"], source)
        if message["round"] > state["round"]:
            raise ValidationError("%s message round exceeds the current round" % source)
        allowed_message_recipients = set(member_ids) | {"all"}
        if has_operator_chat and state["max_operator_questions"] > 0:
            allowed_message_recipients.add("operator")
        if (
            message.get("from") not in member_ids
            or message.get("to") not in allowed_message_recipients
        ):
            raise ValidationError("%s message routing is invalid" % source)
        _validate_text(message.get("body"), "%s message body" % source, MAX_AGENT_TEAM_MESSAGE_CHARS)
        if message.get("body_sha256") != _sha256_text(message["body"]):
            raise ValidationError("%s message body hash is invalid" % source)
    if has_message_tasks:
        message_map = {message["id"]: message for message in messages}
        message_ids = set(message_map)
        if any(
            task["message_id"] not in message_ids
            for task in tasks
            if task["origin"] == "message"
        ):
            raise ValidationError("%s message task references an unknown message" % source)
        observed_message_recipients = set()
        for task in tasks:
            if task["origin"] != "message":
                continue
            message = message_map[task["message_id"]]
            binding = (task["message_id"], task["assignee"])
            if binding in observed_message_recipients:
                raise ValidationError("%s message task recipient binding is duplicated" % source)
            observed_message_recipients.add(binding)
            if (
                message["from"] != task["proposed_by"]
                or message["round"] != task["proposed_round"]
                or message["to"] not in {task["assignee"], "all"}
            ):
                raise ValidationError("%s message task routing binding changed" % source)
            message_index = int(task["message_id"].split("-")[1])
            expected_task_id = _agent_team_message_task_id(
                message_index,
                member_ids.index(task["assignee"]) + 1,
            )
            if task["id"] != expected_task_id:
                raise ValidationError("%s message task identity changed" % source)
    if has_operator_chat:
        operator_messages = {
            message["id"]: message for message in messages if message["to"] == "operator"
        }
        operator_reply_tasks = {
            task["message_id"]: task
            for task in tasks
            if task["origin"] == "operator-reply"
        }
        if len(operator_reply_tasks) != sum(
            task["origin"] == "operator-reply" for task in tasks
        ):
            raise ValidationError("%s operator reply message binding is duplicated" % source)
        if set(operator_messages) != set(operator_reply_tasks):
            raise ValidationError("%s operator question task ledger is inconsistent" % source)
        if len(operator_messages) != state["operator_questions_added"]:
            raise ValidationError("%s operator question message total is inconsistent" % source)
        for message_id, message in operator_messages.items():
            task = operator_reply_tasks[message_id]
            message_index = int(message_id.split("-")[1])
            if (
                task["id"] != _agent_team_operator_reply_task_id(message_index)
                or task["proposed_by"] != message["from"]
                or task["assignee"] != message["from"]
                or task["proposed_round"] != message["round"]
                or task["operator_entry_id"] is not None
                or task["message_depth"] is not None
            ):
                raise ValidationError("%s operator reply task routing binding changed" % source)
            if task["status"] == "waiting" and task["description"] != message["body"]:
                raise ValidationError("%s waiting operator question text binding changed" % source)

    turns = state.get("turns")
    if not isinstance(turns, list) or len(turns) > state["max_turns"]:
        raise ValidationError("%s turns are invalid" % source)
    turn_counts = {member_id: 0 for member_id in member_ids}
    task_turns = {task_id: [] for task_id in task_ids}
    observed_turn_keys = set()
    observed_member_rounds = {}
    observed_outputs = set()
    observed_interruption_entries = set()
    prior_turn_round = None
    for turn in turns:
        turn_fields = (
            AGENT_TEAM_STATE_TURN_FIELDS_V4
            if has_interruptions
            else AGENT_TEAM_STATE_TURN_FIELDS
        )
        if not isinstance(turn, dict) or set(turn) != turn_fields:
            raise ValidationError("%s turn has invalid fields" % source)
        _validate_state_int(turn, "round", 1, state["max_rounds"], source)
        if turn["round"] > state["round"]:
            raise ValidationError("%s turn round exceeds the current round" % source)
        if turn.get("member_id") not in member_ids or turn.get("task_id") not in task_ids:
            raise ValidationError("%s turn binding is invalid" % source)
        allowed_turn_statuses = (
            AGENT_TEAM_STATE_TURN_STATUSES
            if has_interruptions
            else AGENT_TEAM_PROVIDER_TURN_STATUSES
        )
        if turn.get("status") not in allowed_turn_statuses:
            raise ValidationError("%s turn status is invalid" % source)
        interruption_entry_id = turn.get("interruption_entry_id")
        interruption_instruction_sha256 = turn.get("interruption_instruction_sha256")
        if has_interruptions:
            if turn["status"] == "interrupted":
                _validate_id(
                    interruption_entry_id,
                    "%s turn interruption_entry_id" % source,
                )
                if interruption_entry_id in observed_interruption_entries:
                    raise ValidationError(
                        "%s turn interruption identity is duplicated" % source
                    )
                observed_interruption_entries.add(interruption_entry_id)
                _validate_sha256(
                    interruption_instruction_sha256,
                    "%s turn interruption instruction hash" % source,
                )
            elif interruption_entry_id is not None:
                raise ValidationError(
                    "%s non-interrupted turn has interruption metadata" % source
                )
            elif interruption_instruction_sha256 is not None:
                raise ValidationError(
                    "%s non-interrupted turn has interruption instruction metadata" % source
                )
        output = turn.get("output")
        if not isinstance(output, str) or not output:
            raise ValidationError("%s turn output is invalid" % source)
        require_no_path_escape(output)
        turn_key = (turn["round"], turn["member_id"], turn["task_id"])
        if turn_key in observed_turn_keys or output in observed_outputs:
            raise ValidationError("%s turn identity is duplicated" % source)
        member_round_key = (turn["round"], turn["member_id"])
        if member_round_key in observed_member_rounds:
            raise ValidationError("%s member has multiple turns in one round" % source)
        if prior_turn_round is not None and turn["round"] < prior_turn_round:
            raise ValidationError("%s turn order is invalid" % source)
        observed_turn_keys.add(turn_key)
        observed_member_rounds[member_round_key] = turn
        observed_outputs.add(output)
        prior_turn_round = turn["round"]
        for field in ("output_sha256", "session_id_sha256"):
            _validate_sha256(turn.get(field), "%s turn %s" % (source, field))
        for field in ("started_at_utc", "finished_at_utc"):
            _validate_timestamp(turn.get(field), "%s turn %s" % (source, field))
        _validate_state_int(turn, "max_tokens", MIN_CODEX_RUNTIME_TOKEN_CAP, 10**12, source)
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            value = turn.get(field)
            if value is not None and (not _strict_int(value) or value < 0 or value > 10**12):
                raise ValidationError("%s turn %s is invalid" % (source, field))
        if turn["input_tokens"] is not None and turn["output_tokens"] is not None:
            if turn["total_tokens"] != turn["input_tokens"] + turn["output_tokens"]:
                raise ValidationError("%s turn token totals are inconsistent" % source)
        member = member_map[turn["member_id"]]
        if member["session_id"] is None or turn["session_id_sha256"] != _sha256_text(member["session_id"]):
            raise ValidationError("%s turn session binding is invalid" % source)
        turn_counts[turn["member_id"]] += 1
        task_turns[turn["task_id"]].append(turn)
    if any(member["turns"] != turn_counts[member["id"]] for member in members):
        raise ValidationError("%s member turn totals are inconsistent" % source)
    for message in messages:
        parent_turn = observed_member_rounds.get((message["round"], message["from"]))
        if parent_turn is None or parent_turn["status"] == "interrupted":
            raise ValidationError("%s message has no accepted sender turn" % source)
    if has_message_tasks:
        for task in tasks:
            if task["origin"] != "message":
                continue
            message = message_map[task["message_id"]]
            parent_turn = observed_member_rounds[(message["round"], message["from"])]
            parent_depth = task_map[parent_turn["task_id"]].get("message_depth") or 0
            if task["message_depth"] != parent_depth + 1:
                raise ValidationError("%s message task depth binding changed" % source)
    for task in tasks:
        task_history = task_turns[task["id"]]
        if task["attempts"] != len(task_history):
            raise ValidationError("%s task attempt total is inconsistent" % source)
        if task_history:
            latest = task_history[-1]
            if task["output_sha256"] != latest["output_sha256"]:
                raise ValidationError("%s task output binding is inconsistent" % source)
            if (task["status"] == "completed") != (latest["status"] == "completed"):
                raise ValidationError("%s task terminal status is inconsistent" % source)
            if task["status"] == "completed" and task["completed_round"] != latest["round"]:
                raise ValidationError("%s task completion evidence is inconsistent" % source)
    if state["status"] == "completed" and (
        any(task["status"] != "completed" for task in tasks)
        or any(member["current_task_id"] is not None for member in members)
    ):
        raise ValidationError("%s completed state has unfinished work" % source)
    if state["status"] == "running" and all(task["status"] == "completed" for task in tasks):
        raise ValidationError("%s running state has no unfinished work" % source)
    if step is not None:
        if state["step_id"] != step.get("id"):
            raise ValidationError("%s step binding changed" % source)
        if state["max_rounds"] != step.get("max_rounds"):
            raise ValidationError("%s round limit changed" % source)
        if state["max_total_tokens"] != step.get("max_total_tokens"):
            raise ValidationError("%s token limit changed" % source)
        if has_dynamic_tasks and state["max_dynamic_tasks"] != step.get("max_dynamic_tasks", 0):
            raise ValidationError("%s dynamic task limit changed" % source)
        if not has_dynamic_tasks and step.get("max_dynamic_tasks", 0) != 0:
            raise ValidationError("%s legacy state cannot bind dynamic task authority" % source)
        if has_operator_tasks and state["max_operator_tasks"] != step.get("max_operator_tasks", 0):
            raise ValidationError("%s operator task limit changed" % source)
        if not has_operator_tasks and step.get("max_operator_tasks", 0) != 0:
            raise ValidationError("%s legacy state cannot bind operator task authority" % source)
        expected_message_tasks = (step.get("active_messaging") or {}).get("max_tasks", 0)
        if has_message_tasks and state["max_message_tasks"] != expected_message_tasks:
            raise ValidationError("%s active message task limit changed" % source)
        if not has_message_tasks and expected_message_tasks != 0:
            raise ValidationError("%s legacy state cannot bind active message authority" % source)
        if has_message_tasks and step.get("active_messaging") is not None:
            messaging_policy = step["active_messaging"]
            maximum_depth = messaging_policy["max_depth"]
            if any(
                task["message_depth"] > maximum_depth
                for task in tasks
                if task["origin"] == "message"
            ):
                raise ValidationError("%s active message depth exceeds workflow policy" % source)
            if not messaging_policy["allow_broadcast"] and any(
                message_map[task["message_id"]]["to"] == "all"
                for task in tasks
                if task["origin"] == "message"
            ):
                raise ValidationError("%s active message broadcast exceeds workflow policy" % source)
            if any(
                turn["max_tokens"] > messaging_policy["max_tokens"]
                for turn in turns
                if task_map[turn["task_id"]].get("origin") == "message"
            ):
                raise ValidationError("%s active message turn token cap exceeds workflow policy" % source)
        expected_operator_questions = (step.get("operator_chat") or {}).get(
            "max_questions",
            0,
        )
        if has_operator_chat and state["max_operator_questions"] != expected_operator_questions:
            raise ValidationError("%s operator question limit changed" % source)
        if not has_operator_chat and expected_operator_questions != 0:
            raise ValidationError("%s legacy state cannot bind operator chat authority" % source)
        if has_operator_chat and step.get("operator_chat") is not None:
            operator_chat_policy = step["operator_chat"]
            if any(
                turn["max_tokens"] > operator_chat_policy["max_tokens"]
                for turn in turns
                if task_map[turn["task_id"]].get("origin") == "operator-reply"
            ):
                raise ValidationError("%s operator reply turn token cap exceeds workflow policy" % source)
        expected_max_turns = min(MAX_AGENT_TEAM_TURNS, step["max_rounds"] * len(step["members"]))
        if state["max_turns"] != expected_max_turns:
            raise ValidationError("%s turn limit changed" % source)
        member_bindings = [
            {field: member[field] for field in ("id", "role", "agent_profile", "lead")}
            for member in members
        ]
        expected_member_bindings = [
            {field: member[field] for field in ("id", "role", "agent_profile", "lead")}
            for member in step["members"]
        ]
        if member_bindings != expected_member_bindings:
            raise ValidationError("%s member definitions changed" % source)
        static_tasks = tasks if not has_dynamic_tasks else [task for task in tasks if task["origin"] == "static"]
        task_binding_fields = ("id", "assignee", "depends_on") if not has_dynamic_tasks else (
            "id",
            "description",
            "assignee",
            "depends_on",
        )
        task_bindings = [
            {field: task[field] for field in task_binding_fields}
            for task in static_tasks
        ]
        expected_task_bindings = [
            {field: task[field] for field in task_binding_fields}
            for task in step["tasks"]
        ]
        if task_bindings != expected_task_bindings:
            raise ValidationError("%s task definitions changed" % source)
        for turn in turns:
            expected_output = "%s/round-%03d/%s--%s.json" % (
                step["capture_dir"],
                turn["round"],
                turn["member_id"],
                turn["task_id"],
            )
            if turn["output"] != expected_output:
                raise ValidationError("%s turn output binding changed" % source)


def load_agent_team_state(
    path: Path,
    *,
    step: Optional[Dict] = None,
    workflow_fingerprint: Optional[str] = None,
    generation: Optional[int] = None,
) -> Dict:
    reject_symlink_path(path, "agent team state")
    text = read_regular_text_file_no_follow(path, "agent team state", MAX_AGENT_TEAM_STATE_BYTES)
    try:
        state = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("agent team state is invalid JSON: %s" % exc.__class__.__name__)
    validate_agent_team_state(
        state,
        source=str(path),
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=generation,
    )
    return state


def write_agent_team_state(path: Path, state: Dict) -> None:
    validate_agent_team_state(state)
    parent_fd = ensure_dir_no_follow(path.parent, "agent team state parent")
    os.close(parent_fd)
    text = json.dumps(state, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_AGENT_TEAM_STATE_BYTES:
        raise ValidationError("agent team state exceeds %d bytes" % MAX_AGENT_TEAM_STATE_BYTES)
    replace_text_file_no_follow(path, "agent team state", text, ".agent-team-state-")


def verify_agent_team_state_outputs(state: Dict, artifacts_dir: Path, step: Dict) -> None:
    validate_agent_team_state(state, step=step)
    member_ids = [member["id"] for member in state["members"]]
    all_task_ids = [task["id"] for task in state["tasks"]]
    if state["schema"] in {
        AGENT_TEAM_STATE_SCHEMA_V2,
        AGENT_TEAM_STATE_SCHEMA_V3,
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    }:
        known_task_ids = [
            task["id"]
            for task in state["tasks"]
            if task["origin"] in {"static", "operator"}
        ]
    else:
        known_task_ids = list(all_task_ids)
    expected_turn_schema = agent_team_turn_schema_for_state(state)
    latest_turns = {}
    expected_messages = []
    expected_proposals = []
    expected_message_tasks = []
    expected_operator_reply_tasks = []
    state_task_map = {task["id"]: task for task in state["tasks"]}
    for turn_record in state["turns"]:
        relative = turn_record["output"]
        require_no_path_escape(relative)
        raw = read_regular_file_bytes_no_follow(
            Path(artifacts_dir) / relative,
            "agent team turn output",
            max_bytes=MAX_AGENT_TEAM_TURN_BYTES,
        )
        if hashlib.sha256(raw).hexdigest() != turn_record["output_sha256"]:
            raise ValidationError("agent team turn output hash changed: %s" % relative)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValidationError("agent team turn output must be valid UTF-8: %s" % relative)
        if turn_record["status"] == "interrupted":
            if state["schema"] not in {
                AGENT_TEAM_STATE_SCHEMA_V4,
                AGENT_TEAM_STATE_SCHEMA_V5,
                AGENT_TEAM_STATE_SCHEMA,
            }:
                raise ValidationError("legacy agent team state contains an interruption")
            parsed_interruption = parse_agent_team_interruption(
                text,
                task_id=turn_record["task_id"],
            )
            if (
                parsed_interruption["operator_entry_id"]
                != turn_record["interruption_entry_id"]
                or parsed_interruption["instruction_sha256"]
                != turn_record["interruption_instruction_sha256"]
                or parsed_interruption["session_id_sha256"]
                != turn_record["session_id_sha256"]
            ):
                raise ValidationError(
                    "agent team interruption output binding changed: %s" % relative
                )
            latest_turns[turn_record["task_id"]] = {
                "status": "interrupted",
                "summary": AGENT_TEAM_INTERRUPTION_SUMMARY,
            }
            continue
        parsed = parse_agent_team_turn(
            text,
            turn_record["task_id"],
            member_ids,
            known_task_ids,
            expected_schema=expected_turn_schema,
            allow_operator=bool(
                state["schema"] == AGENT_TEAM_STATE_SCHEMA
                and step.get("operator_chat") is not None
            ),
        )
        if parsed["status"] != turn_record["status"]:
            raise ValidationError("agent team turn output status changed: %s" % relative)
        latest_turns[turn_record["task_id"]] = parsed
        for proposal in parsed.get("task_proposals", []):
            expected_proposal = {
                    "id": proposal["id"],
                    "description": proposal["description"],
                    "origin": "proposed",
                    "proposed_by": turn_record["member_id"],
                    "proposed_round": turn_record["round"],
                    "assignee": proposal["assignee"],
                    "depends_on": list(proposal["depends_on"]),
                }
            if state["schema"] in {
                AGENT_TEAM_STATE_SCHEMA_V3,
                AGENT_TEAM_STATE_SCHEMA_V4,
                AGENT_TEAM_STATE_SCHEMA_V5,
                AGENT_TEAM_STATE_SCHEMA,
            }:
                expected_proposal["operator_entry_id"] = None
            if state["schema"] in {AGENT_TEAM_STATE_SCHEMA_V5, AGENT_TEAM_STATE_SCHEMA}:
                expected_proposal["message_id"] = None
                expected_proposal["message_depth"] = None
            if state["schema"] == AGENT_TEAM_STATE_SCHEMA:
                expected_proposal["operator_reply_sha256"] = None
            expected_proposals.append(expected_proposal)
            known_task_ids.append(proposal["id"])
        for message in parsed["messages"]:
            body = message["body"]
            message_index = len(expected_messages) + 1
            message_id = "message-%04d" % message_index
            expected_messages.append(
                {
                    "id": message_id,
                    "round": turn_record["round"],
                    "from": turn_record["member_id"],
                    "to": message["to"],
                    "body": body,
                    "body_sha256": _sha256_text(body),
                }
            )
            policy = step.get("active_messaging")
            if (
                state["schema"] in {AGENT_TEAM_STATE_SCHEMA_V5, AGENT_TEAM_STATE_SCHEMA}
                and policy is not None
                and message["to"] != "operator"
            ):
                if message["to"] == "all":
                    recipients = [
                        member_id
                        for member_id in member_ids
                        if member_id != turn_record["member_id"]
                    ]
                else:
                    recipients = [message["to"]]
                parent_depth = state_task_map[turn_record["task_id"]].get("message_depth") or 0
                for recipient in recipients:
                    task_id = _agent_team_message_task_id(
                        message_index,
                        member_ids.index(recipient) + 1,
                    )
                    expected_message_tasks.append(
                        {
                            "id": task_id,
                            "description": body,
                            "origin": "message",
                            "proposed_by": turn_record["member_id"],
                            "proposed_round": turn_record["round"],
                            "assignee": recipient,
                            "depends_on": [],
                            "operator_entry_id": None,
                            "message_id": message_id,
                            "message_depth": parent_depth + 1,
                            **(
                                {"operator_reply_sha256": None}
                                if state["schema"] == AGENT_TEAM_STATE_SCHEMA
                                else {}
                            ),
                        }
                    )
                    known_task_ids.append(task_id)
            if (
                state["schema"] == AGENT_TEAM_STATE_SCHEMA
                and step.get("operator_chat") is not None
                and message["to"] == "operator"
            ):
                task_id = _agent_team_operator_reply_task_id(message_index)
                expected_operator_reply_tasks.append(
                    {
                        "id": task_id,
                        "origin": "operator-reply",
                        "proposed_by": turn_record["member_id"],
                        "proposed_round": turn_record["round"],
                        "assignee": turn_record["member_id"],
                        "depends_on": [],
                        "operator_entry_id": None,
                        "message_id": message_id,
                        "message_depth": None,
                    }
                )
                known_task_ids.append(task_id)
    if expected_messages != state["messages"]:
        raise ValidationError("agent team message ledger does not match turn outputs")
    if state["schema"] in {
        AGENT_TEAM_STATE_SCHEMA_V2,
        AGENT_TEAM_STATE_SCHEMA_V3,
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    }:
        proposal_fields = [
            "id",
            "description",
            "origin",
            "proposed_by",
            "proposed_round",
            "assignee",
            "depends_on",
        ]
        if state["schema"] in {
            AGENT_TEAM_STATE_SCHEMA_V3,
            AGENT_TEAM_STATE_SCHEMA_V4,
            AGENT_TEAM_STATE_SCHEMA_V5,
            AGENT_TEAM_STATE_SCHEMA,
        }:
            proposal_fields.append("operator_entry_id")
        if state["schema"] in {AGENT_TEAM_STATE_SCHEMA_V5, AGENT_TEAM_STATE_SCHEMA}:
            proposal_fields.extend(["message_id", "message_depth"])
        if state["schema"] == AGENT_TEAM_STATE_SCHEMA:
            proposal_fields.append("operator_reply_sha256")
        observed_proposals = [
            {
                field: task[field] for field in proposal_fields
            }
            for task in state["tasks"]
            if task["origin"] == "proposed"
        ]
        if observed_proposals != expected_proposals:
            raise ValidationError("agent team dynamic tasks do not match turn outputs")
    if state["schema"] in {AGENT_TEAM_STATE_SCHEMA_V5, AGENT_TEAM_STATE_SCHEMA}:
        message_task_fields = [
            "id",
            "description",
            "origin",
            "proposed_by",
            "proposed_round",
            "assignee",
            "depends_on",
            "operator_entry_id",
            "message_id",
            "message_depth",
        ]
        if state["schema"] == AGENT_TEAM_STATE_SCHEMA:
            message_task_fields.append("operator_reply_sha256")
        observed_message_tasks = [
            {field: task[field] for field in message_task_fields}
            for task in state["tasks"]
            if task["origin"] == "message"
        ]
        if observed_message_tasks != expected_message_tasks:
            raise ValidationError("agent team active message tasks do not match turn outputs")
    if state["schema"] == AGENT_TEAM_STATE_SCHEMA:
        operator_reply_fields = [
            "id",
            "origin",
            "proposed_by",
            "proposed_round",
            "assignee",
            "depends_on",
            "operator_entry_id",
            "message_id",
            "message_depth",
        ]
        observed_operator_reply_tasks = [
            {field: task[field] for field in operator_reply_fields}
            for task in state["tasks"]
            if task["origin"] == "operator-reply"
        ]
        if observed_operator_reply_tasks != expected_operator_reply_tasks:
            raise ValidationError("agent team operator reply tasks do not match turn outputs")
    for task in state["tasks"]:
        latest = latest_turns.get(task["id"])
        if latest is not None and task["summary"] != latest["summary"]:
            raise ValidationError("agent team task summary does not match its latest turn output")


def agent_team_state_summary(state: Dict) -> Dict:
    validate_agent_team_state(state)
    return {
        "schema": state["schema"],
        "step_id": state["step_id"],
        "generation": state["generation"],
        "status": state["status"],
        "round": state["round"],
        "max_rounds": state["max_rounds"],
        "member_count": len(state["members"]),
        "lead": next(member["id"] for member in state["members"] if member["lead"]),
        "task_counts": {
            status: sum(task["status"] == status for task in state["tasks"])
            for status in sorted(AGENT_TEAM_TASK_STATUSES)
        },
        "message_count": len(state["messages"]),
        "turn_count": len(state["turns"]),
        "interruption_count": sum(
            turn["status"] == "interrupted" for turn in state["turns"]
        ),
        "max_turns": state["max_turns"],
        "max_dynamic_tasks": state.get("max_dynamic_tasks", 0),
        "dynamic_tasks_added": state.get("dynamic_tasks_added", 0),
        "max_operator_tasks": state.get("max_operator_tasks", 0),
        "operator_tasks_added": state.get("operator_tasks_added", 0),
        "max_message_tasks": state.get("max_message_tasks", 0),
        "message_tasks_added": state.get("message_tasks_added", 0),
        "max_operator_questions": state.get("max_operator_questions", 0),
        "operator_questions_added": state.get("operator_questions_added", 0),
        "pending_operator_questions": sum(
            task.get("origin") == "operator-reply" and task["status"] == "waiting"
            for task in state["tasks"]
        ),
        "authorized_tokens": state["authorized_tokens"],
        "max_total_tokens": state["max_total_tokens"],
        "session_count": sum(member["session_id"] is not None for member in state["members"]),
        "members": [
            {
                "id": member["id"],
                "role": member["role"],
                "agent_profile": member["agent_profile"],
                "lead": member["lead"],
                "status": member["status"],
                "turns": member["turns"],
                "current_task_id": member["current_task_id"],
                "session_id_sha256": (
                    _sha256_text(member["session_id"]) if member["session_id"] is not None else None
                ),
            }
            for member in state["members"]
        ],
        "tasks": [
            {
                "id": task["id"],
                "origin": task.get("origin", "static"),
                "proposed_by": task.get("proposed_by"),
                "proposed_round": task.get("proposed_round"),
                "operator_entry_id": task.get("operator_entry_id"),
                "message_id": task.get("message_id"),
                "message_depth": task.get("message_depth"),
                "operator_reply_sha256": task.get("operator_reply_sha256"),
                "status": task["status"],
                "assignee": task["assignee"],
                "claimed_by": task["claimed_by"],
                "attempts": task["attempts"],
                "completed_round": task["completed_round"],
                "output_sha256": task["output_sha256"],
            }
            for task in state["tasks"]
        ],
    }


def _claim_task(member: Dict, task: Dict) -> None:
    member["status"] = "working"
    member["current_task_id"] = task["id"]
    member["claim_task_ids"] = []
    task["status"] = "claimed"
    task["claimed_by"] = member["id"]


def _validate_task_dependencies(tasks: List[Dict], task_ids: List[str], step_id: str) -> None:
    positions = {task_id: index for index, task_id in enumerate(task_ids)}
    for task in tasks:
        for dependency in task["depends_on"]:
            if dependency not in positions:
                raise ValidationError(
                    "agent_team step %s task %s depends on unknown task %s"
                    % (step_id, task["id"], dependency)
                )
            if dependency == task["id"]:
                raise ValidationError("agent_team step %s task %s depends on itself" % (step_id, task["id"]))
            if positions[dependency] >= positions[task["id"]]:
                raise ValidationError(
                    "agent_team step %s task dependencies must appear earlier" % step_id
                )


def _validate_required_int(values: Dict, field: str, minimum: int, maximum: int, step_id: str) -> None:
    value = values.get(field)
    if not _strict_int(value) or not minimum <= value <= maximum:
        raise ValidationError(
            "agent_team step %s %s must be an integer from %d to %d"
            % (step_id, field, minimum, maximum)
        )


def _validate_state_int(values: Dict, field: str, minimum: int, maximum: int, source: str) -> None:
    value = values.get(field)
    if not _strict_int(value) or not minimum <= value <= maximum:
        raise ValidationError("%s %s is invalid" % (source, field))


def _validate_id(value, label: str) -> None:
    if not isinstance(value, str) or len(value) > 128 or not SAFE_TEAM_ID.match(value):
        raise ValidationError("%s must be a safe identifier" % label)


def _validate_text(value, label: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError("%s must be a non-empty string of at most %d characters" % (label, maximum))


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or not SHA256_PATTERN.match(value):
        raise ValidationError("%s is invalid" % label)


def _validate_uuid(value, label: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValidationError("%s is invalid" % label)
    if str(parsed) != value:
        raise ValidationError("%s is invalid" % label)


def _validate_timestamp(value, label: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError("%s is invalid" % label)
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValidationError("%s is invalid" % label)


def _strict_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _agent_team_message_task_id(message_index: int, recipient_index: int) -> str:
    return "%s%04d-%02d" % (
        AGENT_TEAM_MESSAGE_TASK_PREFIX,
        message_index,
        recipient_index,
    )


def _agent_team_operator_reply_task_id(message_index: int) -> str:
    return "%s%04d" % (AGENT_TEAM_OPERATOR_REPLY_TASK_PREFIX, message_index)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value):
    try:
        number = float(value)
    except ValueError:
        raise ValueError("invalid JSON constant")
    if not math.isfinite(number):
        raise ValueError("non-finite JSON constant")
    return number
