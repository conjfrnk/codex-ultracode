from pathlib import Path
import os
import hashlib
import secrets
import shlex
import stat
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Set

from .agent_skill_mcp import (
    skill_mcp_approval_token,
    skill_mcp_auth_approval_token,
    skill_mcp_stdio_approval_token,
)
from .codex_config import validate_codex_native_agents
from .errors import PolicyError, ValidationError


SHELL_METACHARS = set("|&;<>()`$\\")
DESTRUCTIVE_EXECUTABLES = {
    "rm",
    "rmdir",
    "shred",
    "mkfs",
    "dd",
    "diskutil",
    "truncate",
}
NETWORK_EXECUTABLES = {
    "curl",
    "wget",
    "ssh",
    "scp",
    "rsync",
    "brew",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "pip",
    "pip3",
    "uv",
    "poetry",
    "cargo",
    "go",
    "gem",
    "bundle",
    "composer",
    "gh",
    "docker",
    "kubectl",
    "terraform",
}
NETWORK_WRITE_EXECUTABLES = {
    "brew",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "pip",
    "pip3",
    "uv",
    "poetry",
    "cargo",
    "go",
    "gem",
    "bundle",
    "composer",
    "gh",
    "docker",
    "kubectl",
    "terraform",
    "scp",
    "rsync",
    "wget",
}
WRITE_EXECUTABLES = {
    "mv",
    "cp",
    "install",
    "tee",
    "touch",
    "mkdir",
    "chmod",
    "chown",
}
SHELL_EXECUTABLES = {"sh", "bash", "zsh", "fish"}
INLINE_CODE_EXECUTABLES = {"python", "python3", "node", "perl", "ruby"}
SCRIPTABLE_EXECUTABLES = {"awk", "sed"}
READ_ONLY_EXECUTABLES = {
    "cat",
    "cut",
    "date",
    "echo",
    "false",
    "find",
    "grep",
    "git",
    "head",
    "ls",
    "nl",
    "printf",
    "pwd",
    "rg",
    "sleep",
    "sort",
    "tail",
    "test",
    "tr",
    "true",
    "uniq",
    "wc",
}
RISK_LEVELS = {"low", "medium", "high"}
GIT_WRITE_SUBCOMMANDS = {
    "add",
    "am",
    "apply",
    "branch",
    "checkout",
    "cherry-pick",
    "clean",
    "commit",
    "config",
    "init",
    "merge",
    "mv",
    "rebase",
    "reset",
    "restore",
    "revert",
    "rm",
    "stash",
    "switch",
    "tag",
    "update-index",
    "worktree",
}
GIT_DESTRUCTIVE_SUBCOMMANDS = {"checkout", "clean", "reset", "restore", "rm", "switch"}
GIT_NETWORK_SUBCOMMANDS = {"clone", "fetch", "pull", "push", "ls-remote", "submodule"}
GIT_NETWORK_WRITE_SUBCOMMANDS = {"clone", "fetch", "pull", "push", "submodule"}
CURL_MUTATING_METHODS = {"DELETE", "PATCH", "POST", "PUT"}
CURL_MUTATING_BODY_OPTIONS = {
    "-d",
    "--data",
    "--data-ascii",
    "--data-binary",
    "--data-raw",
    "--data-urlencode",
    "--form",
    "--form-string",
    "--json",
    "--upload-file",
    "-F",
    "-T",
}
FIND_EXEC_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir"}
SAFE_SYSTEM_SYMLINK_ANCESTORS = {Path("/tmp"), Path("/var")}
HIGH_SCALE_AGENT_WORKER_THRESHOLD = 16
NATIVE_AGENT_DELEGATION_APPROVAL = "native-agent-delegation"


@dataclass
class RuntimePolicy:
    allow_writes: bool = False
    allow_destructive: bool = False
    allow_network: bool = False
    allow_agent: bool = False
    allow_parallel: bool = False
    approvals: Set[str] = field(default_factory=set)
    approval_sha256: Set[str] = field(default_factory=set)

    def has_approval(self, token: str) -> bool:
        if token in self.approvals or "all" in self.approvals:
            return True
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return digest in self.approval_sha256


@dataclass
class CommandAssessment:
    argv: List[str]
    writes: bool = False
    destructive: bool = False
    network: bool = False
    external_path: bool = False


def normalize_command(command) -> List[str]:
    if isinstance(command, list):
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValidationError("shell command arrays must contain one or more non-empty strings")
        return command
    if isinstance(command, str):
        if any(char in SHELL_METACHARS for char in command):
            raise ValidationError(
                "shell command strings cannot contain shell metacharacters; use an argv array instead"
            )
        parts = shlex.split(command)
        if not parts:
            raise ValidationError("shell command strings must not be empty")
        return parts
    raise ValidationError("shell command must be an argv array or a simple string")


