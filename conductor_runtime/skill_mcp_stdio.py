import hashlib
import json
import os
import platform
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

from .agent_skill_mcp import (
    SAFE_MCP_NAME,
    skill_mcp_stdio_dependencies,
)
from .agent_tool_policy import validate_command_allowlist
from .errors import ValidationError
from .runtime_self import runtime_self_command
from .security import (
    read_regular_file_bytes_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    write_new_text_file_no_follow,
)


SKILL_MCP_STDIO_RECEIPT_SCHEMA = "conductor.skill_mcp_stdio_receipt.v1"
SKILL_MCP_STDIO_PROCESS_SCHEMA = "conductor.skill_mcp_stdio_process.v1"
MAX_SKILL_MCP_STDIO_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_SKILL_MCP_STDIO_FILES = 257
MAX_SKILL_MCP_STDIO_FILE_BYTES = 8 * 1024 * 1024
MAX_SKILL_MCP_STDIO_TOTAL_BYTES = 32 * 1024 * 1024 + 128 * 1024
_RECEIPT_FIELDS = {
    "schema",
    "name",
    "sandbox",
    "sandbox_executable",
    "workspace",
    "snapshot",
    "runtime",
    "script",
    "args",
    "files",
    "snapshot_sha256",
    "pid_path",
    "nonce",
}
_FILE_FIELDS = {"path", "sha256", "bytes", "mode"}
_PROCESS_FIELDS = {"schema", "pid", "pgid", "nonce"}
_SYSTEM_READ_ROOTS_MACOS = (
    "/System",
    "/Library",
    "/usr",
    "/bin",
    "/sbin",
    "/opt/homebrew",
    "/private/etc",
    "/private/var/db",
)
_SYSTEM_BIND_ROOTS_LINUX = ("/usr", "/bin", "/sbin", "/lib", "/lib64")
_MACOS_PROFILE = """(version 1)
(deny default)
(allow process-exec)
(allow process-info* (target self))
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup)
(deny mach-lookup (global-name "com.apple.securityd"))
(deny mach-lookup (global-name "com.apple.securityd.system"))
(deny mach-lookup (global-name "com.apple.secd"))
(deny mach-lookup (global-name "com.apple.trustd"))
(deny mach-lookup (global-name "com.apple.trustd.agent"))
(allow file-read*)
(deny file-read* (subpath (param "USER_HOME")))
(deny file-read* (subpath "/private/tmp"))
(deny file-read* (subpath "/private/var/folders"))
(deny file-read* (subpath (param "WORKSPACE")))
(allow file-read* (subpath (param "SKILL")))
(allow file-read* (subpath (param "RUNTIME")))
(allow file-write* (subpath (param "RUNTIME")))
(allow file-write* (literal "/dev/null"))
"""


