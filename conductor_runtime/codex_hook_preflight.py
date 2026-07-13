import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Tuple

from .errors import ValidationError


RESTRICTED_CODEX_MINIMUM_VERSION = (0, 143, 0)
RESTRICTED_CODEX_MAXIMUM_VERSION_EXCLUSIVE = (0, 145, 0)
MAX_HOOKS = 256
MAX_SKILLS = 1024
MAX_SKILL_ERRORS = 256
MAX_HOOK_PREFLIGHT_BYTES = 2 * 1024 * 1024
MAX_HOOK_STATE_CONFIG_BYTES = 128 * 1024
HOOK_PREFLIGHT_TIMEOUT_SECONDS = 8
SESSION_HOOK_KEY = "/<session-flags>/config.toml:pre_tool_use:0:0"
_VERSION_PATTERN = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def restricted_codex_version_range() -> str:
    minimum = ".".join(map(str, RESTRICTED_CODEX_MINIMUM_VERSION))
    maximum = ".".join(map(str, RESTRICTED_CODEX_MAXIMUM_VERSION_EXCLUSIVE))
    return ">=%s,<%s" % (minimum, maximum)


def prepare_restricted_hook_state(
    *,
    workspace: Path,
    hook_command: str,
    discovery_args: List[str],
) -> Dict:
    return prepare_pre_tool_hook_state(
        workspace=workspace,
        hook_command=hook_command,
        discovery_args=discovery_args,
        status_message="Enforcing Conductor command policy",
        timeout_seconds=5,
    )


def prepare_pre_tool_hook_state(
    *,
    workspace: Path,
    hook_command: str,
    discovery_args: List[str],
    status_message: str,
    timeout_seconds: int = 5,
) -> Dict:
    if (
        not isinstance(status_message, str)
        or not status_message
        or len(status_message) > 200
        or any(ord(char) < 32 for char in status_message)
    ):
        raise ValidationError("Codex pre-tool hook status message is invalid")
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 1 <= timeout_seconds <= 3600
    ):
        raise ValidationError("Codex pre-tool hook timeout is invalid")
    codex_path, codex_version = restricted_codex_executable(workspace)
    initial = _discover_hooks(codex_path, workspace, discovery_args)
    own = _own_session_hook(
        initial,
        hook_command,
        status_message,
        timeout_seconds,
    )
    states = {}
    for hook in initial:
        key = _hook_key(hook)
        if key == SESSION_HOOK_KEY:
            states[key] = {
                "enabled": True,
                "trusted_hash": own["currentHash"],
            }
        elif hook.get("isManaged") is not True:
            states[key] = {"enabled": False}
    state_arg = hooks_state_config_arg(states)
    verified = _discover_hooks(codex_path, workspace, discovery_args + ["--config", state_arg])
    verified_own = _own_session_hook(
        verified,
        hook_command,
        status_message,
        timeout_seconds,
    )
    if verified_own.get("trustStatus") != "trusted" or verified_own.get("enabled") is not True:
        raise ValidationError("restricted Codex session hook did not become exclusively trusted")
    if verified_own.get("currentHash") != own.get("currentHash"):
        raise ValidationError("restricted Codex session hook hash changed during preflight")
    for hook in verified:
        if hook.get("isManaged") is not True and _hook_key(hook) != SESSION_HOOK_KEY:
            if hook.get("enabled") is not False:
                raise ValidationError("restricted Codex preflight left an external unmanaged hook enabled")
    return {
        "codex_path": codex_path,
        "codex_version": codex_version,
        "hook_hash": own["currentHash"],
        "hook_state_config": state_arg,
        "disabled_unmanaged_hooks": sum(
            1 for hook in verified if hook.get("isManaged") is not True and _hook_key(hook) != SESSION_HOOK_KEY
        ),
        "managed_hooks": sum(1 for hook in verified if hook.get("isManaged") is True),
    }


