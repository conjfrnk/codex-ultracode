import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .clock import utc_now
from .errors import ValidationError
from .paths import default_agent_memory_dir
from .redaction import contains_secret_like, redact_text
from .security import (
    ensure_dir_no_follow,
    open_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
)


AGENT_MEMORY_SCHEMA = "conductor.agent_memory.v1"
AGENT_MEMORY_WRITE_APPROVAL = "agent-memory-write"
AGENT_MEMORY_MODES = {"read-only", "read-write"}
AGENT_MEMORY_SELECTIONS = {"hybrid", "recent", "relevant"}
AGENT_MEMORY_SELECTION_POLICIES = {
    "hybrid": "hybrid-lexical-v1",
    "recent": "recent-v1",
    "relevant": "lexical-relevance-v1",
}
AGENT_MEMORY_CONFIG_FIELDS = {"mode", "selection", "max_entries", "max_bytes"}
AGENT_MEMORY_FIELDS = {
    "schema",
    "workspace_sha256",
    "profile",
    "revision",
    "updated_at_utc",
    "entries",
}
AGENT_MEMORY_ENTRY_FIELDS = {
    "id",
    "created_at_utc",
    "content",
    "content_sha256",
    "content_bytes",
    "tags",
    "source",
}
AGENT_MEMORY_SOURCE_FIELDS = {"kind", "artifact_sha256", "run_id", "step_id"}
AGENT_MEMORY_SOURCE_KINDS = {"operator", "workflow"}
SAFE_MEMORY_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_AGENT_MEMORY_MAX_ENTRIES = 32
DEFAULT_AGENT_MEMORY_MAX_BYTES = 64 * 1024
DEFAULT_AGENT_MEMORY_SELECTION = "recent"
MAX_AGENT_MEMORY_PROFILE_ENTRIES = 64
MAX_AGENT_MEMORY_PROFILE_BYTES = 128 * 1024
MAX_AGENT_MEMORY_STORE_ENTRIES = 128
MAX_AGENT_MEMORY_STORE_BYTES = 512 * 1024
MAX_AGENT_MEMORY_ENTRY_BYTES = 16 * 1024
MAX_AGENT_MEMORY_JSON_BYTES = 1024 * 1024
MAX_AGENT_MEMORY_FILES = 128
MAX_AGENT_MEMORY_TAGS = 8
MAX_AGENT_MEMORY_TAG_CHARS = 64
MAX_AGENT_MEMORY_ID_CHARS = 96
MAX_AGENT_MEMORY_QUERY_BYTES = 128 * 1024
MAX_AGENT_MEMORY_QUERY_TOKENS = 2048
MAX_AGENT_MEMORY_ENTRY_RELEVANCE_TOKENS = 4096
MAX_AGENT_MEMORY_QUERY_TOKEN_WEIGHT = 3
AGENT_MEMORY_TAG_RELEVANCE_MULTIPLIER = 4
AGENT_MEMORY_HYBRID_BIGRAM_MULTIPLIER = 8
AGENT_MEMORY_HYBRID_IDENTIFIER_MULTIPLIER = 16
AGENT_MEMORY_HYBRID_LENGTH_SCALE = 4096

_AGENT_MEMORY_WORD = re.compile(r"[a-z0-9]+")
_AGENT_MEMORY_IDENTIFIER = re.compile(r"[a-z0-9]+(?:[._:/-][a-z0-9]+)+")
_AGENT_MEMORY_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "do",
        "for",
        "from",
        "has",
        "have",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "not",
        "of",
        "on",
        "or",
        "only",
        "the",
        "their",
        "this",
        "to",
        "use",
        "using",
        "was",
        "were",
        "with",
    }
)


