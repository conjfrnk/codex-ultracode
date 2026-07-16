"""Bounded subprocess execution with process-group cleanup."""

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from ..errors import ValidationError


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


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    duration_ms: int


class _BoundedBuffer:
    def __init__(self, limit: int):
        self.limit = limit
        self.data = bytearray()
        self.truncated = False
        self.lock = threading.Lock()

    def append(self, value: bytes) -> None:
        with self.lock:
            remaining = self.limit - len(self.data)
            if remaining > 0:
                self.data.extend(value[:remaining])
            if len(value) > max(0, remaining):
                self.truncated = True

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


def run_process(
    argv: List[str],
    *,
    cwd: Path,
    input_text: str = "",
    timeout_seconds: int = 900,
    output_limit_bytes: int = 1024 * 1024,
    env: Optional[Mapping[str, str]] = None,
) -> ProcessResult:
    if not isinstance(argv, list) or not argv or not all(isinstance(value, str) and value for value in argv):
        raise ValidationError("process argv must contain non-empty strings")
    if not isinstance(input_text, str):
        raise ValidationError("process input must be text")
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
    process_environment = (
        sanitized_subprocess_environment() if env is None else _copy_environment(env)
    )
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
    except OSError as exc:
        raise ValidationError("failed to start %s" % Path(argv[0]).name) from exc
    stdout = _BoundedBuffer(output_limit_bytes)
    stderr = _BoundedBuffer(output_limit_bytes)
    readers = [
        threading.Thread(target=_read_stream, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=_read_stream, args=(process.stderr, stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    writer = threading.Thread(
        target=_write_stream,
        args=(process.stdin, input_text.encode("utf-8")),
        daemon=True,
    )
    writer.start()
    try:
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
    finally:
        writer.join(timeout=1)
        for reader in readers:
            reader.join(timeout=1)
        if writer.is_alive() or any(reader.is_alive() for reader in readers):
            # A descendant may inherit a pipe after the leader exits. Terminate
            # the original process group so output collection cannot leak it.
            _terminate_group(process)
            writer.join(timeout=2)
            for reader in readers:
                reader.join(timeout=2)
        if process.poll() is None:
            _terminate_group(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise ValidationError("process group did not terminate") from exc
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    return ProcessResult(
        returncode=returncode,
        stdout=stdout.text(),
        stderr=stderr.text(),
        stdout_truncated=stdout.truncated,
        stderr_truncated=stderr.truncated,
        timed_out=timed_out,
        duration_ms=duration_ms,
    )


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


def _read_stream(stream, buffer: _BoundedBuffer) -> None:
    if stream is None:
        return
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            buffer.append(chunk)
    except OSError:
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _write_stream(stream, payload: bytes) -> None:
    if stream is None:
        return
    try:
        stream.write(payload)
        stream.close()
    except (BrokenPipeError, OSError):
        try:
            stream.close()
        except OSError:
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
