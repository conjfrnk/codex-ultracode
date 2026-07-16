"""Bounded verifier-driven retries with external, hash-bound goal state."""

import fcntl
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..errors import ValidationError
from ..redaction import redact_text
from .auto import AutoResult, run_direct
from .policy import RuntimePolicy, policy_fingerprint
from .safe import (
    canonical_json_bytes,
    ensure_directory,
    external_goals_dir,
    load_json,
    replace_json,
    require_external_state_path,
    sha256_bytes,
    strict_json_bytes,
    write_new_json,
)
from .state import RunState, utc_now


GOAL_SCHEMA = "conductor.core_goal.v1"
GOAL_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
MAX_ITERATIONS = 10
MAX_FEEDBACK_CHARS = 8000


@dataclass(frozen=True)
class GoalResult:
    status: str
    goal_path: Path
    iterations: int
    latest: AutoResult


def run_goal(
    task: str,
    *,
    workspace: Path,
    policy: RuntimePolicy,
    max_iterations: int = 3,
    goals_dir: Optional[Path] = None,
    goal_id: Optional[str] = None,
    resume_goal: Optional[Path] = None,
    output_path: Optional[Path] = None,
    replace_output: bool = False,
    **workflow_options,
) -> GoalResult:
    if not isinstance(task, str) or not task.strip() or len(task) > 65536:
        raise ValidationError("goal task must be non-empty bounded text")
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or not 1 <= max_iterations <= MAX_ITERATIONS:
        raise ValidationError("max_iterations must be from 1 to %d" % MAX_ITERATIONS)
    workspace_path = Path(workspace).resolve()
    binding_options = dict(workflow_options)
    binding_options["output_path"] = output_path
    if replace_output:
        binding_options["replace_output"] = True
    bindings = _bindings(task, workspace_path, policy, max_iterations, binding_options)
    if resume_goal is not None:
        path = require_external_state_path(resume_goal, workspace_path, "goal state")
    else:
        identifier = goal_id or ("goal-" + os.urandom(6).hex())
        if GOAL_ID.fullmatch(identifier) is None:
            raise ValidationError("goal id must use lowercase letters, digits, and hyphens")
        root = Path(goals_dir).expanduser().resolve(strict=False) if goals_dir else external_goals_dir(workspace_path)
        root = require_external_state_path(root, workspace_path, "goal state directory")
        path = root / (identifier + ".json")
    with _goal_lock(path):
        if resume_goal is not None:
            state = _load_goal(path)
            _verify_bindings(state, bindings)
        else:
            state = _new_goal(path.stem, bindings)
            write_new_json(path, state, "goal state")
        latest = _latest_result(state)
        if state["status"] == "completed" and latest is not None:
            return GoalResult("completed", path, len(state["attempts"]), latest)
        feedback = _feedback_from_latest(state)
        while len(state["attempts"]) < max_iterations:
            state["status"] = "running"
            state = _save_goal(path, state)
            result = run_direct(
                task,
                workspace=workspace_path,
                policy=policy,
                output_path=output_path,
                replace_output=replace_output,
                iteration_context=feedback,
                **workflow_options,
            )
            run = RunState.inspect(result.run_dir)
            feedback, retryable = _verification_feedback(run)
            state["attempts"].append(
                {
                    "iteration": len(state["attempts"]) + 1,
                    "run_dir": str(result.run_dir),
                    "run_id": run.descriptor["run_id"],
                    "status": result.status,
                    "retryable_verification_failure": retryable,
                    "feedback_sha256": sha256_bytes(feedback.encode("utf-8")) if feedback else None,
                }
            )
            if result.status == "completed":
                state["status"] = "completed"
                state = _save_goal(path, state)
                return GoalResult("completed", path, len(state["attempts"]), result)
            if not retryable:
                state["status"] = "failed"
                state = _save_goal(path, state)
                return GoalResult("failed", path, len(state["attempts"]), result)
            state["status"] = "retrying"
            state = _save_goal(path, state)
            latest = result
        state["status"] = "exhausted"
        state = _save_goal(path, state)
        if latest is None:
            latest = _latest_result(state)
        if latest is None:
            raise ValidationError("goal exhausted without a recorded attempt")
        return GoalResult("exhausted", path, len(state["attempts"]), latest)


def _bindings(task, workspace: Path, policy: RuntimePolicy, max_iterations: int, options: dict) -> dict:
    try:
        options_bytes = canonical_json_bytes(_jsonable(options))
    except ValidationError as exc:
        raise ValidationError("goal options must be JSON-compatible") from exc
    return {
        "task_sha256": sha256_bytes(task.encode("utf-8")),
        "workspace_sha256": sha256_bytes(str(workspace).encode("utf-8")),
        "policy_sha256": policy_fingerprint(policy),
        "options_sha256": sha256_bytes(options_bytes),
        "max_iterations": max_iterations,
    }


def _jsonable(value):
    if isinstance(value, Path):
        return str(value.expanduser().resolve(strict=False))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _new_goal(identifier: str, bindings: dict) -> dict:
    if GOAL_ID.fullmatch(identifier) is None:
        raise ValidationError("goal state filename is not a safe goal id")
    value = {
        "schema": GOAL_SCHEMA,
        "goal_id": identifier,
        **bindings,
        "status": "pending",
        "attempts": [],
        "created_at_utc": utc_now(),
        "updated_at_utc": utc_now(),
        "approval_values_persisted": False,
    }
    return _with_hash(value)


def _save_goal(path: Path, value: dict) -> dict:
    updated = dict(value)
    updated["updated_at_utc"] = utc_now()
    updated = _with_hash(updated)
    replace_json(path, updated, "goal state")
    return updated