def validate_agent_memory_config(config: Dict, source: str = "agent profile memory") -> None:
    if not isinstance(config, dict):
        raise ValidationError("%s must be an object" % source)
    unknown = sorted(set(config) - AGENT_MEMORY_CONFIG_FIELDS)
    if unknown:
        raise ValidationError("%s contains unsupported field(s): %s" % (source, ", ".join(unknown)))
    if config.get("mode") not in AGENT_MEMORY_MODES:
        raise ValidationError("%s mode must be read-only or read-write" % source)
    if config.get("selection", DEFAULT_AGENT_MEMORY_SELECTION) not in AGENT_MEMORY_SELECTIONS:
        raise ValidationError("%s selection must be hybrid, recent, or relevant" % source)
    _validate_optional_int(
        config,
        "max_entries",
        "%s max_entries" % source,
        1,
        MAX_AGENT_MEMORY_PROFILE_ENTRIES,
    )
    _validate_optional_int(
        config,
        "max_bytes",
        "%s max_bytes" % source,
        256,
        MAX_AGENT_MEMORY_PROFILE_BYTES,
    )


def effective_agent_memory_config(config: Dict) -> Dict:
    validate_agent_memory_config(config)
    return {
        "mode": config["mode"],
        "selection": config.get("selection", DEFAULT_AGENT_MEMORY_SELECTION),
        "max_entries": config.get("max_entries", DEFAULT_AGENT_MEMORY_MAX_ENTRIES),
        "max_bytes": config.get("max_bytes", DEFAULT_AGENT_MEMORY_MAX_BYTES),
    }


def workspace_memory_sha256(workspace: Path) -> str:
    resolved = Path(workspace).expanduser().resolve()
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()


def empty_agent_memory(workspace: Path, profile: str) -> Dict:
    _validate_profile(profile, "agent memory profile")
    return {
        "schema": AGENT_MEMORY_SCHEMA,
        "workspace_sha256": workspace_memory_sha256(workspace),
        "profile": profile,
        "revision": 0,
        "updated_at_utc": None,
        "entries": [],
    }


def validate_agent_memory(
    memory: Dict,
    source: str = "<memory>",
    *,
    workspace: Optional[Path] = None,
    profile: Optional[str] = None,
) -> None:
    if not isinstance(memory, dict) or set(memory) != AGENT_MEMORY_FIELDS:
        raise ValidationError("%s has invalid agent memory fields" % source)
    if memory.get("schema") != AGENT_MEMORY_SCHEMA:
        raise ValidationError("%s schema must be %s" % (source, AGENT_MEMORY_SCHEMA))
    digest = memory.get("workspace_sha256")
    if not isinstance(digest, str) or not SHA256_PATTERN.match(digest):
        raise ValidationError("%s workspace_sha256 is invalid" % source)
    if workspace is not None and digest != workspace_memory_sha256(workspace):
        raise ValidationError("%s belongs to a different workspace" % source)
    memory_profile = memory.get("profile")
    _validate_profile(memory_profile, "%s profile" % source)
    if profile is not None and memory_profile != profile:
        raise ValidationError("%s belongs to a different agent profile" % source)
    revision = memory.get("revision")
    if not _strict_int(revision) or revision < 0:
        raise ValidationError("%s revision must be a non-negative integer" % source)
    updated = memory.get("updated_at_utc")
    if revision == 0:
        if updated is not None or memory.get("entries") != []:
            raise ValidationError("%s revision zero must be an empty never-written store" % source)
    elif not _valid_timestamp(updated):
        raise ValidationError("%s updated_at_utc is invalid" % source)
    entries = memory.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_AGENT_MEMORY_STORE_ENTRIES:
        raise ValidationError(
            "%s entries must be an array of at most %d entries"
            % (source, MAX_AGENT_MEMORY_STORE_ENTRIES)
        )
    seen = set()
    total_bytes = 0
    previous_timestamp = ""
    for index, entry in enumerate(entries):
        validate_agent_memory_entry(entry, "%s entries[%d]" % (source, index))
        if entry["id"] in seen:
            raise ValidationError("%s contains duplicate entry id %s" % (source, entry["id"]))
        if previous_timestamp and entry["created_at_utc"] < previous_timestamp:
            raise ValidationError("%s entries must be ordered by creation time" % source)
        previous_timestamp = entry["created_at_utc"]
        seen.add(entry["id"])
        total_bytes += entry["content_bytes"]
    if total_bytes > MAX_AGENT_MEMORY_STORE_BYTES:
        raise ValidationError(
            "%s entry content exceeds %d bytes" % (source, MAX_AGENT_MEMORY_STORE_BYTES)
        )