def _basename(executable: str) -> str:
    return executable.rsplit("/", 1)[-1]


def assess_command(command) -> CommandAssessment:
    argv = normalize_command(command)
    executable = _basename(argv[0])
    pathful_executable = _has_path_separator(argv[0])
    assessment = CommandAssessment(argv=argv)
    if pathful_executable or _has_external_path_argument(argv):
        assessment.external_path = True
    if pathful_executable:
        assessment.writes = True
        assessment.network = True
        assessment.destructive = True
    if executable in DESTRUCTIVE_EXECUTABLES:
        assessment.destructive = True
        assessment.writes = True
    if executable in WRITE_EXECUTABLES:
        assessment.writes = True
    if executable in NETWORK_EXECUTABLES:
        assessment.network = True
    if executable in NETWORK_WRITE_EXECUTABLES:
        assessment.writes = True
    if executable == "curl" and _curl_writes_output(argv[1:]):
        assessment.writes = True
    if executable == "curl" and _curl_mutates_remote(argv[1:]):
        assessment.writes = True
    if executable in SCRIPTABLE_EXECUTABLES:
        assessment.writes = True
        assessment.network = True
        assessment.destructive = True
    if (
        executable not in READ_ONLY_EXECUTABLES
        and executable not in WRITE_EXECUTABLES
        and executable not in NETWORK_EXECUTABLES
        and executable not in INLINE_CODE_EXECUTABLES
        and executable not in SCRIPTABLE_EXECUTABLES
    ):
        assessment.writes = True
        assessment.network = True
    if executable == "find" and "-delete" in argv[1:]:
        assessment.destructive = True
        assessment.writes = True
    if executable == "find" and any(arg in FIND_EXEC_ACTIONS for arg in argv[1:]):
        assessment.destructive = True
        assessment.writes = True
        assessment.network = True
    if executable == "find" and any(
        arg in {"-fprint", "-fprint0", "-fprintf"} or arg.startswith("-fprint")
        for arg in argv[1:]
    ):
        assessment.writes = True
    if executable == "sed" and any(arg == "-i" or arg.startswith("-i") for arg in argv[1:]):
        assessment.writes = True
    if executable == "sort" and _has_option_with_value(argv[1:], "-o", "--output"):
        assessment.writes = True
    if executable in SHELL_EXECUTABLES and any(arg in {"-c", "-lc"} for arg in argv[1:]):
        assessment.writes = True
        assessment.network = True
        assessment.destructive = True
    if executable in INLINE_CODE_EXECUTABLES:
        local_transaction = _python_conductor_local_transaction_subcommand(argv)
        cloud_subcommand = _python_conductor_cloud_subcommand(argv)
        if local_transaction:
            assessment.writes = True
            assessment.destructive = True
            assessment.network = False
        elif cloud_subcommand:
            assessment.network = True
            assessment.destructive = cloud_subcommand == "cloud-apply" or (
                cloud_subcommand in {
                    "run-claude-staged-task",
                    "run-codex-staged-task",
                    "run-codex-staged-repair",
                }
                and "--apply-verified-stage" in argv
            )
            assessment.writes = cloud_subcommand in {
                "cloud-submit",
                "cloud-review",
                "cloud-apply",
                "run-claude-readonly-task",
                "run-claude-staged-task",
                "run-codex-readonly-task",
                "run-codex-staged-task",
                "run-codex-staged-repair",
                "run-parity-campaign-trial",
                "run-auto-topology-arm",
                "run-readonly-parity-arm",
            }
        elif _is_safe_python_read(argv):
            if _uses_personal_saved_workflow_scope(argv):
                assessment.external_path = True
        elif _is_safe_python_local_write(argv):
            assessment.writes = True
            assessment.network = False
            assessment.destructive = False
            if _uses_personal_saved_workflow_scope(argv):
                assessment.external_path = True
        elif _is_python_unittest_discover(argv):
            assessment.writes = True
        else:
            assessment.writes = True
            assessment.network = True
            assessment.destructive = True
    if executable == "git" and len(argv) > 1:
        if _git_defines_shell_alias(argv):
            assessment.writes = True
            assessment.network = True
            assessment.destructive = True
        subcommand = _git_subcommand(argv)
        if subcommand in GIT_WRITE_SUBCOMMANDS:
            assessment.writes = True
        if subcommand in GIT_NETWORK_SUBCOMMANDS:
            assessment.network = True
        if subcommand in GIT_NETWORK_WRITE_SUBCOMMANDS:
            assessment.writes = True
        if subcommand in GIT_DESTRUCTIVE_SUBCOMMANDS:
            assessment.destructive = True
        if _has_option_with_value(argv[1:], "", "--output"):
            assessment.writes = True
    return assessment


def _git_subcommand(argv: List[str]) -> str:
    index = 1
    options_with_values = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return argv[index + 1] if index + 1 < len(argv) else ""
        if arg in options_with_values:
            index += 2
            continue
        if any(arg.startswith(option + "=") for option in options_with_values if option.startswith("--")):
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg
    return ""


