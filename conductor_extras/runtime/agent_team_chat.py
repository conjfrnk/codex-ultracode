import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List

from .agent_team import (
    AGENT_TEAM_OPERATOR_REPLY_TASK_PREFIX,
    AGENT_TEAM_STATE_SCHEMA,
    MAX_AGENT_TEAM_GENERATION,
    MAX_AGENT_TEAM_MESSAGE_CHARS,
    MAX_AGENT_TEAM_ROUNDS,
    validate_agent_team_state,
)
from .errors import ValidationError
from .security import (
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
)


AGENT_TEAM_OPERATOR_CHAT_SCHEMA = "conductor.agent_team_operator_chat.v1"
AGENT_TEAM_OPERATOR_CHAT_FIELDS = {
    "schema",
    "step_id",
    "workflow_fingerprint",
    "entries",
}
AGENT_TEAM_OPERATOR_CHAT_ENTRY_FIELDS = {
    "id",
    "generation",
    "message_id",
    "member_id",
    "source_task_id",
    "asked_round",
    "asked_at_utc",
    "question",
    "question_sha256",
    "status",
    "reply",
    "reply_sha256",
    "answered_at_utc",
    "response_task_id",
    "delivered_round",
    "superseded_at_utc",
}
AGENT_TEAM_OPERATOR_CHAT_STATUSES = {
    "pending",
    "answered",
    "delivered",
    "superseded",
}
MAX_AGENT_TEAM_OPERATOR_CHAT_ENTRIES = 1024
MAX_AGENT_TEAM_OPERATOR_CHAT_BYTES = 4 * 1024 * 1024
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def initial_agent_team_operator_chat(step_id: str, workflow_fingerprint: str) -> Dict:
    chat = {
        "schema": AGENT_TEAM_OPERATOR_CHAT_SCHEMA,
        "step_id": step_id,
        "workflow_fingerprint": workflow_fingerprint,
        "entries": [],
    }
    validate_agent_team_operator_chat(chat)
    return chat


def reconcile_agent_team_operator_chat(chat: Dict, state: Dict) -> int:
    validate_agent_team_operator_chat(chat)
    validate_agent_team_state(state)
    if state["schema"] != AGENT_TEAM_STATE_SCHEMA:
        raise ValidationError("legacy agent team state cannot bind operator chat")
    if chat["step_id"] != state["step_id"]:
        raise ValidationError("agent team operator chat step binding changed")
    if chat["workflow_fingerprint"] != state["workflow_fingerprint"]:
        raise ValidationError("agent team operator chat workflow binding changed")

    changed = 0
    for entry in chat["entries"]:
        if entry["generation"] > state["generation"]:
            raise ValidationError("agent team operator chat contains a future generation")
        if (
            entry["generation"] < state["generation"]
            and entry["status"] in {"pending", "answered"}
        ):
            entry["status"] = "superseded"
            entry["superseded_at_utc"] = _utc_now()
            entry["delivered_round"] = None
            changed += 1

    current_entries = {
        entry["message_id"]: entry
        for entry in chat["entries"]
        if entry["generation"] == state["generation"]
    }
    if len(current_entries) != sum(
        entry["generation"] == state["generation"] for entry in chat["entries"]
    ):
        raise ValidationError("agent team operator chat message binding is duplicated")
    parent_turns = {
        (turn["round"], turn["member_id"]): turn
        for turn in state["turns"]
        if turn["status"] != "interrupted"
    }
    for message in state["messages"]:
        if message["to"] != "operator":
            continue
        parent = parent_turns.get((message["round"], message["from"]))
        if parent is None:
            raise ValidationError("agent team operator question has no accepted sender turn")
        entry = current_entries.get(message["id"])
        if entry is None:
            entry = _question_entry(state, message, parent)
            if len(chat["entries"]) >= MAX_AGENT_TEAM_OPERATOR_CHAT_ENTRIES:
                raise ValidationError("agent team operator chat entry limit exceeded")
            chat["entries"].append(entry)
            current_entries[message["id"]] = entry
            changed += 1

    tasks = {
        task["message_id"]: task
        for task in state["tasks"]
        if task.get("origin") == "operator-reply"
    }
    for entry in current_entries.values():
        task = tasks.get(entry["message_id"])
        if task is None:
            raise ValidationError("agent team operator chat response task is missing")
        _verify_entry_state_binding(entry, state, task)
        if task["status"] == "waiting":
            if entry["status"] == "delivered":
                raise ValidationError("agent team delivered operator reply is still waiting")
            continue
        if entry["status"] == "pending":
            raise ValidationError("agent team operator reply task activated without an answer")
        if entry["status"] == "answered":
            entry["status"] = "delivered"
            entry["delivered_round"] = state["round"]
            changed += 1

    validate_agent_team_operator_chat(chat)
    verify_agent_team_operator_chat(chat, state)
    return changed


