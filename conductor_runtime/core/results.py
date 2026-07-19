"""Immutable, hash-bound storage for process output kept outside model context."""

import fcntl
import os
import re
import stat
import threading
from contextlib import contextmanager
from itertools import islice
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..errors import ValidationError
from ..redaction import redact_text
from .process import ProcessResult, StreamCapture, discard_process_captures
from .safe import (
    canonical_json_bytes,
    ensure_directory,
    is_optional_storage_error,
    read_regular_bytes,
    reject_symlink_components,
    sha256_bytes,
    strict_json_bytes,
    write_new_bytes,
)
from .state import RunState, utc_now


RESULT_SCHEMA = "conductor.core_result.v1"
CONTAINER_MAGIC = b"CONDUCTOR-CORE-RESULT-V1\n"
MAX_RESULT_BYTES = 32 * 1024 * 1024
MAX_RESULT_HEADER_BYTES = 64 * 1024
MAX_RESULT_CONTAINER_BYTES = len(CONTAINER_MAGIC) + 9 + MAX_RESULT_HEADER_BYTES + MAX_RESULT_BYTES
MAX_RESULT_RECORDS = 10000
MAX_RUN_RESULT_BYTES = 256 * 1024 * 1024
MAX_QUERY_CHARS = 256
MAX_GET_LINES = 2000
MAX_GET_BYTES = 1024 * 1024
MAX_SEARCH_MATCHES = 200
MAX_OUTLINE_CHUNKS = 500
RESULT_ID = re.compile(r"^result_[0-9a-f]{32}$")
SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,179}$")
TEMPORARY_CONTAINER = re.compile(r"^\.(result_[0-9a-f]{32}\.result)\.[A-Za-z0-9_-]+$")
LINE_BREAK = re.compile(r"\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]")
RESULT_UNAVAILABLE_REASONS = frozenset(
    {
        "capture_missing",
        "capture_storage_unavailable",
        "result_storage_unavailable",
        "run_quota_exceeded",
        "transient_budget_exhausted",
    }
)
_RESULT_OPERATION_MUTEX = threading.RLock()


class ResultQuotaExceeded(ValidationError):
    pass


class ResultStorageUnavailable(ValidationError):
    pass