def _git_defines_shell_alias(argv: List[str]) -> bool:
    index = 1
    while index < len(argv):
        arg = argv[index]
        value = ""
        if arg == "-c" and index + 1 < len(argv):
            value = argv[index + 1]
            index += 2
        elif arg.startswith("-c") and len(arg) > 2:
            value = arg[2:]
            index += 1
        else:
            index += 1
        lowered = value.lower()
        if lowered.startswith("alias.") and "=!" in lowered:
            return True
    return False


def _has_option_with_value(args: List[str], short_option: str, long_option: str) -> bool:
    for index, arg in enumerate(args):
        if arg == short_option or arg == long_option:
            return index + 1 < len(args)
        if short_option and arg.startswith(short_option) and arg != short_option:
            return True
        if long_option and arg.startswith(long_option + "="):
            return True
    return False


def _curl_writes_output(args: List[str]) -> bool:
    for arg in args:
        if arg in {"-O", "-OJ", "-JO", "--remote-name", "--remote-header-name"}:
            return True
        if arg.startswith("-O") and arg != "-":
            return True
    return _has_option_with_value(args, "-o", "--output") or _has_option_with_value(args, "", "--output-dir")


def _curl_mutates_remote(args: List[str]) -> bool:
    for index, arg in enumerate(args):
        method = ""
        if arg in {"-X", "--request"} and index + 1 < len(args):
            method = args[index + 1]
        elif arg.startswith("-X") and len(arg) > 2:
            method = arg[2:]
        elif arg.startswith("--request="):
            method = arg.split("=", 1)[1]
        if method.upper() in CURL_MUTATING_METHODS:
            return True
        if arg in CURL_MUTATING_BODY_OPTIONS:
            return True
        if any(
            arg.startswith(option + "=")
            for option in CURL_MUTATING_BODY_OPTIONS
            if option.startswith("--")
        ):
            return True
        if arg.startswith("-d") and arg != "-d":
            return True
        if arg.startswith("-F") and arg != "-F":
            return True
        if arg.startswith("-T") and arg != "-T":
            return True
    return False


def _is_safe_python_read(argv: List[str]) -> bool:
    executable = _basename(argv[0])
    if executable not in {"python", "python3"}:
        return False
    args = argv[1:]
    if "-c" in args:
        return False
    rest = _python_args_after_interpreter_flags(args)
    if rest[:2] == ["-m", "conductor_runtime"]:
        return _is_safe_conductor_runtime_read(rest[2:])
    if rest and rest[0] == "tools/run_local_audit.py":
        return True
    return False


def _python_conductor_cloud_subcommand(argv: List[str]) -> str:
    executable = _basename(argv[0])
    if executable not in {"python", "python3"} or "-c" in argv[1:]:
        return ""
    rest = _python_args_after_interpreter_flags(argv[1:])
    if rest[:2] != ["-m", "conductor_runtime"] or len(rest) < 3:
        return ""
    command = rest[2]
    if len(rest) == 4 and rest[3] in {"--help", "-h"}:
        return ""
    if command in {"run-parity-campaign-trial", "run-auto-topology-arm", "run-readonly-parity-arm"} and "--dry-run" in rest[3:]:
        return ""
    if command in {
        "cloud-submit",
        "cloud-status",
        "cloud-review",
        "cloud-apply",
        "run-claude-readonly-task",
        "run-claude-staged-task",
        "run-codex-readonly-task",
        "run-codex-staged-task",
        "run-codex-staged-repair",
        "run-parity-campaign-trial",
        "run-auto-topology-arm",
        "run-readonly-parity-arm",
    }:
        return command
    return ""


def _python_conductor_local_transaction_subcommand(argv: List[str]) -> str:
    executable = _basename(argv[0])
    if executable not in {"python", "python3"} or "-c" in argv[1:]:
        return ""
    rest = _python_args_after_interpreter_flags(argv[1:])
    if rest[:2] != ["-m", "conductor_runtime"] or len(rest) < 3:
        return ""
    if len(rest) == 4 and rest[3] in {"--help", "-h"}:
        return ""
    return rest[2] if rest[2] == "apply-verified-stage" else ""


