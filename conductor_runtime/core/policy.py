"""Small explicit capability policy for the default runtime."""

import hashlib
import json
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import AbstractSet, Iterable, List, Mapping, Optional, Set

from ..errors import PolicyError, ValidationError


RISK_LEVELS = {"low", "medium", "high"}
SHELL_METACHARS = set(";&|`$><\n\r")
NETWORK_TOOLS = {"curl", "ftp", "nc", "ncat", "scp", "sftp", "ssh", "telnet", "wget"}
DESTRUCTIVE_TOOLS = {"dd", "mkfs", "rm", "rmdir", "shred"}
HIGH_SCALE_WORKERS = 16
MAX_SHELL_EXECUTABLE_BYTES = 512 * 1024 * 1024
CAPABILITY_FIELDS = (
    "allow_writes",
    "allow_destructive",
    "allow_network",
    "allow_agent",
    "allow_parallel",
)


@dataclass(frozen=True)
class RuntimePolicy:
    allow_writes: bool = False
    allow_destructive: bool = False
    allow_network: bool = False
    allow_agent: bool = False
    allow_parallel: bool = False
    approvals: AbstractSet[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for name in CAPABILITY_FIELDS:
            if type(getattr(self, name)) is not bool:
                raise ValidationError("runtime policy capability %s must be a boolean" % name)
        object.__setattr__(self, "approvals", frozenset(validate_approval_tokens(self.approvals)))

    def has_approval(self, value: str) -> bool:
        return value in self.approvals


@dataclass(frozen=True)
class ExecutableBinding:
    source_path: str
    identity_kind: str
    identity_path: str
    content_sha256: str
    workspace_relative: Optional[str]


@dataclass(frozen=True)
class CommandAssessment:
    argv: List[str]
    writes: bool
    destructive: bool
    network: bool
    executable: Optional[ExecutableBinding] = None


def validate_approval_tokens(tokens: Iterable[str]) -> Set[str]:
    values = set()
    for token in tokens:
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 256
            or any(char.isspace() for char in token)
        ):
            raise ValidationError("approval tokens must be bounded, non-empty, and contain no whitespace")
        values.add(token)
        if len(values) > 128:
            raise ValidationError("at most 128 approval tokens are supported")
    return values


def normalize_command(command) -> List[str]:
    if isinstance(command, list):
        if not command or not all(isinstance(part, str) and part and "\x00" not in part for part in command):
            raise ValidationError("shell command must be a non-empty argv array")
        return list(command)
    if isinstance(command, str):
        if "\x00" in command or any(char in SHELL_METACHARS for char in command):
            raise ValidationError("shell command strings cannot contain shell metacharacters")
        values = shlex.split(command)
        if not values:
            raise ValidationError("shell command must not be empty")
        return values
    raise ValidationError("shell command must be an argv array or simple string")


def assess_command(step: dict) -> CommandAssessment:
    argv = normalize_command(step.get("command"))
    executable = Path(argv[0]).name.lower()
    network = bool(step.get("network", False)) or executable in NETWORK_TOOLS
    destructive = bool(step.get("destructive", False)) or executable in DESTRUCTIVE_TOOLS
    writes = bool(step.get("writes", False)) or destructive
    if executable == "git" and len(argv) > 1:
        operation = _git_operation(argv)
        network = network or operation in {"clone", "fetch", "pull", "push"}
        writes = writes or operation in {
            "add",
            "am",
            "apply",
            "checkout",
            "cherry-pick",
            "clean",
            "commit",
            "merge",
            "mv",
            "pull",
            "rebase",
            "reset",
            "restore",
            "rm",
            "stash",
            "switch",
            "tag",
        }
        destructive = destructive or operation in {"clean", "reset", "restore"}
    return CommandAssessment(argv=argv, writes=writes, destructive=destructive, network=network)


