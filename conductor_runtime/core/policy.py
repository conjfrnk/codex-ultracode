"""Small explicit capability policy for the default runtime."""

import hashlib
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Set

from ..errors import PolicyError, ValidationError


RISK_LEVELS = {"low", "medium", "high"}
SHELL_METACHARS = set(";&|`$><\n\r")
NETWORK_TOOLS = {"curl", "ftp", "nc", "ncat", "scp", "sftp", "ssh", "telnet", "wget"}
DESTRUCTIVE_TOOLS = {"dd", "mkfs", "rm", "rmdir", "shred"}
HIGH_SCALE_WORKERS = 16
INERT_SHELL_TOOLS = {"[", "false", "test", "true"}


@dataclass(frozen=True)
class RuntimePolicy:
    allow_writes: bool = False
    allow_destructive: bool = False
    allow_network: bool = False
    allow_agent: bool = False
    allow_parallel: bool = False
    approvals: Set[str] = field(default_factory=set)

    def has_approval(self, value: str) -> bool:
        return value in self.approvals


@dataclass(frozen=True)
class CommandAssessment:
    argv: List[str]
    writes: bool
    destructive: bool
    network: bool


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


def enforce_shell(step: dict, policy: RuntimePolicy) -> CommandAssessment:
    assessment = assess_command(step)
    _enforce_risk(step, policy)
    if assessment.argv[0] not in INERT_SHELL_TOOLS:
        approval = shell_approval(assessment.argv)
        require_approval(policy, approval, "shell step %s" % step["id"])
    if assessment.writes and not policy.allow_writes:
        raise PolicyError("step %s may write and requires --allow-writes" % step["id"])
    if assessment.destructive and not policy.allow_destructive:
        raise PolicyError("step %s may be destructive and requires --allow-destructive" % step["id"])
    if assessment.network and not policy.allow_network:
        raise PolicyError("step %s may use network and requires --allow-network" % step["id"])
    return assessment


def shell_approval(argv: List[str]) -> str:
    payload = json.dumps(argv, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
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
