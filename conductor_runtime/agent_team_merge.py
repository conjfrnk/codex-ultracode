import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .errors import ValidationError
from .agent_team import validate_agent_team_state
from .security import (
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    require_no_path_escape,
    unlink_regular_file_no_follow,
    write_new_text_file_no_follow,
)
from .staged_workspace import (
    MAX_STAGED_CHANGES,
    WORKSPACE_MERGE_STRATEGY,
    validate_workspace_merge_plan,
    workspace_snapshot_from_manifest,
)


AGENT_TEAM_MERGE_LEDGER_SCHEMA = "conductor.agent_team_merge_ledger.v1"
AGENT_TEAM_MERGE_INTENT_SCHEMA = "conductor.agent_team_merge_intent.v1"
AGENT_TEAM_MERGE_TRANSACTION_SCHEMA = "conductor.agent_team_merge_transaction.v1"
AGENT_TEAM_MERGE_RECOVERY_SCHEMA = "conductor.agent_team_merge_recovery.v1"
MAX_AGENT_TEAM_MERGE_EVENTS = 512
MAX_AGENT_TEAM_MERGE_LEDGER_BYTES = 8 * 1024 * 1024
MAX_AGENT_TEAM_MERGE_TRANSACTION_BYTES = 32 * 1024 * 1024
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
LEDGER_FIELDS = {
    "schema",
    "step_id",
    "workflow_fingerprint",
    "generation",
    "strategy",
    "source_initial_sha256",
    "source_current_sha256",
    "event_count",
    "conflict_count",
    "created_at_utc",
    "updated_at_utc",
    "events",
}
EVENT_FIELDS = {
    "sequence",
    "kind",
    "round",
    "member_id",
    "task_id",
    "turn_output_sha256",
    "status",
    "workspace_base_sha256",
    "workspace_result_sha256",
    "source_before_sha256",
    "source_after_sha256",
    "delta_sha256",
    "plan_sha256",
    "changed_files",
    "applied_files",
    "deduplicated_files",
    "conflicting_files",
}
MERGE_STATUSES = {"applied", "deduplicated", "mixed", "no-change"}
INTENT_FIELDS = {
    "schema",
    "step_id",
    "workflow_fingerprint",
    "generation",
    "round",
    "member_id",
    "task_id",
    "created_at_utc",
    "workspace_relative",
    "workspace_base_sha256",
    "workspace_result_sha256",
    "turn_output",
    "turn_output_sha256",
    "source_expected_sha256",
    "state_before_sha256",
    "ledger_before_sha256",
    "candidate_state_sha256",
    "workspace_base_manifest",
    "state_before",
    "ledger_before",
    "candidate_state",
    "intent_sha256",
}
TRANSACTION_FIELDS = {
    "schema",
    "step_id",
    "workflow_fingerprint",
    "generation",
    "round",
    "member_id",
    "task_id",
    "created_at_utc",
    "workspace_relative",
    "workspace_result_sha256",
    "turn_output_sha256",
    "source_before_sha256",
    "source_after_sha256",
    "state_before_sha256",
    "ledger_before_sha256",
    "candidate_state_sha256",
    "candidate_ledger_sha256",
    "source_before_manifest",
    "merge_plan",
    "candidate_state",
    "candidate_ledger",
    "transaction_sha256",
}
RECOVERY_FIELDS = {
    "schema",
    "transaction_sha256",
    "recovered_at_utc",
    "source_before_sha256",
    "source_after_sha256",
    "source_checkpoint",
    "state_checkpoint",
    "ledger_checkpoint",
    "candidate_state_sha256",
    "candidate_ledger_sha256",
    "provider_replayed",
}


def initial_agent_team_merge_ledger(
    *,
    step_id: str,
    workflow_fingerprint: str,
    generation: int,
    source_sha256: str,
    timestamp: str,
) -> Dict:
    ledger = {
        "schema": AGENT_TEAM_MERGE_LEDGER_SCHEMA,
        "step_id": step_id,
        "workflow_fingerprint": workflow_fingerprint,
        "generation": generation,
        "strategy": WORKSPACE_MERGE_STRATEGY,
        "source_initial_sha256": source_sha256,
        "source_current_sha256": source_sha256,
        "event_count": 0,
        "conflict_count": 0,
        "created_at_utc": timestamp,
        "updated_at_utc": timestamp,
        "events": [],
    }
    validate_agent_team_merge_ledger(ledger)
    return ledger