class SkillMcpStdioLaunch:
    def __init__(
        self,
        dependencies: List[Dict],
        *,
        workspace: Path,
        skill_records: List[Dict],
    ) -> None:
        self.dependencies = skill_mcp_stdio_dependencies(dependencies)
        self.workspace = Path(workspace).resolve()
        self.skill_records = list(skill_records)
        self.root: Optional[Path] = None
        self.commands: Dict[str, List[str]] = {}
        self._routes: List[Dict] = []

    def __enter__(self):
        if not self.dependencies:
            return self
        sandbox, executable = _supported_sandbox()
        _reject_workspace_under_system_roots(self.workspace, sandbox)
        self.root = Path(
            tempfile.mkdtemp(prefix="codex-conductor-skill-mcp-")
        ).resolve()
        os.chmod(self.root, 0o700)
        records = {record.get("path"): record for record in self.skill_records}
        try:
            for index, dependency in enumerate(self.dependencies):
                skill_record = records.get(dependency["skill"])
                if not isinstance(skill_record, dict):
                    raise ValidationError(
                        "selected Skill stdio MCP launch lacks a bound Skill snapshot"
                    )
                route = self._prepare_route(
                    index,
                    dependency,
                    skill_record,
                    sandbox=sandbox,
                    sandbox_executable=executable,
                )
                self._routes.append(route)
                self.commands[dependency["name"]] = list(route["command"])
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        for route in self._routes:
            _terminate_recorded_process(route["pid_path"], route["nonce"])
        self._routes = []
        self.commands = {}
        if self.root is not None:
            _make_tree_removable(self.root)
            shutil.rmtree(self.root, ignore_errors=True)
            self.root = None

    def _prepare_route(
        self,
        index: int,
        dependency: Dict,
        skill_record: Dict,
        *,
        sandbox: str,
        sandbox_executable: str,
    ) -> Dict:
        if self.root is None:
            raise ValidationError("selected Skill stdio MCP launch is not initialized")
        route_root = self.root / ("route-%02d" % index)
        snapshot = route_root / "skill"
        runtime = route_root / "runtime"
        route_root.mkdir(mode=0o700)
        snapshot.mkdir(mode=0o700)
        runtime.mkdir(mode=0o700)
        (runtime / "home").mkdir(mode=0o700)
        (runtime / "tmp").mkdir(mode=0o700)
        files = _snapshot_skill(
            self.workspace,
            dependency,
            skill_record,
            snapshot,
        )
        nonce = secrets.token_hex(32)
        receipt_path = route_root / "receipt.json"
        pid_path = route_root / "process.json"
        receipt = {
            "schema": SKILL_MCP_STDIO_RECEIPT_SCHEMA,
            "name": dependency["name"],
            "sandbox": sandbox,
            "sandbox_executable": sandbox_executable,
            "workspace": str(self.workspace),
            "snapshot": str(snapshot),
            "runtime": str(runtime),
            "script": dependency["script"],
            "args": dependency["args"],
            "files": files,
            "snapshot_sha256": _records_sha256(files),
            "pid_path": str(pid_path),
            "nonce": nonce,
        }
        payload = _canonical_json(receipt)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        write_new_text_file_no_follow(
            receipt_path,
            "selected Skill stdio MCP launch receipt",
            payload,
            mode=0o600,
            sync=True,
        )
        command = runtime_self_command() + [
            "_skill-mcp-stdio-proxy",
            str(receipt_path),
            digest,
        ]
        validate_command_allowlist(
            [{"argv": command}],
            "selected Skill stdio MCP proxy command",
        )
        return {
            "command": command,
            "pid_path": pid_path,
            "nonce": nonce,
        }


def proxy_main(argv: List[str]) -> int:
    try:
        if len(argv) != 2:
            raise ValidationError("selected Skill stdio MCP proxy arguments are invalid")
        receipt = _load_receipt(Path(argv[0]), argv[1])
        try:
            Path(argv[0]).unlink()
        except OSError as exc:
            raise ValidationError(
                "selected Skill stdio MCP receipt could not be consumed"
            ) from exc
        return _supervise_sandboxed_server(receipt)
    except (OSError, ValueError, ValidationError):
        sys.stderr.write("Conductor rejected the selected Skill stdio MCP launch.\n")
        sys.stderr.flush()
        return 126


def _snapshot_skill(
    workspace: Path,
    dependency: Dict,
    skill_record: Dict,
    destination: Path,
) -> List[Dict]:
    source_root = workspace / dependency["skill"]
    records = [
        {
            "path": "SKILL.md",
            "sha256": skill_record.get("sha256"),
            "bytes": skill_record.get("bytes"),
            "mode": skill_record.get("mode"),
        }
    ] + list(skill_record.get("resources", []))
    if not records or len(records) > MAX_SKILL_MCP_STDIO_FILES:
        raise ValidationError("selected Skill stdio MCP snapshot file count is invalid")
    copied = []
    total_bytes = 0
    for record in records:
        path = record.get("path")
        _validate_relative_resource_path(path)
        expected_size = record.get("bytes")
        expected_hash = record.get("sha256")
        source_mode = record.get("mode")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or expected_size > MAX_SKILL_MCP_STDIO_FILE_BYTES
            or not _is_sha256(expected_hash)
            or isinstance(source_mode, bool)
            or not isinstance(source_mode, int)
            or source_mode < 0
            or source_mode > 0o777
        ):
            raise ValidationError("selected Skill stdio MCP binding record is invalid")
        if path == dependency["script"] and not source_mode & 0o111:
            raise ValidationError(
                "selected Skill stdio MCP script binding is not executable"
            )
        raw = read_regular_file_bytes_no_follow(
            source_root / path,
            "selected Skill stdio MCP source resource",
            max_bytes=MAX_SKILL_MCP_STDIO_FILE_BYTES,
        )
        if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_hash:
            raise ValidationError(
                "selected Skill changed while preparing its stdio MCP snapshot"
            )
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        mode = 0o555 if path == dependency["script"] else 0o444
        _write_new_bytes(target, raw, mode)
        copied.append(
            {
                "path": path,
                "sha256": expected_hash,
                "bytes": expected_size,
                "mode": mode,
            }
        )
        total_bytes += expected_size
    if total_bytes > MAX_SKILL_MCP_STDIO_TOTAL_BYTES:
        raise ValidationError("selected Skill stdio MCP snapshot exceeds its byte limit")
    scripts = [record for record in copied if record["path"] == dependency["script"]]
    if len(scripts) != 1 or scripts[0]["bytes"] < 1:
        raise ValidationError("selected Skill stdio MCP script is not a non-empty resource")
    for directory, children, _ in os.walk(destination, topdown=False):
        for child in children:
            os.chmod(Path(directory) / child, 0o555)
    os.chmod(destination, 0o555)
    return copied


