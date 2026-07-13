import json
import mmap
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import Dict, Optional

from .errors import ValidationError
from .provider_telemetry import MAX_PROVIDER_TOKENS
from .security import open_dir_no_follow, reject_symlink_path


MAX_NATIVE_USAGE_SESSIONS = 256
MAX_NATIVE_ROLLOUT_BYTES = 512 * 1024 * 1024
MAX_NATIVE_ROLLOUT_TAIL_BYTES = 8 * 1024 * 1024
MAX_NATIVE_TOKEN_EVENT_BYTES = 256 * 1024
NATIVE_USAGE_FIELDS = {
    "status",
    "session_count",
    "child_count",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "total_tokens",
    "rollout_tokens",
}
NATIVE_USAGE_STATUSES = {"complete", "unavailable", "not-requested"}
_STATE_DATABASE = re.compile(r"^state_([0-9]{1,6})\.sqlite$")


class NativeUsageUnavailable(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def empty_native_usage(status: str) -> Dict:
    if status not in NATIVE_USAGE_STATUSES - {"complete"}:
        raise ValidationError("native usage status is invalid")
    return {
        "status": status,
        "session_count": 0,
        "child_count": 0,
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "rollout_tokens": None,
    }


def validate_native_usage(value: Dict) -> None:
    if not isinstance(value, dict) or set(value) != NATIVE_USAGE_FIELDS:
        raise ValidationError("native usage has invalid fields")
    status_value = value.get("status")
    if status_value not in NATIVE_USAGE_STATUSES:
        raise ValidationError("native usage status is invalid")
    session_count = _bounded_int(
        value.get("session_count"),
        0,
        MAX_NATIVE_USAGE_SESSIONS,
        "session_count",
    )
    child_count = _bounded_int(
        value.get("child_count"),
        0,
        MAX_NATIVE_USAGE_SESSIONS - 1,
        "child_count",
    )
    token_fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "total_tokens",
        "rollout_tokens",
    )
    tokens = {}
    for field in token_fields:
        raw = value.get(field)
        if raw is not None:
            tokens[field] = _bounded_int(raw, 0, MAX_PROVIDER_TOKENS, field)
        else:
            tokens[field] = None
    if status_value != "complete":
        if session_count or child_count or any(tokens[field] is not None for field in token_fields):
            raise ValidationError("unavailable native usage must not claim attribution")
        return
    if session_count < 1 or child_count != session_count - 1:
        raise ValidationError("complete native usage session counts are inconsistent")
    if any(tokens[field] is None for field in token_fields):
        raise ValidationError("complete native usage requires token totals")
    if tokens["cached_input_tokens"] > tokens["input_tokens"]:
        raise ValidationError("native cached input exceeds input tokens")
    if tokens["total_tokens"] != tokens["input_tokens"] + tokens["output_tokens"]:
        raise ValidationError("native gross token totals are inconsistent")
    expected_rollout = (
        tokens["input_tokens"]
        - tokens["cached_input_tokens"]
        + tokens["output_tokens"]
    )
    if tokens["rollout_tokens"] != expected_rollout:
        raise ValidationError("native weighted rollout total is inconsistent")


def reconcile_codex_native_usage(
    parent_session_id: str,
    workspace: Path,
    *,
    codex_home: Optional[Path] = None,
) -> Dict:
    if not _canonical_uuid(parent_session_id):
        raise ValidationError("native usage parent session id is invalid")
    workspace_path = Path(workspace).resolve()
    home = Path(codex_home) if codex_home is not None else _default_codex_home()
    try:
        home = home.expanduser().resolve(strict=True)
        reject_symlink_path(home, "Codex home")
        database = _latest_state_database(home)
        rows, child_count = _native_session_rows(
            database,
            parent_session_id,
            workspace_path,
        )
        usage = [_rollout_usage(home, row) for row in rows]
    except NativeUsageUnavailable:
        raise
    except (OSError, sqlite3.Error, UnicodeError, ValueError, ValidationError):
        raise NativeUsageUnavailable("state-unavailable")

    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "rollout_tokens": 0,
    }
    for item in usage:
        for field in totals:
            totals[field] = _token_sum(totals[field], item[field], field)
    result = {
        "status": "complete",
        "session_count": len(rows),
        "child_count": child_count,
        **totals,
    }
    validate_native_usage(result)
    return result


def reconcile_codex_session_usage(
    parent_session_id: str,
    workspace: Path,
    *,
    codex_home: Optional[Path] = None,
) -> Dict:
    """Reconcile one bounded Codex session and any direct depth-one children."""
    return reconcile_codex_native_usage(
        parent_session_id,
        workspace,
        codex_home=codex_home,
    )


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def _latest_state_database(codex_home: Path) -> Path:
    directory_fd = open_dir_no_follow(codex_home, "Codex home")
    candidates = []
    try:
        for name in os.listdir(directory_fd):
            match = _STATE_DATABASE.fullmatch(name)
            if match is None:
                continue
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                candidates.append((int(match.group(1)), name))
    finally:
        os.close(directory_fd)
    if not candidates:
        raise NativeUsageUnavailable("state-unavailable")
    path = codex_home / max(candidates)[1]
    reject_symlink_path(path, "Codex state database")
    return path


def _native_session_rows(
    database: Path,
    parent_session_id: str,
    workspace: Path,
):
    uri = database.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0.2)
    except sqlite3.Error:
        raise NativeUsageUnavailable("state-unavailable")
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 200")
        parent = connection.execute(
            "SELECT id, rollout_path, cwd, tokens_used FROM threads WHERE id = ?",
            (parent_session_id,),
        ).fetchone()
        if parent is None:
            raise NativeUsageUnavailable("parent-missing")
        edges = connection.execute(
            "SELECT child_thread_id FROM thread_spawn_edges "
            "WHERE parent_thread_id = ? ORDER BY child_thread_id LIMIT ?",
            (parent_session_id, MAX_NATIVE_USAGE_SESSIONS + 1),
        ).fetchall()
        if len(edges) >= MAX_NATIVE_USAGE_SESSIONS:
            raise NativeUsageUnavailable("topology-unavailable")
        child_ids = []
        for edge in edges:
            child_id = edge[0] if isinstance(edge, tuple) and edge else None
            if not _canonical_uuid(child_id) or child_id in child_ids:
                raise NativeUsageUnavailable("topology-unavailable")
            child_ids.append(child_id)
        if child_ids:
            placeholders = ",".join("?" for _ in child_ids)
            nested = connection.execute(
                "SELECT 1 FROM thread_spawn_edges WHERE parent_thread_id IN (%s) LIMIT 1"
                % placeholders,
                child_ids,
            ).fetchone()
            if nested is not None:
                raise NativeUsageUnavailable("topology-unavailable")
            children = connection.execute(
                "SELECT id, rollout_path, cwd, tokens_used FROM threads "
                "WHERE id IN (%s) ORDER BY id" % placeholders,
                child_ids,
            ).fetchall()
            if len(children) != len(child_ids) or {row[0] for row in children} != set(child_ids):
                raise NativeUsageUnavailable("topology-unavailable")
        else:
            children = []
    except NativeUsageUnavailable:
        raise
    except sqlite3.Error:
        raise NativeUsageUnavailable("state-unavailable")
    finally:
        connection.close()

    rows = [parent] + list(children)
    expected_workspace = str(workspace)
    for row in rows:
        if not isinstance(row, tuple) or len(row) != 4:
            raise NativeUsageUnavailable("state-unavailable")
        session_id, rollout_path, cwd, tokens_used = row
        if (
            not _canonical_uuid(session_id)
            or not isinstance(rollout_path, str)
            or not rollout_path
            or cwd != expected_workspace
            or isinstance(tokens_used, bool)
            or not isinstance(tokens_used, int)
            or tokens_used < 0
            or tokens_used > MAX_PROVIDER_TOKENS
        ):
            raise NativeUsageUnavailable("scope-mismatch")
    return rows, len(child_ids)