def append_agent_team_merge_event(
    ledger: Dict,
    *,
    round_number: int,
    member_id: str,
    task_id: str,
    turn_output_sha256: str,
    plan: Dict,
    timestamp: str,
) -> Dict:
    status = plan.get("status") if isinstance(plan, dict) else None
    kind = "conflict" if status == "conflict" else "merge"
    event = {
        "sequence": len(ledger.get("events", [])) + 1,
        "kind": kind,
        "round": round_number,
        "member_id": member_id,
        "task_id": task_id,
        "turn_output_sha256": turn_output_sha256,
        "status": status,
        "workspace_base_sha256": plan.get("workspace_base_sha256"),
        "workspace_result_sha256": plan.get("workspace_result_sha256"),
        "source_before_sha256": plan.get("source_before_sha256"),
        "source_after_sha256": plan.get("source_after_sha256"),
        "delta_sha256": plan.get("delta_sha256"),
        "plan_sha256": plan.get("plan_sha256"),
        "changed_files": list(plan.get("changed_files", [])),
        "applied_files": list(plan.get("apply_files", [])),
        "deduplicated_files": list(plan.get("deduplicated_files", [])),
        "conflicting_files": list(plan.get("conflicting_files", [])),
    }
    candidate = json.loads(json.dumps(ledger))
    candidate["events"].append(event)
    candidate["event_count"] = len(candidate["events"])
    candidate["conflict_count"] = sum(value["kind"] == "conflict" for value in candidate["events"])
    candidate["source_current_sha256"] = event["source_after_sha256"]
    candidate["updated_at_utc"] = timestamp
    validate_agent_team_merge_ledger(candidate)
    return candidate


def build_agent_team_merge_intent(
    *,
    step: Dict,
    workflow_fingerprint: str,
    generation: int,
    state_before: Dict,
    ledger_before: Dict,
    candidate_state: Dict,
    workspace_base_manifest: Dict,
    workspace_result_sha256: str,
    workspace_relative: str,
    timestamp: str,
) -> Dict:
    validate_agent_team_state(
        state_before,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=generation,
    )
    validate_agent_team_merge_ledger(
        ledger_before,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=generation,
        state=state_before,
    )
    validate_agent_team_state(
        candidate_state,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=generation,
    )
    if len(candidate_state["turns"]) != len(state_before["turns"]) + 1:
        raise ValidationError("agent team merge intent candidate state must add one turn")
    turn = candidate_state["turns"][-1]
    workspace_base = workspace_snapshot_from_manifest(workspace_base_manifest)
    intent = {
        "schema": AGENT_TEAM_MERGE_INTENT_SCHEMA,
        "step_id": step["id"],
        "workflow_fingerprint": workflow_fingerprint,
        "generation": generation,
        "round": turn["round"],
        "member_id": turn["member_id"],
        "task_id": turn["task_id"],
        "created_at_utc": timestamp,
        "workspace_relative": workspace_relative,
        "workspace_base_sha256": workspace_base.tracked_fingerprint_sha256,
        "workspace_result_sha256": workspace_result_sha256,
        "turn_output": turn["output"],
        "turn_output_sha256": turn["output_sha256"],
        "source_expected_sha256": ledger_before["source_current_sha256"],
        "state_before_sha256": agent_team_state_sha256(state_before),
        "ledger_before_sha256": ledger_sha256(ledger_before),
        "candidate_state_sha256": agent_team_state_sha256(candidate_state),
        "workspace_base_manifest": json.loads(json.dumps(workspace_base_manifest)),
        "state_before": json.loads(json.dumps(state_before)),
        "ledger_before": json.loads(json.dumps(ledger_before)),
        "candidate_state": json.loads(json.dumps(candidate_state)),
    }
    intent["intent_sha256"] = _intent_sha256(intent)
    validate_agent_team_merge_intent(
        intent,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=generation,
    )
    return intent


