import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .errors import PolicyError, ValidationError
from .redaction import contains_secret_like, redact_json_value, redact_text
from .runner import DEFAULT_OUTPUT_LIMIT_BYTES, ProcessResult, run_process
from .security import RuntimePolicy, read_regular_text_file_no_follow, reject_symlink_path, write_new_text_file_no_follow


CLOUD_TASK_RECORD_SCHEMA = "conductor.cloud_task.v1"
CLOUD_REVIEW_RECEIPT_SCHEMA = "conductor.cloud_review_receipt.v1"
MAX_CLOUD_TASK_TEXT_BYTES = 1024 * 1024
MAX_CLOUD_RECORD_BYTES = 2 * 1024 * 1024
MAX_CLOUD_TIMEOUT_SECONDS = 60 * 60
SAFE_CLOUD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,255}$")
TASK_ID_PATTERNS = [
    re.compile(r"\b(task_[A-Za-z0-9_-]{6,200})\b"),
    re.compile(r"\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b"),
]


def submit_cloud_task(
    *,
    task: str,
    environment_id: str,
    workspace: Path,
    cloud_dir: Path,
    policy: RuntimePolicy,
    branch: str = "",
    attempts: int = 1,
    timeout_seconds: int = 300,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
) -> Dict:
    _require_network_approval(policy, "cloud-submit")
    task_text = _bounded_task(task)
    environment = _safe_id(environment_id, "cloud environment id")
    workspace_path = _workspace(workspace)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 4:
        raise ValidationError("cloud attempts must be from 1 to 4")
    argv = ["codex", "cloud", "exec", "--env", environment, "--attempts", str(attempts)]
    if branch:
        argv.extend(["--branch", _safe_branch(branch)])
    argv.append(task_text)
    started = _utc_now()
    result = run_process(
        argv,
        cwd=workspace_path,
        timeout=_timeout(timeout_seconds),
        output_limit_bytes=output_limit_bytes,
    )
    _require_success(result, "cloud submit")
    task_id = _extract_task_id(result.stdout + "\n" + result.stderr)
    record = {
        "schema": CLOUD_TASK_RECORD_SCHEMA,
        "task_id": task_id,
        "environment_id": environment,
        "branch": branch or None,
        "attempts": attempts,
        "submitted_at_utc": started,
        "workspace": redact_text(str(workspace_path.resolve())),
        "task_sha256": hashlib.sha256(task_text.encode("utf-8")).hexdigest(),
        "task_chars": len(task_text),
        "status": "submitted",
        "approval_values_persisted": False,
        "tool_output": _result_summary(result, omitted_text=task_text),
    }
    output = Path(cloud_dir) / "tasks" / (task_id + ".json")
    _write_json_new(output, record, "cloud task record")
    record["record_path"] = str(output)
    return record


def cloud_task_status(
    task_id: str,
    workspace: Path,
    policy: RuntimePolicy,
    timeout_seconds: int = 120,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
) -> Dict:
    _require_network_approval(policy, "cloud-read")
    identifier = _safe_id(task_id, "cloud task id")
    workspace_path = _workspace(workspace)
    result = run_process(
        ["codex", "cloud", "status", identifier],
        cwd=workspace_path,
        timeout=_timeout(timeout_seconds),
        output_limit_bytes=output_limit_bytes,
    )
    _require_success(result, "cloud status")
    return {
        "task_id": identifier,
        "status": "fetched",
        "checked_at_utc": _utc_now(),
        "tool_output": _result_summary(result),
    }


def review_cloud_task(
    *,
    task_id: str,
    workspace: Path,
    cloud_dir: Path,
    policy: RuntimePolicy,
    attempt: Optional[int] = None,
    timeout_seconds: int = 180,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
) -> Dict:
    _require_network_approval(policy, "cloud-read")
    identifier = _safe_id(task_id, "cloud task id")
    workspace_path = _workspace(workspace)
    argv = ["codex", "cloud", "diff", identifier]
    if attempt is not None:
        argv.extend(["--attempt", str(_attempt(attempt))])
    result = run_process(
        argv,
        cwd=workspace_path,
        timeout=_timeout(timeout_seconds),
        output_limit_bytes=output_limit_bytes,
    )
    _require_success(result, "cloud diff")
    diff_text = result.stdout
    diff_sha256 = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    review_dir = Path(cloud_dir) / "reviews" / identifier
    suffix = "attempt-%d" % attempt if attempt is not None else "default"
    diff_path = review_dir / (suffix + ".diff")
    receipt_path = review_dir / (suffix + ".receipt.json")
    write_new_text_file_no_follow(diff_path, "cloud reviewed diff", redact_text(diff_text))
    receipt = {
        "schema": CLOUD_REVIEW_RECEIPT_SCHEMA,
        "task_id": identifier,
        "attempt": attempt,
        "reviewed_at_utc": _utc_now(),
        "diff_sha256": diff_sha256,
        "diff_path": str(diff_path),
        "workspace": redact_text(str(workspace_path.resolve())),
        "approval_values_persisted": False,
    }
    _write_json_new(receipt_path, receipt, "cloud review receipt")
    receipt["receipt_path"] = str(receipt_path)
    return receipt