def _is_safe_python_local_write(argv: List[str]) -> bool:
    executable = _basename(argv[0])
    if executable not in {"python", "python3"}:
        return False
    args = argv[1:]
    if "-c" in args:
        return False
    rest = _python_args_after_interpreter_flags(args)
    if (
        rest[:3] == ["-m", "conductor_runtime", "run-saved-workflow"]
        and "--dry-run" in rest[3:]
    ):
        return True
    return rest[:3] in [
        ["-m", "conductor_runtime", "export-saved-workflow"],
        ["-m", "conductor_runtime", "bind-agent-profile"],
        ["-m", "conductor_runtime", "import-session-trace"],
        ["-m", "conductor_runtime", "import-usage"],
        ["-m", "conductor_runtime", "prepare-run"],
        ["-m", "conductor_runtime", "queue-team-task"],
        ["-m", "conductor_runtime", "reply-team-question"],
        ["-m", "conductor_runtime", "review-team-plan"],
        ["-m", "conductor_runtime", "pause-routine"],
        ["-m", "conductor_runtime", "record-usage"],
        ["-m", "conductor_runtime", "recover-run"],
        ["-m", "conductor_runtime", "retry-packet"],
        ["-m", "conductor_runtime", "resume-routine"],
        ["-m", "conductor_runtime", "score-benchmark-report"],
        ["-m", "conductor_runtime", "update-routine-schedule"],
        ["-m", "conductor_runtime", "write-evidence-bundle"],
        ["-m", "conductor_runtime", "write-agent-profile"],
        ["-m", "conductor_runtime", "remember-agent-memory"],
        ["-m", "conductor_runtime", "forget-agent-memory"],
        ["-m", "conductor_runtime", "write-live-parity-runbook"],
        ["-m", "conductor_runtime", "write-parity-campaign"],
        ["-m", "conductor_runtime", "write-auto-topology-campaign"],
        ["-m", "conductor_runtime", "write-readonly-parity-campaign"],
        ["-m", "conductor_runtime", "write-run-manifest"],
        ["-m", "conductor_runtime", "write-routine-manifest"],
        ["-m", "conductor_runtime", "write-schema"],
    ]


def _is_safe_conductor_runtime_read(args: List[str]) -> bool:
    if not args:
        return False
    if args[0] in {"--help", "-h", "--version"}:
        return len(args) == 1
    if len(args) == 2 and args[1] in {"--help", "-h"}:
        return bool(args[0]) and all(
            char in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in args[0]
        )
    if args[0] in {"run-parity-campaign-trial", "run-auto-topology-arm", "run-readonly-parity-arm"}:
        return "--dry-run" in args[1:]
    if args[0] == "doctor" and "--repair-codex-host" in args[1:]:
        return False
    read_only_subcommands = {
        "validate",
        "validate-agent-profile",
        "validate-agent-memory",
        "validate-agent-team-state",
        "validate-agent-team-operator-inbox",
        "validate-agent-team-operator-chat",
        "validate-agent-team-transcript",
        "validate-agent-team-interruption",
        "validate-agent-team-plan",
        "validate-agent-team-plan-review",
        "validate-agent-team-plan-approval",
        "validate-agent-team-merge-ledger",
        "validate-agent-map-packet-terminal",
        "validate-codex-step-terminal",
        "validate-codex-progress",
        "validate-agent-team-turn-terminal",
        "validate-agent-team-turn-completion",
        "validate-agent-team-merge-intent",
        "validate-agent-team-merge-transaction",
        "validate-agent-team-merge-recovery",
        "validate-agent-team-quality-retry",
        "list-agent-memory",
        "inspect-agent-memory",
        "inspect-agent-team-state",
        "inspect-agent-team-merge-ledger",
        "inspect-agent-map-packet-terminal",
        "inspect-codex-step-terminal",
        "inspect-codex-progress",
        "inspect-agent-team-turn-terminal",
        "inspect-agent-team-turn-completion",
        "inspect-agent-team-merge-intent",
        "inspect-agent-team-merge-transaction",
        "inspect-agent-team-merge-recovery",
        "inspect-agent-team-quality-retry",
        "validate-saved-workflows",
        "validate-model-workflow-receipt",
        "validate-model-workflow-run-receipt",
        "validate-auto-receipt",
        "list-auto",
        "status-auto",
        "validate-background-run",
        "validate-desktop-notification",
        "validate-background-model-goal",
        "validate-background-routine-supervisor",
        "validate-benchmark-quality-evidence",
        "validate-benchmark-report",
        "validate-benchmark-score-input",
        "validate-claude-provider-evidence",
        "validate-claude-staged-evidence",
        "validate-codex-provider-evidence",
        "validate-codex-staged-evidence",
        "validate-codex-staged-repair",
        "validate-codex-staged-repair-state",
        "validate-staged-apply",
        "inspect",
        "inspect-agent-profile",
        "inspect-agent-team-operator-inbox",
        "inspect-agent-team-operator-chat",
        "inspect-agent-team-transcript",
        "inspect-agent-team-interruption",
        "inspect-agent-team-plan-approval",
        "status",
        "status-goal",
        "status-parity-campaign",
        "status-auto-topology-campaign",
        "status-readonly-parity-campaign",
        "status-background-run",
        "status-background-model-goal",
        "status-background-routine-supervisor",
        "status-routine",
        "list",
        "list-goals",
        "list-runs",
        "list-team-inbox",
        "list-team-questions",
        "read-team-question",
        "list-team-plans",
        "read-team-plan",
        "list-team-transcripts",
        "read-team-transcript",
        "team-console",
        "diagnose-run",
        "run-console",
        "list-saved-workflows",
        "inspect-saved-workflow",
        "doctor",
        "evidence-bundle",
        "plan-live-parity-runbook",
        "plan-parity-campaign",
        "plan-auto-topology-campaign",
        "plan-readonly-parity-campaign",
        "schema",
        "validate-comparison",
        "validate-evidence-bundle",
        "validate-live-parity-runbook",
        "validate-parity-campaign",
        "validate-auto-topology-campaign",
        "validate-readonly-parity-campaign",
        "validate-parity-tasks",
        "validate-run-manifest",
        "validate-routine-manifest",
        "validate-routine-controls",
        "validate-routine-state",
        "validate-routine-service-grant",
        "validate-routine-service-state",
        "status-routine-service",
        "list-routines",
        "wait-background-run",
        "wait-background-model-goal",
        "wait-background-routine-supervisor",
    }
    return args[0] in read_only_subcommands