def validate_agent_team_merge_intent(
    intent: Dict,
    *,
    step: Optional[Dict] = None,
    workflow_fingerprint: Optional[str] = None,
    generation: Optional[int] = None,
) -> None:
    if not isinstance(intent, dict) or set(intent) != INTENT_FIELDS:
        raise ValidationError("agent team merge intent has invalid fields")
    if intent.get("schema") != AGENT_TEAM_MERGE_INTENT_SCHEMA:
        raise ValidationError("agent team merge intent schema is invalid")
    _validate_id(intent.get("step_id"), "agent team merge intent step_id")
    _validate_sha256(
        intent.get("workflow_fingerprint"),
        "agent team merge intent workflow fingerprint",
    )
    if workflow_fingerprint is not None and intent["workflow_fingerprint"] != workflow_fingerprint:
        raise ValidationError("agent team merge intent workflow fingerprint changed")
    if not _strict_int(intent.get("generation")) or intent["generation"] < 0:
        raise ValidationError("agent team merge intent generation is invalid")
    if generation is not None and intent["generation"] != generation:
        raise ValidationError("agent team merge intent generation changed")
    if not _strict_int(intent.get("round")) or intent["round"] < 1:
        raise ValidationError("agent team merge intent round is invalid")
    _validate_id(intent.get("member_id"), "agent team merge intent member_id")
    _validate_id(intent.get("task_id"), "agent team merge intent task_id")
    _validate_timestamp(intent.get("created_at_utc"), "agent team merge intent timestamp")
    for field in ("workspace_relative", "turn_output"):
        value = intent.get(field)
        if not isinstance(value, str) or not value:
            raise ValidationError("agent team merge intent %s is invalid" % field)
        require_no_path_escape(value)
    for field in (
        "workspace_base_sha256",
        "workspace_result_sha256",
        "turn_output_sha256",
        "source_expected_sha256",
        "state_before_sha256",
        "ledger_before_sha256",
        "candidate_state_sha256",
        "intent_sha256",
    ):
        _validate_sha256(intent.get(field), "agent team merge intent %s" % field)
    if intent["intent_sha256"] != _intent_sha256(intent):
        raise ValidationError("agent team merge intent hash is invalid")
    workspace_base = workspace_snapshot_from_manifest(intent.get("workspace_base_manifest"))
    if workspace_base.tracked_fingerprint_sha256 != intent["workspace_base_sha256"]:
        raise ValidationError("agent team merge intent workspace base changed")
    state_before = intent.get("state_before")
    validate_agent_team_state(
        state_before,
        step=step,
        workflow_fingerprint=intent["workflow_fingerprint"],
        generation=intent["generation"],
    )
    if agent_team_state_sha256(state_before) != intent["state_before_sha256"]:
        raise ValidationError("agent team merge intent prior state hash changed")
    ledger_before = intent.get("ledger_before")
    validate_agent_team_merge_ledger(
        ledger_before,
        step=step,
        workflow_fingerprint=intent["workflow_fingerprint"],
        generation=intent["generation"],
        state=state_before,
    )
    if ledger_sha256(ledger_before) != intent["ledger_before_sha256"]:
        raise ValidationError("agent team merge intent prior ledger hash changed")
    if ledger_before["source_current_sha256"] != intent["source_expected_sha256"]:
        raise ValidationError("agent team merge intent source expectation changed")
    candidate_state = intent.get("candidate_state")
    validate_agent_team_state(
        candidate_state,
        step=step,
        workflow_fingerprint=intent["workflow_fingerprint"],
        generation=intent["generation"],
    )
    if agent_team_state_sha256(candidate_state) != intent["candidate_state_sha256"]:
        raise ValidationError("agent team merge intent candidate state hash changed")
    if len(candidate_state["turns"]) != len(state_before["turns"]) + 1:
        raise ValidationError("agent team merge intent candidate state must add one turn")
    if not candidate_state["turns"]:
        raise ValidationError("agent team merge intent candidate turn is missing")
    turn = candidate_state["turns"][-1]
    if (
        turn["round"],
        turn["member_id"],
        turn["task_id"],
        turn["output"],
        turn["output_sha256"],
    ) != (
        intent["round"],
        intent["member_id"],
        intent["task_id"],
        intent["turn_output"],
        intent["turn_output_sha256"],
    ):
        raise ValidationError("agent team merge intent turn identity changed")
    if step is not None:
        if step.get("sandbox", "read-only") != "workspace-write":
            raise ValidationError("agent team merge intent requires a write-capable team")
        if intent["step_id"] != step.get("id"):
            raise ValidationError("agent team merge intent step binding changed")