class RunResultStore:
    """Optional sidecars bound to one validated external run."""

    def __init__(self, run: RunState):
        if not isinstance(run, RunState):
            raise ValidationError("result store requires a validated run")
        self.run = run
        self.root = run.run_dir / "results"
        self.lock_path = self.root / ".lock"

    @classmethod
    def inspect(cls, run_dir: Path):
        return cls(RunState.inspect(run_dir))

    def preserve_process_overflow(
        self,
        result: ProcessResult,
        *,
        source_id: str,
        step_id: str,
        attempt: int,
        preview_limit_bytes: int,
    ) -> Dict[str, str]:
        """Persist only streams whose bounded inline preview was truncated."""

        stored = {}
        try:
            _validate_source(source_id, step_id, attempt, preview_limit_bytes)
            for stream, preview_truncated, capture, unavailable in (
                (
                    "stdout",
                    result.stdout_truncated,
                    result.stdout_capture,
                    result.stdout_capture_unavailable,
                ),
                (
                    "stderr",
                    result.stderr_truncated,
                    result.stderr_capture,
                    result.stderr_capture_unavailable,
                ),
            ):
                if not preview_truncated:
                    continue
                if capture is None:
                    stored["%s_result_unavailable" % stream] = unavailable or "capture_missing"
                    continue
                try:
                    record = self._store_capture(
                        result,
                        capture,
                        source_id=source_id,
                        step_id=step_id,
                        attempt=attempt,
                        stream=stream,
                        preview_limit_bytes=preview_limit_bytes,
                    )
                except ResultQuotaExceeded:
                    stored["%s_result_unavailable" % stream] = "run_quota_exceeded"
                except ResultStorageUnavailable:
                    stored["%s_result_unavailable" % stream] = "result_storage_unavailable"
                else:
                    stored["%s_result_id" % stream] = record["result_id"]
            return stored
        finally:
            discard_process_captures(result)

    def verify(self) -> List[Dict]:
        """Verify every optional container and every persisted state reference."""

        if not self.root.exists() and not self.root.is_symlink():
            self._verify_state_references({})
            return []
        if not self.lock_path.exists() and not self.lock_path.is_symlink():
            reject_symlink_components(self.root, "result store")
            if not self.root.is_dir() or any(self.root.iterdir()):
                raise ValidationError("result store lock is missing")
            self._verify_state_references({})
            return []
        with self._locked(create=False):
            paths, _total_bytes = self._scan_unlocked()
            records_by_id = {}
            for path in paths:
                record, _payload = self._load_container_unlocked(path.name[:-7])
                if record["result_id"] in records_by_id:
                    raise ValidationError("result identifiers are duplicated")
                records_by_id[record["result_id"]] = record
            self._verify_state_references(records_by_id)
            return list(records_by_id.values())

    def list_records(self, *, step_id: Optional[str] = None) -> List[Dict]:
        if step_id is not None and (not isinstance(step_id, str) or SOURCE_ID.fullmatch(step_id) is None):
            raise ValidationError("result step filter is invalid")
        records = self.verify()
        if step_id is not None:
            records = [record for record in records if record["source"]["step_id"] == step_id]
        return sorted(records, key=lambda record: (record["created_at_utc"], record["result_id"]))

    def get(
        self,
        result_id: str,
        *,
        start_line: int = 1,
        max_lines: int = 200,
        max_bytes: int = 64 * 1024,
    ) -> Dict:
        _validate_result_id(result_id)
        _bounded_positive(start_line, "result start line", maximum=10**9)
        _bounded_positive(max_lines, "result line limit", maximum=MAX_GET_LINES)
        _bounded_positive(max_bytes, "result byte limit", maximum=MAX_GET_BYTES)
        with self._locked(create=False):
            self._scan_unlocked()
            record, payload = self._load_container_unlocked(result_id)
        if start_line > max(1, record["line_count"]):
            raise ValidationError("result start line exceeds available content")
        rendered_lines: List[str] = []
        used = 0
        byte_truncated = False
        lines = _iter_text_lines(payload.decode("utf-8"))
        for line in islice(lines, start_line - 1, start_line - 1 + max_lines):
            encoded = line.encode("utf-8")
            separator = 1 if rendered_lines else 0
            if used + separator + len(encoded) > max_bytes:
                remaining = max(0, max_bytes - used - separator)
                if remaining > 0:
                    rendered_lines.append(_utf8_prefix(encoded, remaining).decode("utf-8"))
                byte_truncated = True
                break
            rendered_lines.append(line)
            used += separator + len(encoded)
        end_line = start_line + max(0, len(rendered_lines) - 1)
        return {
            "result_id": result_id,
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": record["line_count"],
            "text": "\n".join(rendered_lines),
            "truncated": end_line < record["line_count"] or byte_truncated,
            "pipe_complete": record["pipe_complete"],
            "producer_status": record["producer_status"],
            "text_fidelity": record["text_fidelity"],
            "citation": "%s:L%d-L%d" % (result_id, start_line, end_line),
        }

    def search(
        self,
        query: str,
        *,
        result_ids: Optional[Iterable[str]] = None,
        step_id: Optional[str] = None,
        max_matches: int = 20,
    ) -> List[Dict]:
        if not isinstance(query, str) or not query or len(query) > MAX_QUERY_CHARS or "\x00" in query:
            raise ValidationError("result search query is invalid")
        _bounded_positive(max_matches, "result match limit", maximum=MAX_SEARCH_MATCHES)
        if step_id is not None and (not isinstance(step_id, str) or SOURCE_ID.fullmatch(step_id) is None):
            raise ValidationError("result step filter is invalid")
        selected_ids = None
        if result_ids is not None:
            selected_ids = list(result_ids)
            if not selected_ids or len(selected_ids) > MAX_RESULT_RECORDS:
                raise ValidationError("result search identifiers are invalid")
            for result_id in selected_ids:
                _validate_result_id(result_id)
            if len(selected_ids) != len(set(selected_ids)):
                raise ValidationError("result search identifiers must be unique")
        needle = query.casefold()
        matches = []
        with self._locked(create=False):
            paths, _total_bytes = self._scan_unlocked()
            identifiers = selected_ids if selected_ids is not None else [path.name[:-7] for path in paths]
            for result_id in identifiers:
                record, payload = self._load_container_unlocked(result_id)
                if step_id is not None and record["source"]["step_id"] != step_id:
                    continue
                for line_number, line in enumerate(
                    _iter_text_lines(payload.decode("utf-8")), start=1
                ):
                    if needle not in line.casefold():
                        continue
                    matches.append(
                        {
                            "result_id": result_id,
                            "line": line_number,
                            "text": _bounded_text(line, 1000),
                            "citation": "%s:L%d" % (result_id, line_number),
                        }
                    )
                    if len(matches) >= max_matches:
                        return matches
        return matches

    def outline(
        self,
        result_id: str,
        *,
        chunk_lines: int = 200,
        max_chunks: int = 50,
    ) -> List[Dict]:
        _validate_result_id(result_id)
        _bounded_positive(chunk_lines, "outline chunk size", maximum=10000)
        _bounded_positive(max_chunks, "outline chunk limit", maximum=MAX_OUTLINE_CHUNKS)
        with self._locked(create=False):
            self._scan_unlocked()
            record, payload = self._load_container_unlocked(result_id)
        lines = iter(_iter_text_lines(payload.decode("utf-8")))
        chunks = []
        for start_index in range(0, record["line_count"], chunk_lines):
            label = None
            selected_count = min(chunk_lines, record["line_count"] - start_index)
            for _index in range(selected_count):
                line = next(lines)
                stripped = line.strip()
                if label is None and stripped:
                    label = stripped
            start_line = start_index + 1
            end_line = min(record["line_count"], start_index + chunk_lines)
            chunks.append(
                {
                    "result_id": result_id,
                    "start_line": start_line,
                    "end_line": end_line,
                    "label": _bounded_text(label if label is not None else "(blank)", 200),
                    "citation": "%s:L%d-L%d" % (result_id, start_line, end_line),
                }
            )
            if len(chunks) >= max_chunks:
                break
        return chunks

    def _store_capture(
        self,
        process: ProcessResult,
        capture: StreamCapture,
        *,
        source_id: str,
        step_id: str,
        attempt: int,
        stream: str,
        preview_limit_bytes: int,
    ) -> Dict:
        # Redaction and container assembly intentionally run one at a time.
        # Together with the process-layer capture budget, this bounds peak
        # transient work independently of workflow fan-out.
        try:
            with self._locked(create=True):
                paths, total_bytes = self._scan_unlocked()
                return self._store_capture_locked(
                    process,
                    capture,
                    source_id=source_id,
                    step_id=step_id,
                    attempt=attempt,
                    stream=stream,
                    preview_limit_bytes=preview_limit_bytes,
                    paths=paths,
                    total_bytes=total_bytes,
                )
        except ResultQuotaExceeded:
            raise
        except ValidationError as exc:
            if is_optional_storage_error(exc):
                raise ResultStorageUnavailable("result storage is unavailable") from exc
            raise
        except OSError as exc:
            if is_optional_storage_error(exc):
                raise ResultStorageUnavailable("result storage is unavailable") from exc
            raise ValidationError("cannot persist process result") from exc

    def _store_capture_locked(
        self,
        process: ProcessResult,
        capture: StreamCapture,
        *,
        source_id: str,
        step_id: str,
        attempt: int,
        stream: str,
        preview_limit_bytes: int,
        paths,
        total_bytes: int,
    ) -> Dict:
        payload = _read_capture(capture)
        try:
            decoded = payload.decode("utf-8")
            text_fidelity = "utf8"
        except UnicodeDecodeError:
            decoded = payload.decode("utf-8", errors="replace")
            text_fidelity = "replacement-decoded"
        redacted = redact_text(decoded).encode("utf-8")
        redaction_truncated = len(redacted) > MAX_RESULT_BYTES
        if redaction_truncated:
            redacted = _utf8_prefix(redacted, MAX_RESULT_BYTES)
        content_sha256 = sha256_bytes(redacted)
        capture_state = {
            "capture_limit_reached": capture.truncated,
            "reader_eof": capture.eof,
            "reader_error": capture.read_error,
            "reader_finished": capture.reader_finished,
            "redaction_limit_reached": redaction_truncated,
        }
        source = {
            "kind": "process-stream",
            "source_id": source_id,
            "step_id": step_id,
            "attempt": attempt,
            "stream": stream,
        }
        producer_status = (
            "timed_out"
            if process.timed_out
            else ("completed" if process.returncode == 0 else "failed")
        )
        record = {
            "schema": RESULT_SCHEMA,
            "run_id": self.run.descriptor["run_id"],
            "run_descriptor_sha256": self.run.descriptor["descriptor_sha256"],
            "source": source,
            "content_sha256": content_sha256,
            "size_bytes": len(redacted),
            "original_bytes": capture.total_bytes,
            "captured_bytes": capture.stored_bytes,
            "preview_limit_bytes": preview_limit_bytes,
            "capture_state": capture_state,
            "producer_status": producer_status,
            "returncode": process.returncode,
            "timed_out": process.timed_out,
            "text_fidelity": text_fidelity,
            "redacted": True,
        }
        result_id = "result_" + sha256_bytes(
            canonical_json_bytes(_result_identity(record))
        )[:32]
        record.update(
            {
                "result_id": result_id,
                "created_at_utc": utc_now(),
                "container_relative": "%s.result" % result_id,
                "line_count": sum(
                    1 for _line in _iter_text_lines(redacted.decode("utf-8"))
                ),
                "pipe_complete": _pipe_complete(capture_state),
            }
        )
        record["record_sha256"] = _record_hash(record)
        _validate_record(record, self.run)
        container = _encode_container(record, redacted)
        destination = self.root / record["container_relative"]
        if destination in paths:
            existing, existing_payload = self._load_container_unlocked(result_id)
            if existing_payload != redacted or not _same_result(existing, record):
                raise ValidationError("result identifier collision")
            return existing
        if len(paths) >= MAX_RESULT_RECORDS:
            raise ResultQuotaExceeded("result record quota exceeded")
        if total_bytes + len(container) > MAX_RUN_RESULT_BYTES:
            raise ResultQuotaExceeded("result byte quota exceeded")
        write_new_bytes(destination, container, "result container")
        return record

    def _prepare_root(self) -> None:
        ensure_directory(self.root, "result store")
        reject_symlink_components(self.root, "result store")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise ValidationError("cannot create result store lock") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValidationError("result store lock is invalid")
        finally:
            os.close(fd)

    @contextmanager
    def _locked(self, *, create: bool):
        if create:
            self._prepare_root()
        else:
            reject_symlink_components(self.root, "result store")
            if self.root.is_symlink() or not self.root.is_dir():
                raise ValidationError("result store is invalid")
            if not self.lock_path.is_file() or self.lock_path.is_symlink():
                raise ValidationError("result store lock is invalid")
        root_info = self.root.lstat()
        anchor_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        if hasattr(os, "O_CLOEXEC"):
            anchor_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            anchor_flags |= os.O_NOFOLLOW
        try:
            anchor_fd = os.open(self.run.run_dir, anchor_flags)
        except OSError as exc:
            raise ValidationError("cannot open result store anchor") from exc
        flags = os.O_RDWR if create else os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            try:
                fd = os.open(self.lock_path, flags)
            except OSError as exc:
                raise ValidationError("cannot open result store lock") from exc
        except Exception:
            os.close(anchor_fd)
            raise
        try:
            anchor_info = os.fstat(anchor_fd)
            info = os.fstat(fd)
            if (
                not stat.S_ISDIR(anchor_info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
            ):
                raise ValidationError("result store lock is invalid")
            with _RESULT_OPERATION_MUTEX:
                with self.run._mutex:
                    lock_mode = fcntl.LOCK_EX if create else fcntl.LOCK_SH
                    fcntl.flock(anchor_fd, lock_mode)
                    fcntl.flock(fd, lock_mode)
                    try:
                        observed_anchor = self.run.run_dir.lstat()
                        observed_lock = self.lock_path.lstat()
                        observed_root = self.root.lstat()
                        if (
                            (observed_anchor.st_dev, observed_anchor.st_ino)
                            != (anchor_info.st_dev, anchor_info.st_ino)
                            or (observed_lock.st_dev, observed_lock.st_ino)
                            != (info.st_dev, info.st_ino)
                            or observed_lock.st_nlink != 1
                            or (observed_root.st_dev, observed_root.st_ino)
                            != (root_info.st_dev, root_info.st_ino)
                        ):
                            raise ValidationError("result store lock changed while acquiring it")
                        if create:
                            self._recover_temporary_unlocked()
                        yield
                    finally:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                        fcntl.flock(anchor_fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            os.close(anchor_fd)

    def _recover_temporary_unlocked(self) -> None:
        """Remove only atomic-write temporaries that were never committed."""

        changed = False
        for path in self.root.iterdir():
            if TEMPORARY_CONTAINER.fullmatch(path.name) is None:
                continue
            if path.is_symlink():
                raise ValidationError("result store temporary is invalid")
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink not in {1, 2}:
                raise ValidationError("result store temporary is invalid")
            try:
                path.unlink()
            except OSError as exc:
                raise ValidationError("cannot recover result store temporary") from exc
            changed = True
        if changed:
            directory_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def _scan_unlocked(self):
        entries = sorted(self.root.iterdir(), key=lambda path: path.name)
        paths = []
        temporaries = {}
        total_bytes = 0
        for path in entries:
            if path.name == ".lock":
                continue
            temporary = TEMPORARY_CONTAINER.fullmatch(path.name)
            if temporary is not None:
                if path.is_symlink():
                    raise ValidationError("result store temporary is invalid")
                info = path.lstat()
                destination_name = temporary.group(1)
                if (
                    destination_name in temporaries
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink not in {1, 2}
                    or info.st_size > MAX_RESULT_CONTAINER_BYTES
                ):
                    raise ValidationError("result store temporary is invalid")
                temporaries[destination_name] = (path, info)
                continue
            if path.is_symlink() or not path.is_file() or not path.name.endswith(".result"):
                raise ValidationError("result store contains an invalid entry")
            _validate_result_id(path.name[:-7])
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink not in {1, 2}
                or info.st_size > MAX_RESULT_CONTAINER_BYTES
            ):
                raise ValidationError("result container is invalid")
            if info.st_nlink == 2:
                temporary_entry = temporaries.get(path.name)
                if temporary_entry is None:
                    raise ValidationError("result container hardlink is invalid")
                temporary_info = temporary_entry[1]
                if (
                    temporary_info.st_nlink != 2
                    or (temporary_info.st_dev, temporary_info.st_ino)
                    != (info.st_dev, info.st_ino)
                ):
                    raise ValidationError("result container hardlink is invalid")
            paths.append(path)
            total_bytes += info.st_size
        if len(temporaries) > 1:
            raise ValidationError("result store has multiple interrupted writes")
        for destination_name, (_temporary_path, temporary_info) in temporaries.items():
            destination = self.root / destination_name
            if temporary_info.st_nlink == 1 and destination.exists():
                raise ValidationError("result store temporary binding is invalid")
            if temporary_info.st_nlink == 2 and destination not in paths:
                raise ValidationError("result store temporary binding is invalid")
        if len(paths) > MAX_RESULT_RECORDS or total_bytes > MAX_RUN_RESULT_BYTES:
            raise ValidationError("result store quota is invalid")
        return paths, total_bytes

    def _load_container_unlocked(self, result_id: str):
        _validate_result_id(result_id)
        path = self.root / (result_id + ".result")
        reject_symlink_components(path, "result container")
        try:
            info = path.lstat()
        except OSError as exc:
            raise ValidationError("cannot inspect result container") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_nlink not in {1, 2}:
            raise ValidationError("result container is invalid")
        container = read_regular_bytes(path, "result container", MAX_RESULT_CONTAINER_BYTES)
        record, payload = _decode_container(container)
        _validate_record(record, self.run)
        if record["result_id"] != result_id:
            raise ValidationError("result container identity does not match")
        if len(payload) != record["size_bytes"]:
            raise ValidationError("result content size does not match")
        if sha256_bytes(payload) != record["content_sha256"]:
            raise ValidationError("result content hash does not match")
        try:
            decoded = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("result content must be UTF-8") from exc
        if sum(1 for _line in _iter_text_lines(decoded)) != record["line_count"]:
            raise ValidationError("result content line count does not match")
        return record, payload

    def _verify_state_references(self, records_by_id) -> None:
        for step_id, step_record in self.run.state.get("steps", {}).items():
            metrics = step_record.get("metrics")
            if isinstance(metrics, dict):
                self._verify_metric_references(
                    step_id,
                    step_record["attempt"],
                    metrics,
                    records_by_id,
                )
            packets = step_record.get("packets")
            if not isinstance(packets, dict):
                continue
            for packet_id, packet in packets.items():
                packet_metrics = packet.get("result_metrics") if isinstance(packet, dict) else None
                if isinstance(packet_metrics, dict):
                    self._verify_metric_references(
                        step_id,
                        step_record["attempt"],
                        packet_metrics,
                        records_by_id,
                        expected_source_id="%s-p%s" % (step_id, packet_id),
                    )

    def _verify_metric_references(
        self,
        step_id,
        attempt,
        metrics,
        records_by_id,
        *,
        expected_source_id=None,
    ) -> None:
        for stream in ("stdout", "stderr"):
            result_key = "%s_result_id" % stream
            unavailable_key = "%s_result_unavailable" % stream
            result_id = metrics.get(result_key)
            unavailable = metrics.get(unavailable_key)
            if result_id is not None and unavailable is not None:
                raise ValidationError("run state result availability is contradictory")
            if unavailable is not None and (
                not isinstance(unavailable, str)
                or unavailable not in RESULT_UNAVAILABLE_REASONS
            ):
                raise ValidationError("run state result availability reason is invalid")
            if result_id is None:
                continue
            _validate_result_id(result_id)
            result_record = records_by_id.get(result_id)
            if result_record is None:
                raise ValidationError("run state references a missing result")
            source = result_record["source"]
            if (
                source["step_id"] != step_id
                or source["stream"] != stream
                or source["attempt"] != attempt
                or (
                    expected_source_id is not None
                    and source["source_id"] != expected_source_id
                )
            ):
                raise ValidationError("run state result reference binding is invalid")


def result_diagnostic_suffix(values: Dict[str, str]) -> str:
    """Render only opaque result ids or bounded availability reasons."""

    available = sorted(value for key, value in values.items() if key.endswith("_result_id"))
    unavailable = sorted(value for key, value in values.items() if key.endswith("_result_unavailable"))
    parts = []
    if available:
        parts.append("recoverable result%s %s" % ("s" if len(available) != 1 else "", ", ".join(available)))
    if unavailable:
        parts.append("overflow capture unavailable: %s" % ", ".join(unavailable))
    return ("; " + "; ".join(parts)) if parts else ""


def _iter_text_lines(value: str) -> Iterable[str]:
    """Yield lines with the same boundaries and terminal behavior as str.splitlines()."""

    start = 0
    for match in LINE_BREAK.finditer(value):
        yield value[start : match.start()]
        start = match.end()
    if start < len(value):
        yield value[start:]


def _read_capture(capture: StreamCapture) -> bytes:
    if not isinstance(capture, StreamCapture):
        raise ValidationError("process capture is invalid")
    reservation = getattr(capture, "reservation", None)
    try:
        fd = capture.handle.fileno()
        before = os.fstat(fd)
    except (AttributeError, OSError, ValueError) as exc:
        raise ValidationError("cannot inspect process capture") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink not in {0, 1}
        or (before.st_dev, before.st_ino) != (capture.device, capture.inode)
        or before.st_size != capture.stored_bytes
        or reservation is None
        or getattr(reservation, "released", True) is not False
        or isinstance(getattr(reservation, "size", None), bool)
        or not isinstance(getattr(reservation, "size", None), int)
        or capture.stored_bytes > reservation.size
        or not 0 <= capture.stored_bytes <= MAX_RESULT_BYTES
        or isinstance(capture.total_bytes, bool)
        or not isinstance(capture.total_bytes, int)
        or not 0 <= capture.total_bytes <= 10**12
        or capture.total_bytes < capture.stored_bytes
        or type(capture.truncated) is not bool
        or type(capture.eof) is not bool
        or type(capture.read_error) is not bool
        or type(capture.reader_finished) is not bool
        or (capture.truncated and capture.stored_bytes >= capture.total_bytes)
        or (not capture.truncated and capture.stored_bytes != capture.total_bytes)
        or (capture.eof and capture.read_error)
        or ((capture.eof or capture.read_error) and not capture.reader_finished)
    ):
        raise ValidationError("process capture binding is invalid")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        payload_buffer = bytearray()
        remaining = MAX_RESULT_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            payload_buffer.extend(chunk)
            remaining -= len(chunk)
        payload = bytes(payload_buffer)
        after = os.fstat(fd)
    except OSError as exc:
        raise ValidationError("cannot read process capture") from exc
    if len(payload) > MAX_RESULT_BYTES:
        raise ValidationError("process capture exceeds result limit")
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        or len(payload) != after.st_size
    ):
        raise ValidationError("process capture changed while it was read")
    return payload


