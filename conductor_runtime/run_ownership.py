import errno
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX ownership proof is unavailable.
    fcntl = None

from .errors import ValidationError
from .security import open_dir_no_follow, reject_symlink_path


RUN_EXECUTION_LOCK_NAME = ".runner-execution.lock"
_LOCAL_LOCKS = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def execution_lock_supported() -> bool:
    return fcntl is not None


@contextmanager
def run_execution_lock(run_dir: Path, require_cross_process: bool = False):
    """Hold exclusive ownership for one workflow execution or recovery mutation."""
    if require_cross_process and fcntl is None:
        raise ValidationError("interrupted-run recovery requires POSIX advisory file locking")

    run_dir = Path(run_dir)
    lock_path = run_dir / RUN_EXECUTION_LOCK_NAME
    reject_symlink_path(lock_path, "run execution lock")
    local_key = str(run_dir.absolute())
    with _LOCAL_LOCKS_GUARD:
        local_lock = _LOCAL_LOCKS.setdefault(local_key, threading.Lock())
    if not local_lock.acquire(blocking=False):
        raise ValidationError("run already has an active execution owner")

    run_fd = None
    lock_fd = None
    locked = False
    try:
        try:
            run_fd = open_dir_no_follow(run_dir, "run directory")
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            lock_fd = os.open(RUN_EXECUTION_LOCK_NAME, flags, 0o600, dir_fd=run_fd)
            info = os.fstat(lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValidationError("run execution lock must be a single-link regular file")
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise ValidationError("run already has an active execution owner")
            raise ValidationError("failed to open run execution lock: %s" % exc.__class__.__name__)
        yield
    finally:
        if lock_fd is not None:
            if locked:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        if run_fd is not None:
            os.close(run_fd)
        local_lock.release()