def build_agent_team_merge_transaction(
    *,
    step: Dict,
    workflow_fingerprint: str,
    generation: int,
    state_before: Dict,
    ledger_before: Dict,
    candidate_state: Dict,
    candidate_ledger: Dict,
    merge_plan: Dict,
    source_before_manifest: Dict,
    workspace_relative: str,
    timestamp: str,
) -> Dict:
    validate_agent_team_state(
        state_before,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=generation,
    )
    validate_agent_team_merge_ledger(
        ledger_before,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=generation,
        state=state_before,
    )
    validate_agent_team_state(
        candidate_state,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=generation,
    )
    validate_agent_team_merge_ledger(
        candidate_ledger,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=generation,
        state=candidate_state,
    )
    if len(candidate_state["turns"]) != len(state_before["turns"]) + 1:
        raise ValidationError("agent team merge transaction candidate state must add one turn")
    if len(candidate_ledger["events"]) != len(ledger_before["events"]) + 1:
        raise ValidationError("agent team merge transaction candidate ledger must add one event")
    event = candidate_ledger["events"][-1]
    turn = candidate_state["turns"][-1]
    transaction = {
        "schema": AGENT_TEAM_MERGE_TRANSACTION_SCHEMA,
        "step_id": step["id"],
        "workflow_fingerprint": workflow_fingerprint,
        "generation": generation,
        "round": turn["round"],
        "member_id": turn["member_id"],
        "task_id": turn["task_id"],
        "created_at_utc": timestamp,
        "workspace_relative": workspace_relative,
        "workspace_result_sha256": merge_plan.get("workspace_result_sha256"),
        "turn_output_sha256": turn["output_sha256"],
        "source_before_sha256": merge_plan.get("source_before_sha256"),
        "source_after_sha256": merge_plan.get("source_after_sha256"),
        "state_before_sha256": agent_team_state_sha256(state_before),
        "ledger_before_sha256": ledger_sha256(ledger_before),
        "candidate_state_sha256": agent_team_state_sha256(candidate_state),
        "candidate_ledger_sha256": ledger_sha256(candidate_ledger),
        "source_before_manifest": json.loads(json.dumps(source_before_manifest)),
        "merge_plan": json.loads(json.dumps(merge_plan)),
        "candidate_state": json.loads(json.dumps(candidate_state)),
        "candidate_ledger": json.loads(json.dumps(candidate_ledger)),
    }
    if event["kind"] != "merge":
        raise ValidationError("agent team merge transaction cannot persist a conflict event")
    transaction["transaction_sha256"] = _transaction_sha256(transaction)
    validate_agent_team_merge_transaction(
        transaction,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=generation,
    )
    return transaction


def validate_agent_team_merge_transaction(
    transaction: Dict,
    *,
    step: Optional[Dict] = None,
    workflow_fingerprint: Optional[str] = None,
    generation: Optional[int] = None,
) -> None:
    if not isinstance(transaction, dict) or set(transaction) != TRANSACTION_FIELDS:
        raise ValidationError("agent team merge transaction has invalid fields")
    if transaction.get("schema") != AGENT_TEAM_MERGE_TRANSACTION_SCHEMA:
        raise ValidationError("agent team merge transaction schema is invalid")
    _validate_id(transaction.get("step_id"), "agent team merge transaction step_id")
    _validate_sha256(
        transaction.get("workflow_fingerprint"),
        "agent team merge transaction workflow fingerprint",
    )
    if workflow_fingerprint is not None and transaction["workflow_fingerprint"] != workflow_fingerprint:
        raise ValidationError("agent team merge transaction workflow fingerprint changed")
    if not _strict_int(transaction.get("generation")) or transaction["generation"] < 0:
        raise ValidationError("agent team merge transaction generation is invalid")
    if generation is not None and transaction["generation"] != generation:
        raise ValidationError("agent team merge transaction generation changed")
    if not _strict_int(transaction.get("round")) or transaction["round"] < 1:
        raise ValidationError("agent team merge transaction round is invalid")
    _validate_id(transaction.get("member_id"), "agent team merge transaction member_id")
    _validate_id(transaction.get("task_id"), "agent team merge transaction task_id")
    _validate_timestamp(transaction.get("created_at_utc"), "agent team merge transaction timestamp")
    workspace_relative = transaction.get("workspace_relative")
    if not isinstance(workspace_relative, str) or not workspace_relative:
        raise ValidationError("agent team merge transaction workspace path is invalid")
    require_no_path_escape(workspace_relative)
    for field in (
        "workspace_result_sha256",
        "turn_output_sha256",
        "source_before_sha256",
        "source_after_sha256",
        "state_before_sha256",
        "ledger_before_sha256",
        "candidate_state_sha256",
        "candidate_ledger_sha256",
        "transaction_sha256",
    ):
        _validate_sha256(transaction.get(field), "agent team merge transaction %s" % field)
    if transaction["transaction_sha256"] != _transaction_sha256(transaction):
        raise ValidationError("agent team merge transaction hash is invalid")
    plan = transaction.get("merge_plan")
    validate_workspace_merge_plan(plan)
    if plan["status"] == "conflict":
        raise ValidationError("agent team merge transaction plan cannot contain a conflict")
    source_before = workspace_snapshot_from_manifest(transaction.get("source_before_manifest"))
    if (
        source_before.tracked_fingerprint_sha256 != transaction["source_before_sha256"]
        or plan["source_before_sha256"] != transaction["source_before_sha256"]
        or plan["source_after_sha256"] != transaction["source_after_sha256"]
        or plan["workspace_result_sha256"] != transaction["workspace_result_sha256"]
    ):
        raise ValidationError("agent team merge transaction workspace fingerprints are inconsistent")
    candidate_state = transaction.get("candidate_state")
    validate_agent_team_state(
        candidate_state,
        step=step,
        workflow_fingerprint=transaction["workflow_fingerprint"],
        generation=transaction["generation"],
    )
    candidate_ledger = transaction.get("candidate_ledger")
    validate_agent_team_merge_ledger(
        candidate_ledger,
        step=step,
        workflow_fingerprint=transaction["workflow_fingerprint"],
        generation=transaction["generation"],
        state=candidate_state,
    )
    if agent_team_state_sha256(candidate_state) != transaction["candidate_state_sha256"]:
        raise ValidationError("agent team merge transaction candidate state hash changed")
    if ledger_sha256(candidate_ledger) != transaction["candidate_ledger_sha256"]:
        raise ValidationError("agent team merge transaction candidate ledger hash changed")
    if not candidate_state["turns"] or not candidate_ledger["events"]:
        raise ValidationError("agent team merge transaction candidate evidence is empty")
    turn = candidate_state["turns"][-1]
    event = candidate_ledger["events"][-1]
    expected_identity = (
        transaction["round"],
        transaction["member_id"],
        transaction["task_id"],
        transaction["turn_output_sha256"],
    )
    if (
        turn["round"],
        turn["member_id"],
        turn["task_id"],
        turn["output_sha256"],
    ) != expected_identity or (
        event["round"],
        event["member_id"],
        event["task_id"],
        event["turn_output_sha256"],
    ) != expected_identity:
        raise ValidationError("agent team merge transaction turn identity changed")
    if (
        event["kind"] != "merge"
        or event["plan_sha256"] != plan["plan_sha256"]
        or event["source_before_sha256"] != transaction["source_before_sha256"]
        or event["source_after_sha256"] != transaction["source_after_sha256"]
    ):
        raise ValidationError("agent team merge transaction ledger event changed")
    if step is not None:
        if step.get("sandbox", "read-only") != "workspace-write":
            raise ValidationError("agent team merge transaction requires a write-capable team")
        if transaction["step_id"] != step.get("id"):
            raise ValidationError("agent team merge transaction step binding changed")