def _load_goal(path: Path) -> dict:
    value = load_json(path, "goal state")
    fields = {
        "schema",
        "goal_id",
        "task_sha256",
        "workspace_sha256",
        "policy_sha256",
        "options_sha256",
        "max_iterations",
        "status",
        "attempts",
        "created_at_utc",
        "updated_at_utc",
        "approval_values_persisted",
        "goal_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields or value.get("schema") != GOAL_SCHEMA:
        raise ValidationError("goal state schema is invalid")
    if value.get("goal_sha256") != _with_hash(value)["goal_sha256"]:
        raise ValidationError("goal state hash does not match")
    if value.get("goal_id") != path.stem or GOAL_ID.fullmatch(value.get("goal_id", "")) is None:
        raise ValidationError("goal state identity is invalid")
    for field in ("task_sha256", "workspace_sha256", "policy_sha256", "options_sha256"):
        if not isinstance(value.get(field), str) or re.fullmatch(r"[0-9a-f]{64}", value[field]) is None:
            raise ValidationError("goal state binding is invalid")
    maximum = value.get("max_iterations")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= MAX_ITERATIONS:
        raise ValidationError("goal state iteration limit is invalid")
    status = value.get("status")
    if not isinstance(status, str) or status not in {
        "pending",
        "running",
        "retrying",
        "completed",
        "failed",
        "exhausted",
    }:
        raise ValidationError("goal state status is invalid")
    attempts = value.get("attempts")
    if not isinstance(attempts, list) or len(attempts) > maximum:
        raise ValidationError("goal attempt history is invalid")
    for index, attempt in enumerate(attempts, start=1):
        if not isinstance(attempt, dict) or set(attempt) != {
            "iteration",
            "run_dir",
            "run_id",
            "status",
            "retryable_verification_failure",
            "feedback_sha256",
        }:
            raise ValidationError("goal attempt record is invalid")
        feedback = attempt.get("feedback_sha256")
        if (
            attempt.get("iteration") != index
            or not isinstance(attempt.get("run_dir"), str)
            or not attempt["run_dir"]
            or not isinstance(attempt.get("run_id"), str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", attempt["run_id"]) is None
            or not isinstance(attempt.get("status"), str)
            or attempt.get("status") not in {"completed", "failed", "blocked", "paused"}
            or not isinstance(attempt.get("retryable_verification_failure"), bool)
            or (
                feedback is not None
                and (not isinstance(feedback, str) or re.fullmatch(r"[0-9a-f]{64}", feedback) is None)
            )
        ):
            raise ValidationError("goal attempt record is invalid")
    if (
        not isinstance(value.get("created_at_utc"), str)
        or not isinstance(value.get("updated_at_utc"), str)
        or value.get("approval_values_persisted") is not False
    ):
        raise ValidationError("goal state metadata is invalid")
    return value


def _verify_bindings(state: dict, expected: dict) -> None:
    for field, value in expected.items():
        if state.get(field) != value:
            raise ValidationError("resumed goal %s changed" % field.replace("_sha256", ""))


def _verification_feedback(run: RunState) -> tuple:
    failed = [step_id for step_id, record in run.state["steps"].items() if record.get("status") == "failed"]
    if len(failed) != 1 or failed[0] not in {"check", "verify"}:
        return "", False
    if run.state["steps"].get("work", {}).get("status") != "completed":
        return "", False
    candidates = ["verification/verdict.json", "verification/check.txt"]
    for relative in candidates:
        if relative not in run.state["artifacts"]:
            continue
        payload = run.read_artifact(relative)
        if relative.endswith(".json"):
            try:
                value = strict_json_bytes(payload, "verification verdict")
            except ValidationError:
                text = payload.decode("utf-8", errors="replace")
            else:
                text = value.get("feedback", "") if isinstance(value, dict) else ""
        else:
            text = payload.decode("utf-8", errors="replace")
        clean = redact_text(text).strip()[:MAX_FEEDBACK_CHARS]
        return clean or "The verification step failed without additional feedback.", True
    return "The verification step failed without a captured result.", True


def _feedback_from_latest(state: dict) -> str:
    attempts = state.get("attempts", [])
    if not attempts or not attempts[-1].get("retryable_verification_failure"):
        return ""
    run = RunState.inspect(Path(attempts[-1]["run_dir"]))
    feedback, _ = _verification_feedback(run)
    expected = attempts[-1].get("feedback_sha256")
    if expected != (sha256_bytes(feedback.encode("utf-8")) if feedback else None):
        raise ValidationError("goal verifier feedback changed")
    return feedback


def _latest_result(state: dict) -> Optional[AutoResult]:
    attempts = state.get("attempts", [])
    if not attempts:
        return None
    run = RunState.inspect(Path(attempts[-1]["run_dir"]))
    result_path = run.artifact_path("result.md") if "result.md" in run.state["artifacts"] else None
    stage_relative = run.state["steps"].get("work", {}).get("stage_evidence")
    return AutoResult(
        status=run.state["status"],
        run_dir=run.run_dir,
        receipt_path=run.run_dir / "auto.json",
        result_path=result_path,
        stage_evidence_path=run.artifact_path(stage_relative) if stage_relative else None,
    )


def _with_hash(value: dict) -> dict:
    result = dict(value)
    result.pop("goal_sha256", None)
    result["goal_sha256"] = sha256_bytes(canonical_json_bytes(result))
    return result


@contextmanager
def _goal_lock(path: Path):
    parent = path.parent.resolve(strict=False)
    ensure_directory(parent, "goal state directory")
    lock = parent / (path.name + ".lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValidationError("goal is already active") from exc
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