def restricted_codex_executable(workspace: Path) -> Tuple[str, str]:
    candidate = shutil.which("codex")
    if not candidate:
        raise ValidationError("restricted agent profile requires Codex CLI on PATH")
    resolved = Path(candidate).resolve()
    _reject_path_inside_workspace(resolved, workspace, "Codex executable")
    try:
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise ValidationError("restricted Codex executable is unavailable") from exc
    if not stat.S_ISREG(mode) or not os.access(str(resolved), os.X_OK):
        raise ValidationError("restricted Codex executable must resolve to an executable regular file")
    try:
        result = subprocess.run(
            [str(resolved), "--version"],
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError("restricted Codex version probe failed") from exc
    output = (result.stdout.strip() or result.stderr.strip())[:300]
    match = _VERSION_PATTERN.search(output)
    if result.returncode != 0 or match is None:
        raise ValidationError("restricted Codex version probe returned invalid output")
    version = tuple(int(part) for part in match.groups())
    if not (
        RESTRICTED_CODEX_MINIMUM_VERSION
        <= version
        < RESTRICTED_CODEX_MAXIMUM_VERSION_EXCLUSIVE
    ):
        raise ValidationError(
            "restricted agent profiles require Codex CLI >= %s and < %s; found %s"
            % (
                ".".join(map(str, RESTRICTED_CODEX_MINIMUM_VERSION)),
                ".".join(map(str, RESTRICTED_CODEX_MAXIMUM_VERSION_EXCLUSIVE)),
                ".".join(map(str, version)),
            )
        )
    return str(resolved), output


def restricted_hook_python(workspace: Path) -> str:
    candidates = [Path("/usr/bin/python3"), Path(sys.executable)]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            _reject_path_inside_workspace(resolved, workspace, "hook Python executable")
            mode = resolved.stat().st_mode
        except (OSError, ValidationError):
            continue
        if stat.S_ISREG(mode) and os.access(str(resolved), os.X_OK):
            return str(resolved)
    raise ValidationError("restricted agent profile requires a Python 3 executable outside the workspace")


def hooks_state_config_arg(states: Dict[str, Dict]) -> str:
    if not isinstance(states, dict) or not states or len(states) > MAX_HOOKS:
        raise ValidationError("restricted hook state map is invalid")
    entries = []
    for key in sorted(states):
        state = states[key]
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 4096
            or not isinstance(state, dict)
        ):
            raise ValidationError("restricted hook state entry is invalid")
        if state.get("enabled") is False and set(state) == {"enabled"}:
            value = "{enabled=false}"
        elif (
            state.get("enabled") is True
            and set(state) == {"enabled", "trusted_hash"}
            and isinstance(state.get("trusted_hash"), str)
            and _SHA256.match(state["trusted_hash"])
        ):
            value = "{enabled=true,trusted_hash=%s}" % json.dumps(state["trusted_hash"])
        else:
            raise ValidationError("restricted hook state value is invalid")
        entries.append("%s=%s" % (json.dumps(key), value))
    rendered = "hooks.state={%s}" % ",".join(entries)
    if len(rendered.encode("utf-8")) > MAX_HOOK_STATE_CONFIG_BYTES:
        raise ValidationError("restricted hook state config is too large")
    return rendered


def _discover_hooks(codex_path: str, workspace: Path, args: List[str]) -> List[Dict]:
    result = _app_server_request(
        codex_path,
        workspace,
        args,
        method="hooks/list",
        params={"cwds": [str(workspace.resolve())]},
        label="restricted Codex hook",
    )
    if not isinstance(result, dict) or set(result) != {"data"}:
        raise ValidationError("restricted Codex hooks/list response is invalid")
    data = result.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValidationError("restricted Codex hooks/list response has invalid workspace data")
    entry = data[0]
    if entry.get("errors") not in ([], None):
        raise ValidationError("restricted Codex hook discovery reported an error")
    hooks = entry.get("hooks")
    if not isinstance(hooks, list) or len(hooks) > MAX_HOOKS or not all(isinstance(item, dict) for item in hooks):
        raise ValidationError("restricted Codex hook list is invalid")
    return hooks


def discover_codex_skill_metadata(workspace: Path) -> Dict:
    codex_path, codex_version = restricted_codex_executable(workspace)
    result = _app_server_request(
        codex_path,
        workspace,
        ["--disable", "skill_mcp_dependency_install"],
        method="skills/list",
        params={
            "cwds": [str(workspace.resolve())],
            "forceReload": True,
        },
        label="Codex Skill dependency",
    )
    if not isinstance(result, dict) or set(result) != {"data"}:
        raise ValidationError("Codex skills/list response is invalid")
    data = result.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValidationError("Codex skills/list response has invalid workspace data")
    entry = data[0]
    if entry.get("cwd") != str(workspace.resolve()):
        raise ValidationError("Codex skills/list response changed workspace")
    skills = entry.get("skills")
    errors = entry.get("errors")
    if (
        not isinstance(skills, list)
        or len(skills) > MAX_SKILLS
        or not all(isinstance(item, dict) for item in skills)
    ):
        raise ValidationError("Codex skills/list returned an invalid Skill list")
    if (
        not isinstance(errors, list)
        or len(errors) > MAX_SKILL_ERRORS
        or not all(isinstance(item, dict) for item in errors)
    ):
        raise ValidationError("Codex skills/list returned invalid errors")
    return {
        "codex_path": codex_path,
        "codex_version": codex_version,
        "skills": skills,
        "errors": errors,
    }