def load_agent_memory(
    workspace: Path,
    profile: str,
    *,
    memory_dir: Optional[Path] = None,
    allow_missing: bool = True,
) -> Dict:
    path = agent_memory_path(workspace, profile, memory_dir=memory_dir)
    if allow_missing and not path.exists() and not path.is_symlink():
        return empty_agent_memory(workspace, profile)
    try:
        memory = load_agent_memory_file(path)
    except FileNotFoundError:
        if allow_missing:
            return empty_agent_memory(workspace, profile)
        raise
    validate_agent_memory(memory, source=str(path), workspace=workspace, profile=profile)
    return memory


def load_agent_memory_file(path: Path) -> Dict:
    reject_symlink_path(path, "agent memory")
    payload = read_regular_text_file_no_follow(path, "agent memory", MAX_AGENT_MEMORY_JSON_BYTES)
    try:
        memory = json.loads(payload, object_pairs_hook=_object_without_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    validate_agent_memory(memory, source=str(path))
    return memory


def append_agent_memory(
    workspace: Path,
    profile: str,
    content: str,
    *,
    tags: Optional[List[str]] = None,
    entry_id: Optional[str] = None,
    source_kind: str = "operator",
    source_artifact_sha256: Optional[str] = None,
    run_id: Optional[str] = None,
    step_id: Optional[str] = None,
    expected_revision: Optional[int] = None,
    memory_dir: Optional[Path] = None,
) -> Dict:
    _validate_profile(profile, "agent memory profile")
    clean_content = _clean_memory_content(content)
    clean_tags = _clean_tags(tags or [])
    if entry_id is None:
        entry_id = "mem-%s" % secrets.token_hex(12)
    _validate_entry_id(entry_id, "agent memory entry id")
    source = _build_source(
        source_kind,
        source_artifact_sha256 or _sha256_text(clean_content),
        run_id,
        step_id,
    )
    with _agent_memory_lock(workspace, profile, memory_dir=memory_dir):
        memory = load_agent_memory(workspace, profile, memory_dir=memory_dir)
        existing = next((entry for entry in memory["entries"] if entry["id"] == entry_id), None)
        if existing is not None:
            if (
                existing["content"] == clean_content
                and existing["tags"] == clean_tags
                and existing["source"] == source
            ):
                return {"memory": memory, "entry": existing, "changed": False}
            raise ValidationError("agent memory entry id %s already exists with different content" % entry_id)
        _require_expected_revision(memory, expected_revision)
        entry = {
            "id": entry_id,
            "created_at_utc": _utc_now(),
            "content": clean_content,
            "content_sha256": _sha256_text(clean_content),
            "content_bytes": len(clean_content.encode("utf-8")),
            "tags": clean_tags,
            "source": source,
        }
        candidate = dict(memory)
        candidate["entries"] = list(memory["entries"]) + [entry]
        candidate["revision"] = memory["revision"] + 1
        candidate["updated_at_utc"] = entry["created_at_utc"]
        validate_agent_memory(candidate, workspace=workspace, profile=profile)
        _write_agent_memory(candidate, workspace, profile, memory_dir=memory_dir)
        return {"memory": candidate, "entry": entry, "changed": True}


def remove_agent_memory_entry(
    workspace: Path,
    profile: str,
    entry_id: str,
    *,
    expected_revision: Optional[int] = None,
    memory_dir: Optional[Path] = None,
) -> Dict:
    _validate_entry_id(entry_id, "agent memory entry id")
    with _agent_memory_lock(workspace, profile, memory_dir=memory_dir):
        memory = load_agent_memory(workspace, profile, memory_dir=memory_dir, allow_missing=False)
        _require_expected_revision(memory, expected_revision)
        entries = [entry for entry in memory["entries"] if entry["id"] != entry_id]
        if len(entries) == len(memory["entries"]):
            raise ValidationError("agent memory entry does not exist: %s" % entry_id)
        candidate = dict(memory)
        candidate["entries"] = entries
        candidate["revision"] = memory["revision"] + 1
        candidate["updated_at_utc"] = _utc_now()
        validate_agent_memory(candidate, workspace=workspace, profile=profile)
        _write_agent_memory(candidate, workspace, profile, memory_dir=memory_dir)
        return {"memory": candidate, "removed_entry_id": entry_id, "changed": True}


def select_agent_memory_snapshot(memory: Dict, config: Dict, query_text: str = "") -> Dict:
    validate_agent_memory(memory)
    effective = effective_agent_memory_config(config)
    if not isinstance(query_text, str):
        raise ValidationError("agent memory selection query must be a string")
    if len(query_text.encode("utf-8")) > MAX_AGENT_MEMORY_QUERY_BYTES:
        raise ValidationError(
            "agent memory selection query exceeds %d bytes" % MAX_AGENT_MEMORY_QUERY_BYTES
        )
    indexed_entries = list(enumerate(memory["entries"]))
    if effective["selection"] == "hybrid":
        candidates = _hybrid_relevance_ranked_entries(indexed_entries, query_text)
    elif effective["selection"] == "relevant":
        candidates = _relevance_ranked_entries(indexed_entries, query_text)
    else:
        candidates = list(reversed(indexed_entries))
    selected_pairs = []
    selected_bytes = 0
    for index, entry in candidates:
        if len(selected_pairs) >= effective["max_entries"]:
            break
        entry_bytes = entry["content_bytes"]
        if selected_bytes + entry_bytes > effective["max_bytes"]:
            continue
        selected_pairs.append((index, entry))
        selected_bytes += entry_bytes
    selected_pairs.sort(key=lambda pair: pair[0])
    selected = [entry for _, entry in selected_pairs]
    store_sha256 = _sha256_json(memory)
    snapshot_value = {
        "store_revision": memory["revision"],
        "entries": selected,
    }
    return {
        "mode": effective["mode"],
        "store_revision": memory["revision"],
        "store_sha256": store_sha256,
        "snapshot_sha256": _sha256_json(snapshot_value),
        "entry_count": len(selected),
        "omitted_entries": len(memory["entries"]) - len(selected),
        "bytes": selected_bytes,
        "entries": selected,
    }


def agent_memory_selection_policy(selection: str) -> str:
    if selection not in AGENT_MEMORY_SELECTIONS:
        raise ValidationError("agent memory selection must be hybrid, recent, or relevant")
    return AGENT_MEMORY_SELECTION_POLICIES[selection]


def _relevance_ranked_entries(indexed_entries, query_text: str):
    query_counts = _relevance_token_counts(query_text, MAX_AGENT_MEMORY_QUERY_TOKENS)
    query_tokens = set(query_counts)
    entry_tokens = []
    entry_tag_tokens = []
    document_frequency = Counter()
    for _, entry in indexed_entries:
        content_tokens = set(
            _relevance_token_counts(
                entry["content"],
                MAX_AGENT_MEMORY_ENTRY_RELEVANCE_TOKENS,
            )
        )
        tag_tokens = set()
        for tag in entry["tags"]:
            tag_tokens.update(
                _relevance_token_counts(
                    tag,
                    MAX_AGENT_MEMORY_ENTRY_RELEVANCE_TOKENS,
                )
            )
        combined = content_tokens | tag_tokens
        entry_tokens.append(combined)
        entry_tag_tokens.append(tag_tokens)
        document_frequency.update(combined & query_tokens)

    document_count = len(indexed_entries)
    ranked = []
    for position, (index, entry) in enumerate(indexed_entries):
        score = 0
        tokens = entry_tokens[position]
        tags = entry_tag_tokens[position]
        for token, query_weight in query_counts.items():
            if token not in tokens:
                continue
            rarity = document_count - document_frequency[token] + 1
            score += rarity * query_weight
            if token in tags:
                score += rarity * query_weight * (AGENT_MEMORY_TAG_RELEVANCE_MULTIPLIER - 1)
        ranked.append((score, index, entry))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(index, entry) for _, index, entry in ranked]


