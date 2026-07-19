"""Bounded subprocess execution with process-group cleanup."""

import os
import select
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, List, Mapping, Optional

from ..errors import ValidationError
from .safe import ensure_directory, is_optional_storage_error, reject_symlink_components


SUBPROCESS_ENVIRONMENT_KEYS = frozenset(
    {
        "APPDATA",
        "COLORTERM",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "LOGNAME",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "ProgramData",
        "SHELL",
        "SYSTEMROOT",
        "SystemRoot",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "TZ",
        "USER",
        "USERPROFILE",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    }
)
MAX_CAPTURE_BYTES = 32 * 1024 * 1024
MAX_TRANSIENT_CAPTURE_BYTES = 256 * 1024 * 1024
_CAPTURE_BUDGET_LOCK = threading.Lock()
_capture_budget_used = 0


class _CaptureReservation:
    def __init__(self, size: int):
        self.size = size
        self.released = False

    def release(self) -> None:
        global _capture_budget_used
        with _CAPTURE_BUDGET_LOCK:
            if self.released:
                return
            _capture_budget_used -= self.size
            self.released = True

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass


def _reserve_capture(size: int) -> Optional[_CaptureReservation]:
    global _capture_budget_used
    with _CAPTURE_BUDGET_LOCK:
        if _capture_budget_used + size > MAX_TRANSIENT_CAPTURE_BYTES:
            return None
        _capture_budget_used += size
    return _CaptureReservation(size)