def _app_server_request(
    codex_path: str,
    workspace: Path,
    args: List[str],
    *,
    method: str,
    params: Dict,
    label: str,
) -> Dict:
    command = [codex_path, "app-server"] + list(args) + ["--listen", "stdio://"]
    try:
        process = subprocess.Popen(
            command,
            cwd=str(workspace),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise ValidationError("%s discovery could not start" % label) from exc
    messages = queue.Queue()
    byte_count = [0]

    def read_stream(name, stream):
        try:
            for line in iter(stream.readline, ""):
                encoded = line.encode("utf-8", errors="replace")
                byte_count[0] += len(encoded)
                if byte_count[0] > MAX_HOOK_PREFLIGHT_BYTES:
                    messages.put(("error", "%s discovery output exceeded its limit" % label))
                    return
                messages.put((name, line))
        finally:
            messages.put((name, None))

    stdout_thread = threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True)
    stderr_thread = threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    request = "\n".join(
        [
            json.dumps(
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {"name": "codex-conductor", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                },
                separators=(",", ":"),
            ),
            json.dumps({"method": "initialized", "params": {}}, separators=(",", ":")),
            json.dumps(
                {
                    "method": method,
                    "id": 2,
                    "params": params,
                },
                separators=(",", ":"),
            ),
        ]
    ) + "\n"
    result = None
    stderr_lines = []
    closed_streams = set()
    deadline = time.monotonic() + HOOK_PREFLIGHT_TIMEOUT_SECONDS
    try:
        if process.stdin is None:
            raise ValidationError("%s discovery has no input stream" % label)
        process.stdin.write(request)
        process.stdin.flush()
        while time.monotonic() < deadline:
            try:
                name, line = messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if name == "error":
                raise ValidationError(str(line))
            if line is None:
                closed_streams.add(name)
                if {"stdout", "stderr"} <= closed_streams:
                    break
                continue
            if name == "stderr" and line is not None:
                stderr_lines.append(line)
                continue
            if name != "stdout" or line is None:
                continue
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                continue
            if isinstance(message, dict) and message.get("id") == 2:
                if "error" in message:
                    raise ValidationError("%s %s request failed" % (label, method))
                result = message.get("result")
                break
        if result is None:
            detail = "".join(stderr_lines).strip()[:300]
            suffix = ": %s" % detail if detail else ""
            raise ValidationError("%s discovery timed out%s" % (label, suffix))
    finally:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
    return result


def _own_session_hook(
    hooks: List[Dict],
    hook_command: str,
    status_message: str = "Enforcing Conductor command policy",
    timeout_seconds: int = 5,
) -> Dict:
    matches = [hook for hook in hooks if hook.get("key") == SESSION_HOOK_KEY]
    if len(matches) != 1:
        raise ValidationError("restricted Codex preflight did not find exactly one session hook")
    hook = matches[0]
    expected = {
        "eventName": "preToolUse",
        "handlerType": "command",
        "matcher": ".*",
        "command": hook_command,
        "timeoutSec": timeout_seconds,
        "statusMessage": status_message,
        "source": "sessionFlags",
        "isManaged": False,
    }
    for key, value in expected.items():
        if hook.get(key) != value:
            raise ValidationError("restricted Codex session hook metadata is invalid")
    if not isinstance(hook.get("currentHash"), str) or not _SHA256.match(hook["currentHash"]):
        raise ValidationError("restricted Codex session hook hash is invalid")
    return hook


def _hook_key(hook: Dict) -> str:
    key = hook.get("key")
    if not isinstance(key, str) or not key or len(key) > 4096:
        raise ValidationError("restricted Codex hook key is invalid")
    return key


def _reject_path_inside_workspace(path: Path, workspace: Path, label: str) -> None:
    try:
        path.relative_to(workspace.resolve())
    except ValueError:
        return
    raise ValidationError("restricted %s cannot be inside the workspace" % label)
