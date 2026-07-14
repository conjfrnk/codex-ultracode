import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List

from .agent_team import (
    AGENT_TEAM_STATE_SCHEMA,
    AGENT_TEAM_STATE_SCHEMA_V3,
    AGENT_TEAM_STATE_SCHEMA_V4,
    AGENT_TEAM_STATE_SCHEMA_V5,
    MAX_AGENT_TEAM_GENERATION,
    MAX_AGENT_TEAM_MESSAGE_CHARS,
    MAX_AGENT_TEAM_ROUNDS,
)
from .clock import utc_now
from .errors import ValidationError
from .security import (
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
)


AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1 = "conductor.agent_team_operator_inbox.v1"
AGENT_TEAM_OPERATOR_INBOX_SCHEMA = "conductor.agent_team_operator_inbox.v2"
AGENT_TEAM_OPERATOR_INBOX_FIELDS = {
    "schema",
    "step_id",
    "workflow_fingerprint",
    "next_sequence",
    "entries",
}
AGENT_TEAM_OPERATOR_ENTRY_FIELDS_V1 = {
    "id",
    "generation",
    "created_at_utc",
    "member_id",
    "instruction",
    "instruction_sha256",
    "status",
    "task_id",
    "accepted_round",
    "superseded_at_utc",
}
AGENT_TEAM_OPERATOR_ENTRY_FIELDS = AGENT_TEAM_OPERATOR_ENTRY_FIELDS_V1 | {
    "delivery",
    "interrupt_round",
    "interrupt_task_id",
    "interrupted_at_utc",
}
AGENT_TEAM_OPERATOR_ENTRY_STATUSES = {"pending", "accepted", "superseded"}
AGENT_TEAM_OPERATOR_DELIVERIES = {"next-turn", "interrupt-current"}
MAX_AGENT_TEAM_OPERATOR_INBOX_ENTRIES = 1024
MAX_AGENT_TEAM_OPERATOR_INBOX_BYTES = 4 * 1024 * 1024
MAX_AGENT_TEAM_OPERATOR_SEQUENCE = 10**9
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def initial_agent_team_operator_inbox(
    step_id: str,
    workflow_fingerprint: str,
    *,
    schema: str = AGENT_TEAM_OPERATOR_INBOX_SCHEMA,
) -> Dict:
    inbox = {
        "schema": schema,
        "step_id": step_id,
        "workflow_fingerprint": workflow_fingerprint,
        "next_sequence": 1,
        "entries": [],
    }
    validate_agent_team_operator_inbox(inbox)
    return inbox


def append_agent_team_operator_entry(
    inbox: Dict,
    *,
    generation: int,
    member_id: str,
    instruction: str,
    delivery: str = "next-turn",
    interrupt_round: int = None,
    interrupt_task_id: str = None,
) -> Dict:
    validate_agent_team_operator_inbox(inbox)
    if len(inbox["entries"]) >= MAX_AGENT_TEAM_OPERATOR_INBOX_ENTRIES:
        raise ValidationError("agent team operator inbox entry limit exceeded")
    if not isinstance(generation, int) or isinstance(generation, bool) or not 0 <= generation <= MAX_AGENT_TEAM_GENERATION:
        raise ValidationError("agent team operator generation is invalid")
    _validate_id(member_id, "agent team operator member_id")
    _validate_text(instruction, "agent team operator instruction", MAX_AGENT_TEAM_MESSAGE_CHARS)
    if delivery not in AGENT_TEAM_OPERATOR_DELIVERIES:
        raise ValidationError("agent team operator delivery is invalid")
    if inbox["schema"] == AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1 and delivery != "next-turn":
        raise ValidationError("legacy agent team operator inbox cannot interrupt a turn")
    if delivery == "interrupt-current":
        if (
            not isinstance(interrupt_round, int)
            or isinstance(interrupt_round, bool)
            or not 1 <= interrupt_round <= MAX_AGENT_TEAM_ROUNDS
        ):
            raise ValidationError("agent team operator interrupt round is invalid")
        _validate_id(interrupt_task_id, "agent team operator interrupt task_id")
    elif interrupt_round is not None or interrupt_task_id is not None:
        raise ValidationError("next-turn operator entry cannot carry interrupt metadata")
    sequence = inbox["next_sequence"]
    entry = {
        "id": "operator-message-%06d" % sequence,
        "generation": generation,
        "created_at_utc": _utc_now(),
        "member_id": member_id,
        "instruction": instruction,
        "instruction_sha256": _sha256_text(instruction),
        "status": "pending",
        "task_id": "operator-g%09d-%06d" % (generation, sequence),
        "accepted_round": None,
        "superseded_at_utc": None,
    }
    if inbox["schema"] == AGENT_TEAM_OPERATOR_INBOX_SCHEMA:
        entry.update(
            {
                "delivery": delivery,
                "interrupt_round": interrupt_round,
                "interrupt_task_id": interrupt_task_id,
                "interrupted_at_utc": None,
            }
        )
    inbox["entries"].append(entry)
    inbox["next_sequence"] += 1
    validate_agent_team_operator_inbox(inbox)
    return dict(entry)