def capture_limit_for_parallelism(workers: int) -> int:
    """Return a per-stream cap that keeps one fan-out wave within its raw budget."""

    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 32:
        raise ValidationError("capture parallelism is invalid")
    return min(MAX_CAPTURE_BYTES, MAX_TRANSIENT_CAPTURE_BYTES // (2 * workers))


@dataclass(frozen=True)
class StreamCapture:
    """Private temporary capture of one drained process stream."""

    handle: BinaryIO = field(repr=False, compare=False)
    total_bytes: int
    stored_bytes: int
    truncated: bool
    eof: bool
    read_error: bool
    reader_finished: bool
    device: int
    inode: int
    reservation: _CaptureReservation = field(repr=False, compare=False)


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    duration_ms: int = 0
    stdout_pipe_complete: bool = True
    stderr_pipe_complete: bool = True
    stdout_capture: Optional[StreamCapture] = None
    stderr_capture: Optional[StreamCapture] = None
    stdout_capture_unavailable: Optional[str] = None
    stderr_capture_unavailable: Optional[str] = None


class _BoundedBuffer:
    def __init__(self, limit: int):
        self.limit = limit
        self.data = bytearray()
        self.truncated = False
        self.eof = False
        self.read_error = False
        self.reader_finished = False
        self.lock = threading.Lock()

    def append(self, value: bytes) -> Optional[bytes]:
        with self.lock:
            remaining = self.limit - len(self.data)
            overflow_prefix = None
            if not self.truncated and len(value) > max(0, remaining):
                overflow_prefix = bytes(self.data)
            if remaining > 0:
                self.data.extend(value[:remaining])
            if len(value) > max(0, remaining):
                self.truncated = True
            return overflow_prefix

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")

    def mark_reader_finished(self, *, eof: bool, read_error: bool) -> None:
        with self.lock:
            self.eof = bool(eof)
            self.read_error = bool(read_error)
            self.reader_finished = True

    def pipe_complete(self) -> bool:
        with self.lock:
            return bool(self.eof and not self.read_error and self.reader_finished)

    def reader_state(self):
        with self.lock:
            return self.eof, self.read_error, self.reader_finished


class _CaptureBuffer:
    def __init__(self, root: Path, limit: int):
        self.root = root
        self.handle: Optional[BinaryIO] = None
        self.fd = -1
        self.reservation: Optional[_CaptureReservation] = None
        self.limit = limit
        self.total_bytes = 0
        self.stored_bytes = 0
        self.truncated = False
        self.failure: Optional[BaseException] = None
        self.unavailable_reason: Optional[str] = None

    def _activate(self) -> bool:
        reservation = _reserve_capture(self.limit)
        if reservation is None:
            self.unavailable_reason = "transient_budget_exhausted"
            return False
        handle = None
        try:
            capture_root = ensure_directory(self.root, "process capture directory")
            reject_symlink_components(capture_root, "process capture directory")
            handle = tempfile.TemporaryFile(mode="w+b", dir=capture_root)
            fd = handle.fileno()
            os.fchmod(fd, 0o600)
            os.set_inheritable(fd, False)
        except (OSError, ValidationError) as exc:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            reservation.release()
            self.failure = exc
            return False
        self.handle = handle
        self.fd = fd
        self.reservation = reservation
        return True

    def append(self, value: bytes, *, overflow_prefix: Optional[bytes]) -> None:
        if self.unavailable_reason is not None or self.failure is not None:
            return
        if self.handle is None:
            if overflow_prefix is None:
                return
            if not self._activate():
                return
            self._append_bytes(overflow_prefix)
        self._append_bytes(value)

    def _append_bytes(self, value: bytes) -> None:
        self.total_bytes += len(value)
        if self.failure is not None:
            return
        remaining = self.limit - self.stored_bytes
        selected = memoryview(value)[: max(0, remaining)]
        try:
            while selected:
                written = os.write(self.fd, selected)
                if written <= 0:
                    raise OSError("short process capture write")
                selected = selected[written:]
                self.stored_bytes += written
        except OSError as exc:
            self.failure = exc
        if len(value) > max(0, remaining):
            self.truncated = True

    def finish(self, reader_state=None) -> Optional[StreamCapture]:
        if self.failure is not None:
            return self._finish_failure(self.failure)
        if self.handle is None:
            return None
        try:
            if reader_state is None:
                raise ValidationError("process capture reader state is missing")
            eof, read_error, reader_finished = reader_state
            handle = self.handle
            os.fsync(self.fd)
            info = os.fstat(self.fd)
            if info.st_size != self.stored_bytes:
                raise ValidationError("process stream capture size changed")
            os.lseek(self.fd, 0, os.SEEK_SET)
            reservation = self.reservation
            if reservation is None:
                raise ValidationError("process stream capture reservation is missing")
            capture = StreamCapture(
                handle=handle,
                total_bytes=self.total_bytes,
                stored_bytes=self.stored_bytes,
                truncated=self.truncated,
                eof=eof,
                read_error=read_error,
                reader_finished=reader_finished,
                device=info.st_dev,
                inode=info.st_ino,
                reservation=reservation,
            )
            self.handle = None
            self.fd = -1
            self.reservation = None
            return capture
        except OSError as exc:
            return self._finish_failure(exc)
        except ValidationError:
            self.discard()
            raise

    def _finish_failure(self, error: BaseException) -> Optional[StreamCapture]:
        self.discard()
        if is_optional_storage_error(error):
            self.unavailable_reason = "capture_storage_unavailable"
            return None
        raise ValidationError("process capture integrity check failed") from error

    def discard(self) -> None:
        if self.handle is not None:
            try:
                self.handle.close()
            except OSError:
                pass
            self.handle = None
            self.fd = -1
        if self.reservation is not None:
            self.reservation.release()
            self.reservation = None


def run_process(
    argv: List[str],
    *,
    cwd: Path,
    input_text: str = "",
    timeout_seconds: int = 900,
    output_limit_bytes: int = 1024 * 1024,
    env: Optional[Mapping[str, str]] = None,
    capture_directory: Optional[Path] = None,
    capture_limit_bytes: int = MAX_CAPTURE_BYTES,
) -> ProcessResult:
    if not isinstance(argv, list) or not argv or not all(isinstance(value, str) and value for value in argv):
        raise ValidationError("process argv must contain non-empty strings")
    if not isinstance(input_text, str):
        raise ValidationError("process input must be text")
    try:
        input_payload = input_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError("process input must be valid UTF-8 text") from exc
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds < 1
        or timeout_seconds > 24 * 60 * 60
    ):
        raise ValidationError("process timeout is invalid")
    if (
        not isinstance(output_limit_bytes, int)
        or isinstance(output_limit_bytes, bool)
        or output_limit_bytes < 1
        or output_limit_bytes > 10 * 1024 * 1024
    ):
        raise ValidationError("process output limit is invalid")
    if (
        not isinstance(capture_limit_bytes, int)
        or isinstance(capture_limit_bytes, bool)
        or capture_limit_bytes < 1
        or capture_limit_bytes > MAX_CAPTURE_BYTES
    ):
        raise ValidationError("process capture limit is invalid")
    process_environment = (
        sanitized_subprocess_environment() if env is None else _copy_environment(env)
    )
    capture_buffers: List[_CaptureBuffer] = []
    stdout_capture_buffer: Optional[_CaptureBuffer] = None
    stderr_capture_buffer: Optional[_CaptureBuffer] = None
    if capture_directory is not None:
        try:
            capture_root = Path(capture_directory)
        except TypeError as exc:
            raise ValidationError("process capture directory is invalid") from exc
        reject_symlink_components(
            capture_root,
            "process capture directory",
            include_leaf=False,
        )
        if capture_root.exists() or capture_root.is_symlink():
            reject_symlink_components(capture_root, "process capture directory")
            try:
                capture_info = capture_root.lstat()
            except OSError as exc:
                raise ValidationError("cannot inspect process capture directory") from exc
            if not stat.S_ISDIR(capture_info.st_mode):
                raise ValidationError("process capture directory must be a real directory")
        stdout_capture_buffer = _CaptureBuffer(capture_root, capture_limit_bytes)
        stderr_capture_buffer = _CaptureBuffer(capture_root, capture_limit_bytes)
        capture_buffers.extend((stdout_capture_buffer, stderr_capture_buffer))
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=Path(cwd),
            env=process_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, TypeError, ValueError) as exc:
        for capture in capture_buffers:
            capture.discard()
        raise ValidationError("failed to start %s" % Path(argv[0]).name) from exc
    return _collect_spawned_process(
        process,
        input_payload=input_payload,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
        capture_buffers=capture_buffers,
        stdout_capture_buffer=stdout_capture_buffer,
        stderr_capture_buffer=stderr_capture_buffer,
        started=started,
    )


