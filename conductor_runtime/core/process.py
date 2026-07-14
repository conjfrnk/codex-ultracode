"""Bounded subprocess execution with process-group cleanup."""

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from ..errors import ValidationError


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
    env: Optional[Dict[str, str]] = None,
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
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=Path(cwd),
            env=env,
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
    try:
        if process.stdin is not None:
            try:
                process.stdin.write(input_text.encode("utf-8"))
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        timed_out = False
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_group(process)
            returncode = process.wait(timeout=5)
    finally:
        for reader in readers:
            reader.join(timeout=5)
        if process.poll() is None:
            _terminate_group(process)
            process.wait(timeout=5)
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


def _terminate_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