def _hybrid_relevance_ranked_entries(indexed_entries, query_text: str):
    query_sequence = _relevance_token_sequence(
        query_text,
        MAX_AGENT_MEMORY_QUERY_TOKENS,
    )
    query_counts = Counter(query_sequence)
    for token in list(query_counts):
        query_counts[token] = min(
            query_counts[token],
            MAX_AGENT_MEMORY_QUERY_TOKEN_WEIGHT,
        )
    query_bigrams = Counter(zip(query_sequence, query_sequence[1:]))
    for bigram in list(query_bigrams):
        query_bigrams[bigram] = min(
            query_bigrams[bigram],
            MAX_AGENT_MEMORY_QUERY_TOKEN_WEIGHT,
        )
    query_identifiers = _relevance_identifiers(
        query_text,
        MAX_AGENT_MEMORY_QUERY_TOKENS,
    )
    query_tokens = set(query_counts)
    entry_features = []
    document_frequency = Counter()
    for _, entry in indexed_entries:
        content_sequence = _relevance_token_sequence(
            entry["content"],
            MAX_AGENT_MEMORY_ENTRY_RELEVANCE_TOKENS,
        )
        tag_sequence = []
        for tag in entry["tags"]:
            tag_sequence.extend(
                _relevance_token_sequence(
                    tag,
                    MAX_AGENT_MEMORY_ENTRY_RELEVANCE_TOKENS - len(tag_sequence),
                )
            )
            if len(tag_sequence) >= MAX_AGENT_MEMORY_ENTRY_RELEVANCE_TOKENS:
                break
        content_counts = Counter(content_sequence)
        tag_counts = Counter(tag_sequence)
        combined = set(content_counts) | set(tag_counts)
        document_frequency.update(combined & query_tokens)
        entry_features.append(
            {
                "content_counts": content_counts,
                "tag_counts": tag_counts,
                "content_bigrams": set(zip(content_sequence, content_sequence[1:])),
                "tag_bigrams": set(zip(tag_sequence, tag_sequence[1:])),
                "content_identifiers": _relevance_identifiers(
                    entry["content"],
                    MAX_AGENT_MEMORY_ENTRY_RELEVANCE_TOKENS,
                ),
                "tag_identifiers": set().union(
                    *(
                        _relevance_identifiers(
                            tag,
                            MAX_AGENT_MEMORY_ENTRY_RELEVANCE_TOKENS,
                        )
                        for tag in entry["tags"]
                    )
                )
                if entry["tags"]
                else set(),
                "length": len(content_sequence) + len(tag_sequence),
            }
        )

    document_count = len(indexed_entries)
    ranked = []
    for position, (index, entry) in enumerate(indexed_entries):
        features = entry_features[position]
        score = 0
        for token, query_weight in query_counts.items():
            content_frequency = min(features["content_counts"].get(token, 0), 4)
            tag_frequency = min(features["tag_counts"].get(token, 0), 4)
            if not content_frequency and not tag_frequency:
                continue
            rarity = document_count - document_frequency[token] + 1
            score += rarity * query_weight * (2 + content_frequency)
            if tag_frequency:
                score += (
                    rarity
                    * query_weight
                    * tag_frequency
                    * AGENT_MEMORY_TAG_RELEVANCE_MULTIPLIER
                )
        for bigram, query_weight in query_bigrams.items():
            if bigram in features["content_bigrams"]:
                score += (
                    (document_count + 1)
                    * query_weight
                    * AGENT_MEMORY_HYBRID_BIGRAM_MULTIPLIER
                )
            if bigram in features["tag_bigrams"]:
                score += (
                    (document_count + 1)
                    * query_weight
                    * AGENT_MEMORY_HYBRID_BIGRAM_MULTIPLIER
                    * AGENT_MEMORY_TAG_RELEVANCE_MULTIPLIER
                )
        for identifier in query_identifiers:
            if identifier in features["content_identifiers"]:
                score += (
                    (document_count + 1)
                    * AGENT_MEMORY_HYBRID_IDENTIFIER_MULTIPLIER
                )
            if identifier in features["tag_identifiers"]:
                score += (
                    (document_count + 1)
                    * AGENT_MEMORY_HYBRID_IDENTIFIER_MULTIPLIER
                    * AGENT_MEMORY_TAG_RELEVANCE_MULTIPLIER
                )
        score = (
            score
            * AGENT_MEMORY_HYBRID_LENGTH_SCALE
            // (AGENT_MEMORY_HYBRID_LENGTH_SCALE + features["length"])
        )
        ranked.append((score, index, entry))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(index, entry) for _, index, entry in ranked]