def mark_agent_team_operator_entry_accepted(entry: Dict, round_number: int) -> None:
    if entry.get("status") != "pending":
        raise ValidationError("only a pending operator entry can be accepted")
    if not isinstance(round_number, int) or isinstance(round_number, bool) or not 0 <= round_number <= MAX_AGENT_TEAM_ROUNDS:
        raise ValidationError("agent team operator accepted round is invalid")
    entry["status"] = "accepted"
    entry["accepted_round"] = round_number


def mark_agent_team_operator_entry_interrupted(entry: Dict, interrupted_at_utc: str) -> None:
    if entry.get("status") != "pending":
        raise ValidationError("only a pending operator entry can interrupt a turn")
    if entry.get("delivery") != "interrupt-current":
        raise ValidationError("only interrupt-current operator entries can interrupt a turn")
    if entry.get("interrupted_at_utc") is not None:
        raise ValidationError("operator entry already interrupted a turn")
    _validate_timestamp(interrupted_at_utc, "agent team operator interrupted_at_utc")
    entry["interrupted_at_utc"] = interrupted_at_utc


def supersede_stale_agent_team_operator_entries(inbox: Dict, generation: int) -> int:
    validate_agent_team_operator_inbox(inbox)
    changed = 0
    for entry in inbox["entries"]:
        if entry["generation"] > generation:
            raise ValidationError("agent team operator inbox contains a future generation")
        if entry["generation"] < generation and entry["status"] == "pending":
            entry["status"] = "superseded"
            entry["superseded_at_utc"] = _utc_now()
            changed += 1
    validate_agent_team_operator_inbox(inbox)
    return changed