def _uses_personal_saved_workflow_scope(argv: List[str]) -> bool:
    executable = _basename(argv[0])
    if executable not in {"python", "python3"}:
        return False
    rest = _python_args_after_interpreter_flags(argv[1:])
    if rest[:2] != ["-m", "conductor_runtime"] or len(rest) < 3:
        return False
    if rest[2] not in {
        "validate-saved-workflows",
        "list-saved-workflows",
        "inspect-saved-workflow",
        "export-saved-workflow",
        "run-saved-workflow",
        "write-routine-manifest",
    }:
        return False
    args = rest[3:]
    return any(
        arg == "--include-personal"
        or arg == "--personal-workflows-dir"
        or arg.startswith("--personal-workflows-dir=")
        for arg in args
    )


def _is_python_unittest_discover(argv: List[str]) -> bool:
    executable = _basename(argv[0])
    if executable not in {"python", "python3"}:
        return False
    rest = _python_args_after_interpreter_flags(argv[1:])
    return rest[:4] == ["-m", "unittest", "discover", "-s"] and len(rest) >= 5


def _python_args_after_interpreter_flags(args: List[str]) -> List[str]:
    index = 0
    while index < len(args) and args[index] in {"-B", "-I", "-S"}:
        index += 1
    return args[index:]


def _has_external_path_argument(argv: List[str]) -> bool:
    for arg in argv[1:]:
        if _looks_external_path(arg):
            return True
        if "=" in arg and _looks_external_path(arg.split("=", 1)[1]):
            return True
    return False


def _looks_external_path(value: str) -> bool:
    return value.startswith("/") or ".." in value.split("/")


def _has_path_separator(value: str) -> bool:
    return "/" in value or "\\" in value


def require_no_path_escape(path: str) -> None:
    if path.startswith("/") or ".." in path.split("/"):
        raise ValidationError("artifact paths must be relative and must not contain '..'")


def reject_symlink_path(path: Path, label: str) -> None:
    candidates = [path]
    candidates.extend(path.parents)
    for candidate in candidates:
        if str(candidate) in {"", "."}:
            continue
        if candidate in SAFE_SYSTEM_SYMLINK_ANCESTORS:
            continue
        if candidate.is_symlink():
            if candidate == path:
                raise ValidationError("%s must not be a symlink: %s" % (label, candidate))
            raise ValidationError("%s must not include symlink path component: %s" % (label, candidate))


def open_dir_no_follow(directory: Path, label: str) -> int:
    path = _normalize_safe_system_symlink_prefix(Path(directory))
    flags = _dir_open_flags()
    try:
        if path.is_absolute():
            components = list(path.parts[1:])
            current_fd = os.open(path.anchor or "/", flags)
        else:
            components = list(path.parts)
            current_fd = os.open(".", flags)
    except OSError as exc:
        raise ValidationError("failed to open %s %s: %s" % (label, directory, exc.__class__.__name__))
    try:
        for component in components:
            if component in {"", "."}:
                continue
            if component == "..":
                raise ValidationError("%s must not include '..': %s" % (label, directory))
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        info = os.fstat(current_fd)
        if not stat.S_ISDIR(info.st_mode):
            raise ValidationError("%s must be a directory: %s" % (label, directory))
        return current_fd
    except OSError as exc:
        os.close(current_fd)
        raise ValidationError("failed to open %s %s: %s" % (label, directory, exc.__class__.__name__))
    except Exception:
        os.close(current_fd)
        raise