def answer_agent_team_operator_question(chat: Dict, question_id: str, reply: str) -> Dict:
    validate_agent_team_operator_chat(chat)
    _validate_id(question_id, "agent team operator question id")
    _validate_text(reply, "agent team operator reply", MAX_AGENT_TEAM_MESSAGE_CHARS)
    matches = [entry for entry in chat["entries"] if entry["id"] == question_id]
    if len(matches) != 1:
        raise ValidationError("agent team operator question is unknown")
    entry = matches[0]
    if entry["status"] != "pending":
        raise ValidationError("agent team operator question is not pending")
    entry["status"] = "answered"
    entry["reply"] = reply
    entry["reply_sha256"] = _sha256_text(reply)
    entry["answered_at_utc"] = _utc_now()
    validate_agent_team_operator_chat(chat)
    return dict(entry)


def verify_agent_team_operator_chat(chat: Dict, state: Dict) -> None:
    validate_agent_team_operator_chat(chat)
    validate_agent_team_state(state)
    if state["schema"] != AGENT_TEAM_STATE_SCHEMA:
        raise ValidationError("legacy agent team state cannot bind operator chat")
    if chat["step_id"] != state["step_id"]:
        raise ValidationError("agent team operator chat step binding changed")
    if chat["workflow_fingerprint"] != state["workflow_fingerprint"]:
        raise ValidationError("agent team operator chat workflow binding changed")
    current_entries = {
        entry["message_id"]: entry
        for entry in chat["entries"]
        if entry["generation"] == state["generation"]
    }
    operator_messages = {
        message["id"]: message for message in state["messages"] if message["to"] == "operator"
    }
    if set(current_entries) != set(operator_messages):
        raise ValidationError("agent team operator chat question ledger is inconsistent")
    parent_turns = {
        (turn["round"], turn["member_id"]): turn
        for turn in state["turns"]
        if turn["status"] != "interrupted"
    }
    tasks = {
        task["message_id"]: task
        for task in state["tasks"]
        if task.get("origin") == "operator-reply"
    }
    if set(tasks) != set(operator_messages):
        raise ValidationError("agent team operator chat response-task ledger is inconsistent")
    for message_id, entry in current_entries.items():
        message = operator_messages[message_id]
        parent = parent_turns.get((message["round"], message["from"]))
        if parent is None:
            raise ValidationError("agent team operator question has no accepted sender turn")
        if (
            entry["member_id"] != message["from"]
            or entry["source_task_id"] != parent["task_id"]
            or entry["asked_round"] != message["round"]
            or entry["asked_at_utc"] != parent["finished_at_utc"]
            or entry["question"] != message["body"]
            or entry["question_sha256"] != message["body_sha256"]
        ):
            raise ValidationError("agent team operator question binding changed")
        task = tasks[message_id]
        _verify_entry_state_binding(entry, state, task)
        if task["status"] == "waiting" and entry["status"] not in {"pending", "answered"}:
            raise ValidationError("agent team operator reply delivery state changed")
        if task["status"] != "waiting" and entry["status"] != "delivered":
            raise ValidationError("agent team operator reply delivery is not reconciled")


def agent_team_operator_chat_summary(chat: Dict) -> Dict:
    validate_agent_team_operator_chat(chat)
    return {
        "schema": chat["schema"],
        "step_id": chat["step_id"],
        "entry_count": len(chat["entries"]),
        "status_counts": {
            status: sum(entry["status"] == status for entry in chat["entries"])
            for status in sorted(AGENT_TEAM_OPERATOR_CHAT_STATUSES)
        },
        "entries": [
            {
                field: entry[field]
                for field in (
                    "id",
                    "generation",
                    "message_id",
                    "member_id",
                    "source_task_id",
                    "asked_round",
                    "asked_at_utc",
                    "question_sha256",
                    "status",
                    "reply_sha256",
                    "answered_at_utc",
                    "response_task_id",
                    "delivered_round",
                    "superseded_at_utc",
                )
            }
            for entry in chat["entries"]
        ],
    }


def validate_agent_team_operator_chat(
    chat: Dict,
    source: str = "agent team operator chat",
) -> None:
    if not isinstance(chat, dict) or set(chat) != AGENT_TEAM_OPERATOR_CHAT_FIELDS:
        raise ValidationError("%s has invalid fields" % source)
    if chat.get("schema") != AGENT_TEAM_OPERATOR_CHAT_SCHEMA:
        raise ValidationError("%s schema is invalid" % source)
    _validate_id(chat.get("step_id"), "%s step_id" % source)
    _validate_sha256(chat.get("workflow_fingerprint"), "%s workflow_fingerprint" % source)
    entries = chat.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_AGENT_TEAM_OPERATOR_CHAT_ENTRIES:
        raise ValidationError("%s entries are invalid" % source)
    observed_ids = set()
    observed_bindings = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != AGENT_TEAM_OPERATOR_CHAT_ENTRY_FIELDS:
            raise ValidationError("%s entry has invalid fields" % source)
        _validate_id(entry.get("id"), "%s entry id" % source)
        if entry["id"] in observed_ids:
            raise ValidationError("%s entry id is duplicated" % source)
        observed_ids.add(entry["id"])
        generation = entry.get("generation")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or not 0 <= generation <= MAX_AGENT_TEAM_GENERATION
        ):
            raise ValidationError("%s entry generation is invalid" % source)
        _validate_id(entry.get("message_id"), "%s entry message_id" % source)
        binding = (generation, entry["message_id"])
        if binding in observed_bindings:
            raise ValidationError("%s entry message binding is duplicated" % source)
        observed_bindings.add(binding)
        message_index = _message_index(entry["message_id"], source)
        expected_id = "operator-question-g%09d-%04d" % (generation, message_index)
        if entry["id"] != expected_id:
            raise ValidationError("%s entry id binding is invalid" % source)
        _validate_id(entry.get("member_id"), "%s entry member_id" % source)
        _validate_id(entry.get("source_task_id"), "%s entry source_task_id" % source)
        _validate_round(entry.get("asked_round"), "%s entry asked_round" % source)
        _validate_timestamp(entry.get("asked_at_utc"), "%s entry asked_at_utc" % source)
        _validate_text(entry.get("question"), "%s entry question" % source, MAX_AGENT_TEAM_MESSAGE_CHARS)
        if entry.get("question_sha256") != _sha256_text(entry["question"]):
            raise ValidationError("%s entry question hash is invalid" % source)
        expected_task_id = "%s%04d" % (AGENT_TEAM_OPERATOR_REPLY_TASK_PREFIX, message_index)
        if entry.get("response_task_id") != expected_task_id:
            raise ValidationError("%s entry response task binding is invalid" % source)
        status = entry.get("status")
        if status not in AGENT_TEAM_OPERATOR_CHAT_STATUSES:
            raise ValidationError("%s entry status is invalid" % source)
        reply = entry.get("reply")
        reply_sha256 = entry.get("reply_sha256")
        answered_at = entry.get("answered_at_utc")
        delivered_round = entry.get("delivered_round")
        superseded_at = entry.get("superseded_at_utc")
        if reply is None:
            if reply_sha256 is not None or answered_at is not None:
                raise ValidationError("%s entry reply metadata is incomplete" % source)
        else:
            _validate_text(reply, "%s entry reply" % source, MAX_AGENT_TEAM_MESSAGE_CHARS)
            if reply_sha256 != _sha256_text(reply):
                raise ValidationError("%s entry reply hash is invalid" % source)
            _validate_timestamp(answered_at, "%s entry answered_at_utc" % source)
        if status == "pending":
            if reply is not None or delivered_round is not None or superseded_at is not None:
                raise ValidationError("%s pending entry has terminal metadata" % source)
        elif status == "answered":
            if reply is None or delivered_round is not None or superseded_at is not None:
                raise ValidationError("%s answered entry metadata is invalid" % source)
        elif status == "delivered":
            if reply is None or superseded_at is not None:
                raise ValidationError("%s delivered entry metadata is invalid" % source)
            _validate_round(delivered_round, "%s entry delivered_round" % source, minimum=0)
        else:
            if delivered_round is not None:
                raise ValidationError("%s superseded entry has delivery metadata" % source)
            _validate_timestamp(superseded_at, "%s entry superseded_at_utc" % source)


def load_agent_team_operator_chat(path: Path) -> Dict:
    reject_symlink_path(path, "agent team operator chat")
    text = read_regular_text_file_no_follow(
        path,
        "agent team operator chat",
        MAX_AGENT_TEAM_OPERATOR_CHAT_BYTES,
    )
    try:
        chat = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError(
            "agent team operator chat is invalid JSON: %s" % exc.__class__.__name__
        )
    validate_agent_team_operator_chat(chat, source=str(path))
    return chat


def write_agent_team_operator_chat(path: Path, chat: Dict) -> None:
    validate_agent_team_operator_chat(chat)
    parent_fd = ensure_dir_no_follow(path.parent, "agent team operator chat parent")
    os.close(parent_fd)
    text = json.dumps(chat, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_AGENT_TEAM_OPERATOR_CHAT_BYTES:
        raise ValidationError(
            "agent team operator chat exceeds %d bytes" % MAX_AGENT_TEAM_OPERATOR_CHAT_BYTES
        )
    replace_text_file_no_follow(path, "agent team operator chat", text, ".agent-team-chat-")


@contextmanager
def agent_team_operator_chat_lock(path: Path) -> Iterator[None]:
    parent_fd = ensure_dir_no_follow(path.parent, "agent team operator chat parent")
    lock_name = ".operator-chat.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = None
    try:
        lock_fd = os.open(lock_name, flags, 0o600, dir_fd=parent_fd)
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValidationError("agent team operator chat lock must be a single-link regular file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise ValidationError(
            "failed to lock agent team operator chat: %s" % exc.__class__.__name__
        )
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(parent_fd)


def _question_entry(state: Dict, message: Dict, parent_turn: Dict) -> Dict:
    message_index = _message_index(message["id"], "agent team operator question")
    generation = state["generation"]
    return {
        "id": "operator-question-g%09d-%04d" % (generation, message_index),
        "generation": generation,
        "message_id": message["id"],
        "member_id": message["from"],
        "source_task_id": parent_turn["task_id"],
        "asked_round": message["round"],
        "asked_at_utc": parent_turn["finished_at_utc"],
        "question": message["body"],
        "question_sha256": message["body_sha256"],
        "status": "pending",
        "reply": None,
        "reply_sha256": None,
        "answered_at_utc": None,
        "response_task_id": "%s%04d" % (AGENT_TEAM_OPERATOR_REPLY_TASK_PREFIX, message_index),
        "delivered_round": None,
        "superseded_at_utc": None,
    }


def _verify_entry_state_binding(entry: Dict, state: Dict, task: Dict) -> None:
    if (
        task.get("id") != entry["response_task_id"]
        or task.get("origin") != "operator-reply"
        or task.get("message_id") != entry["message_id"]
        or task.get("proposed_by") != entry["member_id"]
        or task.get("assignee") != entry["member_id"]
        or task.get("proposed_round") != entry["asked_round"]
    ):
        raise ValidationError("agent team operator chat response task binding changed")
    if task["status"] != "waiting":
        if (
            entry.get("reply") != task.get("description")
            or entry.get("reply_sha256") != task.get("operator_reply_sha256")
        ):
            raise ValidationError("agent team operator reply text binding changed")
    if entry["status"] == "delivered":
        delivered_round = entry["delivered_round"]
        if delivered_round is None or delivered_round > state["round"]:
            raise ValidationError("agent team operator reply delivery round is invalid")


def _message_index(message_id: str, source: str) -> int:
    match = re.fullmatch(r"message-(\d{4})", str(message_id))
    if match is None or int(match.group(1)) < 1:
        raise ValidationError("%s message id is invalid" % source)
    return int(match.group(1))


def _validate_id(value, label: str) -> None:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValidationError("%s is invalid" % label)


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ValidationError("%s is invalid" % label)


def _validate_text(value, label: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError(
            "%s must be a non-empty string of at most %d characters" % (label, maximum)
        )


def _validate_round(value, label: str, minimum: int = 1) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= MAX_AGENT_TEAM_ROUNDS
    ):
        raise ValidationError("%s is invalid" % label)


def _validate_timestamp(value, label: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError("%s is invalid" % label)
    try:
        datetime.fromisoformat(value[:-1])
    except ValueError:
        raise ValidationError("%s is invalid" % label)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _object_without_duplicate_keys(pairs: List) -> Dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result