def apply_cloud_task(
    *,
    task_id: str,
    review_receipt: Path,
    workspace: Path,
    policy: RuntimePolicy,
    attempt: Optional[int] = None,
    timeout_seconds: int = 300,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
) -> Dict:
    _require_network_approval(policy, "cloud-apply")
    if not policy.allow_writes:
        raise PolicyError("cloud apply requires --allow-writes")
    identifier = _safe_id(task_id, "cloud task id")
    workspace_path = _workspace(workspace)
    receipt = _load_review_receipt(review_receipt)
    if receipt["task_id"] != identifier or receipt.get("attempt") != attempt:
        raise ValidationError("cloud review receipt does not match the requested task and attempt")
    if receipt["workspace"] != redact_text(str(workspace_path.resolve())):
        raise ValidationError("cloud review receipt workspace does not match the current workspace")
    diff_argv = ["codex", "cloud", "diff", identifier]
    if attempt is not None:
        diff_argv.extend(["--attempt", str(_attempt(attempt))])
    current = run_process(
        diff_argv,
        cwd=workspace_path,
        timeout=_timeout(timeout_seconds),
        output_limit_bytes=output_limit_bytes,
    )
    _require_success(current, "cloud diff verification")
    current_hash = hashlib.sha256(current.stdout.encode("utf-8")).hexdigest()
    if current_hash != receipt["diff_sha256"]:
        raise PolicyError("cloud task diff changed after review; run cloud-review again")
    apply_argv = ["codex", "cloud", "apply", identifier]
    if attempt is not None:
        apply_argv.extend(["--attempt", str(_attempt(attempt))])
    result = run_process(
        apply_argv,
        cwd=workspace_path,
        timeout=_timeout(timeout_seconds),
        output_limit_bytes=output_limit_bytes,
    )
    _require_success(result, "cloud apply")
    return {
        "task_id": identifier,
        "attempt": attempt,
        "status": "applied",
        "applied_at_utc": _utc_now(),
        "review_receipt": str(review_receipt),
        "verified_diff_sha256": current_hash,
        "approval_values_persisted": False,
        "tool_output": _result_summary(result),
    }


def _load_review_receipt(path: Path) -> Dict:
    reject_symlink_path(path, "cloud review receipt")
    try:
        data = json.loads(read_regular_text_file_no_follow(path, "cloud review receipt", MAX_CLOUD_RECORD_BYTES))
    except json.JSONDecodeError as exc:
        raise ValidationError("cloud review receipt is not valid JSON: %s" % exc)
    if not isinstance(data, dict) or data.get("schema") != CLOUD_REVIEW_RECEIPT_SCHEMA:
        raise ValidationError("cloud review receipt has an unsupported schema")
    _safe_id(data.get("task_id"), "cloud review receipt task id")
    if data.get("attempt") is not None:
        _attempt(data["attempt"])
    digest = data.get("diff_sha256")
    if not isinstance(digest, str) or not re.match(r"^[a-f0-9]{64}$", digest):
        raise ValidationError("cloud review receipt diff_sha256 is invalid")
    if not isinstance(data.get("workspace"), str) or not data["workspace"]:
        raise ValidationError("cloud review receipt workspace is invalid")
    return data


def _require_network_approval(policy: RuntimePolicy, approval: str) -> None:
    if not policy.allow_network:
        raise PolicyError("cloud operation requires --allow-network")
    if not policy.has_approval(approval):
        raise PolicyError("cloud operation requires --approve %s" % approval)


def _bounded_task(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationError("cloud task must not be empty")
    if len(text.encode("utf-8")) > MAX_CLOUD_TASK_TEXT_BYTES:
        raise ValidationError("cloud task must be at most %d bytes" % MAX_CLOUD_TASK_TEXT_BYTES)
    if contains_secret_like(text):
        raise ValidationError("cloud task must not contain secret-like values")
    return text


def _safe_id(value, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_CLOUD_ID.match(text):
        raise ValidationError("%s must be a safe identifier" % label)
    return text


def _safe_branch(value: str) -> str:
    branch = str(value or "").strip()
    if (
        not branch
        or len(branch) > 255
        or branch.startswith(("-", "/"))
        or branch.endswith(("/", "."))
        or ".." in branch
        or "//" in branch
        or not re.match(r"^[A-Za-z0-9._/-]+$", branch)
    ):
        raise ValidationError("cloud branch must be a safe git branch name")
    return branch


def _workspace(value: Path) -> Path:
    path = Path(value)
    reject_symlink_path(path, "cloud workspace")
    if not path.is_dir():
        raise ValidationError("cloud workspace does not exist: %s" % path)
    return path


def _attempt(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 4:
        raise ValidationError("cloud attempt must be from 1 to 4")
    return value


def _timeout(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > MAX_CLOUD_TIMEOUT_SECONDS:
        raise ValidationError("cloud timeout_seconds must be from 1 to %d" % MAX_CLOUD_TIMEOUT_SECONDS)
    return value


def _extract_task_id(text: str) -> str:
    for pattern in TASK_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return _safe_id(match.group(1), "cloud task id")
    raise ValidationError("cloud submit succeeded but no task id could be parsed from bounded tool output")


def _require_success(result: ProcessResult, label: str) -> None:
    if result.timed_out:
        raise ValidationError("%s timed out" % label)
    if result.returncode != 0:
        detail = redact_text(result.stderr or result.stdout).strip()[:500]
        raise ValidationError("%s failed with exit code %d: %s" % (label, result.returncode, detail))


def _result_summary(result: ProcessResult, omitted_text: str = "") -> Dict:
    stdout = result.stdout.replace(omitted_text, "<task omitted>") if omitted_text else result.stdout
    stderr = result.stderr.replace(omitted_text, "<task omitted>") if omitted_text else result.stderr
    return {
        "returncode": result.returncode,
        "stdout": redact_text(stdout),
        "stderr": redact_text(stderr),
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
        "timed_out": result.timed_out,
    }


def _write_json_new(path: Path, value: Dict, label: str) -> None:
    text = json.dumps(redact_json_value(value), indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_CLOUD_RECORD_BYTES:
        raise ValidationError("%s exceeds the supported size" % label)
    write_new_text_file_no_follow(path, label, text)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