def _load_receipt(path: Path, expected_sha256: str) -> Dict:
    if not path.is_absolute() or not _is_sha256(expected_sha256):
        raise ValidationError("selected Skill stdio MCP receipt identity is invalid")
    reject_symlink_path(path, "selected Skill stdio MCP receipt")
    info = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
    ):
        raise ValidationError("selected Skill stdio MCP receipt mode is invalid")
    text = read_regular_text_file_no_follow(
        path,
        "selected Skill stdio MCP receipt",
        MAX_SKILL_MCP_STDIO_RECEIPT_BYTES,
    )
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected_sha256:
        raise ValidationError("selected Skill stdio MCP receipt hash changed")
    try:
        receipt = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError("selected Skill stdio MCP receipt is invalid JSON") from exc
    _validate_receipt(receipt, path)
    _verify_snapshot(receipt)
    return receipt


def _validate_receipt(receipt: Dict, path: Path) -> None:
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ValidationError("selected Skill stdio MCP receipt fields are invalid")
    if receipt.get("schema") != SKILL_MCP_STDIO_RECEIPT_SCHEMA:
        raise ValidationError("selected Skill stdio MCP receipt schema is invalid")
    if not isinstance(receipt.get("name"), str) or not SAFE_MCP_NAME.fullmatch(
        receipt["name"]
    ):
        raise ValidationError("selected Skill stdio MCP receipt name is invalid")
    sandbox, executable = _supported_sandbox()
    if receipt.get("sandbox") != sandbox or receipt.get("sandbox_executable") != executable:
        raise ValidationError("selected Skill stdio MCP sandbox binding changed")
    route_root = path.parent.resolve()
    if stat.S_IMODE(route_root.stat().st_mode) != 0o700:
        raise ValidationError("selected Skill stdio MCP route directory mode is invalid")
    snapshot = Path(receipt.get("snapshot", ""))
    runtime = Path(receipt.get("runtime", ""))
    pid_path = Path(receipt.get("pid_path", ""))
    workspace = Path(receipt.get("workspace", ""))
    if (
        not snapshot.is_absolute()
        or snapshot != route_root / "skill"
        or runtime != route_root / "runtime"
        or pid_path != route_root / "process.json"
        or not workspace.is_absolute()
    ):
        raise ValidationError("selected Skill stdio MCP receipt paths are invalid")
    _reject_workspace_under_system_roots(workspace.resolve(), sandbox)
    _validate_relative_resource_path(receipt.get("script"))
    if not receipt["script"].startswith("scripts/"):
        raise ValidationError("selected Skill stdio MCP script path is invalid")
    validate_command_allowlist(
        [{"argv": [receipt["script"]] + receipt.get("args", [])}],
        "selected Skill stdio MCP receipt command",
    )
    nonce = receipt.get("nonce")
    if (
        not isinstance(nonce, str)
        or len(nonce) != 64
        or any(char not in "0123456789abcdef" for char in nonce)
    ):
        raise ValidationError("selected Skill stdio MCP receipt nonce is invalid")
    files = receipt.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_SKILL_MCP_STDIO_FILES:
        raise ValidationError("selected Skill stdio MCP receipt file list is invalid")
    if _records_sha256(files) != receipt.get("snapshot_sha256"):
        raise ValidationError("selected Skill stdio MCP snapshot identity changed")