def ensure_dir_no_follow(directory: Path, label: str, mode: int = 0o700) -> int:
    path = _normalize_safe_system_symlink_prefix(Path(directory))
    flags = _dir_open_flags()
    try:
        if path.is_absolute():
            components = list(path.parts[1:])
            current_fd = os.open(path.anchor or "/", flags)
        else:
            components = list(path.parts)
            current_fd = os.open(".", flags)
    except OSError as exc:
        raise ValidationError("failed to open %s %s: %s" % (label, directory, exc.__class__.__name__))
    try:
        for component in components:
            if component in {"", "."}:
                continue
            if component == "..":
                raise ValidationError("%s must not include '..': %s" % (label, directory))
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode, dir_fd=current_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ValidationError("failed to create %s %s: %s" % (label, directory, exc.__class__.__name__))
                next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        info = os.fstat(current_fd)
        if not stat.S_ISDIR(info.st_mode):
            raise ValidationError("%s must be a directory: %s" % (label, directory))
        return current_fd
    except OSError as exc:
        os.close(current_fd)
        raise ValidationError("failed to open %s %s: %s" % (label, directory, exc.__class__.__name__))
    except Exception:
        os.close(current_fd)
        raise


def read_regular_text_file_no_follow(path: Path, label: str, max_bytes: Optional[int] = None) -> str:
    raw = read_regular_file_bytes_no_follow(path, label, max_bytes=max_bytes)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("%s must be valid UTF-8: %s" % (label, path))