def _collect_spawned_process(
    process: subprocess.Popen,
    *,
    input_payload: bytes,
    timeout_seconds: int,
    output_limit_bytes: int,
    capture_buffers,
    stdout_capture_buffer: Optional[_CaptureBuffer],
    stderr_capture_buffer: Optional[_CaptureBuffer],
    started: float,
) -> ProcessResult:
    stdout = _BoundedBuffer(output_limit_bytes)
    stderr = _BoundedBuffer(output_limit_bytes)
    readers = []
    writer = None
    started_threads = []
    transferred_captures = []
    result_returned = False
    stop_io = threading.Event()
    try:
        readers = [
            threading.Thread(
                target=_read_stream,
                args=(process.stdout, stdout, stdout_capture_buffer, stop_io),
                daemon=True,
            ),
            threading.Thread(
                target=_read_stream,
                args=(process.stderr, stderr, stderr_capture_buffer, stop_io),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
            started_threads.append(reader)
        writer = threading.Thread(
            target=_write_stream,
            args=(process.stdin, input_payload, stop_io),
            daemon=True,
        )
        writer.start()
        started_threads.append(writer)

        timed_out = False
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_group(process)
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise ValidationError("process group did not terminate") from exc

        writer.join(timeout=1)
        for reader in readers:
            reader.join(timeout=1)
        if writer.is_alive() or any(reader.is_alive() for reader in readers):
            # A descendant may inherit a pipe after the leader exits. Terminate
            # the original process group so output collection cannot leak it.
            _terminate_group(process)
            stop_io.set()
            writer.join(timeout=2)
            for reader in readers:
                reader.join(timeout=2)
        if writer.is_alive() or any(reader.is_alive() for reader in readers):
            raise ValidationError("process I/O worker did not terminate")

        stdout_capture = stdout_capture_buffer.finish(stdout.reader_state()) if stdout_capture_buffer else None
        if stdout_capture is not None:
            transferred_captures.append(stdout_capture)
        stderr_capture = stderr_capture_buffer.finish(stderr.reader_state()) if stderr_capture_buffer else None
        if stderr_capture is not None:
            transferred_captures.append(stderr_capture)
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        result = ProcessResult(
            returncode=returncode,
            stdout=stdout.text(),
            stderr=stderr.text(),
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            timed_out=timed_out,
            duration_ms=duration_ms,
            stdout_pipe_complete=stdout.pipe_complete(),
            stderr_pipe_complete=stderr.pipe_complete(),
            stdout_capture=stdout_capture,
            stderr_capture=stderr_capture,
            stdout_capture_unavailable=(
                stdout_capture_buffer.unavailable_reason
                if stdout_capture_buffer is not None and stdout.truncated
                else None
            ),
            stderr_capture_unavailable=(
                stderr_capture_buffer.unavailable_reason
                if stderr_capture_buffer is not None and stderr.truncated
                else None
            ),
        )
        result_returned = True
        return result
    except (OSError, RuntimeError) as exc:
        raise ValidationError("process I/O worker failed") from exc
    finally:
        stop_io.set()
        if process.poll() is None:
            _terminate_group(process)
        for thread in started_threads:
            thread.join(timeout=2)
        alive_threads = [thread for thread in started_threads if thread.is_alive()]
        if alive_threads:
            _close_process_fds(process)
            for thread in alive_threads:
                thread.join(timeout=1)
        if any(thread.is_alive() for thread in started_threads):
            _close_process_fds(process)
        else:
            _close_process_pipes(process)
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for capture_buffer in capture_buffers:
            capture_buffer.discard()
        if not result_returned:
            for capture in transferred_captures:
                try:
                    capture.handle.close()
                except OSError:
                    pass
                finally:
                    capture.reservation.release()


def _close_process_pipes(process: subprocess.Popen) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _close_process_fds(process: subprocess.Popen) -> None:
    """Interrupt a stuck I/O worker without taking a buffered stream lock."""

    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            os.close(stream.fileno())
        except (OSError, ValueError):
            pass


def discard_process_captures(result: ProcessResult) -> None:
    """Remove private stream spools after a caller has consumed or rejected them."""

    for capture in (result.stdout_capture, result.stderr_capture):
        if capture is None:
            continue
        try:
            capture.handle.close()
        except OSError:
            pass
        finally:
            capture.reservation.release()


def sanitized_subprocess_environment(
    *,
    extra_keys: Iterable[str] = (),
    source: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Copy only explicitly supported ambient values into a child process."""
    allowed = set(SUBPROCESS_ENVIRONMENT_KEYS)
    for key in extra_keys:
        if not isinstance(key, str) or not key or "=" in key or "\x00" in key:
            raise ValidationError("process environment allowlist contains an invalid name")
        allowed.add(key)
    ambient = os.environ if source is None else source
    selected = {key: ambient[key] for key in allowed if key in ambient}
    if "PATH" not in selected:
        selected["PATH"] = os.defpath
    return _copy_environment(selected)


def _copy_environment(environment: Mapping[str, str]) -> Dict[str, str]:
    if not isinstance(environment, Mapping):
        raise ValidationError("process environment must be a string mapping")
    copied = {}
    for key, value in environment.items():
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\x00" in key
            or not isinstance(value, str)
            or "\x00" in value
        ):
            raise ValidationError("process environment must contain valid string entries")
        copied[key] = value
    return copied


def _read_stream(
    stream,
    buffer: _BoundedBuffer,
    capture: Optional[_CaptureBuffer] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    if stream is None:
        buffer.mark_reader_finished(eof=True, read_error=False)
        return
    eof = False
    read_error = False
    try:
        fd = stream.fileno()
        os.set_blocking(fd, False)
        while True:
            if stop_event is not None and stop_event.is_set():
                read_error = True
                break
            readable, _writable, _errors = select.select([fd], [], [], 0.1)
            if not readable:
                continue
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                eof = True
                break
            overflow_prefix = buffer.append(chunk)
            if capture is not None:
                capture.append(chunk, overflow_prefix=overflow_prefix)
    except (OSError, ValueError):
        read_error = True
    finally:
        buffer.mark_reader_finished(eof=eof, read_error=read_error)
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _write_stream(
    stream,
    payload: bytes,
    stop_event: Optional[threading.Event] = None,
) -> None:
    if stream is None:
        return
    try:
        fd = stream.fileno()
        os.set_blocking(fd, False)
        offset = 0
        while offset < len(payload):
            if stop_event is not None and stop_event.is_set():
                break
            _readable, writable, _errors = select.select([], [fd], [], 0.1)
            if not writable:
                continue
            try:
                written = os.write(fd, payload[offset : offset + 65536])
            except BlockingIOError:
                continue
            if written <= 0:
                break
            offset += written
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _terminate_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            break
        time.sleep(0.01)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
