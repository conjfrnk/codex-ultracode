"""Model-first automatic execution with no planning call on the default path."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from ..errors import ValidationError
from ..redaction import redact_text
from .policy import RuntimePolicy
from .runner import WorkflowRunner
from .safe import canonical_json_bytes, replace_bytes, sha256_bytes, write_new_bytes
from .workflow import slugify, workflow_fingerprint


DEFAULT_AGENT_TOKENS = 20000
DEFAULT_VERIFIER_TOKENS = 8000
VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["satisfied", "feedback", "evidence", "confidence"],
    "properties": {
        "satisfied": {"type": "boolean"},
        "feedback": {"type": "string", "maxLength": 8000},
        "evidence": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


@dataclass(frozen=True)
class AutoResult:
    status: str
    run_dir: Path
    receipt_path: Path
    result_path: Optional[Path]
    stage_evidence_path: Optional[Path]


def parse_command_json(text: str) -> List[str]:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError("--check-command-json must be a JSON argv array") from exc
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValidationError("--check-command-json must be a non-empty JSON argv array")
    return value


def build_direct_workflow(
    task: str,
    *,
    writes: bool,
    check_command: Optional[List[str]] = None,
    check_prompt: Optional[str] = None,
    name: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    max_tokens: int = DEFAULT_AGENT_TOKENS,
    verifier_tokens: int = DEFAULT_VERIFIER_TOKENS,
    timeout_seconds: int = 900,
    check_timeout_seconds: int = 300,
    output_limit_bytes: int = 1024 * 1024,
) -> Dict:
    if not isinstance(task, str) or not task.strip() or len(task) > 65536:
        raise ValidationError("task must be non-empty text of at most 65536 characters")
    worker = {
        "id": "work",
        "kind": "codex_exec",
        "description": "Execute the task directly with the model.",
        "prompt": _worker_prompt(task, writes),
        "capture": "result.md",
        "sandbox": "workspace-write" if writes else "read-only",
        "max_tokens": max_tokens,
        "timeout_seconds": timeout_seconds,
        "output_limit_bytes": output_limit_bytes,
    }
    if model:
        worker["model"] = model
    if effort:
        worker["effort"] = effort
    steps = [worker]
    dependencies = ["work"]
    if check_command is not None:
        steps.append(
            {
                "id": "check",
                "kind": "shell",
                "description": "Run the caller-supplied verification command.",
                "depends_on": list(dependencies),
                "command": check_command,
                "capture": "verification/check.txt",
                "writes": False,
                "destructive": False,
                "network": False,
                "timeout_seconds": check_timeout_seconds,
                "output_limit_bytes": output_limit_bytes,
            }
        )
        dependencies = ["check"]
    if check_prompt is not None or (writes and check_command is None):
        prompt = check_prompt or (
            "Independently inspect the staged workspace and determine whether the requested task is "
            "fully correct, appropriately scoped, and supported by relevant checks. Run useful read-only "
            "checks. Return only the required JSON verdict."
        )
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 65536:
            raise ValidationError("check prompt must be non-empty bounded text")
        verifier_dependencies = list(dict.fromkeys(dependencies + ["work"]))
        verifier = {
            "id": "verify",
            "kind": "codex_exec",
            "description": "Independently verify the direct result.",
            "depends_on": verifier_dependencies,
            "context_from": ["work"],
            "prompt": prompt,
            "capture": "verification/verdict.json",
            "sandbox": "read-only",
            "completion_verdict": "strict-v1",
            "output_schema": VERDICT_SCHEMA,
            "max_tokens": verifier_tokens,
            "timeout_seconds": timeout_seconds,
            "output_limit_bytes": output_limit_bytes,
        }
        if model:
            verifier["model"] = model
        if effort:
            verifier["effort"] = effort
        steps.append(verifier)
    workflow = {
        "schema": "conductor.workflow.v1",
        "name": slugify(name or "direct-task"),
        "description": "Direct model execution compiled by the automatic entry point.",
        "mode": "workspace_write" if writes else "read_only",
        "risk": "medium" if writes else "low",
        "max_workers": 1,
        "default_timeout_seconds": timeout_seconds,
        "output_limit_bytes": output_limit_bytes,
        "result_artifact": "result.md",
        "steps": steps,
    }
    return workflow


def run_direct(
    task: str,
    *,
    workspace: Path,
    policy: RuntimePolicy,
    runs_dir: Optional[Path] = None,
    run_id: Optional[str] = None,
    resume_dir: Optional[Path] = None,
    dry_run: bool = False,
    receipt_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    iteration_context: Optional[str] = None,
    **workflow_options,
) -> AutoResult:
    workflow = build_direct_workflow(task, writes=policy.allow_writes, **workflow_options)
    runner = WorkflowRunner(
        workflow,
        workspace,
        runs_dir,
        policy,
        dry_run=dry_run,
        run_id=run_id,
        resume_dir=resume_dir,
        iteration_context=iteration_context,
    )
    run = runner.execute()
    result_path = None
    if run.state["status"] == "completed":
        payload = run.read_artifact("result.md")
        result_path = run.artifact_path("result.md")
        if output_path is not None:
            write_requested(Path(output_path), payload)
            result_path = Path(output_path)
    stage_relative = run.state["steps"]["work"].get("stage_evidence")
    stage_path = run.artifact_path(stage_relative) if stage_relative else None
    receipt = {
        "schema": "conductor.core_auto_receipt.v1",
        "route": "direct",
        "status": run.state["status"],
        "run_id": run.descriptor["run_id"],
        "run_dir": redact_text(str(run.run_dir)),
        "workflow_sha256": workflow_fingerprint(workflow),
        "result_sha256": run.state["artifacts"].get("result.md", {}).get("sha256"),
        "stage_evidence_sha256": (
            run.state["artifacts"].get(stage_relative, {}).get("sha256") if stage_relative else None
        ),
        "provider_planning_calls": 0,
        "approval_values_persisted": False,
    }
    receipt["receipt_sha256"] = sha256_bytes(canonical_json_bytes(receipt))
    destination = Path(receipt_path) if receipt_path is not None else run.run_dir / "auto.json"
    write_requested(destination, canonical_json_bytes(receipt))
    return AutoResult(
        status=run.state["status"],
        run_dir=run.run_dir,
        receipt_path=destination,
        result_path=result_path,
        stage_evidence_path=stage_path,
    )


def _worker_prompt(task: str, writes: bool) -> str:
    mode = (
        "Work only in the staged workspace. Implement the task, inspect your changes, and run relevant checks."
        if writes
        else "Inspect the workspace without modifying it. Answer the task directly and cite checks actually run."
    )
    return "%s\n\nBEGIN_UNTRUSTED_TASK\n%s\nEND_UNTRUSTED_TASK" % (mode, task)


def write_requested(path: Path, payload: bytes) -> None:
    path = path.parent.resolve(strict=False) / path.name
    if path.exists() and not path.is_symlink():
        replace_bytes(path, payload, "requested output")
    else:
        write_new_bytes(path, payload, "requested output")
