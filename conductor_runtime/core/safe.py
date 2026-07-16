"""No-follow filesystem operations and external-state paths."""

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from ..errors import ValidationError


MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_RELATIVE_PATH_CHARS = 4096


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json_bytes(value) -> bytes:
    try:
        return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("value is not canonical JSON") from exc


def strict_json_bytes(payload: bytes, label: str):
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValidationError("%s is not strict UTF-8 JSON" % label) from exc


def require_relative(value: str, label: str = "path") -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_RELATIVE_PATH_CHARS
        or "\x00" in value
    ):
        raise ValidationError("%s must be a non-empty relative path" % label)
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValidationError("%s must not escape its base directory" % label)
    return path.as_posix()


def resolve_under(base: Path, relative: str, label: str = "path") -> Path:
    clean = require_relative(relative, label)
    root = Path(base).resolve()
    candidate = root.joinpath(*Path(clean).parts)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValidationError("%s escapes its base directory" % label) from exc
    return candidate


def reject_symlink_components(path: Path, label: str, *, include_leaf: bool = True) -> None:
    candidate = Path(path).expanduser()
    absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
    parts = absolute.parts
    current = Path(parts[0])
    limit = len(parts) if include_leaf else max(1, len(parts) - 1)
    for part in parts[1:limit]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValidationError("cannot inspect %s" % label) from exc
        if stat.S_ISLNK(info.st_mode):
            raise ValidationError("%s must not contain symlinks" % label)


def ensure_directory(path: Path, label: str, mode: int = 0o700) -> Path:
    candidate = Path(path).expanduser()
    absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
    parts = absolute.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        try:
            os.mkdir(current, mode)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ValidationError("cannot create %s" % label) from exc
        try:
            info = current.lstat()
        except OSError as exc:
            raise ValidationError("cannot inspect %s" % label) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValidationError("%s must contain only real directories" % label)
    return absolute


def read_regular_bytes(path: Path, label: str, max_bytes: int = MAX_JSON_BYTES) -> bytes:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValidationError("%s byte limit is invalid" % label)
    candidate = Path(path).expanduser()
    reject_symlink_components(candidate, label)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(candidate, flags)
    except OSError as exc:
        raise ValidationError("cannot open %s" % label) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValidationError("%s must be a regular file" % label)
        if before.st_size > max_bytes:
            raise ValidationError("%s exceeds %d bytes" % (label, max_bytes))
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(fd)
        if len(payload) > max_bytes:
            raise ValidationError("%s exceeds %d bytes" % (label, max_bytes))
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or len(payload) != after.st_size:
            raise ValidationError("%s changed while it was read" % label)
        return payload
    finally:
        os.close(fd)


def read_regular_text(path: Path, label: str, max_bytes: int = MAX_JSON_BYTES) -> str:
    try:
        return read_regular_bytes(path, label, max_bytes).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("%s must be UTF-8 text" % label) from exc


def write_new_bytes(path: Path, payload: bytes, label: str, mode: int = 0o600) -> None:
    _write_bytes(path, payload, label, mode=mode, replace=False)


def replace_bytes(path: Path, payload: bytes, label: str, mode: int = 0o600) -> None:
    _write_bytes(path, payload, label, mode=mode, replace=True)


def write_new_json(path: Path, value, label: str) -> None:
    write_new_bytes(path, canonical_json_bytes(value), label)


def replace_json(path: Path, value, label: str) -> None:
    replace_bytes(path, canonical_json_bytes(value), label)


def load_json(path: Path, label: str, max_bytes: int = MAX_JSON_BYTES):
    return strict_json_bytes(read_regular_bytes(path, label, max_bytes), label)


def _write_bytes(path: Path, payload: bytes, label: str, *, mode: int, replace: bool) -> None:
    if not isinstance(payload, bytes):
        raise ValidationError("%s payload must be bytes" % label)
    candidate = Path(path).expanduser()
    parent = ensure_directory(candidate.parent, "%s parent" % label)
    reject_symlink_components(parent, "%s parent" % label)
    if candidate.exists() or candidate.is_symlink():
        if not replace:
            raise ValidationError("%s already exists" % label)
        reject_symlink_components(candidate, label)
        info = candidate.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError("%s must be a regular file" % label)
    prefix = ".%s." % candidate.name
    try:
        fd, temporary = tempfile.mkstemp(prefix=prefix, dir=parent)
    except OSError as exc:
        raise ValidationError("cannot stage %s" % label) from exc
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ValidationError("cannot write %s" % label)
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        reject_symlink_components(candidate, label, include_leaf=False)
        if replace:
            os.replace(temporary_path, candidate)
        else:
            try:
                os.link(temporary_path, candidate, follow_symlinks=False)
            except FileExistsError as exc:
                raise ValidationError("%s already exists" % label) from exc
            except OSError as exc:
                raise ValidationError("cannot install %s" % label) from exc
            temporary_path.unlink()
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def conductor_home() -> Path:
    configured = os.environ.get("CODEX_CONDUCTOR_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".codex" / "conductor"
    return ensure_directory(root.resolve(strict=False), "Conductor home")


def workspace_state_root(workspace: Path) -> Path:
    resolved = Path(workspace).resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    name = "".join(char if char.isalnum() or char in "-_" else "-" for char in resolved.name) or "workspace"
    return ensure_directory(conductor_home() / "workspaces" / (name[:48] + "-" + digest), "workspace state")


def external_runs_dir(workspace: Path) -> Path:
    return ensure_directory(workspace_state_root(workspace) / "runs", "run state")


def external_goals_dir(workspace: Path) -> Path:
    return ensure_directory(workspace_state_root(workspace) / "goals", "goal state")


def require_external_state_path(path: Path, workspace: Path, label: str) -> Path:
    candidate = Path(path).expanduser().resolve(strict=False)
    source = Path(workspace).expanduser().resolve()
    try:
        candidate.relative_to(source)
    except ValueError:
        return candidate
    raise ValidationError("%s must remain outside the workspace" % label)


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key %s" % key)
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant %s" % value)