def _encode_container(record: Dict, payload: bytes) -> bytes:
    header = canonical_json_bytes(record)
    if len(header) > MAX_RESULT_HEADER_BYTES:
        raise ValidationError("result header exceeds limit")
    return CONTAINER_MAGIC + ("%08x\n" % len(header)).encode("ascii") + header + payload


def _decode_container(container: bytes):
    if not container.startswith(CONTAINER_MAGIC):
        raise ValidationError("result container magic is invalid")
    offset = len(CONTAINER_MAGIC)
    length_line = container[offset : offset + 9]
    if len(length_line) != 9 or length_line[-1:] != b"\n" or re.fullmatch(rb"[0-9a-f]{8}\n", length_line) is None:
        raise ValidationError("result container header length is invalid")
    header_length = int(length_line[:8], 16)
    if not 1 <= header_length <= MAX_RESULT_HEADER_BYTES:
        raise ValidationError("result container header length is invalid")
    header_start = offset + 9
    header_end = header_start + header_length
    if header_end > len(container):
        raise ValidationError("result container is incomplete")
    header = container[header_start:header_end]
    record = strict_json_bytes(header, "result header")
    if not isinstance(record, dict):
        raise ValidationError("result header must be an object")
    if canonical_json_bytes(record) != header:
        raise ValidationError("result header must use canonical JSON")
    return record, container[header_end:]