def _relevance_token_counts(text: str, max_unique: int) -> Dict[str, int]:
    counts = {}
    for token in _AGENT_MEMORY_WORD.findall(text.casefold()):
        if len(token) < 2 or token in _AGENT_MEMORY_STOP_WORDS:
            continue
        if token not in counts and len(counts) >= max_unique:
            break
        counts[token] = min(
            counts.get(token, 0) + 1,
            MAX_AGENT_MEMORY_QUERY_TOKEN_WEIGHT,
        )
    return counts


def _relevance_token_sequence(text: str, max_tokens: int) -> List[str]:
    if max_tokens <= 0:
        return []
    tokens = []
    for token in _AGENT_MEMORY_WORD.findall(text.casefold()):
        if len(token) < 2 or token in _AGENT_MEMORY_STOP_WORDS:
            continue
        tokens.append(token)
        if len(tokens) >= max_tokens:
            break
    return tokens


def _relevance_identifiers(text: str, max_identifiers: int) -> set:
    if max_identifiers <= 0:
        return set()
    identifiers = set()
    for identifier in _AGENT_MEMORY_IDENTIFIER.findall(text.casefold()):
        identifiers.add(identifier)
        if len(identifiers) >= max_identifiers:
            break
    return identifiers


def validate_agent_memory_entry(entry: Dict, source: str = "agent memory entry") -> None:
    _validate_agent_memory_entry(entry, source)


def agent_memory_snapshot_sha256(store_revision: int, entries: List[Dict]) -> str:
    if not _strict_int(store_revision) or store_revision < 0:
        raise ValidationError("agent memory snapshot revision is invalid")
    if not isinstance(entries, list):
        raise ValidationError("agent memory snapshot entries must be an array")
    for index, entry in enumerate(entries):
        validate_agent_memory_entry(entry, "agent memory snapshot entries[%d]" % index)
    return _sha256_json({"store_revision": store_revision, "entries": entries})


def list_agent_memories(
    workspace: Path,
    *,
    memory_dir: Optional[Path] = None,
) -> List[Dict]:
    directory = _memory_directory(workspace, memory_dir)
    if not directory.exists():
        return []
    reject_symlink_path(directory, "agent memory directory")
    directory_fd = open_dir_no_follow(directory, "agent memory directory")
    try:
        names = sorted(name for name in os.listdir(directory_fd) if name.endswith(".json"))
    finally:
        os.close(directory_fd)
    if len(names) > MAX_AGENT_MEMORY_FILES:
        raise ValidationError(
            "agent memory directory contains more than %d stores" % MAX_AGENT_MEMORY_FILES
        )
    records = []
    for name in names:
        profile = name[:-5]
        _validate_profile(profile, "agent memory filename")
        memory = load_agent_memory(workspace, profile, memory_dir=directory, allow_missing=False)
        records.append(agent_memory_summary(memory))
    return records