def read_regular_file_prefix_no_follow(path: Path, label: str, limit_bytes: int) -> bytes:
    if not isinstance(limit_bytes, int) or limit_bytes < 0:
        raise ValidationError("%s prefix limit must be a non-negative integer" % label)
    reject_symlink_path(path, label)
    parent_fd = open_dir_no_follow(path.parent, "%s parent" % label)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = None
    try:
        fd = os.open(path.name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        raise FileNotFoundError("missing %s: %s" % (label, path))
    except OSError as exc:
        raise ValidationError("failed to open %s %s: %s" % (label, path, exc.__class__.__name__))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError("%s must be a regular file: %s" % (label, path))
        chunks = []
        total = 0
        while total < limit_bytes:
            read_size = min(65536, limit_bytes - total)
            chunk = os.read(fd, read_size)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def read_regular_file_bytes_no_follow(path: Path, label: str, max_bytes: Optional[int] = None) -> bytes:
    reject_symlink_path(path, label)
    parent_fd = open_dir_no_follow(path.parent, "%s parent" % label)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = None
    try:
        fd = os.open(path.name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        raise FileNotFoundError("missing %s: %s" % (label, path))
    except OSError as exc:
        raise ValidationError("failed to open %s %s: %s" % (label, path, exc.__class__.__name__))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError("%s must be a regular file: %s" % (label, path))
        if max_bytes is not None and info.st_size > max_bytes:
            raise ValidationError("%s must be at most %d bytes" % (label, max_bytes))
        chunks = []
        total = 0
        while True:
            read_size = 65536
            if max_bytes is not None:
                read_size = min(read_size, max_bytes + 1 - total)
            chunk = os.read(fd, read_size)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ValidationError("%s must be at most %d bytes" % (label, max_bytes))
        return b"".join(chunks)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def write_text_file_no_follow(path: Path, label: str, text: str, mode: int = 0o600, sync: bool = False) -> None:
    reject_symlink_path(path, label)
    parent_fd = ensure_dir_no_follow(path.parent, "%s parent" % label)
    fd = None
    try:
        fd = _open_regular_output_file(parent_fd, path.name, path, label, mode, append=False)
        _write_text_fd(fd, text, sync=sync)
    except OSError as exc:
        raise ValidationError("failed to write %s %s: %s" % (label, path, exc.__class__.__name__))
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def write_new_text_file_no_follow(path: Path, label: str, text: str, mode: int = 0o600, sync: bool = False) -> None:
    reject_symlink_path(path, label)
    parent_fd = ensure_dir_no_follow(path.parent, "%s parent" % label)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = None
    try:
        fd = os.open(path.name, flags, mode, dir_fd=parent_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError("%s must be a regular file: %s" % (label, path))
        _write_text_fd(fd, text, sync=sync)
    except FileExistsError:
        raise
    except OSError as exc:
        raise ValidationError("failed to write %s %s: %s" % (label, path, exc.__class__.__name__))
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def append_text_file_no_follow(path: Path, label: str, text: str, mode: int = 0o600, sync: bool = False) -> None:
    reject_symlink_path(path, label)
    parent_fd = ensure_dir_no_follow(path.parent, "%s parent" % label)
    fd = None
    try:
        fd = _open_regular_output_file(parent_fd, path.name, path, label, mode, append=True)
        _write_text_fd(fd, text, sync=sync)
    except OSError as exc:
        raise ValidationError("failed to append %s %s: %s" % (label, path, exc.__class__.__name__))
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def replace_text_file_no_follow(
    path: Path,
    label: str,
    text: str,
    temp_prefix: str,
    mode: int = 0o600,
    sync: bool = True,
) -> None:
    reject_symlink_path(path, label)
    _validate_temp_prefix(temp_prefix)
    parent_fd = open_dir_no_follow(path.parent, "%s parent" % label)
    temp_name = None
    fd = None
    try:
        fd, temp_name = _create_temp_file_no_follow(parent_fd, temp_prefix, mode)
        _write_text_fd(fd, text, sync=sync)
        os.close(fd)
        fd = None
        os.replace(temp_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        if sync:
            os.fsync(parent_fd)
    except OSError as exc:
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
        raise ValidationError("failed to replace %s %s: %s" % (label, path, exc.__class__.__name__))
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def replace_file_bytes_no_follow(
    path: Path,
    label: str,
    raw: bytes,
    temp_prefix: str,
    mode: int = 0o600,
    sync: bool = True,
) -> None:
    if not isinstance(raw, bytes):
        raise ValidationError("%s content must be bytes" % label)
    reject_symlink_path(path, label)
    _validate_temp_prefix(temp_prefix)
    parent_fd = ensure_dir_no_follow(path.parent, "%s parent" % label)
    temp_name = None
    fd = None
    try:
        try:
            existing = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValidationError("%s must be a regular file: %s" % (label, path))
        fd, temp_name = _create_temp_file_no_follow(parent_fd, temp_prefix, mode)
        _write_bytes_fd(fd, raw, sync=sync)
        os.close(fd)
        fd = None
        os.replace(temp_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        if sync:
            os.fsync(parent_fd)
    except OSError as exc:
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
        raise ValidationError("failed to replace %s %s: %s" % (label, path, exc.__class__.__name__))
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def unlink_regular_file_no_follow(path: Path, label: str, sync: bool = True) -> None:
    reject_symlink_path(path, label)
    parent_fd = open_dir_no_follow(path.parent, "%s parent" % label)
    try:
        try:
            info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise ValidationError("missing %s: %s" % (label, path))
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError("%s must be a regular file: %s" % (label, path))
        os.unlink(path.name, dir_fd=parent_fd)
        if sync:
            os.fsync(parent_fd)
    except OSError as exc:
        raise ValidationError("failed to remove %s %s: %s" % (label, path, exc.__class__.__name__))
    finally:
        os.close(parent_fd)


def _open_regular_output_file(parent_fd: int, name: str, path: Path, label: str, mode: int, append: bool) -> int:
    flags = os.O_WRONLY
    if append:
        flags |= os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, mode, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError("%s must be a regular file: %s" % (label, path))
        if not append:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except Exception:
        os.close(fd)
        raise


def _create_temp_file_no_follow(parent_fd: int, prefix: str, mode: int) -> tuple:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for _ in range(100):
        name = "%s%s.tmp" % (prefix, secrets.token_hex(8))
        try:
            fd = os.open(name, flags, mode, dir_fd=parent_fd)
            return fd, name
        except FileExistsError:
            continue
    raise ValidationError("failed to create temporary file after repeated name collisions")


def _write_text_fd(fd: int, text: str, sync: bool) -> None:
    _write_bytes_fd(fd, text.encode("utf-8"), sync=sync)


def _write_bytes_fd(fd: int, payload: bytes, sync: bool) -> None:
    written = 0
    while written < len(payload):
        chunk_size = os.write(fd, payload[written:])
        if chunk_size <= 0:
            raise OSError("short write")
        written += chunk_size
    if sync:
        os.fsync(fd)


def _validate_temp_prefix(prefix: str) -> None:
    if not isinstance(prefix, str) or not prefix:
        raise ValidationError("temporary file prefix must be non-empty")
    if "/" in prefix or "\\" in prefix or ".." in prefix.split("/"):
        raise ValidationError("temporary file prefix must not contain path separators or '..'")


def _dir_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _normalize_safe_system_symlink_prefix(path: Path) -> Path:
    if not path.is_absolute() or len(path.parts) < 2:
        return path
    prefix = Path(path.anchor) / path.parts[1]
    if prefix in SAFE_SYSTEM_SYMLINK_ANCESTORS and prefix.is_symlink():
        return prefix.resolve().joinpath(*path.parts[2:])
    return path


def resolve_under(base: Path, relative_path: str) -> Path:
    require_no_path_escape(relative_path)
    base_resolved = base.resolve()
    candidate = (base_resolved / relative_path).resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        raise ValidationError("path escapes base directory: %s" % relative_path)
    return candidate


def enforce_shell_policy(step: dict, policy: RuntimePolicy) -> CommandAssessment:
    step_id = step.get("id", "<unknown>")
    risk = step.get("risk", "low")
    if risk not in RISK_LEVELS:
        raise ValidationError("step %s has invalid risk %r" % (step_id, risk))
    assessment = assess_command(step.get("command"))
    writes = bool(step.get("writes", False)) or assessment.writes
    destructive = bool(step.get("destructive", False)) or assessment.destructive
    network = bool(step.get("network", False)) or assessment.network
    external_path = bool(step.get("external_path", False)) or assessment.external_path

    if risk == "high" and not (policy.has_approval(step_id) or policy.has_approval("high-risk")):
        raise PolicyError("step %s is high risk and requires --approve %s or --approve high-risk" % (step_id, step_id))
    if external_path and not (policy.has_approval(step_id) or policy.has_approval("external-path")):
        raise PolicyError(
            "step %s references an absolute or parent path and requires --approve %s or --approve external-path"
            % (step_id, step_id)
        )
    if writes and not policy.allow_writes:
        raise PolicyError("step %s may write files and requires --allow-writes" % step_id)
    if destructive and not policy.allow_destructive:
        raise PolicyError("step %s may be destructive and requires --allow-destructive" % step_id)
    if network and not policy.allow_network:
        raise PolicyError("step %s may use network and requires --allow-network" % step_id)
    return assessment


def enforce_agent_policy(step: dict, policy: RuntimePolicy, workers: int = 1) -> None:
    step_id = step.get("id", "<unknown>")
    risk = step.get("risk", "medium")
    if risk == "high" and not (policy.has_approval(step_id) or policy.has_approval("high-risk")):
        raise PolicyError("step %s is high risk and requires --approve %s or --approve high-risk" % (step_id, step_id))
    if not policy.allow_agent:
        raise PolicyError("step %s invokes Codex and requires --allow-agent" % step_id)
    skill_mcp_dependencies = step.get("_agent_skill_mcp_dependencies", [])
    if skill_mcp_dependencies:
        if any(
            dependency.get("transport") != "stdio"
            for dependency in skill_mcp_dependencies
        ) and not policy.allow_network:
            raise PolicyError(
                "step %s activates selected Skill HTTP MCP dependencies and requires --allow-network"
                % step_id
            )
        for dependency in skill_mcp_dependencies:
            approval = skill_mcp_approval_token(dependency.get("name"))
            if not policy.has_approval(approval):
                raise PolicyError(
                    "step %s activates selected Skill MCP dependency %s and requires --approve %s"
                    % (step_id, dependency.get("name"), approval)
                )
            if dependency.get("transport") == "stdio":
                stdio_approval = skill_mcp_stdio_approval_token(
                    dependency.get("name")
                )
                if not policy.has_approval(stdio_approval):
                    raise PolicyError(
                        "step %s launches selected Skill stdio MCP dependency %s in an OS sandbox "
                        "and requires --approve %s"
                        % (step_id, dependency.get("name"), stdio_approval)
                    )
            if dependency.get("auth"):
                auth_approval = skill_mcp_auth_approval_token(dependency.get("name"))
                if not policy.has_approval(auth_approval):
                    raise PolicyError(
                        "step %s accesses environment credentials for selected Skill MCP dependency %s "
                        "and requires --approve %s"
                        % (step_id, dependency.get("name"), auth_approval)
                    )
    native_agents = step.get("native_agents")
    if native_agents is not None:
        validate_codex_native_agents(native_agents, "step %s native_agents" % step_id)
        if step.get("kind") != "codex_exec":
            raise PolicyError("step %s can use native agents only with codex_exec" % step_id)
        if step.get("sandbox", "read-only") != "read-only":
            raise PolicyError("step %s native agents require a read-only sandbox" % step_id)
        if step.get("_agent_tool_policy", "standard") != "standard":
            raise PolicyError("step %s native agents require the standard tool policy" % step_id)
        if not policy.allow_parallel:
            raise PolicyError("step %s native agents require --allow-parallel" % step_id)
        if not policy.has_approval(NATIVE_AGENT_DELEGATION_APPROVAL):
            raise PolicyError(
                "step %s native agents require --approve %s"
                % (step_id, NATIVE_AGENT_DELEGATION_APPROVAL)
            )
    if step.get("sandbox") == "workspace-write" and not policy.allow_writes:
        raise PolicyError("step %s uses a write-capable Codex sandbox and requires --allow-writes" % step_id)
    if workers > 1 and not policy.allow_parallel:
        raise PolicyError("step %s uses parallel workers and requires --allow-parallel" % step_id)
    if workers > HIGH_SCALE_AGENT_WORKER_THRESHOLD and not policy.has_approval("high-scale-agent-map"):
        raise PolicyError(
            "step %s uses %d workers and requires --approve high-scale-agent-map above %d workers"
            % (step_id, workers, HIGH_SCALE_AGENT_WORKER_THRESHOLD)
        )


def validate_approval_tokens(tokens: Iterable[str]) -> Set[str]:
    approvals = set()
    for token in tokens:
        if not token or any(char.isspace() for char in token):
            raise ValidationError("approval tokens must be non-empty and cannot contain whitespace")
        approvals.add(token)
    return approvals