def _git_operation(argv: List[str]) -> str:
    options_with_values = {
        "-C",
        "-c",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
    value_prefixes = tuple(value + "=" for value in options_with_values if value.startswith("--"))
    index = 1
    while index < len(argv):
        value = argv[index]
        if value in options_with_values:
            index += 2
            continue
        if value.startswith(value_prefixes) or (value.startswith("-c") and value != "-c"):
            index += 1
            continue
        if value == "--":
            return argv[index + 1] if index + 1 < len(argv) else ""
        if value.startswith("-"):
            index += 1
            continue
        return value
    return ""


def enforce_shell(
    step: dict,
    policy: RuntimePolicy,
    *,
    cwd: Optional[Path] = None,
    workspace: Optional[Path] = None,
    workspace_alias: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> CommandAssessment:
    requested = assess_command(step)
    _enforce_risk(step, policy)
    binding = _bind_shell_executable(
        requested.argv,
        cwd=Path.cwd() if cwd is None else cwd,
        workspace=workspace,
        workspace_alias=workspace_alias,
        environment=environment,
    )
    bound_argv = [binding.source_path, *requested.argv[1:]]
    resolved = assess_command({**step, "command": bound_argv})
    assessment = CommandAssessment(
        argv=bound_argv,
        writes=requested.writes or resolved.writes,
        destructive=requested.destructive or resolved.destructive,
        network=requested.network or resolved.network,
        executable=binding,
    )
    approval = _bound_shell_approval(binding, assessment.argv[1:])
    require_approval(policy, approval, "shell step %s" % step["id"])
    if assessment.writes and not policy.allow_writes:
        raise PolicyError("step %s may write and requires --allow-writes" % step["id"])
    if assessment.destructive and not policy.allow_destructive:
        raise PolicyError("step %s may be destructive and requires --allow-destructive" % step["id"])
    if assessment.network and not policy.allow_network:
        raise PolicyError("step %s may use network and requires --allow-network" % step["id"])
    return assessment


def shell_approval(
    argv: List[str],
    *,
    cwd: Optional[Path] = None,
    workspace: Optional[Path] = None,
    workspace_alias: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> str:
    """Return the approval for the canonical executable that would be launched."""
    normalized = normalize_command(argv)
    binding = _bind_shell_executable(
        normalized,
        cwd=Path.cwd() if cwd is None else cwd,
        workspace=workspace,
        workspace_alias=workspace_alias,
        environment=environment,
    )
    return _bound_shell_approval(binding, normalized[1:])


def resolve_shell_argv(
    argv: List[str],
    *,
    cwd: Path,
    workspace: Optional[Path] = None,
    workspace_alias: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """Resolve argv[0] to the canonical source executable for inspection."""
    normalized = normalize_command(argv)
    binding = _bind_shell_executable(
        normalized,
        cwd=cwd,
        workspace=workspace,
        workspace_alias=workspace_alias,
        environment=environment,
    )
    return [binding.source_path, *normalized[1:]]


def prepare_shell_launch(assessment: CommandAssessment, *, isolated_workspace: Path) -> List[str]:
    """Map an approved workspace executable into the isolated copy and recheck it."""
    binding = assessment.executable
    if binding is None:
        raise ValidationError("shell executable approval binding is missing")
    if binding.workspace_relative is None:
        launch_path = Path(binding.source_path)
    else:
        from .safe import resolve_under

        launch_path = resolve_under(
            isolated_workspace,
            binding.workspace_relative,
            "isolated shell executable",
        )
    try:
        canonical = launch_path.resolve(strict=True)
        observed_sha256 = _executable_sha256(launch_path)
    except (OSError, RuntimeError, ValidationError) as exc:
        raise ValidationError("shell executable changed after approval") from exc
    if binding.workspace_relative is None and str(canonical) != binding.source_path:
        raise ValidationError("shell executable changed after approval")
    if observed_sha256 != binding.content_sha256:
        raise ValidationError("shell executable changed after approval")
    return [str(canonical), *assessment.argv[1:]]


def _bind_shell_executable(
    argv: List[str],
    *,
    cwd: Path,
    workspace: Optional[Path],
    workspace_alias: Optional[Path],
    environment: Optional[Mapping[str, str]] = None,
) -> ExecutableBinding:
    """Resolve and fingerprint argv[0] once for approval and isolated launch."""
    from .process import sanitized_subprocess_environment

    requested = argv[0]
    requested_cwd = Path(cwd).expanduser()
    try:
        resolved_cwd = requested_cwd.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationError("shell cwd cannot be resolved") from exc
    if not resolved_cwd.is_dir():
        raise ValidationError("shell cwd must be a directory")
    workspace_root = _resolved_directory(workspace, "shell workspace") if workspace is not None else None
    alias_root = (
        _resolved_directory(workspace_alias, "shell workspace alias")
        if workspace_alias is not None
        else workspace_root
    )
    process_environment = sanitized_subprocess_environment(source=environment)
    resolution_command = _map_absolute_workspace_command(requested, workspace_root, alias_root)
    executable = _resolve_executable(resolution_command, resolved_cwd, process_environment)
    if workspace_root is not None and alias_root is not None and workspace_root != alias_root:
        try:
            alias_relative = executable.relative_to(alias_root)
        except ValueError:
            pass
        else:
            executable = _resolve_executable(
                str(_workspace_executable_path(workspace_root, alias_relative)),
                resolved_cwd,
                process_environment,
            )
    workspace_relative: Optional[str] = None
    if workspace_root is not None:
        try:
            relative = executable.relative_to(workspace_root)
        except ValueError:
            pass
        else:
            from .safe import require_relative

            workspace_relative = require_relative(relative.as_posix(), "workspace shell executable")
    return ExecutableBinding(
        source_path=str(executable),
        identity_kind="workspace" if workspace_relative is not None else "absolute",
        identity_path=workspace_relative if workspace_relative is not None else str(executable),
        content_sha256=_executable_sha256(executable),
        workspace_relative=workspace_relative,
    )


def _resolved_directory(path: Path, label: str) -> Path:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationError("%s cannot be resolved" % label) from exc
    if not resolved.is_dir():
        raise ValidationError("%s must be a directory" % label)
    return resolved


def _map_absolute_workspace_command(
    command: str,
    workspace: Optional[Path],
    workspace_alias: Optional[Path],
) -> str:
    candidate = Path(command)
    if not candidate.is_absolute() or workspace is None or workspace_alias is None:
        return command
    try:
        relative = candidate.resolve(strict=False).relative_to(workspace_alias)
    except (OSError, RuntimeError, ValueError):
        return command
    return str(_workspace_executable_path(workspace, relative))


def _workspace_executable_path(workspace: Path, relative: Path) -> Path:
    from .safe import resolve_under

    return resolve_under(workspace, relative.as_posix(), "workspace shell executable")


def _resolve_executable(command: str, cwd: Path, environment: Mapping[str, str]) -> Path:
    candidate = Path(command)
    if candidate.is_absolute() or os.path.dirname(command):
        base = candidate if candidate.is_absolute() else cwd / candidate
        executable = _first_executable_candidate(base, environment)
        if executable is not None:
            return executable
    else:
        for directory in _executable_search_directories(cwd, environment):
            executable = _first_executable_candidate(directory / command, environment)
            if executable is not None:
                return executable
    raise ValidationError("shell executable cannot be resolved: %s" % Path(command).name)


def _executable_search_directories(cwd: Path, environment: Mapping[str, str]) -> List[Path]:
    directories = [cwd] if os.name == "nt" else []
    for value in environment.get("PATH", os.defpath).split(os.pathsep):
        candidate = Path(value or os.curdir)
        directories.append(candidate if candidate.is_absolute() else cwd / candidate)
    unique = []
    seen = set()
    for directory in directories:
        key = os.path.normcase(os.path.abspath(str(directory)))
        if key not in seen:
            seen.add(key)
            unique.append(directory)
    return unique


def _first_executable_candidate(path: Path, environment: Mapping[str, str]) -> Optional[Path]:
    for candidate in _platform_executable_candidates(path, environment):
        try:
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            return candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
    return None


def _executable_sha256(path: Path) -> str:
    from .safe import read_regular_bytes, sha256_bytes

    if not os.access(path, os.X_OK):
        raise ValidationError("shell executable is not executable")
    return sha256_bytes(
        read_regular_bytes(
            path,
            "shell executable",
            MAX_SHELL_EXECUTABLE_BYTES,
        )
    )


def _platform_executable_candidates(path: Path, environment: Mapping[str, str]) -> List[Path]:
    if os.name != "nt":
        return [path]
    configured = environment.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    extensions = [value for value in configured.split(os.pathsep) if value]
    suffix = path.suffix.casefold()
    if suffix and any(suffix == value.casefold() for value in extensions):
        return [path]
    return [Path(str(path) + value) for value in extensions]


def _bound_shell_approval(binding: ExecutableBinding, arguments: List[str]) -> str:
    identity = {
        "arguments": arguments,
        "executable": {
            "kind": binding.identity_kind,
            "path": binding.identity_path,
            "sha256": binding.content_sha256,
        },
    }
    payload = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "shell-command-%s" % hashlib.sha256(payload).hexdigest()[:16]


def enforce_agent(step: dict, policy: RuntimePolicy, workers: int = 1) -> None:
    _enforce_risk(step, policy)
    if not policy.allow_agent:
        raise PolicyError("step %s invokes Codex and requires --allow-agent" % step["id"])
    if step.get("sandbox", "read-only") == "workspace-write" and not policy.allow_writes:
        raise PolicyError("step %s writes through Codex and requires --allow-writes" % step["id"])
    if workers > 1 and not policy.allow_parallel:
        raise PolicyError("step %s uses parallel workers and requires --allow-parallel" % step["id"])
    if workers > HIGH_SCALE_WORKERS and not policy.has_approval("high-scale-agent-map"):
        raise PolicyError("step %s requires --approve high-scale-agent-map" % step["id"])


def enforce_workflow_risk(workflow: dict, policy: RuntimePolicy) -> None:
    risk = workflow.get("risk", "low")
    if not isinstance(risk, str) or risk not in RISK_LEVELS:
        raise ValidationError("workflow has invalid risk")
    if risk == "high" and not policy.has_approval("high-risk"):
        raise PolicyError("high-risk workflow requires --approve high-risk")


def require_approval(policy: RuntimePolicy, value: str, label: str) -> None:
    if not policy.has_approval(value):
        raise PolicyError("%s requires --approve %s" % (label, value))


def _enforce_risk(step: dict, policy: RuntimePolicy) -> None:
    risk = step.get("risk", "low")
    if not isinstance(risk, str) or risk not in RISK_LEVELS:
        raise ValidationError("step %s has invalid risk" % step.get("id", "<unknown>"))
    if risk == "high" and not (
        policy.has_approval(step.get("id", "")) or policy.has_approval("high-risk")
    ):
        raise PolicyError("step %s requires explicit high-risk approval" % step.get("id", "<unknown>"))


def policy_fingerprint(policy: RuntimePolicy) -> str:
    values = {
        "allow_writes": policy.allow_writes,
        "allow_destructive": policy.allow_destructive,
        "allow_network": policy.allow_network,
        "allow_agent": policy.allow_agent,
        "allow_parallel": policy.allow_parallel,
        "approval_sha256": sorted(
            hashlib.sha256(value.encode("utf-8")).hexdigest() for value in policy.approvals
        ),
    }
    from .safe import canonical_json_bytes

    return hashlib.sha256(canonical_json_bytes(values)).hexdigest()