def agent_memory_summary(memory: Dict) -> Dict:
    validate_agent_memory(memory)
    return {
        "schema": AGENT_MEMORY_SCHEMA,
        "profile": memory["profile"],
        "workspace_sha256": memory["workspace_sha256"],
        "revision": memory["revision"],
        "updated_at_utc": memory["updated_at_utc"],
        "entry_count": len(memory["entries"]),
        "content_bytes": sum(entry["content_bytes"] for entry in memory["entries"]),
        "store_sha256": _sha256_json(memory),
        "entries": [
            {
                "id": entry["id"],
                "created_at_utc": entry["created_at_utc"],
                "content_sha256": entry["content_sha256"],
                "content_bytes": entry["content_bytes"],
                "tags": list(entry["tags"]),
                "source": dict(entry["source"]),
            }
            for entry in memory["entries"]
        ],
    }


def agent_memory_path(
    workspace: Path,
    profile: str,
    *,
    memory_dir: Optional[Path] = None,
) -> Path:
    _validate_profile(profile, "agent memory profile")
    return _memory_directory(workspace, memory_dir) / (profile + ".json")


def _memory_directory(workspace: Path, memory_dir: Optional[Path]) -> Path:
    return Path(memory_dir) if memory_dir is not None else default_agent_memory_dir(workspace)