def _validate_source(source_id: object, step_id: object, attempt: object, preview_limit_bytes: int) -> None:
    if not isinstance(source_id, str) or SOURCE_ID.fullmatch(source_id) is None:
        raise ValidationError("result source id is invalid")
    if not isinstance(step_id, str) or SOURCE_ID.fullmatch(step_id) is None:
        raise ValidationError("result step id is invalid")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 1000:
        raise ValidationError("result attempt is invalid")
    _bounded_positive(preview_limit_bytes, "result preview limit", maximum=10 * 1024 * 1024)


def _validate_result_id(result_id: object) -> None:
    if not isinstance(result_id, str) or RESULT_ID.fullmatch(result_id) is None:
        raise ValidationError("result id is invalid")


def _validate_record(record, run: RunState) -> None:
    fields = {
        "schema",
        "result_id",
        "run_id",
        "run_descriptor_sha256",
        "source",
        "created_at_utc",
        "container_relative",
        "content_sha256",
        "size_bytes",
        "line_count",
        "original_bytes",
        "captured_bytes",
        "preview_limit_bytes",
        "capture_state",
        "pipe_complete",
        "producer_status",
        "returncode",
        "timed_out",
        "text_fidelity",
        "redacted",
        "record_sha256",
    }
    if not isinstance(record, dict) or set(record) != fields or record.get("schema") != RESULT_SCHEMA:
        raise ValidationError("result record fields are invalid")
    _validate_result_id(record.get("result_id"))
    result_id = record["result_id"]
    if (
        record.get("run_id") != run.descriptor["run_id"]
        or record.get("run_descriptor_sha256") != run.descriptor["descriptor_sha256"]
        or record.get("container_relative") != "%s.result" % result_id
        or not isinstance(record.get("created_at_utc"), str)
        or not 0 < len(record["created_at_utc"]) <= 64
        or not isinstance(record.get("content_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", record["content_sha256"]) is None
        or record.get("redacted") is not True
        or type(record.get("pipe_complete")) is not bool
        or type(record.get("timed_out")) is not bool
        or record.get("producer_status") not in {"completed", "failed", "timed_out"}
        or record.get("text_fidelity") not in {"utf8", "replacement-decoded"}
    ):
        raise ValidationError("result record binding is invalid")
    returncode = record.get("returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int) or not -(2**31) <= returncode < 2**31:
        raise ValidationError("result producer return code is invalid")
    expected_status = "timed_out" if record["timed_out"] else ("completed" if returncode == 0 else "failed")
    if record["producer_status"] != expected_status:
        raise ValidationError("result producer status is invalid")
    for field, maximum in (
        ("size_bytes", MAX_RESULT_BYTES),
        ("line_count", 10**9),
        ("original_bytes", 10**12),
        ("captured_bytes", MAX_RESULT_BYTES),
        ("preview_limit_bytes", 10 * 1024 * 1024),
    ):
        value = record.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValidationError("result record %s is invalid" % field)
    if record["captured_bytes"] > record["original_bytes"]:
        raise ValidationError("result record capture sizes are invalid")
    capture_state = record.get("capture_state")
    capture_state_fields = {
        "capture_limit_reached",
        "reader_eof",
        "reader_error",
        "reader_finished",
        "redaction_limit_reached",
    }
    if (
        not isinstance(capture_state, dict)
        or set(capture_state) != capture_state_fields
        or any(type(capture_state[field]) is not bool for field in capture_state_fields)
        or (capture_state["reader_eof"] and capture_state["reader_error"])
        or (
            (capture_state["reader_eof"] or capture_state["reader_error"])
            and not capture_state["reader_finished"]
        )
        or (
            capture_state["capture_limit_reached"]
            and record["captured_bytes"] >= record["original_bytes"]
        )
        or (
            not capture_state["capture_limit_reached"]
            and record["captured_bytes"] != record["original_bytes"]
        )
        or record["pipe_complete"] != _pipe_complete(capture_state)
    ):
        raise ValidationError("result capture state is invalid")
    source = record.get("source")
    if not isinstance(source, dict) or set(source) != {"kind", "source_id", "step_id", "attempt", "stream"}:
        raise ValidationError("result source is invalid")
    if source.get("kind") != "process-stream" or source.get("stream") not in {"stdout", "stderr"}:
        raise ValidationError("result source is invalid")
    _validate_source(
        source.get("source_id"),
        source.get("step_id"),
        source.get("attempt"),
        record["preview_limit_bytes"],
    )
    if source["step_id"] not in run.state.get("steps", {}):
        raise ValidationError("result source step binding is invalid")
    expected_result_id = "result_" + sha256_bytes(
        canonical_json_bytes(_result_identity(record))
    )[:32]
    if result_id != expected_result_id:
        raise ValidationError("result identifier binding is invalid")
    if not isinstance(record.get("record_sha256"), str) or record["record_sha256"] != _record_hash(record):
        raise ValidationError("result record hash does not match")


def _result_identity(record: Dict) -> Dict:
    return {
        key: record[key]
        for key in (
            "schema",
            "run_id",
            "run_descriptor_sha256",
            "source",
            "content_sha256",
            "size_bytes",
            "original_bytes",
            "captured_bytes",
            "preview_limit_bytes",
            "capture_state",
            "producer_status",
            "returncode",
            "timed_out",
            "text_fidelity",
            "redacted",
        )
    }


def _pipe_complete(capture_state: Dict) -> bool:
    return bool(
        not capture_state["capture_limit_reached"]
        and capture_state["reader_eof"]
        and not capture_state["reader_error"]
        and capture_state["reader_finished"]
        and not capture_state["redaction_limit_reached"]
    )


def _record_hash(record: Dict) -> str:
    payload = dict(record)
    payload.pop("record_sha256", None)
    return sha256_bytes(canonical_json_bytes(payload))


def _same_result(left: Dict, right: Dict) -> bool:
    ignored = {"created_at_utc", "record_sha256"}
    return {key: value for key, value in left.items() if key not in ignored} == {
        key: value for key, value in right.items() if key not in ignored
    }


def _bounded_positive(value, label: str, *, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValidationError("%s is invalid" % label)


def _utf8_prefix(payload: bytes, limit: int) -> bytes:
    selected = payload[:limit]
    while selected:
        try:
            selected.decode("utf-8")
            return selected
        except UnicodeDecodeError:
            selected = selected[:-1]
    return b""


def _bounded_text(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return _utf8_prefix(encoded, max_bytes).decode("utf-8")