def validate_agent_team_operator_inbox(
    inbox: Dict,
    source: str = "agent team operator inbox",
) -> None:
    if not isinstance(inbox, dict) or set(inbox) != AGENT_TEAM_OPERATOR_INBOX_FIELDS:
        raise ValidationError("%s has invalid fields" % source)
    inbox_schema = inbox.get("schema")
    if inbox_schema not in {
        AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1,
        AGENT_TEAM_OPERATOR_INBOX_SCHEMA,
    }:
        raise ValidationError("%s schema is invalid" % source)
    _validate_id(inbox.get("step_id"), "%s step_id" % source)
    if not isinstance(inbox.get("workflow_fingerprint"), str) or not SHA256_PATTERN.fullmatch(inbox["workflow_fingerprint"]):
        raise ValidationError("%s workflow_fingerprint is invalid" % source)
    sequence = inbox.get("next_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or not 1 <= sequence <= MAX_AGENT_TEAM_OPERATOR_SEQUENCE:
        raise ValidationError("%s next_sequence is invalid" % source)
    entries = inbox.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_AGENT_TEAM_OPERATOR_INBOX_ENTRIES:
        raise ValidationError("%s entries are invalid" % source)
    for index, entry in enumerate(entries, start=1):
        entry_fields = (
            AGENT_TEAM_OPERATOR_ENTRY_FIELDS_V1
            if inbox_schema == AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1
            else AGENT_TEAM_OPERATOR_ENTRY_FIELDS
        )
        if not isinstance(entry, dict) or set(entry) != entry_fields:
            raise ValidationError("%s entry has invalid fields" % source)
        if entry.get("id") != "operator-message-%06d" % index:
            raise ValidationError("%s entry id sequence is invalid" % source)
        generation = entry.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or not 0 <= generation <= MAX_AGENT_TEAM_GENERATION:
            raise ValidationError("%s entry generation is invalid" % source)
        _validate_timestamp(entry.get("created_at_utc"), "%s entry created_at_utc" % source)
        _validate_id(entry.get("member_id"), "%s entry member_id" % source)
        _validate_text(entry.get("instruction"), "%s entry instruction" % source, MAX_AGENT_TEAM_MESSAGE_CHARS)
        if entry.get("instruction_sha256") != _sha256_text(entry["instruction"]):
            raise ValidationError("%s entry instruction hash is invalid" % source)
        status = entry.get("status")
        if status not in AGENT_TEAM_OPERATOR_ENTRY_STATUSES:
            raise ValidationError("%s entry status is invalid" % source)
        _validate_id(entry.get("task_id"), "%s entry task_id" % source)
        accepted_round = entry.get("accepted_round")
        superseded_at = entry.get("superseded_at_utc")
        if inbox_schema == AGENT_TEAM_OPERATOR_INBOX_SCHEMA:
            delivery = entry.get("delivery")
            if delivery not in AGENT_TEAM_OPERATOR_DELIVERIES:
                raise ValidationError("%s entry delivery is invalid" % source)
            interrupt_round = entry.get("interrupt_round")
            interrupt_task_id = entry.get("interrupt_task_id")
            interrupted_at = entry.get("interrupted_at_utc")
            if delivery == "next-turn":
                if (
                    interrupt_round is not None
                    or interrupt_task_id is not None
                    or interrupted_at is not None
                ):
                    raise ValidationError(
                        "%s next-turn entry has interrupt metadata" % source
                    )
            else:
                if (
                    not isinstance(interrupt_round, int)
                    or isinstance(interrupt_round, bool)
                    or not 1 <= interrupt_round <= MAX_AGENT_TEAM_ROUNDS
                ):
                    raise ValidationError("%s entry interrupt_round is invalid" % source)
                _validate_id(
                    interrupt_task_id,
                    "%s entry interrupt_task_id" % source,
                )
                if interrupted_at is not None:
                    _validate_timestamp(
                        interrupted_at,
                        "%s entry interrupted_at_utc" % source,
                    )
        if status == "pending":
            if accepted_round is not None or superseded_at is not None:
                raise ValidationError("%s pending entry has terminal metadata" % source)
        elif status == "accepted":
            if not isinstance(accepted_round, int) or isinstance(accepted_round, bool) or not 0 <= accepted_round <= MAX_AGENT_TEAM_ROUNDS:
                raise ValidationError("%s accepted entry round is invalid" % source)
            if superseded_at is not None:
                raise ValidationError("%s accepted entry has superseded metadata" % source)
        else:
            if accepted_round is not None:
                raise ValidationError("%s superseded entry has an accepted round" % source)
            _validate_timestamp(superseded_at, "%s superseded_at_utc" % source)
    if sequence != len(entries) + 1:
        raise ValidationError("%s next_sequence is inconsistent" % source)


def load_agent_team_operator_inbox(path: Path) -> Dict:
    reject_symlink_path(path, "agent team operator inbox")
    text = read_regular_text_file_no_follow(
        path,
        "agent team operator inbox",
        MAX_AGENT_TEAM_OPERATOR_INBOX_BYTES,
    )
    try:
        inbox = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("agent team operator inbox is invalid JSON: %s" % exc.__class__.__name__)
    validate_agent_team_operator_inbox(inbox, source=str(path))
    return inbox


def write_agent_team_operator_inbox(path: Path, inbox: Dict) -> None:
    validate_agent_team_operator_inbox(inbox)
    parent_fd = ensure_dir_no_follow(path.parent, "agent team operator inbox parent")
    os.close(parent_fd)
    text = json.dumps(inbox, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_AGENT_TEAM_OPERATOR_INBOX_BYTES:
        raise ValidationError(
            "agent team operator inbox exceeds %d bytes" % MAX_AGENT_TEAM_OPERATOR_INBOX_BYTES
        )
    replace_text_file_no_follow(path, "agent team operator inbox", text, ".agent-team-operator-")


def agent_team_operator_inbox_summary(inbox: Dict) -> Dict:
    validate_agent_team_operator_inbox(inbox)
    return {
        "schema": inbox["schema"],
        "step_id": inbox["step_id"],
        "workflow_fingerprint": inbox["workflow_fingerprint"],
        "entry_count": len(inbox["entries"]),
        "status_counts": {
            status: sum(entry["status"] == status for entry in inbox["entries"])
            for status in sorted(AGENT_TEAM_OPERATOR_ENTRY_STATUSES)
        },
        "entries": [
            {
                "id": entry["id"],
                "generation": entry["generation"],
                "created_at_utc": entry["created_at_utc"],
                "member_id": entry["member_id"],
                "instruction_sha256": entry["instruction_sha256"],
                "delivery": entry.get("delivery", "next-turn"),
                "interrupt_round": entry.get("interrupt_round"),
                "interrupt_task_id": entry.get("interrupt_task_id"),
                "interrupted_at_utc": entry.get("interrupted_at_utc"),
                "status": entry["status"],
                "task_id": entry["task_id"],
                "accepted_round": entry["accepted_round"],
                "superseded_at_utc": entry["superseded_at_utc"],
            }
            for entry in inbox["entries"]
        ],
    }


def reconcile_agent_team_operator_interruptions(inbox: Dict, state: Dict) -> int:
    validate_agent_team_operator_inbox(inbox)
    if state.get("schema") not in {
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    }:
        if any(turn.get("status") == "interrupted" for turn in state.get("turns", [])):
            raise ValidationError("legacy agent team state contains an interruption")
        return 0
    if inbox["schema"] != AGENT_TEAM_OPERATOR_INBOX_SCHEMA:
        raise ValidationError("current agent team state requires a current operator inbox")
    entries = {entry["id"]: entry for entry in inbox["entries"]}
    changed = 0
    for turn in state["turns"]:
        if turn["status"] != "interrupted":
            continue
        entry = entries.get(turn["interruption_entry_id"])
        if entry is None:
            raise ValidationError("agent team interruption has no operator inbox entry")
        _verify_interruption_binding(entry, turn, state)
        if entry["interrupted_at_utc"] is None:
            mark_agent_team_operator_entry_interrupted(
                entry,
                turn["finished_at_utc"],
            )
            changed += 1
        elif entry["interrupted_at_utc"] != turn["finished_at_utc"]:
            raise ValidationError("agent team operator interruption timestamp changed")
    validate_agent_team_operator_inbox(inbox)
    return changed


def verify_agent_team_operator_inbox(inbox: Dict, state: Dict) -> None:
    validate_agent_team_operator_inbox(inbox)
    state_schema = state.get("schema")
    if state_schema not in {
        AGENT_TEAM_STATE_SCHEMA_V3,
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    }:
        raise ValidationError("legacy agent team state cannot bind an operator inbox")
    expected_inbox_schema = (
        AGENT_TEAM_OPERATOR_INBOX_SCHEMA
        if state_schema in {
            AGENT_TEAM_STATE_SCHEMA_V4,
            AGENT_TEAM_STATE_SCHEMA_V5,
            AGENT_TEAM_STATE_SCHEMA,
        }
        else AGENT_TEAM_OPERATOR_INBOX_SCHEMA_V1
    )
    if inbox["schema"] != expected_inbox_schema:
        raise ValidationError("agent team operator inbox schema does not match team state")
    if inbox["step_id"] != state.get("step_id"):
        raise ValidationError("agent team operator inbox step binding changed")
    if inbox["workflow_fingerprint"] != state.get("workflow_fingerprint"):
        raise ValidationError("agent team operator inbox workflow binding changed")
    accepted = {
        entry["id"]: entry
        for entry in inbox["entries"]
        if entry["generation"] == state["generation"] and entry["status"] == "accepted"
    }
    operator_tasks = {
        task["operator_entry_id"]: task
        for task in state["tasks"]
        if task["origin"] == "operator"
    }
    if set(accepted) != set(operator_tasks):
        raise ValidationError("agent team operator inbox task ledger is inconsistent")
    for entry_id, entry in accepted.items():
        task = operator_tasks[entry_id]
        if (
            task["id"] != entry["task_id"]
            or task["description"] != entry["instruction"]
            or task["assignee"] != entry["member_id"]
            or task["proposed_round"] != entry["accepted_round"]
        ):
            raise ValidationError("agent team operator inbox task binding changed")
    interruption_turns = {
        turn.get("interruption_entry_id"): turn
        for turn in state["turns"]
        if turn["status"] == "interrupted"
    }
    if state_schema in {
        AGENT_TEAM_STATE_SCHEMA_V4,
        AGENT_TEAM_STATE_SCHEMA_V5,
        AGENT_TEAM_STATE_SCHEMA,
    }:
        for entry in inbox["entries"]:
            if entry["generation"] != state["generation"]:
                continue
            turn = interruption_turns.get(entry["id"])
            if entry["interrupted_at_utc"] is None:
                if turn is not None:
                    raise ValidationError(
                        "agent team operator interruption is not reconciled"
                    )
                continue
            if turn is None:
                raise ValidationError(
                    "agent team operator inbox interruption has no state turn"
                )
            _verify_interruption_binding(entry, turn, state)
            if entry["interrupted_at_utc"] != turn["finished_at_utc"]:
                raise ValidationError("agent team operator interruption timestamp changed")
        current_entry_ids = {
            entry["id"]
            for entry in inbox["entries"]
            if entry["generation"] == state["generation"]
        }
        if set(interruption_turns) - current_entry_ids:
            raise ValidationError("agent team interruption ledger is incomplete")


def _verify_interruption_binding(entry: Dict, turn: Dict, state: Dict) -> None:
    if (
        entry.get("generation") != state.get("generation")
        or entry.get("delivery") != "interrupt-current"
        or entry.get("member_id") != turn.get("member_id")
        or entry.get("interrupt_round") != turn.get("round")
        or entry.get("interrupt_task_id") != turn.get("task_id")
        or entry.get("instruction_sha256")
        != turn.get("interruption_instruction_sha256")
    ):
        raise ValidationError("agent team operator interruption binding changed")


@contextmanager
def agent_team_operator_inbox_lock(path: Path) -> Iterator[None]:
    parent_fd = ensure_dir_no_follow(path.parent, "agent team operator inbox parent")
    lock_name = ".operator-inbox.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = None
    try:
        lock_fd = os.open(lock_name, flags, 0o600, dir_fd=parent_fd)
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValidationError("agent team operator inbox lock must be a single-link regular file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise ValidationError("failed to lock agent team operator inbox: %s" % exc.__class__.__name__)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(parent_fd)


def _validate_id(value, label: str) -> None:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValidationError("%s is invalid" % label)


def _validate_text(value, label: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError("%s must be a non-empty string of at most %d characters" % (label, maximum))


def _validate_timestamp(value, label: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError("%s is invalid" % label)
    try:
        datetime.fromisoformat(value[:-1])
    except ValueError:
        raise ValidationError("%s is invalid" % label)


def _utc_now() -> str:
    return utc_now().isoformat(timespec="milliseconds") + "Z"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _object_without_duplicate_keys(pairs: List) -> Dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result