@contextmanager
def _agent_memory_lock(
    workspace: Path,
    profile: str,
    *,
    memory_dir: Optional[Path],
):
    directory = _memory_directory(workspace, memory_dir)
    directory_fd = ensure_dir_no_follow(directory, "agent memory directory")
    lock_name = profile + ".lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = None
    try:
        try:
            lock_fd = os.open(lock_name, flags, 0o600, dir_fd=directory_fd)
        except FileNotFoundError:
            # A first-writer directory creation can race on some filesystems.
            os.close(directory_fd)
            directory_fd = ensure_dir_no_follow(directory, "agent memory directory")
            lock_fd = os.open(lock_name, flags, 0o600, dir_fd=directory_fd)
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError("agent memory lock must be a regular file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise ValidationError("failed to lock agent memory: %s" % exc.__class__.__name__)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(directory_fd)


def _write_agent_memory(
    memory: Dict,
    workspace: Path,
    profile: str,
    *,
    memory_dir: Optional[Path],
) -> None:
    validate_agent_memory(memory, workspace=workspace, profile=profile)
    path = agent_memory_path(workspace, profile, memory_dir=memory_dir)
    parent_fd = ensure_dir_no_follow(path.parent, "agent memory directory")
    os.close(parent_fd)
    serialized = json.dumps(memory, indent=2, sort_keys=True) + "\n"
    if len(serialized.encode("utf-8")) > MAX_AGENT_MEMORY_JSON_BYTES:
        raise ValidationError("agent memory exceeds %d bytes" % MAX_AGENT_MEMORY_JSON_BYTES)
    replace_text_file_no_follow(path, "agent memory", serialized, ".agent-memory-")


def _validate_agent_memory_entry(entry: Dict, source: str) -> None:
    if not isinstance(entry, dict) or set(entry) != AGENT_MEMORY_ENTRY_FIELDS:
        raise ValidationError("%s has invalid fields" % source)
    _validate_entry_id(entry.get("id"), "%s id" % source)
    if not _valid_timestamp(entry.get("created_at_utc")):
        raise ValidationError("%s created_at_utc is invalid" % source)
    content = entry.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValidationError("%s content must be a non-empty string" % source)
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_AGENT_MEMORY_ENTRY_BYTES:
        raise ValidationError("%s content exceeds %d bytes" % (source, MAX_AGENT_MEMORY_ENTRY_BYTES))
    if contains_secret_like(content) or redact_text(content) != content:
        raise ValidationError("%s content contains secret-like material" % source)
    if entry.get("content_bytes") != len(encoded):
        raise ValidationError("%s content_bytes does not match content" % source)
    if entry.get("content_sha256") != hashlib.sha256(encoded).hexdigest():
        raise ValidationError("%s content_sha256 does not match content" % source)
    _clean_tags(entry.get("tags"), source="%s tags" % source)
    source_value = entry.get("source")
    if not isinstance(source_value, dict) or set(source_value) != AGENT_MEMORY_SOURCE_FIELDS:
        raise ValidationError("%s source has invalid fields" % source)
    if source_value.get("kind") not in AGENT_MEMORY_SOURCE_KINDS:
        raise ValidationError("%s source kind is invalid" % source)
    artifact_hash = source_value.get("artifact_sha256")
    if not isinstance(artifact_hash, str) or not SHA256_PATTERN.match(artifact_hash):
        raise ValidationError("%s source artifact_sha256 is invalid" % source)
    run_id = source_value.get("run_id")
    step_id = source_value.get("step_id")
    if source_value["kind"] == "operator":
        if run_id is not None or step_id is not None:
            raise ValidationError("%s operator source cannot set run_id or step_id" % source)
    else:
        _validate_entry_id(run_id, "%s source run_id" % source)
        _validate_entry_id(step_id, "%s source step_id" % source)


def _build_source(kind: str, artifact_sha256: str, run_id, step_id) -> Dict:
    if kind not in AGENT_MEMORY_SOURCE_KINDS:
        raise ValidationError("agent memory source kind is invalid")
    if not isinstance(artifact_sha256, str) or not SHA256_PATTERN.match(artifact_sha256):
        raise ValidationError("agent memory source artifact hash is invalid")
    if kind == "operator":
        if run_id is not None or step_id is not None:
            raise ValidationError("operator agent memory source cannot set run_id or step_id")
    else:
        _validate_entry_id(run_id, "agent memory source run_id")
        _validate_entry_id(step_id, "agent memory source step_id")
    return {
        "kind": kind,
        "artifact_sha256": artifact_sha256,
        "run_id": run_id,
        "step_id": step_id,
    }


def _clean_memory_content(content: str) -> str:
    if not isinstance(content, str) or not content.strip():
        raise ValidationError("agent memory content must be a non-empty string")
    clean = redact_text(content)
    if len(clean.encode("utf-8")) > MAX_AGENT_MEMORY_ENTRY_BYTES:
        raise ValidationError("agent memory content exceeds %d bytes" % MAX_AGENT_MEMORY_ENTRY_BYTES)
    return clean


def _clean_tags(tags, source: str = "agent memory tags") -> List[str]:
    if not isinstance(tags, list) or len(tags) > MAX_AGENT_MEMORY_TAGS:
        raise ValidationError("%s must be an array of at most %d tags" % (source, MAX_AGENT_MEMORY_TAGS))
    clean = []
    for tag in tags:
        if (
            not isinstance(tag, str)
            or not tag
            or len(tag) > MAX_AGENT_MEMORY_TAG_CHARS
            or not SAFE_MEMORY_ID.match(tag)
        ):
            raise ValidationError("%s contains an invalid tag" % source)
        if tag in clean:
            raise ValidationError("%s contains duplicate tag %s" % (source, tag))
        clean.append(tag)
    return clean


def _require_expected_revision(memory: Dict, expected_revision: Optional[int]) -> None:
    if expected_revision is None:
        return
    if not _strict_int(expected_revision) or expected_revision < 0:
        raise ValidationError("expected agent memory revision must be a non-negative integer")
    if memory["revision"] != expected_revision:
        raise ValidationError(
            "agent memory revision changed: expected %d, found %d"
            % (expected_revision, memory["revision"])
        )


def _validate_profile(value, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > MAX_AGENT_MEMORY_ID_CHARS
        or not SAFE_MEMORY_ID.match(value)
    ):
        raise ValidationError("%s must be a safe identifier" % label)


def _validate_entry_id(value, label: str) -> None:
    _validate_profile(value, label)


def _validate_optional_int(values: Dict, key: str, label: str, minimum: int, maximum: int) -> None:
    if key not in values:
        return
    value = values[key]
    if not _strict_int(value) or value < minimum or value > maximum:
        raise ValidationError("%s must be an integer from %d to %d" % (label, minimum, maximum))


def _valid_timestamp(value) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def _utc_now() -> str:
    return utc_now().isoformat(timespec="milliseconds") + "Z"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Dict) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _strict_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("agent memory JSON contains duplicate key %s" % key)
        result[key] = value
    return result