def validate_agent_team_merge_ledger(
    ledger: Dict,
    *,
    step: Optional[Dict] = None,
    workflow_fingerprint: Optional[str] = None,
    generation: Optional[int] = None,
    state: Optional[Dict] = None,
) -> None:
    if not isinstance(ledger, dict) or set(ledger) != LEDGER_FIELDS:
        raise ValidationError("agent team merge ledger has invalid fields")
    if ledger.get("schema") != AGENT_TEAM_MERGE_LEDGER_SCHEMA:
        raise ValidationError("agent team merge ledger schema is invalid")
    _validate_id(ledger.get("step_id"), "agent team merge step_id")
    _validate_sha256(ledger.get("workflow_fingerprint"), "agent team merge workflow fingerprint")
    if workflow_fingerprint is not None and ledger["workflow_fingerprint"] != workflow_fingerprint:
        raise ValidationError("agent team merge workflow fingerprint changed")
    if not _strict_int(ledger.get("generation")) or ledger["generation"] < 0:
        raise ValidationError("agent team merge generation is invalid")
    if generation is not None and ledger["generation"] != generation:
        raise ValidationError("agent team merge generation changed")
    if ledger.get("strategy") != WORKSPACE_MERGE_STRATEGY:
        raise ValidationError("agent team merge strategy is invalid")
    for field in ("source_initial_sha256", "source_current_sha256"):
        _validate_sha256(ledger.get(field), "agent team merge %s" % field)
    for field in ("created_at_utc", "updated_at_utc"):
        _validate_timestamp(ledger.get(field), "agent team merge %s" % field)
    events = ledger.get("events")
    if not isinstance(events, list) or len(events) > MAX_AGENT_TEAM_MERGE_EVENTS:
        raise ValidationError("agent team merge events are invalid")
    if ledger.get("event_count") != len(events):
        raise ValidationError("agent team merge event count is inconsistent")
    if ledger.get("conflict_count") != sum(
        isinstance(event, dict) and event.get("kind") == "conflict" for event in events
    ):
        raise ValidationError("agent team merge conflict count is inconsistent")
    source_sha256 = ledger["source_initial_sha256"]
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise ValidationError("agent team merge event has invalid fields")
        if event.get("sequence") != index:
            raise ValidationError("agent team merge event sequence is invalid")
        if event.get("kind") not in {"merge", "conflict"}:
            raise ValidationError("agent team merge event kind is invalid")
        if not _strict_int(event.get("round")) or event["round"] < 1:
            raise ValidationError("agent team merge event round is invalid")
        _validate_id(event.get("member_id"), "agent team merge member_id")
        _validate_id(event.get("task_id"), "agent team merge task_id")
        for field in (
            "turn_output_sha256",
            "workspace_base_sha256",
            "workspace_result_sha256",
            "source_before_sha256",
            "source_after_sha256",
            "delta_sha256",
            "plan_sha256",
        ):
            _validate_sha256(event.get(field), "agent team merge event %s" % field)
        if event["source_before_sha256"] != source_sha256:
            raise ValidationError("agent team merge source chain is inconsistent")
        for field in (
            "changed_files",
            "applied_files",
            "deduplicated_files",
            "conflicting_files",
        ):
            _validate_paths(event.get(field), "agent team merge event %s" % field)
        changed = set(event["changed_files"])
        if any(set(event[field]) - changed for field in ("applied_files", "deduplicated_files", "conflicting_files")):
            raise ValidationError("agent team merge event file classes exceed changed_files")
        if event["kind"] == "conflict":
            if event.get("status") != "conflict" or not event["conflicting_files"]:
                raise ValidationError("agent team merge conflict event is inconsistent")
            if event["applied_files"] or event["source_after_sha256"] != source_sha256:
                raise ValidationError("agent team merge conflict event cannot mutate source")
        else:
            if event.get("status") not in MERGE_STATUSES or event["conflicting_files"]:
                raise ValidationError("agent team merge event status is inconsistent")
        source_sha256 = event["source_after_sha256"]
    if ledger["source_current_sha256"] != source_sha256:
        raise ValidationError("agent team merge current source fingerprint is inconsistent")
    if step is not None:
        if step.get("sandbox", "read-only") != "workspace-write":
            raise ValidationError("agent team merge ledger requires a write-capable team step")
        if ledger["step_id"] != step.get("id"):
            raise ValidationError("agent team merge step binding changed")
    if state is not None:
        if state.get("generation") != ledger["generation"]:
            raise ValidationError("agent team merge state generation changed")
        merged = [event for event in events if event["kind"] == "merge"]
        turns = state.get("turns") if isinstance(state, dict) else None
        accepted_turns = (
            [turn for turn in turns if turn.get("status") != "interrupted"]
            if isinstance(turns, list)
            else None
        )
        if accepted_turns is None or len(merged) != len(accepted_turns):
            raise ValidationError("agent team merge ledger does not match accepted turns")
        for event, turn in zip(merged, accepted_turns):
            if (
                event["round"],
                event["member_id"],
                event["task_id"],
                event["turn_output_sha256"],
            ) != (
                turn.get("round"),
                turn.get("member_id"),
                turn.get("task_id"),
                turn.get("output_sha256"),
            ):
                raise ValidationError("agent team merge event binding changed")


def load_agent_team_merge_ledger(
    path: Path,
    **validation,
) -> Dict:
    reject_symlink_path(path, "agent team merge ledger")
    text = read_regular_text_file_no_follow(
        path,
        "agent team merge ledger",
        MAX_AGENT_TEAM_MERGE_LEDGER_BYTES,
    )
    try:
        ledger = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("agent team merge ledger is invalid JSON: %s" % exc.__class__.__name__)
    validate_agent_team_merge_ledger(ledger, **validation)
    return ledger


def write_agent_team_merge_ledger(path: Path, ledger: Dict) -> None:
    validate_agent_team_merge_ledger(ledger)
    parent_fd = ensure_dir_no_follow(path.parent, "agent team merge ledger parent")
    os.close(parent_fd)
    text = json.dumps(ledger, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_AGENT_TEAM_MERGE_LEDGER_BYTES:
        raise ValidationError("agent team merge ledger exceeds its byte limit")
    replace_text_file_no_follow(path, "agent team merge ledger", text, ".agent-team-merge-")


def load_agent_team_merge_transaction(path: Path, **validation) -> Dict:
    reject_symlink_path(path, "agent team merge transaction")
    text = read_regular_text_file_no_follow(
        path,
        "agent team merge transaction",
        MAX_AGENT_TEAM_MERGE_TRANSACTION_BYTES,
    )
    try:
        transaction = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError(
            "agent team merge transaction is invalid JSON: %s" % exc.__class__.__name__
        )
    validate_agent_team_merge_transaction(transaction, **validation)
    return transaction


def load_agent_team_merge_intent(path: Path, **validation) -> Dict:
    reject_symlink_path(path, "agent team merge intent")
    text = read_regular_text_file_no_follow(
        path,
        "agent team merge intent",
        MAX_AGENT_TEAM_MERGE_TRANSACTION_BYTES,
    )
    try:
        intent = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError(
            "agent team merge intent is invalid JSON: %s" % exc.__class__.__name__
        )
    validate_agent_team_merge_intent(intent, **validation)
    return intent


def write_agent_team_merge_intent(path: Path, intent: Dict) -> None:
    validate_agent_team_merge_intent(intent)
    parent_fd = ensure_dir_no_follow(path.parent, "agent team merge intent parent")
    os.close(parent_fd)
    text = json.dumps(intent, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_AGENT_TEAM_MERGE_TRANSACTION_BYTES:
        raise ValidationError("agent team merge intent exceeds its byte limit")
    write_new_text_file_no_follow(path, "agent team merge intent", text)


def remove_agent_team_merge_intent(path: Path) -> None:
    unlink_regular_file_no_follow(path, "agent team merge intent")


def write_agent_team_merge_transaction(path: Path, transaction: Dict) -> None:
    validate_agent_team_merge_transaction(transaction)
    parent_fd = ensure_dir_no_follow(path.parent, "agent team merge transaction parent")
    os.close(parent_fd)
    text = json.dumps(transaction, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_AGENT_TEAM_MERGE_TRANSACTION_BYTES:
        raise ValidationError("agent team merge transaction exceeds its byte limit")
    replace_text_file_no_follow(
        path,
        "agent team merge transaction",
        text,
        ".agent-team-merge-transaction-",
    )


def remove_agent_team_merge_transaction(path: Path) -> None:
    unlink_regular_file_no_follow(path, "agent team merge transaction")


def build_agent_team_merge_recovery(
    transaction: Dict,
    *,
    recovered_at_utc: str,
    source_checkpoint: str,
    state_checkpoint: str,
    ledger_checkpoint: str,
) -> Dict:
    validate_agent_team_merge_transaction(transaction)
    recovery = {
        "schema": AGENT_TEAM_MERGE_RECOVERY_SCHEMA,
        "transaction_sha256": transaction["transaction_sha256"],
        "recovered_at_utc": recovered_at_utc,
        "source_before_sha256": transaction["source_before_sha256"],
        "source_after_sha256": transaction["source_after_sha256"],
        "source_checkpoint": source_checkpoint,
        "state_checkpoint": state_checkpoint,
        "ledger_checkpoint": ledger_checkpoint,
        "candidate_state_sha256": transaction["candidate_state_sha256"],
        "candidate_ledger_sha256": transaction["candidate_ledger_sha256"],
        "provider_replayed": False,
    }
    validate_agent_team_merge_recovery(recovery)
    return recovery


def validate_agent_team_merge_recovery(recovery: Dict) -> None:
    if not isinstance(recovery, dict) or set(recovery) != RECOVERY_FIELDS:
        raise ValidationError("agent team merge recovery receipt has invalid fields")
    if recovery.get("schema") != AGENT_TEAM_MERGE_RECOVERY_SCHEMA:
        raise ValidationError("agent team merge recovery receipt schema is invalid")
    for field in (
        "transaction_sha256",
        "source_before_sha256",
        "source_after_sha256",
        "candidate_state_sha256",
        "candidate_ledger_sha256",
    ):
        _validate_sha256(recovery.get(field), "agent team merge recovery %s" % field)
    _validate_timestamp(recovery.get("recovered_at_utc"), "agent team merge recovery timestamp")
    if recovery.get("source_checkpoint") not in {"before", "partial", "candidate"}:
        raise ValidationError("agent team merge recovery source checkpoint is invalid")
    for field in ("state_checkpoint", "ledger_checkpoint"):
        if recovery.get(field) not in {"before", "candidate"}:
            raise ValidationError("agent team merge recovery %s is invalid" % field)
    if recovery.get("provider_replayed") is not False:
        raise ValidationError("agent team merge recovery cannot claim provider replay")


def load_agent_team_merge_recovery(path: Path) -> Dict:
    reject_symlink_path(path, "agent team merge recovery receipt")
    text = read_regular_text_file_no_follow(
        path,
        "agent team merge recovery receipt",
        64 * 1024,
    )
    try:
        recovery = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError(
            "agent team merge recovery receipt is invalid JSON: %s" % exc.__class__.__name__
        )
    validate_agent_team_merge_recovery(recovery)
    return recovery


def write_agent_team_merge_recovery(path: Path, recovery: Dict) -> Dict:
    validate_agent_team_merge_recovery(recovery)
    text = json.dumps(recovery, indent=2, sort_keys=True) + "\n"
    try:
        write_new_text_file_no_follow(
            path,
            "agent team merge recovery receipt",
            text,
        )
        return recovery
    except FileExistsError:
        existing = load_agent_team_merge_recovery(path)
        comparable_fields = RECOVERY_FIELDS - {"recovered_at_utc"}
        if any(existing[field] != recovery[field] for field in comparable_fields):
            raise ValidationError("agent team merge recovery receipt already exists with different evidence")
        return existing


def agent_team_merge_transaction_summary(transaction: Dict) -> Dict:
    validate_agent_team_merge_transaction(transaction)
    return {
        "schema": transaction["schema"],
        "transaction_sha256": transaction["transaction_sha256"],
        "generation": transaction["generation"],
        "round": transaction["round"],
        "member_id": transaction["member_id"],
        "task_id": transaction["task_id"],
        "source_before_sha256": transaction["source_before_sha256"],
        "source_after_sha256": transaction["source_after_sha256"],
        "changed_file_count": len(transaction["merge_plan"]["changed_files"]),
        "status": "recovery-pending",
        "provider_replay_required": False,
    }


def agent_team_merge_intent_summary(intent: Dict) -> Dict:
    validate_agent_team_merge_intent(intent)
    return {
        "schema": intent["schema"],
        "intent_sha256": intent["intent_sha256"],
        "generation": intent["generation"],
        "round": intent["round"],
        "member_id": intent["member_id"],
        "task_id": intent["task_id"],
        "source_expected_sha256": intent["source_expected_sha256"],
        "workspace_base_sha256": intent["workspace_base_sha256"],
        "workspace_result_sha256": intent["workspace_result_sha256"],
        "turn_output_sha256": intent["turn_output_sha256"],
        "status": "accepted-turn-recovery-pending",
        "provider_replay_required": False,
    }


def agent_team_merge_recovery_summary(recovery: Dict) -> Dict:
    validate_agent_team_merge_recovery(recovery)
    return dict(recovery)


def agent_team_merge_summary(ledger: Dict) -> Dict:
    validate_agent_team_merge_ledger(ledger)
    return {
        "schema": ledger["schema"],
        "strategy": ledger["strategy"],
        "source_initial_sha256": ledger["source_initial_sha256"],
        "source_current_sha256": ledger["source_current_sha256"],
        "event_count": ledger["event_count"],
        "merge_count": ledger["event_count"] - ledger["conflict_count"],
        "conflict_count": ledger["conflict_count"],
        "changed_file_count": sum(
            len(event["changed_files"]) for event in ledger["events"] if event["kind"] == "merge"
        ),
        "applied_file_count": sum(
            len(event["applied_files"]) for event in ledger["events"] if event["kind"] == "merge"
        ),
        "deduplicated_file_count": sum(
            len(event["deduplicated_files"])
            for event in ledger["events"]
            if event["kind"] == "merge"
        ),
    }


def ledger_sha256(ledger: Dict) -> str:
    validate_agent_team_merge_ledger(ledger)
    payload = json.dumps(ledger, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def agent_team_state_sha256(state: Dict) -> str:
    validate_agent_team_state(state)
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _transaction_sha256(transaction: Dict) -> str:
    unhashed = dict(transaction)
    unhashed.pop("transaction_sha256", None)
    payload = json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _intent_sha256(intent: Dict) -> str:
    unhashed = dict(intent)
    unhashed.pop("intent_sha256", None)
    payload = json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_paths(value, label: str) -> None:
    if (
        not isinstance(value, list)
        or len(value) > MAX_STAGED_CHANGES
        or value != sorted(set(value))
        or not all(isinstance(path, str) and path for path in value)
    ):
        raise ValidationError("%s is invalid" % label)
    for path in value:
        require_no_path_escape(path)


def _validate_id(value, label: str) -> None:
    if not isinstance(value, str) or not SAFE_ID.match(value):
        raise ValidationError("%s is invalid" % label)


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or not SHA256_PATTERN.match(value):
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


def _object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value