def _verify_snapshot(receipt: Dict) -> None:
    snapshot = Path(receipt["snapshot"])
    runtime = Path(receipt["runtime"])
    if (
        snapshot.is_symlink()
        or not snapshot.is_dir()
        or stat.S_IMODE(snapshot.stat().st_mode) != 0o555
        or runtime.is_symlink()
        or not runtime.is_dir()
        or stat.S_IMODE(runtime.stat().st_mode) != 0o700
    ):
        raise ValidationError("selected Skill stdio MCP snapshot directories are invalid")
    expected = {}
    total_bytes = 0
    for record in receipt["files"]:
        if not isinstance(record, dict) or set(record) != _FILE_FIELDS:
            raise ValidationError("selected Skill stdio MCP snapshot record is invalid")
        path = record.get("path")
        _validate_relative_resource_path(path)
        if path in expected:
            raise ValidationError("selected Skill stdio MCP snapshot path is duplicated")
        size = record.get("bytes")
        mode = record.get("mode")
        digest = record.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > MAX_SKILL_MCP_STDIO_FILE_BYTES
            or mode not in {0o444, 0o555}
            or not _is_sha256(digest)
        ):
            raise ValidationError("selected Skill stdio MCP snapshot record is invalid")
        expected[path] = record
        total_bytes += size
    if total_bytes > MAX_SKILL_MCP_STDIO_TOTAL_BYTES:
        raise ValidationError("selected Skill stdio MCP snapshot exceeds its byte limit")
    script = expected.get(receipt["script"])
    if script is None or script["bytes"] < 1 or script["mode"] != 0o555:
        raise ValidationError("selected Skill stdio MCP snapshot script is invalid")
    observed = set()
    for directory, children, files in os.walk(snapshot, followlinks=False):
        directory_path = Path(directory)
        if directory_path.is_symlink() or stat.S_IMODE(directory_path.stat().st_mode) != 0o555:
            raise ValidationError("selected Skill stdio MCP snapshot directory changed")
        for child in children:
            child_path = directory_path / child
            if child_path.is_symlink():
                raise ValidationError("selected Skill stdio MCP snapshot contains a symlink")
        for filename in files:
            file_path = directory_path / filename
            relative = file_path.relative_to(snapshot).as_posix()
            record = expected.get(relative)
            if record is None or file_path.is_symlink():
                raise ValidationError("selected Skill stdio MCP snapshot contains an extra file")
            raw = read_regular_file_bytes_no_follow(
                file_path,
                "selected Skill stdio MCP snapshot resource",
                MAX_SKILL_MCP_STDIO_FILE_BYTES,
            )
            info = file_path.stat(follow_symlinks=False)
            if (
                len(raw) != record["bytes"]
                or hashlib.sha256(raw).hexdigest() != record["sha256"]
                or stat.S_IMODE(info.st_mode) != record["mode"]
            ):
                raise ValidationError("selected Skill stdio MCP snapshot resource changed")
            observed.add(relative)
    if observed != set(expected):
        raise ValidationError("selected Skill stdio MCP snapshot is incomplete")
    for name in ("home", "tmp"):
        child = runtime / name
        if child.is_symlink() or not child.is_dir() or stat.S_IMODE(child.stat().st_mode) != 0o700:
            raise ValidationError("selected Skill stdio MCP runtime directory is invalid")


def _supervise_sandboxed_server(receipt: Dict) -> int:
    command, environment, cwd = _sandbox_command(receipt)
    handled_signals = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
    except BaseException:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        raise
    pid_path = Path(receipt["pid_path"])
    process_record = {
        "schema": SKILL_MCP_STDIO_PROCESS_SCHEMA,
        "pid": process.pid,
        "pgid": process.pid,
        "nonce": receipt["nonce"],
    }
    try:
        write_new_text_file_no_follow(
            pid_path,
            "selected Skill stdio MCP process record",
            _canonical_json(process_record),
            mode=0o600,
            sync=True,
        )
    except BaseException:
        _terminate_process_group(process.pid)
        process.wait()
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        raise

    previous_handlers = {}

    def stop_child(signum, frame) -> None:
        _terminate_process_group(process.pid)

    for signum in handled_signals:
        previous_handlers[signum] = signal.signal(signum, stop_child)
    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    try:
        returncode = process.wait()
    finally:
        if process.poll() is None:
            _terminate_process_group(process.pid)
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
    return returncode if returncode >= 0 else 128 + abs(returncode)