def _rollout_usage(codex_home: Path, row) -> Dict:
    session_id, raw_path, _cwd, indexed_total = row
    sessions_root = codex_home / "sessions"
    try:
        root = sessions_root.resolve(strict=True)
        path = Path(raw_path).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError):
        raise NativeUsageUnavailable("scope-mismatch")
    if not path.name.endswith("-%s.jsonl" % session_id):
        raise NativeUsageUnavailable("scope-mismatch")
    reject_symlink_path(path, "Codex rollout")
    usage = _latest_rollout_token_usage(path)
    if usage["total_tokens"] != indexed_total:
        raise NativeUsageUnavailable("usage-unavailable")
    return usage


def _latest_rollout_token_usage(path: Path) -> Dict:
    parent_fd = open_dir_no_follow(path.parent, "Codex rollout parent")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = None
    try:
        fd = os.open(path.name, flags, dir_fd=parent_fd)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size <= 0
            or info.st_size > MAX_NATIVE_ROLLOUT_BYTES
        ):
            raise NativeUsageUnavailable("usage-unavailable")
        start = max(0, info.st_size - MAX_NATIVE_ROLLOUT_TAIL_BYTES)
        with mmap.mmap(fd, 0, access=mmap.ACCESS_READ) as mapped:
            cursor = info.st_size
            while cursor > start:
                marker = mapped.rfind(b'"token_count"', start, cursor)
                if marker < 0:
                    break
                line_start = mapped.rfind(b"\n", start, marker) + 1
                line_end = mapped.find(b"\n", marker, min(info.st_size, marker + MAX_NATIVE_TOKEN_EVENT_BYTES + 1))
                if line_end < 0:
                    line_end = info.st_size
                if line_end - line_start <= MAX_NATIVE_TOKEN_EVENT_BYTES:
                    usage = _token_usage_from_line(mapped[line_start:line_end])
                    if usage is not None:
                        return usage
                cursor = line_start
    except NativeUsageUnavailable:
        raise
    except (OSError, ValueError):
        raise NativeUsageUnavailable("usage-unavailable")
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)
    raise NativeUsageUnavailable("usage-unavailable")


def _token_usage_from_line(raw_line: bytes) -> Optional[Dict]:
    try:
        raw = json.loads(
            raw_line.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("type") != "event_msg":
        return None
    payload = raw.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    total = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(total, dict):
        return None
    fields = {
        "input_tokens": total.get("input_tokens"),
        "cached_input_tokens": total.get("cached_input_tokens"),
        "output_tokens": total.get("output_tokens"),
        "total_tokens": total.get("total_tokens"),
    }
    try:
        for field, value in fields.items():
            fields[field] = _bounded_int(value, 0, MAX_PROVIDER_TOKENS, field)
    except ValidationError:
        return None
    if fields["cached_input_tokens"] > fields["input_tokens"]:
        return None
    if fields["total_tokens"] != fields["input_tokens"] + fields["output_tokens"]:
        return None
    fields["rollout_tokens"] = (
        fields["input_tokens"]
        - fields["cached_input_tokens"]
        + fields["output_tokens"]
    )
    return fields


def _token_sum(current: int, value: int, field: str) -> int:
    total = current + value
    if total > MAX_PROVIDER_TOKENS:
        raise NativeUsageUnavailable("usage-unavailable")
    return total


def _bounded_int(value, minimum: int, maximum: int, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ValidationError("native usage %s is invalid" % field)
    return value


def _canonical_uuid(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return str(parsed) == value


def _reject_duplicate_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError("invalid JSON constant: %s" % value)