def _sandbox_command(receipt: Dict):
    snapshot = receipt["snapshot"]
    runtime = receipt["runtime"]
    script = receipt["script"]
    args = receipt["args"]
    if receipt["sandbox"] == "macos-seatbelt":
        command = [
            receipt["sandbox_executable"],
            "-D",
            "SKILL=%s" % snapshot,
            "-D",
            "RUNTIME=%s" % runtime,
            "-D",
            "USER_HOME=%s" % Path.home().resolve(),
            "-D",
            "WORKSPACE=%s" % receipt["workspace"],
            "-p",
            _MACOS_PROFILE,
            str(Path(snapshot) / script),
            *args,
        ]
        visible_snapshot = snapshot
        visible_runtime = runtime
        cwd = snapshot
    else:
        command = [
            receipt["sandbox_executable"],
            "--die-with-parent",
            "--unshare-net",
            "--unshare-pid",
            "--new-session",
        ]
        for root in _SYSTEM_BIND_ROOTS_LINUX:
            if Path(root).is_dir() and not Path(root).is_symlink():
                command.extend(["--ro-bind", root, root])
            elif Path(root).is_symlink():
                target = os.readlink(root)
                command.extend(["--symlink", target, root])
        command.extend(
            [
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--tmpfs",
                "/home",
                "--tmpfs",
                "/root",
                "--dir",
                "/skill",
                "--ro-bind",
                snapshot,
                "/skill",
                "--dir",
                "/runtime",
                "--bind",
                runtime,
                "/runtime",
                "--chdir",
                "/skill",
                "--",
                "/skill/%s" % script,
                *args,
            ]
        )
        visible_snapshot = "/skill"
        visible_runtime = "/runtime"
        cwd = snapshot
    environment = {
        "HOME": "%s/home" % visible_runtime,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": "%s/tmp" % visible_runtime,
        "CONDUCTOR_SKILL_ROOT": visible_snapshot,
    }
    return command, environment, cwd


def _supported_sandbox():
    system = platform.system()
    if system == "Darwin":
        executable = Path("/usr/bin/sandbox-exec")
        if not executable.is_file():
            raise ValidationError(
                "selected Skill stdio MCP requires /usr/bin/sandbox-exec on macOS"
            )
        return "macos-seatbelt", str(executable)
    if system == "Linux":
        candidate = shutil.which("bwrap", path="/usr/bin:/bin:/usr/local/bin")
        if candidate is None:
            raise ValidationError("selected Skill stdio MCP requires bubblewrap on Linux")
        executable = str(Path(candidate).resolve())
        if not any(
            executable == root or executable.startswith(root + "/")
            for root in ("/usr/bin", "/bin", "/usr/local/bin")
        ):
            raise ValidationError("selected Skill stdio MCP bubblewrap path is invalid")
        return "linux-bwrap", executable
    raise ValidationError(
        "selected Skill stdio MCP is not supported on %s" % (system or "this host")
    )


def _reject_workspace_under_system_roots(workspace: Path, sandbox: str) -> None:
    roots = _SYSTEM_READ_ROOTS_MACOS if sandbox == "macos-seatbelt" else _SYSTEM_BIND_ROOTS_LINUX
    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            continue
        resolved = root_path.resolve()
        try:
            workspace.relative_to(resolved)
        except ValueError:
            continue
        raise ValidationError(
            "selected Skill stdio MCP cannot isolate a workspace under a required system runtime root"
        )


def _terminate_recorded_process(path: Path, nonce: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    try:
        record = json.loads(
            read_regular_text_file_no_follow(
                path,
                "selected Skill stdio MCP process record",
                4096,
            )
        )
        if (
            not isinstance(record, dict)
            or set(record) != _PROCESS_FIELDS
            or record.get("schema") != SKILL_MCP_STDIO_PROCESS_SCHEMA
            or record.get("nonce") != nonce
            or isinstance(record.get("pid"), bool)
            or not isinstance(record.get("pid"), int)
            or record["pid"] < 2
            or record.get("pgid") != record["pid"]
        ):
            return
        pid = record["pid"]
        if pid == os.getpid() or pid == os.getpgrp() or os.getpgid(pid) != pid:
            return
        _terminate_process_group(pid)
    except (FileNotFoundError, OSError, ValueError, ValidationError):
        return


def _terminate_process_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        return
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except OSError:
            return
        time.sleep(0.02)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _write_new_bytes(path: Path, payload: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written < 1:
                raise OSError("short write")
            offset += written
        os.fsync(fd)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _make_tree_removable(root: Path) -> None:
    if root.is_symlink() or not root.exists():
        return
    for directory, children, _ in os.walk(root, topdown=False):
        for child in children:
            child_path = Path(directory) / child
            if child_path.is_dir() and not child_path.is_symlink():
                try:
                    os.chmod(child_path, 0o700)
                except OSError:
                    pass
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass


def _validate_relative_resource_path(value) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValidationError("selected Skill stdio MCP resource path is invalid")


def _records_sha256(records: List[Dict]) -> str:
    return hashlib.sha256(
        json.dumps(records, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()


def _canonical_json(value: Dict) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"


def _is_sha256(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )
